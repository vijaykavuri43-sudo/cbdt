"""Collector: full manifest sweep + incremental watermark polling + SSE event bus."""

import threading
import time
import queue
from typing import Any, Optional

from . import config, db, evidence
from .enrich import EnrichmentWorker


class EventBus:
    """Thread-safe pub/sub bridging collector threads -> SSE clients."""

    def __init__(self):
        self._subs: list[queue.Queue] = []
        self._lock = threading.Lock()

    def subscribe(self) -> queue.Queue:
        q: queue.Queue = queue.Queue(maxsize=500)
        with self._lock:
            self._subs.append(q)
        return q

    def unsubscribe(self, q: queue.Queue):
        with self._lock:
            if q in self._subs:
                self._subs.remove(q)

    def publish(self, event: dict):
        with self._lock:
            subs = list(self._subs)
        for q in subs:
            try:
                q.put_nowait(event)
            except queue.Full:
                pass


bus = EventBus()


class Collector:
    def __init__(self, s3: Any):
        self.s3 = s3
        self.state = {
            "phase": "idle",
            "sweep_done": False,
            "sweep_progress": {},
            "objects_total": 0,
            "new_since_start": 0,
            "last_poll_utc": None,
            "bucket_live": None,
            "bucket_live_fails": 0,
            "bucket_live_ok_utc": None,
            "started_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        }
        # Consecutive liveness-probe failures (debounce counter — see
        # record_liveness). Not part of state: exposed as bucket_live_fails.
        self._live_fails = 0
        self._stop = threading.Event()
        # Monotonic timestamp of the last full COUNT(*) — hot paths use the
        # incrementally bumped counter and recount at most every 10 min
        # (log review 2026-09-03: per-poll COUNT(*) over ~1.4M rows saturated
        # a core and stalled sync-daemon notifies past their timeout).
        self._total_recount_mono = 0.0
        self.enricher: Optional[EnrichmentWorker] = None
        # Rolling per-prefix crawl cursors for the wrapping cold crawl
        # (crawl_once) — see comment there for why the watermark poll alone
        # cannot see new uploads whose keys sort before the watermark.
        self._crawl_cursor: dict[str, Optional[str]] = {}

    def record_liveness(self, ok: bool) -> bool:
        """Debounced bucket-liveness recorder (bucket-blip review 2026-09-05).

        A single transient list failure (connection reset, brief GCS wobble)
        must NOT flip the sidebar to red "BUCKET UNREACHABLE" for a full
        minute while every other probe stays green. Declare down only after
        3 consecutive failures (~30s at the 10s poll cadence); any success
        resets the counter immediately. Returns the published bucket_live.
        """
        if ok:
            self._live_fails = 0
            self.state["bucket_live"] = True
            self.state["bucket_live_ok_utc"] = time.strftime(
                "%Y-%m-%dT%H:%M:%SZ", time.gmtime()
            )
        else:
            self._live_fails += 1
            if self._live_fails >= 3:
                self.state["bucket_live"] = False
        self.state["bucket_live_fails"] = self._live_fails
        return bool(self.state["bucket_live"])

    def bump_total(self, n: int):
        """Add freshly inserted objects to the cached total (exact: upsert
        helpers return inserted-only counts)."""
        try:
            self.state["objects_total"] = int(
                self.state.get("objects_total") or 0
            ) + max(0, int(n or 0))
        except Exception:
            pass

    def total_objects(self) -> int:
        """Cached total with a throttled exact recount (600s)."""
        try:
            now = time.monotonic()
            if now - self._total_recount_mono >= 600.0:
                self.state["objects_total"] = db.total_count()
                self._total_recount_mono = now
            return int(self.state.get("objects_total") or 0)
        except Exception:
            try:
                return int(self.state.get("objects_total") or 0)
            except Exception:
                return 0

    @staticmethod
    def _changed_objects(objects: list[dict]) -> list[dict]:
        """Return new or version-changed objects from a listed batch."""
        versions = db.object_versions([o["key"] for o in objects])
        changed = []
        for o in objects:
            current = (
                int(o.get("size") or 0),
                o.get("etag") or "",
                db.normalize_object_ts(o.get("last_modified")),
            )
            if versions.get(o["key"]) != current:
                changed.append(o)
        return changed

    # ---------- full sweep ----------
    def full_sweep(self):
        self.state["phase"] = "sweep"
        source_id = getattr(self.s3, "source_id", config.SOURCE_ID)
        source_changed = db.reset_collection_state(source_id)
        self.state["source_id"] = source_id
        self.state["source_changed"] = source_changed
        try:
            prefixes = sorted(
                p
                for p in set(config.DEFAULT_PREFIXES) | set(self.s3.list_prefixes_top())
                if p.strip("/")
            )
        except Exception as e:
            prefixes = list(config.DEFAULT_PREFIXES)
            self.state["sweep_error"] = str(e)[:200]
        prefixes = [p for p in prefixes if not p.endswith("//")]
        db.set_meta("prefixes", "\n".join(prefixes))

        n_root = self.poll_root_files()
        if n_root:
            self.state["sweep_progress"]["(root)"] = {"count": n_root}

        # Fast restart: warm-watermark poll in parallel; cold only for prefixes
        # that have no watermark yet. Keeps first-paint fast after every boot.
        done_before = db.get_meta("sweep_complete_utc")
        if done_before:
            self.state["sweep_done"] = True
            self.state["objects_total"] = db.total_count()
            self._total_recount_mono = time.monotonic()
            self.state["phase"] = "live-polling"

            from concurrent.futures import ThreadPoolExecutor

            def catch(pfx: str) -> int:
                try:
                    return self.poll_once(pfx, cold=self._watermark(pfx) is None)
                except Exception:
                    return 0

            with ThreadPoolExecutor(max_workers=config.FULL_SWEEP_CONCURRENCY) as ex:
                n = sum(
                    f.result() or 0 for f in [ex.submit(catch, p) for p in prefixes]
                )
            evidence.append_collection_log(
                {
                    "event": "monitor_restart_catchup",
                    "method": "object-store parallel warm/catch-up poll",
                    "bucket": source_id,
                    "counts": {"new_objects": n},
                }
            )
            return

        def sweep_one(pfx: str):
            n = 0
            batch: list[dict] = []
            try:
                for obj in self.s3.iter_all(pfx):
                    obj["top_prefix"] = pfx
                    batch.append(obj)
                    if len(batch) >= 1000:
                        n += db.upsert_objects(batch)
                        batch = []
                n += db.upsert_objects(batch)
            except Exception as e:
                self.state["sweep_progress"][pfx] = {"error": str(e)[:120]}
            self.state["sweep_progress"][pfx] = {"count": n}
            return n

        from concurrent.futures import ThreadPoolExecutor

        total = 0
        with ThreadPoolExecutor(max_workers=config.FULL_SWEEP_CONCURRENCY) as ex:
            for fut in [ex.submit(sweep_one, p) for p in prefixes]:
                total += fut.result() or 0
        self.state["sweep_done"] = True
        self.state["objects_total"] = db.total_count()
        self._total_recount_mono = time.monotonic()
        self.state["phase"] = "live-polling"
        db.set_meta(
            "sweep_complete_utc", time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        )
        evidence.append_collection_log(
            {
                "event": "monitor_full_manifest_sweep",
                "method": "complete object-store pagination",
                "bucket": source_id,
                "counts": {
                    "total_objects_indexed": self.state["objects_total"],
                    "inserted_this_run": total,
                },
                "artifacts_dir": str(config.DOWNLOADS_ROOT),
            }
        )

    # ---------- incremental poll ----------
    def _watermark(self, prefix: str) -> Optional[str]:
        v = db.get_meta(f"wm:{prefix}")
        return v or None

    def poll_once(self, prefix: str, cold: bool = False) -> int:
        start_after = None if cold else self._watermark(prefix)
        fresh: list[dict] = []
        max_key = None
        token = None
        try:
            while True:
                page = self.s3.list_page(
                    prefix,
                    continuation_token=token,
                    start_after=start_after,
                    max_keys=1000,
                )
                for o in page["objects"]:
                    o["top_prefix"] = prefix
                    fresh.append(o)
                    max_key = max(max_key or "", o["key"])
                    if len(fresh) >= 2000:
                        break
                if (
                    not page["is_truncated"]
                    or not page["next_token"]
                    or len(fresh) >= 2000
                ):
                    break
                token = page["next_token"]
        except Exception:
            return 0
        inserted = 0
        if fresh:
            changed = self._changed_objects(fresh)
            inserted = db.upsert_objects(fresh)
            if max_key:
                db.set_meta(f"wm:{prefix}", max_key)
            for o in changed:
                bus.publish(
                    {
                        "type": "new_object",
                        "key": o["key"],
                        "size": o["size"],
                        "top_prefix": o["top_prefix"],
                        "last_modified": o.get("last_modified", ""),
                        "seen_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                    }
                )
            self.state["new_since_start"] += len(changed)
        return inserted

    # ---------- wrapping key-order crawl ----------
    def crawl_once(self, prefix: str, cap: int | None = None) -> int:
        """Re-check a prefix in key order behind a rolling cursor that wraps.

        poll_once can only see keys lexicographically AFTER the stored
        watermark. Object keys start with usernames, so new uploads from
        names sorting before the watermark ('014anmol…', '9573970849…',
        'Bibi…', '425amit…' vs watermark 'chat_file/pavan_…') were
        permanently invisible until a full sweep. crawl_once advances a
        cursor through the entire keyspace of the prefix (wrapping to the
        start at the end), so every object — and every region where new
        uploads may land — is revisited at least once per full cold cycle.

        Efficiency: the cap limits objects *visited*, not only objects newly
        found. Fully unchanged pages advance the persisted cursor without a DB
        write, while size/ETag/timestamp changes are still detected.
        """
        if cap is None:
            cap = max(1000, int(getattr(config, "CRAWL_CAP", 2000)))
        if prefix not in self._crawl_cursor:
            self._crawl_cursor[prefix] = db.get_meta(f"crawl:{prefix}") or None
        cursor = self._crawl_cursor.get(prefix)
        changed: list[dict] = []
        stale_pages = 0
        visited = 0
        last_key: Optional[str] = None
        token: Optional[str] = None
        while visited < cap:
            page = self.s3.list_page(
                prefix, continuation_token=token, start_after=cursor, max_keys=1000
            )
            objs = page["objects"]
            visited += len(objs)
            page_changed: list[dict] = []
            if objs:
                page_changed = self._changed_objects(objs)
                # Advance over a fully unchanged page without a DB write.
                if not page_changed:
                    stale_pages += 1
                    last_key = objs[-1]["key"]
                    token = page.get("next_token")
                    if not page["is_truncated"] or not token:
                        last_key = None  # exhausted keyspace -> wrap next call
                        break
                    continue
            for o in page_changed:
                o["top_prefix"] = prefix
                changed.append(o)
            if objs:
                last_key = objs[-1]["key"]
            token = page.get("next_token")
            if not page["is_truncated"] or not token:
                last_key = None  # exhausted keyspace -> wrap to start next call
                break
        inserted = 0
        try:
            inserted = db.upsert_objects(changed)
            for o in changed:
                bus.publish(
                    {
                        "type": "new_object",
                        "key": o["key"],
                        "size": o["size"],
                        "top_prefix": o["top_prefix"],
                        "last_modified": o.get("last_modified", ""),
                        "seen_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                    }
                )
            self.state["new_since_start"] += len(changed)
        finally:
            self._crawl_cursor[prefix] = last_key
            db.set_meta(f"crawl:{prefix}", last_key or "")
            self.state.setdefault("crawl_progress", {})[prefix] = {
                "visited": visited,
                "changed": len(changed),
                "cursor": last_key,
            }
            if stale_pages:
                self.state.setdefault("crawl_skipped_pages", {})
                self.state["crawl_skipped_pages"][prefix] = stale_pages
        return inserted

    def poll_root_files(self) -> int:
        """Index objects sitting at bucket root (no folder) — missed by prefix sweeps."""
        fresh: list[dict] = []
        try:
            token = None
            while True:
                page = self.s3.list_page(
                    "", delimiter="/", continuation_token=token, max_keys=1000
                )
                for o in page["objects"]:
                    if "/" in o["key"]:
                        continue  # skip the folder-marker placeholder keys
                    o["top_prefix"] = "(root)"
                    fresh.append(o)
                if not page["is_truncated"] or not page["next_token"]:
                    break
                token = page["next_token"]
        except Exception:
            return 0
        inserted = db.upsert_objects(fresh)
        for o in fresh[:inserted]:
            bus.publish(
                {
                    "type": "new_object",
                    "key": o["key"],
                    "size": o["size"],
                    "top_prefix": "(root)",
                    "last_modified": o.get("last_modified", ""),
                    "seen_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                }
            )
        return inserted

    def rediscover_prefixes(self) -> list[str]:
        """Operators may add new top-level folders; pick them up."""
        try:
            live = [p for p in self.s3.list_prefixes_top() if p.strip("/")]
        except Exception:
            return []
        known = set((db.get_meta("prefixes") or "").splitlines())
        new_pfx = sorted(set(live) - known)
        new_pfx = [p for p in new_pfx if p.strip("/")]
        if new_pfx:
            db.set_meta("prefixes", "\n".join(sorted(known | set(new_pfx))))
            for p in new_pfx:
                self.poll_once(p, cold=True)
            evidence.append_collection_log(
                {
                    "event": "monitor_new_top_level_folders",
                    "method": "object-store delimiter listing",
                    "bucket": config.SOURCE_ID,
                    "new_folders": new_pfx,
                }
            )
        return new_pfx

    def poll_loop(self):
        tick = 0
        # parallel hot-poll cuts staleness: 8 prefixes sequentially took ~8-12s; threaded ~2-3s
        from concurrent.futures import ThreadPoolExecutor as _TPE

        while not self._stop.is_set():
            self.state["phase"] = "live-polling"
            # — hot prefixes in parallel (respect MAX_GET_RATE via client throttling)
            workers = max(1, getattr(config, "HOT_POLL_CONCURRENCY", 4))
            total_new = 0
            if workers > 1:
                with _TPE(max_workers=workers, thread_name_prefix="hot-poll") as ex:
                    futs = {
                        ex.submit(self.poll_once, p): p
                        for p in config.HOT_PREFIXES
                        if not self._stop.is_set()
                    }
                    for fut in futs:
                        try:
                            total_new += fut.result() or 0
                        except Exception:
                            pass
            else:
                for p in config.HOT_PREFIXES:
                    if self._stop.is_set():
                        break
                    total_new += self.poll_once(p) or 0
            if total_new and self.enricher:
                # drain once for the batch, not per-prefix (efficiency)
                self.enricher.drain_pending(
                    batch=config.ENRICH_BATCH, workers=config.ENRICH_WORKERS
                )
            tick += 1
            cold_every = max(
                1, int(config.COLD_RECHECK_SEC / max(1.0, config.POLL_INTERVAL_SEC))
            )
            if tick % cold_every == 0:
                self.poll_root_files()
                new_pfx = self.rediscover_prefixes()
                if new_pfx:
                    bus.publish({"type": "new_folders", "prefixes": new_pfx})
                all_p = (
                    db.get_meta("prefixes") or ""
                ).splitlines() or config.DEFAULT_PREFIXES
                # cold sweep — parallel wrapping crawl of the FULL keyspace.
                # poll_once(cold=True) would only revisit the first 2000 keys
                # and cannot see uploads sorting before the watermark, so use
                # crawl_once for every prefix (hot prefixes included: their
                # watermark blind spot is exactly how fresh chat evidence was
                # being missed).
                crawl_targets = [p for p in all_p if p.strip("/")]
                if crawl_targets:
                    with _TPE(max_workers=2, thread_name_prefix="cold-poll") as ex:
                        list(ex.map(self.crawl_once, crawl_targets))
            self.state["last_poll_utc"] = time.strftime(
                "%Y-%m-%dT%H:%M:%SZ", time.gmtime()
            )
            # Incremental + throttled recount (log review 2026-09-03):
            # total_new already sums poll_once inserted counts, so the hot
            # loop never needs a full COUNT(*) here.
            self.bump_total(total_new)
            self.state["objects_total"] = self.total_objects()
            try:
                self.record_liveness(bool(self.s3.head_bucket_ok()))
            except Exception:
                self.record_liveness(False)
            # adaptive: if we just found new objects, don't wait full interval — re-poll quickly
            if total_new > 0:
                self._stop.wait(min(3.0, config.POLL_INTERVAL_SEC))
            else:
                self._stop.wait(config.POLL_INTERVAL_SEC)

    def enrich_loop(self):
        if not self.enricher:
            return
        while not self._stop.is_set():
            self.enricher.drain_pending(
                batch=config.ENRICH_BATCH, workers=config.ENRICH_WORKERS
            )
            self._stop.wait(2.0)

    def wallet_loop(self):
        """Wallet-priority drain: hint-named chat images + bankingqr/qrcode/
        paymentgateway first, so crypto wallets surface without waiting for
        the ~1M backlog. Alternates with enrich_loop on the same drain lock."""
        if not self.enricher:
            return
        while not self._stop.is_set():
            try:
                keys = db.pending_wallets(96)
                if keys:
                    self.enricher.drain_specific(
                        keys, workers=max(1, config.ENRICH_WORKERS // 2)
                    )
            except Exception:
                pass
            self._stop.wait(20.0)

    def slip_loop(self):
        """Gambler-slip drain: deposit/withdrawal/payment images newest-first.

        Surfaces receiving-rail VPAs/UTRs the QR-tier queue never reaches.
        Gated by S3LM_SLIP_OCR (default off — slip backlog is 500k+).
        """
        if not self.enricher or not config.SLIP_OCR:
            return
        while not self._stop.is_set():
            try:
                keys = db.pending_slips(config.SLIP_BATCH)
                if keys:
                    self.enricher.drain_specific(
                        keys, workers=max(1, config.ENRICH_WORKERS // 2)
                    )
            except Exception:
                pass
            self._stop.wait(config.SLIP_INTERVAL_SEC)

    def start_background(self):
        master = evidence.load_master_vpas()
        master_wallets = evidence.load_master_wallets()

        def on_enriched(evt):
            bus.publish(evt)
            # dedicated alert channel: every freshly-extracted crypto wallet
            payload = evt.get("payload") or {}
            if payload.get("wallets"):
                bus.publish(
                    {
                        "type": "wallet_found",
                        "key": evt.get("key"),
                        "top_prefix": (evt.get("key") or "").split("/", 1)[0] + "/",
                        "seen_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                        "wallets": payload["wallets"],
                        "amount_inr": payload.get("amount_inr"),
                        "kind": payload.get("kind"),
                    }
                )

        self.enricher = EnrichmentWorker(
            self.s3, master, on_result=on_enriched, master_wallets=master_wallets
        )
        threading.Thread(target=self.full_sweep, daemon=True, name="sweep").start()
        threading.Thread(target=self.poll_loop, daemon=True, name="poll").start()
        threading.Thread(target=self.enrich_loop, daemon=True, name="enrich").start()
        threading.Thread(target=self.wallet_loop, daemon=True, name="wallet").start()
        threading.Thread(target=self.slip_loop, daemon=True, name="slip").start()

        def payment_index():
            try:
                result = db.rebuild_payment_index()
                evidence.append_collection_log(
                    {
                        "event": "monitor_payment_flow_index",
                        "method": "normalize OCR/QR and UTR reference joins into indexed money flows",
                        "counts": result,
                    }
                )
            except Exception as exc:
                evidence.append_collection_log(
                    {
                        "event": "monitor_payment_flow_index_error",
                        "error": str(exc)[:300],
                    }
                )

        threading.Thread(
            target=payment_index, daemon=True, name="payment-index"
        ).start()

        def payment_summary_loop():
            # A one-minute materialized view keeps dashboard aggregates fast
            # while newly OCR'd payments continue entering the normalized table.
            if self._stop.wait(60):
                return
            while not self._stop.is_set():
                try:
                    db.refresh_payment_summaries()
                except Exception:
                    pass
                try:
                    db.refresh_prefix_stats()
                except Exception:
                    pass
                self._stop.wait(60)

        threading.Thread(
            target=payment_summary_loop, daemon=True, name="payment-summary"
        ).start()

        def ext_backfill():
            try:
                n = db.backfill_ext()
                if n:
                    evidence.append_collection_log(
                        {
                            "event": "monitor_ext_backfill",
                            "method": "one-time indexed ext stamping for census",
                            "counts": {"rows": n},
                        }
                    )
            except Exception:
                pass

        threading.Thread(target=ext_backfill, daemon=True, name="ext-backfill").start()

    def stop(self):
        self._stop.set()
        if self.enricher:
            self.enricher.close()


collector_holder: dict = {}


def get_collector() -> Collector:
    return collector_holder["collector"]
