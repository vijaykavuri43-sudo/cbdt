"""GCS evidence monitor API for the preserved CASE-2026-CBTF object mirror."""

import asyncio
from datetime import datetime, timedelta
import csv
import hashlib
import io
import json
import mimetypes
import os
import re
import threading
import time
from pathlib import Path

from fastapi import Depends, FastAPI, HTTPException, Query, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, PlainTextResponse, StreamingResponse
from pydantic import BaseModel, ConfigDict, Field
from typing import Any

from . import config, db
from .collector import Collector, bus, collector_holder
from .evidence import DownloadManager, append_collection_log
from .s3client import S3PublicClient
from .gcsclient import GCSMirrorClient

_stats_cache: dict[str, tuple[float, Any]] = {}
_stats_locks: dict[str, threading.Lock] = {}
_stats_locks_guard = threading.Lock()
_stats_rebuilding: set[str] = set()


def _key_lock(key: str) -> threading.Lock:
    with _stats_locks_guard:
        lk = _stats_locks.get(key)
        if lk is None:
            lk = threading.Lock()
            _stats_locks[key] = lk
        return lk


def cached(key: str, ttl: float, builder, stale_grace: float = 600.0):
    """TTL cache with stampede protection (singleflight + stale-while-rebuild).

    Why: heavy aggregates (ledger/gateway summaries ~100s at current data
    scale) expire mid-morning when several tabs are open; without this, every
    tab fires its own rebuild concurrently — N x 100s queries hammer SQLite +
    CPU, the threadpool fills, and ALL tabs time out ("app not accessible").
    With this: at most one rebuild per key runs at a time; requests arriving
    mid-rebuild get the previous (stale) value instantly; expiry triggers one
    background rebuild while callers keep served-stale. First-ever miss still
    builds inline (unavoidable, once per deploy).
    """
    global _stats_cache
    now = time.monotonic()
    ent = _stats_cache.get(key)
    if ent and now - ent[0] < ttl:
        return ent[1]
    lk = _key_lock(key)
    acquired = lk.acquire(blocking=False)
    if not acquired:
        # Another thread is (re)building: serve stale rather than pile on.
        ent = _stats_cache.get(key)
        if ent:
            return ent[1]
        lk.acquire()  # no value at all — wait for the in-flight build
        acquired = True
    try:
        now = time.monotonic()
        ent = _stats_cache.get(key)
        if ent and now - ent[0] < ttl:
            return ent[1]
        if ent and key not in _stats_rebuilding and now - ent[0] < ttl + stale_grace:
            _stats_rebuilding.add(key)

            def _rebuild():
                try:
                    val = builder()
                    _stats_cache[key] = (time.monotonic(), val)
                except Exception:
                    pass
                finally:
                    _stats_rebuilding.discard(key)

            threading.Thread(
                target=_rebuild, daemon=True, name=f"cache-rebuild-{key}"
            ).start()
            return ent[1]
        if key in _stats_rebuilding and ent:
            return ent[1]
        val = builder()
        _stats_cache[key] = (time.monotonic(), val)
        return val
    finally:
        if acquired:
            lk.release()


def _invalidate_prefix(prefix: str):
    for k in list(_stats_cache.keys()):
        if k.startswith(prefix):
            _stats_cache.pop(k, None)


# Invalidate money-flows/status/stats the moment collector/enricher publishes,
# so dashboards never sit on a 20s stale cache while the bucket is live.
_orig_publish = None
try:
    from .collector import bus as _bus

    _orig_publish = _bus.publish

    def _cached_aware_publish(event: dict):
        try:
            t = (event or {}).get("type") or ""
            if t in ("new_object", "enriched", "wallet_found", "new_folders"):
                _invalidate_prefix("flows:")
                _invalidate_prefix("mule-rails:")
                # NOTE (log review 2026-09-03): status/stats/types used to be
                # popped here too. During live intake that busted the caches
                # on EVERY event, so each dashboard poll rebuilt them with
                # full-table scans (COUNT(*) + GROUP BY over ~1.4M rows),
                # saturating a CPU core and starving /api/sync/object past
                # the sync daemon's 10s notify timeout (68 notify errors).
                # Bounded staleness via TTL (5s/30s/60s) is the correct
                # freshness mechanism for aggregates; per-event busting is
                # kept only for the cheap paginated flows/mule-rails builders.
        except Exception:
            pass
        return _orig_publish(event)

    _bus.publish = _cached_aware_publish  # type: ignore[assignment]
except Exception:
    pass


app = FastAPI(title="GCS Evidence Monitor — CASE-2026-CBTF", version="2.0.0")
# In production the frontend dist is served by this same process (single VM / single port).
# Dev CORS kept for `npm run dev` on :5173
app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:5173", "http://127.0.0.1:5173"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# ---- serve built frontend (if present) ----
_FRONTEND_DIST = Path(__file__).resolve().parents[1] / "frontend" / "dist"
if _FRONTEND_DIST.exists():
    from fastapi.staticfiles import StaticFiles

    # mount assets
    if (_FRONTEND_DIST / "assets").exists():
        app.mount(
            "/assets",
            StaticFiles(directory=str(_FRONTEND_DIST / "assets")),
            name="assets",
        )


def _init_object_store():
    """Lazy init: GCS needs ADC credentials absent on dev machines.

    Fall back to the public-S3 client so `import backend.app` (tests,
    local dev without `gcloud auth`) never crashes at import time.
    The VM has the service-account scope, so production still uses GCS.
    """
    if config.OBJECT_SOURCE == "gcs":
        try:
            return GCSMirrorClient(config.GCS_BUCKET, config.GCS_PREFIX)
        except Exception as e:
            print(f"[store] GCS init failed ({e}); falling back to public S3")
            return S3PublicClient(
                config.BUCKET_URL, max_rate_per_sec=config.MAX_GET_RATE_PER_SEC
            )
    return S3PublicClient(
        config.BUCKET_URL, max_rate_per_sec=config.MAX_GET_RATE_PER_SEC
    )


object_store = _init_object_store()
collector = Collector(object_store)
collector_holder["collector"] = collector
downloads = DownloadManager(object_store)


def _actual_source() -> str:
    """Effective store type — differs from config when GCS creds are absent."""
    try:
        from .gcsclient import GCSMirrorClient as _GCS

        if isinstance(object_store, _GCS):
            return "gcs"
    except Exception:
        pass
    return "s3-fallback" if config.OBJECT_SOURCE == "gcs" else config.OBJECT_SOURCE


def _norm_prefix(prefix: str) -> str:
    """Canonical folder prefix: no leading slashes, trailing slash iff non-empty.

    The Explorer used to build '/banner/' (leading slash) when navigating from
    bucket root (2026-09-03: every folder click returned 0 rows). Object keys
    never start with '/', so strip them here as well — belt and braces.
    2026-09-11: also collapse interior '//' — openDir() appended '/' to a base
    that already ended with '/', so EVERY subfolder click below root produced
    e.g. 'paymentgateway//csv-files/' (LIKE-matched zero keys, ghost crumb).
    Collapsing here keeps old/bookmarked URLs working even before the
    frontend fix reaches the browser.
    """
    p = re.sub(r"/+", "/", (prefix or "").strip("/"))
    if p:
        p += "/"
    return p


def auth(request: Request):
    """Gate: every /api route requires the access key (header or ?token=).

    OFF-switch: start backend with S3LM_NO_GATE=1 (dashboard holds case PII —
    only do this on a single-investigator machine).
    """
    if os.environ.get("S3LM_NO_GATE") == "1":
        return
    tok = request.headers.get("authorization", "")
    if tok.lower().startswith("bearer "):
        tok = tok[7:]
    if not tok:
        tok = request.headers.get("x-api-token", "")
    if not tok:
        tok = request.query_params.get("token", "")
    if tok != config.API_TOKEN:
        raise HTTPException(401, "locked — access key required")


@app.on_event("startup")
def startup():
    for d in config.ensure_dirs:
        Path(d).mkdir(parents=True, exist_ok=True)
    db.connect()
    print(f"[gate] enabled; access key stored at {config.TOKEN_FILE}")

    # One-time repair of amounts persisted by pre-fix extraction (unbounded QR am=).
    # Runs off the request thread; Overview totals correct from first page load.
    def _repair():
        try:
            res = db.sanitize_stored_amounts()
            print(
                f"[maintenance] stored-amount sanitization: {res.get('impossible_amounts_cleared', 0)} rows fixed"
            )
        except Exception as e:
            print(f"[maintenance] sanitize_stored_amounts failed: {e}")
        try:
            # RFC-1123 -> ISO repair for rows swept from the mirror before
            # normalize_object_ts existed (Defect B, staleness review 2026-09-02).
            res = db.repair_non_iso_timestamps()
            if res.get("normalized"):
                print(
                    f"[maintenance] timestamp normalization: {res['normalized']} rows "
                    f"repaired ({res['unparseable']} unparseable left untouched)"
                )
        except Exception as e:
            print(f"[maintenance] repair_non_iso_timestamps failed: {e}")
        try:
            # 2026-09-16: revive rows poisoned by the pre-fix 5-attempt fetch
            # budget (fetch-miss went terminal during mirror lag). Runs every
            # startup; no-op when there is nothing to revive.
            res = db.requeue_fetch_misses()
            if res.get("requeued"):
                print(
                    f"[maintenance] fetch-miss requeue: {res['requeued']} rows revived"
                )
        except Exception as e:
            print(f"[maintenance] requeue_fetch_misses failed: {e}")

    threading.Thread(target=_repair, daemon=True).start()

    # 2026-09-16: prebuild the heavy officer aggregates in the background so
    # the first tab open after a restart never pays the cold-cache inline
    # build (ledger/gateway summaries scan ~660k rows — past the 25s frontend
    # timeout — which rendered Cash Flow as a timeout error).
    def _warm():
        try:
            from . import gateway, ledger

            cached("ledger:summary", 300.0, ledger.summary)
            cached("gateway:summary", 300.0, gateway.summary)
            print("[warmup] ledger+gateway summaries prebuilt")
        except Exception as e:
            print(f"[warmup] summary prebuild failed: {e}")

    threading.Thread(target=_warm, daemon=True, name="summary-warmup").start()

    if os.environ.get("S3LM_NO_AUTOSTART") != "1":
        collector.start_background()


@app.on_event("shutdown")
def shutdown():
    collector.stop()
    db.close()


@app.get("/api/live/list")
def live_list(
    prefix: str = "",
    flat: int = 0,
    q: str = "",
    limit: int = Query(200, le=1000),
    cursor: str = "",
    _=Depends(auth),
):
    """Ground-truth listing straight from the configured object store.

    prefix='' lists top-level folders + root files. Substring filter `q` is
    applied after fetch and bounded to eight object-store pages.
    """
    prefix = _norm_prefix(prefix)
    delimiter = None if flat else "/"
    items: list[dict] = []
    folders: list[str] = []
    from urllib.parse import unquote, quote as urlquote

    token = None
    start_after = None
    if cursor:
        if cursor.startswith("after:"):
            start_after = unquote(cursor[6:])
        else:
            token = cursor
    pages = 0
    truncated = False
    next_token = None
    try:
        while pages < 8:
            page = object_store.list_page(
                prefix,
                delimiter=delimiter,
                continuation_token=token,
                start_after=start_after,
                max_keys=1000,
            )
            pages += 1
            for o in page["objects"]:
                if (
                    "/" in o["key"]
                    and o["key"].endswith("/")
                    and not flat
                    and prefix == ""
                ):
                    continue  # folder marker objects
                if q and q.lower() not in o["key"].lower():
                    continue
                from .s3client import parse_s3_ts

                items.append(
                    {
                        "key": o["key"],
                        "size": o["size"],
                        "last_modified": parse_s3_ts(o.get("last_modified")),
                        "etag": o.get("etag", ""),
                    }
                )
                if len(items) >= limit:
                    break
            for p in page.get("prefixes") or []:
                p = p.rstrip("/") + "/"
                if p != "/" and p.strip("/"):
                    folders.append(p)
            if len(items) >= limit:
                truncated = True
                next_token = page.get("next_token")
                if not next_token:
                    if items:
                        next_token = "after:" + urlquote(items[-1]["key"], safe="")
                break
            if not page["is_truncated"] or not page["next_token"]:
                # one more consideration: folders may exist beyond this page
                next_token = None
                break
            token = page["next_token"]
    except Exception as e:
        raise HTTPException(502, f"bucket error: {e}")

    return {
        "prefix": prefix,
        "items": items,
        "folders": sorted(set(folders)),
        "has_more": bool(next_token),
        "next_cursor": next_token,
        "pages_fetched": pages,
        "source": config.SOURCE_ID,
    }


@app.get("/api/bucket-health")
def bucket_health(request: Request):
    """Live reachability probe with latency (1 list request).

    Deliberately NOT behind Depends(auth) (review 2026-09-02): the dashboard
    uses this endpoint to distinguish "evidence bucket down" from "session
    token stale". When it returned 401 for a stale token, the UI's
    .catch(() => setHealth({live:false})) showed "bucket not reachable" —
    misinformation that sent officers chasing a storage outage that never
    happened. It exposes no case data: only a boolean, latency, the bucket
    id, and the checked timestamp. It does NOT leak whether the token is
    valid; the rest of the app still gates everything.
    """
    t0 = time.monotonic()
    try:
        page = object_store.list_page("", delimiter="/", max_keys=1)
        ms = int((time.monotonic() - t0) * 1000)
        checked = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        return {
            "live": True,
            "latency_ms": ms,
            "checked_utc": checked,
            "checked_ist": _ist_str(checked),
            "bucket_url": config.SOURCE_ID,
            "object_source": config.OBJECT_SOURCE,
            "effective_source": _actual_source(),
        }
    except Exception as e:
        checked = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        return {
            "live": False,
            "error": str(e)[:200],
            "latency_ms": int((time.monotonic() - t0) * 1000),
            "checked_utc": checked,
            "checked_ist": _ist_str(checked),
            "bucket_url": config.SOURCE_ID,
            "object_source": config.OBJECT_SOURCE,
            "effective_source": _actual_source(),
        }


@app.get("/api/mirror-sync")
def mirror_sync(_=Depends(auth)):
    """Continuous S3->GCS mirror-sync daemon health (status.json it publishes).

    The sync daemon (s3-live-monitor/sync/s3-gcs-sync.py, container s3sync)
    keeps gs://<bucket>/s3-mirror/ continuously in sync with the live S3
    evidence bucket and publishes gs://<bucket>/s3-mirror-sync/status.json
    every cycle. Absent/old status => mirror frozen => dashboard data stale
    at the source, which officers must know (staleness review 2026-09-02).
    """
    payload: dict = {
        "gcs_bucket": config.GCS_BUCKET,
        "status_prefix": config.SYNC_STATUS_PREFIX,
        "authorization_ref": config.AUTH_REF,
    }
    try:
        from .gcsclient import GCSMirrorClient as _GCS

        client = object_store.client if isinstance(object_store, _GCS) else None
        if client is None:
            raise RuntimeError("GCS client unavailable (object_source not gcs)")
        bucket = client.bucket(config.GCS_BUCKET)
        blob = bucket.get_blob(config.SYNC_STATUS_PREFIX + "status.json")
        if blob is None:
            payload.update(
                {
                    "daemon_seen": False,
                    "verdict": "NO SYNC YET — mirror may be frozen at source; "
                    "continuous sync daemon has never published status",
                }
            )
        else:
            import json as _json

            data = _json.loads(blob.download_as_bytes(client=client))
            published = data.get("utc") or ""
            age_min = None
            if published:
                try:
                    pt = time.strptime(published, "%Y-%m-%dT%H:%M:%SZ")
                    import calendar as _cal

                    age_min = round((time.time() - _cal.timegm(pt)) / 60.0, 1)
                except ValueError:
                    pass
            daemon_state = (data.get("state") or "").lower()
            healthy = age_min is not None and age_min <= 15
            if daemon_state == "stopped":
                verdict = (
                    "STOPPED — sync daemon exited; mirror is frozen until it restarts"
                )
            elif daemon_state == "starting":
                verdict = "STARTING — daemon is up; first cycle in progress"
            elif daemon_state == "syncing" and healthy:
                verdict = (
                    f"LIVE — syncing continuously ({data.get('queued_pending', 0):,} "
                    f"objects queued this cycle)"
                )
            elif healthy:
                verdict = "LIVE — mirror syncing continuously"
            else:
                verdict = f"STALE — last daemon cycle {age_min} min ago"
            payload.update(
                {
                    "daemon_seen": True,
                    "status": data,
                    "daemon_state": daemon_state or "unknown",
                    "tombstones_pending": data.get("tombstones_pending", 0),
                    "status_age_min": age_min,
                    "healthy": healthy,
                    "verdict": verdict,
                }
            )
    except Exception as e:
        payload.update(
            {
                "daemon_seen": False,
                "error": str(e)[:200],
                "verdict": "UNKNOWN — status unreadable (source may not be GCS)",
            }
        )
    return payload


# ---------------- status / stats ----------------
@app.get("/api/status")
def status(_=Depends(auth)):
    return cached("status", 5.0, _build_status)


# Officer-facing times follow Indian Standard Time (UTC+5:30) — the bucket
# content is Indian betting evidence. UTC stays canonical (chain-of-custody);
# every *_ist field is a display sibling, never a replacement.
_IST_DELTA = timedelta(hours=5, minutes=30)


def _ist_str(utc_iso) -> str:
    try:
        dt = datetime.strptime(str(utc_iso)[:19], "%Y-%m-%dT%H:%M:%S") + _IST_DELTA
        return dt.strftime("%d %b %Y, %H:%M IST")
    except Exception:
        return ""


def _build_status():
    st = dict(collector.state)
    try:
        st["objects_total"] = collector.total_objects()
    except Exception:
        st["objects_total"] = st.get("objects_total", 0)
    st["bucket_url"] = config.SOURCE_ID
    st["source_label"] = config.SOURCE_LABEL
    st["peers"] = config.PEERS
    st["object_source"] = config.OBJECT_SOURCE
    st["effective_source"] = _actual_source()
    st["case"] = config.CASE_ID
    st["auth_ref"] = config.AUTH_REF
    st["prefixes"] = (
        db.get_meta("prefixes") or "\n".join(config.DEFAULT_PREFIXES)
    ).splitlines()
    st["enrichment"] = db.enrichment_progress()
    st["last_poll_ist"] = _ist_str(st.get("last_poll_utc"))
    st["started_ist"] = _ist_str(st.get("started_utc"))
    st["gemini_enabled"] = config.GEMINI_ENABLED
    st["gemini_model"] = config.GEMINI_MODEL if config.GEMINI_ENABLED else None
    try:
        from .enrich import drain_stats as _ds
        from .enrich import gemini_stats as _gs

        st["gemini_stats"] = _gs()
        st["drain_stats"] = _ds()
    except Exception:
        st["gemini_stats"] = {}
        st["drain_stats"] = {}
    st["max_amount_inr"] = config.MAX_AMOUNT_INR
    return st


@app.get("/api/types")
def types(_=Depends(auth)):
    """Census: what data exists in the bucket, by category."""
    return cached("types", 60.0, db.type_census)


@app.get("/api/stats")
def stats(_=Depends(auth)):
    # 30s TTL (log review 2026-09-03): the builder runs GROUP BY + json
    # aggregates over ~1.4M rows; 10s cadence kept a core busy.
    return cached("stats", 30.0, _build_stats)


def _build_stats():
    import time as _t

    today = _t.strftime("%Y-%m-%d", _t.gmtime())
    kinds = db.flow_kind_totals()
    dep, wd = kinds["deposit"], kinds["withdrawal"]
    try:
        _objects_total = collector.total_objects()
    except Exception:
        _objects_total = db.total_count()
    return {
        "objects_total": _objects_total,
        "per_prefix": db.prefix_stats(),
        "day_histogram": db.day_histogram(14),
        "deposits": {"count": dep["total"], "sum_amount_inr": dep["sum_amount_inr"]},
        "withdrawals": {"count": wd["total"], "sum_amount_inr": wd["sum_amount_inr"]},
        "today_utc": today,
    }


# ---------------- browsing ----------------
@app.get("/api/tree")
def tree(prefix: str = Query(""), _=Depends(auth)):
    prefix = _norm_prefix(prefix)
    return {"prefix": prefix, **db.subfolders(prefix)}


@app.get("/api/list")
def list_objs(
    request: Request,
    prefix: str = "",
    q: str = "",
    limit: int = Query(200, le=1000),
    offset: int = 0,
    order: str = Query("key", pattern="^(key|-modified|-seen|size)$"),
    ext: str = Query("", description="comma list e.g. .jpg,.png"),
    since: str = Query("", description="YYYY-MM-DD or ISO ts"),
    until: str = Query("", description="YYYY-MM-DD or ISO ts inclusive end"),
    flat: int = 0,
    _=Depends(auth),
):
    from fastapi.responses import JSONResponse as _JR

    prefix = _norm_prefix(prefix)
    exts = [e.strip().lower() for e in ext.split(",") if e.strip()]
    payload = db.list_objects(
        prefix,
        limit=limit,
        offset=offset,
        order=order,
        q=q,
        exts=exts,
        since=since,
        until=until,
        flat=bool(flat),
    )
    # Content ETag: total + page-window fingerprints. Repeat nav (Back button,
    # re-click, poll) answers 304 with zero JSON rebuild (2026-09-03 review).
    items = payload.get("items", [])
    first = items[0] if items else {}
    last = items[-1] if items else {}
    etag = hashlib.sha1(
        f"{prefix}\x00{order}\x00{limit}\x00{offset}\x00{payload.get('total')}\x00"
        f"{len(items)}\x00{first.get('key')}\x00{first.get('last_modified')}\x00"
        f"{last.get('key')}\x00{last.get('last_modified')}".encode()
    ).hexdigest()[:32]
    headers = {
        "ETag": f'"{etag}"',
        "Cache-Control": "private, max-age=30",
        "X-Cache-Status": "MISS",
    }
    if request is not None:
        inm = request.headers.get("if-none-match", "")
        if inm and etag in inm.replace('"', ""):
            from fastapi.responses import Response as _Resp

            return _Resp(
                status_code=304,
                headers={"ETag": f'"{etag}"', "X-Cache-Status": "REVALIDATED"},
            )
    return _JR(content=payload, headers=headers)


@app.get("/api/recent")
def recent(
    hours: float = Query(24, gt=0, le=168),
    limit: int = Query(60, le=500),
    _=Depends(auth),
):
    # 2026-09-10: seed for LiveFeed on page mount — newest arrivals first so
    # the feed is never initially empty (live SSE only appends post-mount).
    from fastapi.responses import JSONResponse as _JR

    return _JR(
        content={"items": db.recent_new(hours=hours, limit=limit)},
        headers={"Cache-Control": "private, max-age=30"},
    )


@app.get("/api/search")
def search(
    q: str = "",
    ext: str = "",
    since: str = "",
    prefix: str = "",
    min_size: int = 0,
    limit: int = Query(500, le=2000),
    _=Depends(auth),
):
    return {
        "items": db.search(
            q, ext=ext, since=since, prefix=prefix, min_size=min_size, limit=limit
        )
    }


@app.get("/api/prefixes/live")
def prefixes_live(_=Depends(auth)):
    """Fresh top-level folder discovery straight from the bucket (1 request)."""
    try:
        live = object_store.list_prefixes_top()
    except Exception as e:
        raise HTTPException(502, f"bucket unreachable: {e}")
    known_rows = (db.get_meta("prefixes") or "").splitlines()
    known = set(known_rows) | {"(root)"}
    unknown = [p for p in live if p not in known]
    return {
        "live_prefixes": live,
        "known_count": len(known_rows),
        "unknown_not_indexed": unknown,
        "root_files_hint": True,
    }


@app.post("/api/sync")
def sync(
    prefix: str = Query(
        "", description="folder to sync, empty = current prefix or all"
    ),
    _=Depends(auth),
):
    """On-demand object-store manifest refresh with proper pagination."""
    c = collector_holder["collector"]
    started = time.time()
    if prefix:
        prefix = _norm_prefix(prefix)
        # handle (root) pseudo-folder
        if prefix == "(root)/":
            n = c.poll_root_files()
            return {
                "synced_prefix": "(root)",
                "new_objects": n,
                "took_ms": int((time.time() - started) * 1000),
            }
        n = c.poll_once(prefix, cold=False)
        # one more cold pass catches lexicographically-earlier new keys missed by watermark
        if n == 0:
            n2 = c.poll_once(prefix, cold=True)
            if n2:
                n = n2
        c.state["last_poll_utc"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        c.bump_total(n)
        c.state["objects_total"] = c.total_objects()
        if n:
            db.invalidate_list_totals(prefix)
        return {
            "synced_prefix": prefix,
            "new_objects": n,
            "took_ms": int((time.time() - started) * 1000),
        }
    # no prefix -> sync all known prefixes (bounded, paginated per-prefix, not "fetch all to window")
    prefixes = [p for p in (db.get_meta("prefixes") or "").splitlines() if p.strip("/")]
    found: dict[str, int] = {}
    for p in prefixes:
        n = c.poll_once(p, cold=False)
        if n:
            found[p] = n
    root_new = c.poll_root_files()
    new_pfx = c.rediscover_prefixes()
    if found or root_new or new_pfx:
        db.invalidate_list_totals()
    return {
        "new_by_prefix": found,
        "new_root_files": root_new,
        "new_top_level_folders": new_pfx,
        "took_ms": int((time.time() - started) * 1000),
    }


class SyncObjectNotice(BaseModel):
    key: str
    size: int
    etag: str = ""
    last_modified: str = ""


@app.post("/api/sync/object")
def sync_object_notice(notice: SyncObjectNotice, _=Depends(auth)):
    """Low-latency metadata handoff from the local S3->GCS daemon.

    The daemon calls this only after the GCS upload succeeds. It closes the
    key-watermark blind spot without transferring evidence bytes through the
    API; the bounded version-aware cold crawl remains the recovery path.
    """
    key = notice.key.strip()
    if not key or key.endswith("/") or "\x00" in key:
        raise HTTPException(400, "invalid object key")
    top = key.split("/", 1)[0] + "/" if "/" in key else "(root)"
    obj = {
        "key": key,
        "top_prefix": top,
        "size": max(0, int(notice.size)),
        "etag": notice.etag,
        "last_modified": notice.last_modified,
    }
    c = collector_holder["collector"]
    changed = bool(c._changed_objects([obj]))
    inserted = db.upsert_objects([obj])
    if changed:
        db.invalidate_list_totals(top if top != "(root)" else "")
        c.state["new_since_start"] += 1
        c.state["last_poll_utc"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        # Incremental (log review 2026-09-03): a full COUNT(*) here on every
        # daemon upload stalled notifies past the 10s client timeout.
        c.bump_total(inserted)
        c.state["objects_total"] = c.total_objects()
        bus.publish(
            {
                "type": "new_object",
                "key": key,
                "size": obj["size"],
                "top_prefix": top,
                "last_modified": db.normalize_object_ts(notice.last_modified),
                "seen_utc": c.state["last_poll_utc"],
                "source": "s3sync-notify",
            }
        )
    return {"ok": True, "inserted": inserted, "changed": changed}


@app.get("/api/sheet")
def sheet_preview(
    key: str = Query(..., description="CSV/XLSX object key"),
    limit: int = Query(500, le=2000),
    _=Depends(auth),
):
    """Inline sheet preview for officers — parses CSV/XLSX server-side, returns paginated rows."""
    ext = key.rsplit(".", 1)[-1].lower() if "." in key.rsplit("/", 1)[-1] else ""
    if ext not in ("csv", "xlsx", "xls"):
        raise HTTPException(400, "sheet preview only for csv/xlsx/xls")
    try:
        resp, _ = object_store.get_object_stream(key)
        data = resp.read()
        resp.close()
    except RuntimeError as e:
        raise HTTPException(404, str(e))
    # cap download
    if len(data) > 15 * 1024 * 1024:
        raise HTTPException(
            413, "sheet too large for preview (>15 MB) — download instead"
        )
    headers: list[str] = []
    rows: list[list[str]] = []
    try:
        if ext == "csv":
            import csv
            import io

            text = data.decode("utf-8", errors="replace")
            # handle BOM
            if text.startswith("\ufeff"):
                text = text[1:]
            reader = csv.reader(io.StringIO(text))
            all_rows = list(reader)
            if not all_rows:
                return {"key": key, "headers": [], "rows": [], "total_rows": 0}
            headers = [h.strip() for h in all_rows[0]]
            rows = all_rows[1 : limit + 1]
        else:
            # xlsx/xls via openpyxl if available, fallback to error
            try:
                import openpyxl  # type: ignore
                import io as bio

                wb = openpyxl.load_workbook(
                    bio.BytesIO(data), read_only=True, data_only=True
                )
                ws = wb.active
                it = ws.iter_rows(values_only=True)
                first = next(it, None)
                if first is None:
                    return {"key": key, "headers": [], "rows": [], "total_rows": 0}
                headers = [str(c).strip() if c is not None else "" for c in first]
                for r in it:
                    if len(rows) >= limit:
                        break
                    rows.append([str(c) if c is not None else "" for c in r])
            except ImportError:
                raise HTTPException(
                    501,
                    "xlsx preview requires openpyxl on server (pip install openpyxl)",
                )
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(500, f"parse failed: {e}")
    total_rows = len(rows) + (1 if headers else 0)
    # try to detect if truncated
    truncated = len(rows) >= limit
    return {
        "key": key,
        "headers": headers,
        "rows": rows,
        "total_rows": total_rows,
        "truncated": truncated,
        "limit": limit,
    }


@app.get("/api/ledger/summary")
def ledger_summary(_=Depends(auth)):
    """Officer ledger totals: corrected ex-bonus cash from csv-files/ exports."""
    from . import ledger

    return cached("ledger:summary", 300.0, ledger.summary)


@app.post("/api/ledger/sync")
def ledger_sync(
    limit: int = Query(25, ge=1, le=200),
    keys: str = Query(
        "", description="comma-separated csv-files/ keys to force-reprocess (max 50)"
    ),
    _=Depends(auth),
):
    """Index new/changed csv-files/ exports (bounded per call).

    keys= (2026-09-17): one-time repair — reprocess already-indexed sheets
    whose wallet cells were never scanned pre-RC2 (bank_data xlsx). Ignored
    when empty; entries outside csv-files/ are dropped, capped at 50.
    """
    from . import ledger

    only = [k.strip() for k in (keys or "").split(",") if k.strip()] or None
    out = ledger.sync_ledger(object_store, limit_files=limit, only_keys=only)
    _invalidate_prefix("ledger:")
    return out


@app.get("/api/gateway/summary")
def gateway_summary(_=Depends(auth)):
    """Officer gateway totals: real deposits by brand + payout mule accounts."""
    from . import gateway

    return cached("gateway:summary", 300.0, gateway.summary)


@app.get("/api/network-urls")
def network_urls(_=Depends(auth)):
    """Officer network inventory: brand domains + QR-embedded URLs (CSV-ready)."""
    from . import gateway

    return cached("gateway:network-urls", 300.0, gateway.network_urls)


@app.post("/api/gateway/sync")
def gateway_sync(limit: int = Query(25, ge=1, le=200), _=Depends(auth)):
    """Index new/changed paymentgateway/csv-files/ exports (bounded per call)."""
    from . import gateway

    out = gateway.sync_gateway(object_store, limit_files=limit)
    _invalidate_prefix("gateway:")
    return out


@app.post("/api/verify")
def verify(_=Depends(auth)):
    """Cold catch-up poll of every prefix + root files; returns what was missed by hot polling."""
    c = collector_holder["collector"]
    prefixes = [p for p in (db.get_meta("prefixes") or "").splitlines() if p]
    found: dict[str, int] = {}
    for p in prefixes:
        n = c.poll_once(p, cold=True)
        if n:
            found[p] = n
    root_new = c.poll_root_files()
    # merge any newly discovered top-level folders
    try:
        live = object_store.list_prefixes_top()
    except Exception:
        live = []
    new_pfx = [p for p in live if p and p not in prefixes]
    for p in new_pfx:
        c.poll_once(p, cold=True)
    if new_pfx:
        db.set_meta("prefixes", "\n".join(sorted(set(prefixes) | set(new_pfx))))
    if found or root_new or new_pfx:
        db.invalidate_list_totals()
    return {
        "new_by_prefix": found,
        "new_root_files": root_new,
        "new_top_level_folders": new_pfx,
    }


# ---------------- object proxy (preview + download) ----------------
# ---- /api/object disk cache + thumbnails ----
# Grid views were fetching every full-size image live from GCS on each page
# paint (50 × ~0.5–2 MB per page). This cache stores fetched bytes (and
# resized thumbnails) on local disk keyed by key+etag, serves repeats with
# zero GCS traffic, and answers browser revalidation (304) from the manifest.
# 23GB free disk + ~15GB free RAM on the host (2026-09-03 review): split the
# disk cache so rare full-size Previews can't evict hot grid thumbnails, and
# keep the hottest thumbs in RAM (VM disk reads measured ~100ms vs ~7ms local).
_OBJ_CACHE_DIR = config.DATA_DIR / "objcache"
_OBJ_CACHE_THUMB_BYTES = (
    int(os.environ.get("S3LM_OBJCACHE_THUMB_MB", "6144")) * 1024 * 1024
)
_OBJ_CACHE_FULL_BYTES = (
    int(os.environ.get("S3LM_OBJCACHE_FULL_MB", "2048")) * 1024 * 1024
)
# Back-compat: single S3LM_OBJCACHE_MB overrides the combined default.
if "S3LM_OBJCACHE_MB" in os.environ and (
    "S3LM_OBJCACHE_THUMB_MB" not in os.environ
    and "S3LM_OBJCACHE_FULL_MB" not in os.environ
):
    _total = int(os.environ["S3LM_OBJCACHE_MB"]) * 1024 * 1024
    _OBJ_CACHE_THUMB_BYTES = _total * 3 // 4
    _OBJ_CACHE_FULL_BYTES = _total - _OBJ_CACHE_THUMB_BYTES
_OBJ_CACHE_FILE_CAP = 64 * 1024 * 1024  # don't cache single files above this
_objcache_writes = 0
_objcache_hits_ram = 0
_objcache_hits_disk = 0
_objcache_miss = 0


def _objcache_kind(thumb: int) -> str:
    return "t" if thumb else "f"


def _objcache_entry(key: str, etag: str, thumb: int, format: str = ""):
    # format is part of the key: a ?format=wav transcode gets its own entry,
    # otherwise the first-cached original shadows it forever (and a cached
    # transcode would poison plain requests). Voice-note 0:00, 2026-09-11.
    h = hashlib.sha256(f"{key}\x00{etag}\x00{thumb}\x00{format}".encode()).hexdigest()
    return _OBJ_CACHE_DIR / _objcache_kind(thumb) / h[:2] / (h + ".bin")


def _objcache_cap(thumb: int) -> int:
    return _OBJ_CACHE_THUMB_BYTES if thumb else _OBJ_CACHE_FULL_BYTES


def _objcache_trim(thumb: int):
    """Evict oldest entries of one kind (thumb/full) when over its own cap."""
    sub = _OBJ_CACHE_DIR / _objcache_kind(thumb)
    try:
        files = [(p.stat().st_mtime, p.stat().st_size, p) for p in sub.rglob("*.bin")]
    except OSError:
        return
    cap = _objcache_cap(thumb)
    total = sum(s for _, s, _ in files)
    if total <= cap:
        return
    files.sort()
    for mtime, size, p in files:
        try:
            p.unlink()
            try:
                p.with_suffix(".json").unlink()
            except OSError:
                pass
        except OSError:
            continue
        total -= size
        if total <= cap:
            break


# In-RAM hot LRU for thumbnails only (small ~8KB JPEGs; full images stay on
# disk). Kills the ~100ms VM disk-read stall on repeat grid paints.
from collections import OrderedDict as _OrderedDict

_RAM_CACHE_MAX_BYTES = int(os.environ.get("S3LM_RAMCACHE_MB", "512")) * 1024 * 1024
_ram_cache: _OrderedDict = _OrderedDict()
_ram_cache_bytes = 0


def _ram_get(cetag: str):
    global _ram_cache_bytes
    try:
        media, body = _ram_cache.pop(cetag)
    except KeyError:
        return None
    _ram_cache[cetag] = (media, body)  # re-insert = most-recently-used
    return media, body


def _ram_put(cetag: str, media: str, body: bytes):
    global _ram_cache_bytes
    if len(body) > 2 * 1024 * 1024:  # never RAM-cache big payloads
        return
    old = _ram_cache.pop(cetag, None)
    if old is not None:
        _ram_cache_bytes -= len(old[1])
    _ram_cache[cetag] = (media, body)
    _ram_cache_bytes += len(body)
    while _ram_cache_bytes > _RAM_CACHE_MAX_BYTES and _ram_cache:
        _, (_, b) = _ram_cache.popitem(last=False)
        _ram_cache_bytes -= len(b)


def _parse_range(range_header: str, total: int):
    """Parse a single HTTP Range header. Returns (start, end) inclusive or None.

    Voice-note seek fix (2026-09-11): <audio> seeks + Safari refuse streams
    that answer 200 to Range requests. Malformed/unsatisfiable ranges return
    None (caller serves 200) — never 500 on a preview path.
    """
    try:
        if not range_header or "=" not in range_header or total <= 0:
            return None
        unit, spec = range_header.split("=", 1)
        if unit.strip().lower() != "bytes":
            return None
        spec = spec.strip().split(",")[0].strip()
        if spec.startswith("-"):  # suffix range: last N bytes
            tail = int(spec[1:])
            if tail <= 0:
                return None
            return (max(total - tail, 0), total - 1)
        if "-" not in spec:
            return None
        s, _, e = spec.partition("-")
        start = int(s) if s.strip() else 0
        end = int(e) if e.strip() else total - 1
        if start >= total or end < start:
            return None
        return (start, min(end, total - 1))
    except (ValueError, AttributeError):
        return None


def _range_response(body: bytes, media: str, base_headers: dict, range_header: str):
    """Serve bytes with HTTP Range support (200 full / 206 partial)."""
    total = len(body)
    span = _parse_range(range_header, total)
    if span is None:
        # Full-200 audio/video MUST carry Content-Length + Accept-Ranges
        # (seek/duration fix 2026-09-16): without a length the <audio>
        # element shows 0:00, never advances its progress ("not moving
        # forward") and cannot seek. 206 branch already had both.
        return StreamingResponse(
            [body].__iter__(),
            media_type=media,
            headers={
                **base_headers,
                "Content-Length": str(total),
                "Accept-Ranges": "bytes",
            },
        )
    start, end = span
    chunk = body[start : end + 1]
    headers = {
        **base_headers,
        "Content-Range": f"bytes {start}-{end}/{total}",
        "Content-Length": str(len(chunk)),
        "Accept-Ranges": "bytes",
    }
    return StreamingResponse(
        [chunk].__iter__(), media_type=media, headers=headers, status_code=206
    )


def _transcode_wav(data: bytes) -> bytes | None:
    """Transcode voice-note bytes to WAV (universal voice-note fix).

    WhatsApp voice notes arrive as Opus-in-WebM misnamed .wav, AMR, 3GP,
    raw Opus/Ogg, or M4A — none of which plays everywhere (Safari cannot
    decode WebM/Opus or AMR; many desktop browsers cannot decode AMR/3GP).
    An officer pressing play on such a clip used to see 0:00 forever.
    A mono 16kHz WAV plays everywhere. Small clips transcode in ~ms; the
    result flows through the normal disk cache (keyed per ?format=wav so a
    transcode never poisons plain requests). Returns None when ffmpeg is
    missing or the transcode fails (caller serves original).
    """
    import shutil
    import subprocess

    if not data or shutil.which("ffmpeg") is None:
        return None
    try:
        p = subprocess.run(
            [
                "ffmpeg",
                "-hide_banner",
                "-loglevel",
                "error",
                "-i",
                "pipe:0",
                "-ac",
                "1",
                "-ar",
                "16000",
                "-f",
                "wav",
                "pipe:1",
            ],
            input=data,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=30,
        )
        out = p.stdout or b""
        if p.returncode == 0 and out[:4] == b"RIFF" and len(out) > 44:
            return out
        return None
    except Exception:
        return None


# Media types that NO browser plays reliably as voice notes (WebM/Opus on
# Safari, AMR/3GP almost everywhere, raw Ogg/Opus + M4A on older stacks).
# Served natively where possible for zero latency, transcoded to WAV via
# ?format=wav on demand. Genuine RIFF WAV and MP3 play everywhere natively
# and skip the transcode untouched. Voice-note 0:00/delay fix 2026-09-12.
_NEEDS_WAV_TRANSCODE = frozenset(
    {
        "audio/webm",
        "audio/amr",
        "audio/3gpp",
        "video/3gpp",
        "audio/ogg",
        "audio/opus",
        "audio/mp4",
        "audio/x-m4a",
        "audio/aac",
        "audio/flac",
    }
)


def _serve_cached(
    path,
    media,
    filename,
    etag,
    download,
    cache_status="HIT-disk",
    extra=None,
    range_header="",
):
    disp = f'{"attachment" if download else "inline"}; filename="{filename}"'
    headers = {
        "Content-Disposition": disp,
        "Cache-Control": "private, max-age=3600",
        "ETag": f'"{etag}"',
        "X-Cache-Status": cache_status,
        "X-Content-Type-Options": "nosniff",
    }
    if extra:
        headers.update(extra)
    if (
        range_header
        and not download
        and (media.startswith("audio/") or media.startswith("video/"))
    ):
        # Seekable voice-note/video serving (2026-09-11): slice cached bytes
        # into a 206 response. Files here are bounded by the cache file cap.
        try:
            return _range_response(path.read_bytes(), media, headers, range_header)
        except OSError:
            pass
    elif not download and (media.startswith("audio/") or media.startswith("video/")):
        headers["Accept-Ranges"] = "bytes"
    return FileResponse(
        str(path),
        media_type=media,
        filename=filename,
        headers=headers,
    )


def _sniff_media(head: bytes) -> str | None:
    """Magic-byte media override (QR/content-type fix 2026-09-03).

    Some mirrored objects carry a wrong stored content_type (e.g. text/plain
    for a real JPEG — the S3 source lied and the sync upload trusted it), so
    ext-first guessing still loses on ext-less keys. Magic bytes don't lie.
    Returns a media type or None when nothing conclusive.
    """
    if not head:
        return None
    if head[:3] == b"\xff\xd8\xff":
        return "image/jpeg"
    if head[:8] == b"\x89PNG\r\n\x1a\n":
        return "image/png"
    if head[:6] in (b"GIF87a", b"GIF89a"):
        return "image/gif"
    if head[:4] == b"RIFF" and head[8:12] == b"WEBP":
        return "image/webp"
    if head[:5] == b"%PDF-":
        return "application/pdf"
    if head[:4] == b"PK\x03\x04":
        return "application/zip"
    # ---- audio/video magic (voice-note fix 2026-09-03) ----
    # Chat voice notes are WhatsApp Opus-in-WebM misnamed .wav (EBML magic);
    # mislabeled audio/mp3 + nosniff made <audio> refuse them (0:00).
    if head[:4] == b"\x1a\x45\xdf\xa3":
        return "audio/webm"
    if head[:4] == b"OggS":
        return "audio/ogg"
    if head[:4] == b"RIFF" and head[8:12] == b"WAVE":
        return "audio/wav"
    if head[:4] == b"RIFF" and head[8:12] == b"AVI ":
        return "video/x-msvideo"
    if len(head) >= 12 and head[4:8] == b"ftyp":
        brand = head[8:12]
        if brand in (b"M4A ", b"mp42", b"isom", b"msf1", b"M4B ", b"heix"):
            return "audio/mp4"
        if brand[:3] in (b"3gp", b"3g2", b"3ge", b"3gs"):
            # WhatsApp-era .3gp / .amr-mode voice notes: browsers (esp.
            # Safari/Chrome) cannot decode these reliably -> transcode path.
            # Voice-note delay/0:00 fix 2026-09-12: sniff must name them so
            # the ?format=wav transcode triggers instead of serving a
            # natively-unplayable stream the <audio> tag shows as 0:00.
            return "audio/3gpp"
        return "video/mp4"
    if head[:3] == b"ID3" or (
        len(head) >= 2 and head[0] == 0xFF and (head[1] & 0xE0) == 0xE0
    ):
        return "audio/mpeg"
    if head[:5] == b"#!AMR":
        return "audio/amr"
    if head[:4] == b"fLaC":
        return "audio/flac"
    return None


# Extensions whose bytes MUST match a known magic to be servable. A media file
# that sniffs to None is not an exotic codec — it is corrupt/truncated mirror
# bytes (chat voice-note 0:00, 2026-09-05: valid Opus/WebM on the S3 source,
# unplayable bytes served from the GCS-mirror/cache path).
_VERIFY_MEDIA_EXTS = frozenset(
    set(config.AUDIO_EXTS)
    | set(config.IMAGE_EXTS)
    | {
        ".mp4",
        ".m4v",
        ".mov",
        ".3gp",
        ".3g2",
        ".avi",
        ".mkv",
        ".m4a",
        ".oga",
        ".opus",
        ".flac",
        ".webm",
    }
)


def _ext_of_key(key: str) -> str:
    base = (key or "").rsplit("/", 1)[-1]
    return ("." + base.rsplit(".", 1)[-1].lower()) if "." in base else ""


def verify_object_bytes(
    key: str, data: bytes, sha256_hex: str = "", expected_size: int = 0
) -> str:
    """Integrity gate for bytes fetched from the object store.

    Returns "" when the bytes look trustworthy, else a short reason:
    "empty" | "hash-mismatch" | "size-mismatch" | "magic-mismatch".
    Only runs checks for which evidence is available (no sha metadata ->
    no hash check; non-media extensions -> no magic check).
    """
    if not data:
        return "empty"
    if sha256_hex:
        try:
            if hashlib.sha256(data).hexdigest() != sha256_hex.strip().lower():
                return "hash-mismatch"
        except Exception:
            pass
    if expected_size and len(data) != expected_size:
        return "size-mismatch"
    if _ext_of_key(key) in _VERIFY_MEDIA_EXTS and _sniff_media(data[:32]) is None:
        return "magic-mismatch"
    return ""


_s3_repair_client = None


def _s3_repair_store():
    """Public-S3 client used ONLY to repair corrupt GCS-mirror bytes (2026-09-05).

    The evidence bucket is publicly readable; the GCS mirror stays primary.
    Read-only GETs, rate-limited like the main S3 client.
    """
    global _s3_repair_client
    if _s3_repair_client is None:
        _s3_repair_client = S3PublicClient(
            config.BUCKET_URL, max_rate_per_sec=config.MAX_GET_RATE_PER_SEC
        )
    return _s3_repair_client


def _fetch_repair_bytes(key: str) -> tuple[bytes | None, str]:
    """Fetch key from public S3. Returns (bytes|None, error)."""
    try:
        resp, _h = _s3_repair_store().get_object_stream(key)
        try:
            return b"".join(resp.iter_bytes(65536)), ""
        finally:
            try:
                resp.close()
            except Exception:
                pass
    except Exception as e:
        return None, str(e)[:160]


@app.get("/api/object")
def get_object(
    key: str,
    request: Request,
    download: int = 0,
    thumb: int = Query(0, ge=0, le=1024),
    format: str = Query("", pattern="^(|wav|mp3)$"),
    _=Depends(auth),
):
    global _objcache_writes
    if 0 < thumb < 64:
        thumb = 64
    filename = key.rsplit("/", 1)[-1] or "object"
    # Manifest row gives a fast etag for cache key + 304 handling (PK lookup).
    manifest_etag = ""
    manifest_size = 0
    if not download:
        try:
            row = (
                db.connect()
                .execute("SELECT etag, size FROM objects WHERE key=?", (key,))
                .fetchone()
            )
            if row:
                manifest_etag = row["etag"] or ""
                try:
                    manifest_size = int(row["size"] or 0)
                except (TypeError, ValueError):
                    manifest_size = 0
        except Exception:
            pass
    cache_etag = hashlib.sha1(
        f"{key}\x00{manifest_etag}\x00{thumb}\x00{format}".encode()
    ).hexdigest()[:32]
    global _objcache_hits_ram, _objcache_hits_disk, _objcache_miss
    if not download:
        inm = request.headers.get("if-none-match", "")
        if inm and cache_etag in inm.replace('"', ""):
            from fastapi.responses import Response as _Resp

            return _Resp(
                status_code=304,
                headers={"ETag": f'"{cache_etag}"', "X-Cache-Status": "REVALIDATED"},
            )
        if thumb:
            ram = _ram_get(cache_etag)
            if ram is not None:
                _objcache_hits_ram += 1
                ram_media, ram_body = ram
                return StreamingResponse(
                    [ram_body].__iter__(),
                    media_type=ram_media,
                    headers={
                        "Content-Disposition": f'inline; filename="{filename}"',
                        "Cache-Control": "private, max-age=3600",
                        "ETag": f'"{cache_etag}"',
                        "X-Cache-Status": "HIT-ram",
                        "X-Content-Type-Options": "nosniff",
                    },
                )
        hit = _objcache_entry(key, manifest_etag, thumb, format)
        if hit.exists():
            refetch = False
            try:
                meta = json.loads(hit.with_suffix(".json").read_text())
                _objcache_hits_disk += 1
                hit_media = meta.get("media", "application/octet-stream")
                # heal wrongly-cached media (text/plain JPEG etc.): one 32B read
                try:
                    with open(hit, "rb") as _hf:
                        _head = _hf.read(32)
                    _healed = _sniff_media(_head)
                    if _healed is None and _ext_of_key(key) in _VERIFY_MEDIA_EXTS:
                        # Corrupt-bytes eviction (voice-note 0:00, 2026-09-05):
                        # a cached media file matching NO known magic is mirror
                        # garbage, not an exotic codec — drop it so the MISS
                        # path below refetches (with S3 repair) instead of
                        # serving 0:00 forever.
                        try:
                            hit.unlink()
                            hit.with_suffix(".json").unlink(missing_ok=True)
                        except OSError:
                            pass
                        print(
                            f"[object] {key}: evicted corrupt cache entry (magic-mismatch)"
                        )
                        refetch = True
                    else:
                        hit_media = _healed or hit_media
                except OSError:
                    pass
                if not refetch:
                    if thumb and hit_media.startswith("image/"):
                        try:
                            _ram_put(cache_etag, hit_media, hit.read_bytes())
                        except OSError:
                            pass
                    return _serve_cached(
                        hit,
                        hit_media,
                        filename,
                        cache_etag,
                        download,
                        range_header=request.headers.get("range", ""),
                    )
            except Exception:
                pass
        _objcache_miss += 1
    mirror_src: dict = {}
    try:
        resp, headers = object_store.get_object_stream(key)
    except Exception as e:
        # Never 500 on preview (UPI/Mules "view QR" internal-error, 2026-09-08):
        # GCS raises google.api_core exceptions (NotFound/Forbidden/ServiceUnavailable),
        # not just RuntimeError — all become a clean 404/502 with an S3-repair attempt.
        # Mirror-lag fallback (chat PNG broken-preview, 2026-09-05): a key can
        # be listed (manifest/live) before the GCS mirror copy lands, so the
        # primary fetch 404s while public S3 already serves the bytes. Try S3
        # before 404ing so new uploads preview instead of broken-img.
        if _actual_source() == "gcs":
            alt, alt_err = _fetch_repair_bytes(key)
            if alt is not None and not verify_object_bytes(
                key, alt, "", manifest_size or len(alt)
            ):
                print(f"[object] {key}: GCS mirror miss; serving ({len(alt)}B) from S3")

                class _BufStream:
                    def __init__(self, payload):
                        self._p = payload

                    def iter_bytes(self, _chunk=65536):
                        yield self._p

                    def close(self):
                        pass

                resp = _BufStream(alt)
                headers = {
                    "content_length": str(len(alt)),
                    "sha256": hashlib.sha256(alt).hexdigest(),
                    "content_type": mimetypes.guess_type(key)[0] or "",
                }
                mirror_src = {"X-Object-Source": "s3-repair"}
            else:
                # Missing in both stores -> 404; fetch itself blew up (non-404
                # error) -> 502, never a bare 500 "Internal Server Error".
                msg = str(e)[:200]
                if "not found" in msg.lower() or "404" in msg or alt is None:
                    raise HTTPException(
                        404,
                        f"{msg} (S3 fallback "
                        f"{'failed: ' + alt_err if alt is None else 'also failed verify'}"
                        " — new uploads can lag the mirror; retry shortly)",
                    )
                raise HTTPException(502, f"object fetch failed: {msg}")
        else:
            raise HTTPException(404, str(e)[:200])
    mt = (
        mimetypes.guess_type(key)[0]
        or headers.get("content_type")
        or "application/octet-stream"
    )
    media = mt.split(";")[0].strip()
    if download:
        media = "application/octet-stream"
    else:
        # avoid browsers executing anything
        if media in ("text/html", "application/xhtml+xml", "image/svg+xml"):
            media = "text/plain"
    try:
        size_hint = int(headers.get("content_length") or 0)
    except (TypeError, ValueError):
        size_hint = 0
    cacheable = not download and (
        size_hint <= _OBJ_CACHE_FILE_CAP if size_hint else True
    )
    # Small evidence downloads are buffered too so their bytes can be
    # integrity-checked (never served corrupt); cache write stays inline-only.
    small_download = bool(download) and 0 < size_hint <= _OBJ_CACHE_FILE_CAP
    data = None
    if cacheable or small_download or (thumb and media.startswith("image/")):
        try:
            data = b"".join(resp.iter_bytes(65536))
        except Exception:
            data = None
        try:
            resp.close()
        except Exception:
            pass
    integrity_reason = ""
    if (
        data is not None
        and len(data) <= _OBJ_CACHE_FILE_CAP
        and (not download or small_download)
        and _actual_source() == "gcs"
    ):
        # Integrity gate (voice-note 0:00, 2026-09-05): corrupt GCS-mirror
        # bytes must never be cached or served when public S3 holds good ones.
        integrity_reason = verify_object_bytes(
            key, data, headers.get("sha256") or "", manifest_size or size_hint
        )
        if integrity_reason:
            alt, alt_err = _fetch_repair_bytes(key)
            if alt is not None and not verify_object_bytes(
                key, alt, "", manifest_size or len(alt)
            ):
                print(
                    f"[object] {key}: mirror bytes failed ({integrity_reason}); "
                    f"repaired from S3 ({len(alt)}B)"
                )
                data = alt
                size_hint = len(alt)
                if download:
                    media = "application/octet-stream"
                else:
                    media = _sniff_media(alt[:32]) or media
                integrity_reason = ""
            else:
                print(
                    f"[object] {key}: mirror bytes failed ({integrity_reason}); "
                    f"S3 repair {'failed: ' + alt_err if alt is None else 'also failed verify'}"
                )
    if data is not None and len(data) <= _OBJ_CACHE_FILE_CAP and not download:
        # bytes in hand: magic wins over any stored/ext guess (fixes QR-as-text)
        out_media = _sniff_media(data[:32]) or media
        body = data
        if (
            format == "wav"
            and out_media in _NEEDS_WAV_TRANSCODE
            and not integrity_reason
        ):
            # Browser can't decode this codec natively (WebM/Opus on Safari,
            # AMR/3GP almost everywhere): serve a transcoded WAV instead.
            # Genuine RIFF WAVs + MP3 (audio/wav, audio/mpeg) skip this untouched.
            wav = _transcode_wav(data)
            if wav is not None:
                body = wav
                out_media = "audio/wav"
        if thumb and media.startswith("image/"):
            try:
                from PIL import Image as _Image
                import io as _io

                im = _Image.open(_io.BytesIO(data))
                im.thumbnail((thumb, thumb))
                if im.mode in ("RGBA", "LA", "P"):
                    im = im.convert("RGB")
                buf = _io.BytesIO()
                im.save(buf, "JPEG", quality=72, optimize=True)
                body = buf.getvalue()
                out_media = "image/jpeg"
            except Exception:
                body = data
                out_media = media
        dest = _objcache_entry(key, manifest_etag, thumb, format)
        # Never poison the cache with bytes that failed the integrity gate,
        # and never cache downloads under the inline key (empty-etag orphan).
        if not integrity_reason and not download:
            try:
                dest.parent.mkdir(parents=True, exist_ok=True)
                dest.write_bytes(body)
                dest.with_suffix(".json").write_text(json.dumps({"media": out_media}))
                _objcache_writes += 1
                if thumb and out_media.startswith("image/"):
                    _ram_put(cache_etag, out_media, body)
                if _objcache_writes % 50 == 0:
                    _objcache_trim(thumb)
                return _serve_cached(
                    dest,
                    out_media,
                    filename,
                    cache_etag,
                    download,
                    cache_status="MISS",
                    extra=mirror_src or None,
                    range_header=request.headers.get("range", ""),
                )
            except OSError:
                pass
        # cache write failed/skipped: serve bytes directly from memory
        _mem_headers = {
            "Content-Disposition": f'inline; filename="{filename}"',
            "Cache-Control": "private, max-age=300",
            "ETag": f'"{cache_etag}"',
            "X-Cache-Status": "MISS",
            "X-Content-Type-Options": "nosniff",
            **mirror_src,
        }
        if integrity_reason:
            _mem_headers["X-Object-Integrity"] = integrity_reason
        if not download and (
            out_media.startswith("audio/") or out_media.startswith("video/")
        ):
            return _range_response(
                body,
                out_media,
                _mem_headers,
                request.headers.get("range", ""),
            )
        return StreamingResponse(
            [body].__iter__(),
            media_type=out_media,
            headers=_mem_headers,
        )
    if small_download and data is not None:
        # Buffered small download: verified (or unrepairable-source) bytes with
        # the exact pre-change download semantics (attachment + octet-stream).
        _dl_headers = {
            "Content-Disposition": f'attachment; filename="{filename}"',
            "Cache-Control": "private, max-age=300",
            "X-Cache-Status": "BYPASS-download",
            "X-Content-Type-Options": "nosniff",
            **mirror_src,
        }
        if integrity_reason:
            _dl_headers["X-Object-Integrity"] = integrity_reason
        return StreamingResponse(
            [data].__iter__(),
            media_type="application/octet-stream",
            headers=_dl_headers,
        )
    if data is not None:
        # too big to cache: serve from memory, keep stream semantics
        if not download and (media.startswith("audio/") or media.startswith("video/")):
            return _range_response(
                data,
                media,
                {
                    "Content-Disposition": f'inline; filename="{filename}"',
                    "Cache-Control": "private, max-age=300",
                    "X-Cache-Status": "BYPASS-too-big",
                    "X-Content-Type-Options": "nosniff",
                    **mirror_src,
                },
                request.headers.get("range", ""),
            )
        return StreamingResponse(
            [data].__iter__(),
            media_type=media,
            headers={
                "Content-Disposition": f'{"attachment" if download else "inline"}; filename="{filename}"',
                "Cache-Control": "private, max-age=300",
                "X-Cache-Status": "BYPASS-too-big",
                "X-Content-Type-Options": "nosniff",
                **mirror_src,
            },
        )
    # Unbuffered path: pull bytes NOW when bounded (clean 502 on mirror failure,
    # never a truncated 200 the browser reports as "internal error" on view QR).
    # Huge downloads (>file cap) keep true streaming to avoid memory blowup.
    if size_hint and size_hint <= _OBJ_CACHE_FILE_CAP:
        try:
            _tail = b"".join(resp.iter_bytes(65536))
        except Exception as e:
            try:
                resp.close()
            except Exception:
                pass
            raise HTTPException(502, f"object stream failed: {str(e)[:160]}")
        try:
            resp.close()
        except Exception:
            pass
        stream_body = [_tail].__iter__()
    else:
        stream_body = resp.iter_bytes(65536)
    return StreamingResponse(
        stream_body,
        media_type=media,
        headers={
            "Content-Disposition": f'{"attachment" if download else "inline"}; filename="{filename}"',
            "Cache-Control": "private, max-age=300",
            "X-Cache-Status": "BYPASS-stream",
            "X-Content-Type-Options": "nosniff",
            **mirror_src,
        },
    )


@app.get("/api/cache-stats")
def cache_stats(_=Depends(auth)):
    """Cache observability for tuning: RAM LRU + disk kinds + hit counters."""
    disk = {}
    for kind, cap in (("t", _OBJ_CACHE_THUMB_BYTES), ("f", _OBJ_CACHE_FULL_BYTES)):
        sub = _OBJ_CACHE_DIR / kind
        try:
            entries = list(sub.rglob("*.bin"))
            used = sum(p.stat().st_size for p in entries)
        except OSError:
            entries, used = [], 0
        disk[kind] = {
            "entries": len(entries),
            "bytes": used,
            "cap_bytes": cap,
            "pct": round(100.0 * used / cap, 1) if cap else 0,
        }
    hits = _objcache_hits_ram + _objcache_hits_disk
    total = hits + _objcache_miss
    return {
        "ram": {
            "entries": len(_ram_cache),
            "bytes": _ram_cache_bytes,
            "cap_bytes": _RAM_CACHE_MAX_BYTES,
        },
        "disk": disk,
        "hits_ram": _objcache_hits_ram,
        "hits_disk": _objcache_hits_disk,
        "miss": _objcache_miss,
        "hit_pct": round(100.0 * hits / total, 1) if total else None,
        "list_total_cache_entries": len(db._list_total_cache),
    }


class _AskBody(BaseModel):
    question: str = ""


@app.post("/api/ask")
def ai_ask(body: _AskBody, _=Depends(auth)):
    """AI investigator assistant: Gemini answers over live case data.

    Read-only (SELECTs + one bounded object fetch), gated, separately
    budgeted (S3LM_AIASK_DAILY_CAP) + per-minute rate limited. The Gemini
    key never leaves the server.
    """
    from .aiask import aiask_stats, ask

    try:
        out = ask(body.question or "", object_store)
        out["usage"] = aiask_stats()
        return out
    except Exception as e:
        return {
            "answer": f"Assistant error: {str(e)[:200]}",
            "sources": [],
            "model": "unavailable",
        }


@app.get("/api/ask/stats")
def ai_ask_stats(_=Depends(auth)):
    from .aiask import aiask_stats

    return aiask_stats()


@app.post("/api/enrich")
def enrich_now(
    key: str,
    gemini: int = 0,
    kind: str = Query("", pattern="^(|qr|deposit|withdrawal)$"),
    _=Depends(auth),
):
    """Re-run enrichment for a single object; gemini=1 forces Vision re-OCR.

    Vision provider is Groq (Gemini off by 2026-09-21 instruction; Groq holds
    the OCR quota). QR images decode locally (zxing) with no Vision call.
    kind= overrides classification for keys with no enrichable prefix
    (single-key officer action, recorded as enrich_kind_override)."""
    from .enrich import (
        EnrichmentWorker,
        classify,
        decode_qr_image,
        fetch_bytes,
        groq_ocr_slip,
        ocr_slip,
    )

    # Clear previous enrichment so process_key will not early-return
    try:
        # reset enriched flag via direct update so re-run works even on already-enriched rows
        db.connect().execute(
            "UPDATE objects SET enriched=0, extract_json=NULL WHERE key=?", (key,)
        )
        db.connect().commit()
    except Exception:
        pass
    if gemini and not config.GEMINI_ENABLED:
        raise HTTPException(
            400,
            "Gemini not configured on this host — set GEMINI_API_KEY / S3LM_GEMINI_API_KEY and restart",
        )
    if gemini:
        # Forced Vision path: bypass worker heuristic and call groq_ocr_slip
        # directly (Groq holds the quota; no Gemini per 2026-09-21).
        try:
            data = fetch_bytes(object_store, key)
        except Exception as e:
            raise HTTPException(404, f"object not found: {e}")
        auto_kind = classify(key)
        kind_overridden = False
        want = (kind or "").strip().lower()
        if auto_kind == "none" and want in ("qr", "deposit", "withdrawal"):
            # Officer override for single-key Vision re-OCR: the key carries
            # no enrichable prefix (e.g. chat_file without filename hint) but
            # the image is known evidence. Recorded in payload for audit.
            auto_kind = want
            kind_overridden = True
        kind = auto_kind
        if kind in ("deposit", "withdrawal"):
            payload = groq_ocr_slip(data)
            payload["kind"] = kind
            if kind_overridden:
                payload["enrich_kind_override"] = kind
            # still merge QR as EnrichmentWorker does
            q = decode_qr_image(data)
            if q.get("amount_inr") is not None or q.get("vpa"):
                payload.update({k: v for k, v in q.items() if v and not payload.get(k)})
            if q.get("vpas"):
                payload["vpas"] = list(
                    dict.fromkeys((payload.get("vpas") or []) + q["vpas"])
                )
                payload["vpa"] = payload.get("vpa") or payload["vpas"][0]
                merged_details = {
                    d.get("vpa"): d
                    for d in (payload.get("vpa_details") or [])
                    if d.get("vpa")
                }
                for detail in q.get("vpa_details") or []:
                    merged_details[detail.get("vpa")] = detail
                payload["vpa_details"] = list(merged_details.values())
            if q.get("wallets"):
                merged = {w["address"]: w for w in (payload.get("wallets") or [])}
                for w in q["wallets"]:
                    merged.setdefault(w["address"], w)
                payload["wallets"] = list(merged.values())
            from .evidence import load_master_vpas, load_master_wallets

            try:
                mv = load_master_vpas()
                if payload.get("vpa"):
                    payload["known_mule"] = payload["vpa"].lower() in mv
            except Exception:
                pass
            try:
                mw = load_master_wallets()
                if payload.get("wallets"):
                    for w in payload["wallets"]:
                        w["known_operator"] = (w.get("address") or "").lower() in mw
            except Exception:
                pass
            db.mark_enriched(key, payload)
        elif kind == "qr":
            payload = decode_qr_image(data)
            db.mark_enriched(key, payload)
        else:
            raise HTTPException(
                400, f"key not in enrichable prefixes (qr/deposit/withdrawal): {key}"
            )
        return db.get_object_row(key) or {"ok": True}
    # Normal tesseract (+ heuristic gemini) path
    w = EnrichmentWorker(object_store, set(), None)
    # reload masters for worker as well (empty set still works, enrich will still store)
    w.process_key(key)
    return db.get_object_row(key) or {"ok": False}


@app.get("/api/enrich/completeness")
def enrich_completeness(_=Depends(auth)):
    """Full-bucket parse audit: per-prefix indexed/enriched/pending/retry/dead/skipped.

    dead = 5 failed attempts (needs officer revive). skipped = triaged
    non-evidence (kind=none). Complete = pending 0 AND retry_wait 0 AND dead 0.
    """
    try:
        rows = (
            db.connect()
            .execute(
                "SELECT top_prefix, COUNT(*) n, "
                "SUM(enriched) done, "
                "SUM(CASE WHEN enriched=0 AND (enrich_retry_utc IS NULL OR enrich_retry_utc <= strftime('%Y-%m-%dT%H:%M:%SZ','now')) THEN 1 ELSE 0 END) pending, "
                "SUM(CASE WHEN enriched=0 AND enrich_retry_utc > strftime('%Y-%m-%dT%H:%M:%SZ','now') THEN 1 ELSE 0 END) retry_wait, "
                "SUM(CASE WHEN enrich_attempts>=5 THEN 1 ELSE 0 END) dead, "
                'SUM(CASE WHEN enriched=1 AND extract_json LIKE \'%"kind": "none"%\' THEN 1 ELSE 0 END) skipped '
                "FROM objects GROUP BY top_prefix ORDER BY n DESC"
            )
            .fetchall()
        )
        by_prefix = [dict(r) for r in rows]
    except Exception as e:
        raise HTTPException(500, f"completeness query failed: {e}")
    tot = lambda k: sum(int(r.get(k) or 0) for r in by_prefix)
    try:
        pay_rows = db.payment_row_count()
    except Exception:
        pay_rows = None
    complete = tot("pending") == 0 and tot("retry_wait") == 0 and tot("dead") == 0
    return {
        "complete": complete,
        "by_prefix": by_prefix,
        "totals": {
            "indexed": tot("n"),
            "enriched": tot("done"),
            "pending": tot("pending"),
            "retry_wait": tot("retry_wait"),
            "dead": tot("dead"),
            "skipped_none": tot("skipped"),
            "payment_rows": pay_rows,
        },
        "checked_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }


@app.post("/api/enrich/revive")
def enrich_revive(
    prefix: str = Query(""),
    limit: int = Query(5000, ge=1, le=50000),
    _=Depends(auth),
):
    """Re-queue dead objects (5 failed attempts) for another OCR pass.

    prefix scopes to one folder (e.g. deposit/); empty = all prefixes.
    """
    try:
        c = db.connect()
        if prefix:
            cur = c.execute(
                "UPDATE objects SET enriched=0, enrich_attempts=0, enrich_error=NULL, "
                "enrich_retry_utc=NULL, extract_json=NULL, enriched_utc=NULL "
                "WHERE enrich_attempts>=5 AND top_prefix=? "
                "AND key IN (SELECT key FROM objects WHERE enrich_attempts>=5 AND top_prefix=? LIMIT ?)",
                (prefix, prefix, limit),
            )
        else:
            cur = c.execute(
                "UPDATE objects SET enriched=0, enrich_attempts=0, enrich_error=NULL, "
                "enrich_retry_utc=NULL, extract_json=NULL, enriched_utc=NULL "
                "WHERE enrich_attempts>=5 "
                "AND key IN (SELECT key FROM objects WHERE enrich_attempts>=5 LIMIT ?)",
                (limit,),
            )
        c.commit()
        return {"revived": cur.rowcount, "prefix": prefix or "all", "limit": limit}
    except Exception as e:
        raise HTTPException(500, f"revive failed: {e}")


@app.post("/api/enrich/repair-twins")
def enrich_repair_twins(
    dry_run: int = Query(1, ge=0, le=1),
    limit: int = Query(20000, ge=1, le=100000),
    _=Depends(auth),
):
    """Repair persisted twin-misread amounts (₹ eaten into leading digit).

    dry_run=1 (default) scans and reports without writing. dry_run=0 corrects
    decidable rows (signature visible in stored text sample) and rebuilds
    their payment rows + summaries. Undecidable rows are counted as
    need_reocr for a future Vision pass.
    """
    try:
        return db.repair_twin_artifacts(dry_run=bool(dry_run), limit=limit)
    except Exception as e:
        raise HTTPException(500, f"repair-twins failed: {e}")


@app.post("/api/enrich/repair-fetch-misses")
def enrich_repair_fetch_misses(
    limit: int = Query(200000, ge=1, le=1000000),
    _=Depends(auth),
):
    """Revive evidence rows poisoned by the pre-fix 5-attempt fetch budget.

    Rows with enriched=1, no payload, and a fetch-miss error went terminal
    while the GCS mirror was still catching up. Resets them to pending with
    a fresh attempt budget so the live/slip loops pick them up. Also runs
    automatically on every startup; this endpoint is the manual re-run.
    """
    try:
        return db.requeue_fetch_misses(limit=limit)
    except Exception as e:
        raise HTTPException(500, f"repair-fetch-misses failed: {e}")


@app.post("/api/enrich/repair-grouping")
def enrich_repair_grouping(
    dry_run: int = Query(1, ge=0, le=1),
    limit: int = Query(20000, ge=1, le=100000),
    _=Depends(auth),
):
    """Repair persisted OCR-decoy amounts (bad-comma 10-100x inflation,
    loan/postpaid ad picks, ref/RRN ids, scratch ids, VPA-digit runs).

    dry_run=1 (default) scans and reports without writing. dry_run=0 nulls
    decidable rows for Gemini re-adjudication and rebuilds their payment
    rows + summaries. Undecidable rows are counted as need_reocr.
    """
    try:
        return db.repair_malformed_amounts(dry_run=bool(dry_run), limit=limit)
    except Exception as e:
        raise HTTPException(500, f"repair-grouping failed: {e}")


@app.post("/api/enrich/repair-wallets")
def enrich_repair_wallets(
    dry_run: int = Query(1, ge=0, le=1),
    limit: int = Query(20000, ge=1, le=100000),
    force: int = Query(0, ge=0, le=1),
    _=Depends(auth),
):
    """Remove stored transaction-id fragments kept as crypto wallets.

    dry_run=1 (default) scans and reports without writing. dry_run=0 drops
    wallets with positive txid-fragment evidence (label gap, ellipsis
    truncation, long-hex run, failed TRON checksum) and flags the row
    repaired. Undecidable rows are counted as need_reocr.
    force=1 reprocesses rows already stamped by an earlier repair run
    (recovery-graph upgrades deserve a second pass on still-invalid rows).
    """
    try:
        return db.repair_txid_wallets(
            dry_run=bool(dry_run), limit=limit, force=bool(force)
        )
    except Exception as e:
        raise HTTPException(500, f"repair-wallets failed: {e}")


class _SourceReplace(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    frm: str = Field("", alias="from")
    to: str = ""


class _SourceDrop(BaseModel):
    address: str = ""
    reason: str = ""
    evidence: str = ""


class _SourceCorrection(BaseModel):
    key: str = ""
    replace: list[_SourceReplace] = []
    add: list[str] = []
    drop: list[_SourceDrop] = []
    provenance: str = ""


class _SourceReadsBody(BaseModel):
    corrections: list[_SourceCorrection] = []
    dry_run: bool = True


@app.post("/api/enrich/apply-source-reads")
def enrich_apply_source_reads(body: _SourceReadsBody, _=Depends(auth)):
    """Apply officer-verified source-image wallet reads (strict math proof).

    dry_run=true (default) validates and reports without writing. Each
    correction: replace needs from-addr in row + checksum-valid to-addr;
    add needs checksum-valid addr; drop needs in-row addr + reason +
    non-wallet evidence (QR payload / txid context). Failures land in
    `skipped`, never written. Every write is provenance-stamped per key.
    """
    try:
        corrections = [
            {
                "key": c.key,
                "replace": [{"from": r.frm, "to": r.to} for r in c.replace],
                "add": list(c.add),
                "drop": [
                    {
                        "address": d.address,
                        "reason": d.reason,
                        "evidence": d.evidence,
                    }
                    for d in c.drop
                ],
                "provenance": c.provenance,
            }
            for c in body.corrections
        ]
        return db.apply_source_wallet_reads(corrections, dry_run=bool(body.dry_run))
    except Exception as e:
        raise HTTPException(500, f"apply-source-reads failed: {e}")


# ---------------- SSE live feed ----------------
@app.get("/api/events")
async def events(_=Depends(auth)):
    q = bus.subscribe()

    async def gen():
        try:
            yield f"retry: 5000\n\n"
            while True:
                try:
                    evt = await asyncio.to_thread(q.get, True, 30)
                    yield f"data: {json.dumps(evt, ensure_ascii=False)}\n\n"
                except Exception:
                    yield ": keepalive\n\n"
        finally:
            bus.unsubscribe(q)

    return StreamingResponse(gen(), media_type="text/event-stream")


# ---------------- deposits / UPI ----------------
@app.get("/api/deposits")
def deposits_api(
    kind: str = Query("deposit", pattern="^(deposit|withdrawal)$"),
    limit: int = 200,
    offset: int = 0,
    _=Depends(auth),
):
    return db.deposits(limit=limit, offset=offset, kind=kind)


@app.get("/api/money-flows")
def money_flows_api(
    direction: str = Query("deposit", pattern="^(deposit|withdrawal|both)$"),
    rail_type: str = Query("", pattern="^(|upi|bank_account|crypto_wallet|unknown)$"),
    q: str = "",
    limit: int = Query(200, ge=1, le=1000),
    offset: int = Query(0, ge=0),
    source: str = Query("", pattern="^(|object|reference)$"),
    since: str = Query("", max_length=32),
    until: str = Query("", max_length=32),
    folder: str = Query("", pattern="^(|all|deposit|withdrawal|bankingqr|other)$"),
    _=Depends(auth),
):
    """Normalized gambler↔payment-rail flows from OCR, QR, and case references.

    Officer Query tab uses direction=both + folder (deposit/withdrawal/bankingqr/other)
    + since/until. Existing Mule Money Flows tab keeps deposit|withdrawal defaults.
    """
    cache_key = f"flows:{direction}:{rail_type}:{q}:{limit}:{offset}:{source}:{since}:{until}:{folder}"
    # q-searches were cached 5s: every retry re-ran the expensive GROUP BYs.
    # 60s is fine for a search view (no-q fast path stays at 5s).
    return cached(
        cache_key,
        60.0 if q or since or until or folder else 5.0,
        lambda: db.money_flows(
            direction=direction,
            rail_type=rail_type,
            q=q,
            limit=limit,
            offset=offset,
            source=source,
            since=since,
            until=until,
            folder=folder,
        ),
    )


def _score_mule_rail_items(items: list) -> list:
    """Score + master-badge raw mule_rails() rows, highest-priority first.

    Shared by /api/mule-rails (paginated JSON) and /api/export/rails (full
    CSV) so the LE exhibit always matches on-screen triage exactly.
    Scoring fields are analyst-conclusion triage — never criminal attribution.
    """
    from .evidence import load_master_vpas, load_master_wallets

    master_vpas = load_master_vpas()
    master_wallets = load_master_wallets()
    for item in items:
        rail_id = str(item.get("rail_id") or "")
        known = (
            rail_id.lower() in master_vpas
            if item["rail_type"] == "upi"
            else rail_id.lower() in master_wallets
            if item["rail_type"] == "crypto_wallet"
            else False
        )
        both = bool(item["deposit_transactions"] and item["withdrawal_transactions"])
        role_evidence = int(item.get("high_role_evidence") or 0) + int(
            item.get("medium_role_evidence") or 0
        )
        score = min(
            100,
            (
                (30 if both else 0)
                + min(25, int(item.get("gamblers") or 0) * 4)
                + min(20, int(item.get("transactions") or 0) * 2)
                + (10 if int(item.get("object_evidence_rows") or 0) else 0)
                + (10 if role_evidence else 0)
                + (20 if known else 0)
            ),
        )
        item["known_indicator"] = known
        item["candidate_score"] = score
        item["priority"] = (
            "high" if score >= 60 else "medium" if score >= 30 else "lead"
        )
        item["fact_type"] = "fact"
        item["assessment_fact_type"] = "analyst_conclusion"
    return sorted(
        items,
        key=lambda item: (
            item["candidate_score"],
            item["deposit_transactions"] > 0 and item["withdrawal_transactions"] > 0,
            item["transactions"],
            item.get("last_seen") or "",
        ),
        reverse=True,
    )


_MULE_RAIL_CSV_COLUMNS = (
    "priority",
    "candidate_score",
    "rail_type",
    "rail_id",
    "utrs",
    "utr_count",
    "account_holders",
    "ifscs",
    "networks",
    "transactions",
    "deposit_transactions",
    "deposit_amount_inr",
    "deposit_gamblers",
    "withdrawal_transactions",
    "withdrawal_amount_inr",
    "withdrawal_gamblers",
    "gamblers",
    "object_evidence_rows",
    "reference_rows",
    "high_role_evidence",
    "medium_role_evidence",
    "low_role_evidence",
    "known_indicator",
    "first_seen",
    "last_seen",
    "fact_type",
    "assessment_fact_type",
)


def _iter_mule_rail_csv(scored: list):
    """Yield the full scored rail list as CSV text chunks (bounded memory)."""
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(_MULE_RAIL_CSV_COLUMNS)
    yield buf.getvalue()
    buf.seek(0)
    buf.truncate(0)
    for item in scored:
        writer.writerow([item.get(col, "") for col in _MULE_RAIL_CSV_COLUMNS])
        yield buf.getvalue()
        buf.seek(0)
        buf.truncate(0)


@app.get("/api/mule-rails")
def mule_rails_api(
    rail_type: str = Query("", pattern="^(|upi|bank_account|crypto_wallet)$"),
    q: str = "",
    priority: str = Query("", pattern="^(|high|medium|lead)$"),
    limit: int = Query(200, ge=1, le=1000),
    offset: int = Query(0, ge=0),
    source: str = Query("", pattern="^(|object|reference)$"),
    new_since: str = Query("", max_length=32),
    active_since: str = Query("", max_length=32),
    _=Depends(auth),
):
    """Rank observed payment rails for corroboration—not automatic attribution."""
    cache_key = f"mule-rails:{rail_type}:{q}:{priority}:{limit}:{offset}:{source}:{new_since}:{active_since}"

    def build():
        # Rank the complete filtered set before pagination so page order and
        # priority totals remain stable.
        raw = db.mule_rails(
            rail_type=rail_type,
            q=q,
            limit=10000,
            offset=0,
            source=source,
            new_since=new_since,
            active_since=active_since,
        )
        all_items = _score_mule_rail_items(raw["items"])
        # Scoring sorts by mule heuristic. When officers ask for a live window,
        # re-rank by deposit inflow in that window so Overview is not a
        # hardcoded UPI/wallet/account mix or a lifetime-score list.
        if active_since:
            all_items.sort(
                key=lambda item: (
                    float(item.get("window_deposit_amount_inr") or 0),
                    int(item.get("window_deposit_transactions") or 0),
                    item.get("window_last_seen") or item.get("last_seen") or "",
                ),
                reverse=True,
            )
        priority_counts = {
            name: sum(1 for item in all_items if item["priority"] == name)
            for name in ("high", "medium", "lead")
        }
        if priority:
            all_items = [item for item in all_items if item["priority"] == priority]
        raw["summary"]["priority_counts"] = priority_counts
        raw["summary"]["by_rail_type"] = {
            "upi": sum(1 for item in all_items if item.get("rail_type") == "upi"),
            "crypto_wallet": sum(
                1 for item in all_items if item.get("rail_type") == "crypto_wallet"
            ),
            "bank_account": sum(
                1 for item in all_items if item.get("rail_type") == "bank_account"
            ),
        }
        raw["total"] = len(all_items)
        raw["items"] = all_items[offset : offset + limit]
        raw["scoring_note"] = (
            "Heuristic triage score uses both-direction activity, linked gamblers, "
            "transaction frequency, object evidence, role evidence, and master indicators."
        )
        return raw

    return cached(cache_key, 15.0, build)


@app.get("/api/export/rails")
def export_rails(_=Depends(auth)):
    """Full suspected-mule-rail CSV for LE exhibits.

    Same one-pass GROUP BY aggregate as /api/mule-rails (db.mule_rails does
    the whole grouping in a single scan; pagination there is only slicing),
    same scoring + master badging via _score_mule_rail_items, streamed as CSV
    chunks so a 10k-rail exhibit never buffers the whole file in memory.
    Scores are analyst-conclusion triage — not criminal attribution.
    """

    def build():
        return _score_mule_rail_items(db.mule_rails(limit=10**9, offset=0)["items"])

    scored = cached("export-rails:full", 60.0, build)
    stamp = time.strftime("%Y-%m-%d", time.gmtime())
    return StreamingResponse(
        _iter_mule_rail_csv(scored),
        media_type="text/csv",
        headers={
            "Content-Disposition": (
                f'attachment; filename="suspected-mule-rails-full-{stamp}.csv"'
            ),
            "X-Content-Type-Options": "nosniff",
        },
    )


_MONEY_FLOW_EXPORT_COLUMNS = (
    "bucket_last_modified_utc",
    "indexed_seen_utc",
    "direction",
    "gambler",
    "amount_inr",
    "utr",
    "rail_type",
    "rail_id",
    "account_holder",
    "ifsc",
    "network",
    "confidence",
    "rail_role",
    "role_confidence",
    "source_object_key",
    "dashboard_source_link",
    "case",
    "auth_ref",
    "fact_type",
)


@app.get("/api/export/money-flows")
def export_money_flows(
    direction: str = Query("both", pattern="^(deposit|withdrawal|both)$"),
    rail_type: str = Query("", pattern="^(|upi|bank_account|crypto_wallet|unknown)$"),
    q: str = "",
    source: str = Query("object", pattern="^(|object|reference)$"),
    since: str = Query("", max_length=32),
    until: str = Query("", max_length=32),
    folder: str = Query("", pattern="^(|all|deposit|withdrawal|bankingqr|other)$"),
    _=Depends(auth),
):
    """Officer Query CSV (opens in Excel): filtered flows with object + stamps.

    Same filters as /api/money-flows. Defaults to live object evidence only.
    Includes bucket last_modified, mirror seen_utc, source object key, and an
    absolute dashboard link for manual verification.
    """

    def build():
        rows = db.money_flows_export_rows(
            direction=direction,
            rail_type=rail_type,
            q=q,
            source=source,
            since=since,
            until=until,
            folder=folder,
        )
        out = []
        for r in rows:
            key = r.get("source_key") or ""
            out.append(
                {
                    "bucket_last_modified_utc": r.get("last_modified") or "",
                    "indexed_seen_utc": r.get("seen_utc") or "",
                    "direction": r.get("direction") or "",
                    "gambler": r.get("gambler") or "",
                    "amount_inr": r.get("amount_inr")
                    if r.get("amount_inr") is not None
                    else "",
                    "utr": r.get("utr") or "",
                    "rail_type": r.get("rail_type") or "",
                    "rail_id": r.get("rail_id") or "",
                    "account_holder": r.get("account_holder") or "",
                    "ifsc": r.get("ifsc") or "",
                    "network": r.get("network") or "",
                    "confidence": r.get("confidence") or "",
                    "rail_role": r.get("rail_role") or "",
                    "role_confidence": r.get("role_confidence") or "",
                    "source_object_key": key,
                    "dashboard_source_link": _source_link(key)
                    if key and not key.startswith("reference:")
                    else "",
                    "case": config.CASE_ID,
                    "auth_ref": config.AUTH_REF,
                    "fact_type": "fact",
                }
            )
        return out

    cache_key = (
        f"export-flows:{direction}:{rail_type}:{q}:{source}:{since}:{until}:{folder}"
    )
    rows = cached(cache_key, 30.0, build)
    tag = f"{folder or direction}-{(rail_type or 'all')}-{since or 'start'}-to-{until or 'now'}"
    tag = re.sub(r"[^A-Za-z0-9._-]+", "_", tag)[:80]
    return _export_response(
        rows, _MONEY_FLOW_EXPORT_COLUMNS, f"officer-money-flows-{tag}"
    )


_UNIQUE_EXPORT_COLUMNS = (
    "rail_type",
    "rail_id",
    "rail_quality",
    "utrs",
    "utr_count",
    "account_holder",
    "ifsc",
    "network",
    "slips",
    "image_hits",
    "amount_inr",
    "first_seen_utc",
    "last_seen_utc",
    "sample_object_key",
    "dashboard_source_link",
    "case",
    "auth_ref",
    "fact_type",
)


@app.get("/api/money-flows-unique")
def money_flows_unique_api(
    rail_type: str = Query("", pattern="^(|upi|bank_account|crypto_wallet|unknown)$"),
    q: str = "",
    limit: int = Query(200, ge=1, le=1000),
    offset: int = Query(0, ge=0),
    source: str = Query("object", pattern="^(|object|reference)$"),
    since: str = Query("", max_length=32),
    until: str = Query("", max_length=32),
    folder: str = Query("", pattern="^(|all|deposit|withdrawal|bankingqr|other)$"),
    direction: str = Query("both", pattern="^(deposit|withdrawal|both)$"),
    _=Depends(auth),
):
    """One row per UPI/VPA, bank account, or crypto wallet for officer unique lists."""
    cache_key = f"flows-u:{rail_type}:{q}:{limit}:{offset}:{source}:{since}:{until}:{folder}:{direction}"
    return cached(
        cache_key,
        60.0,
        lambda: db.money_flows_unique(
            rail_type=rail_type,
            q=q,
            limit=limit,
            offset=offset,
            source=source,
            since=since,
            until=until,
            folder=folder,
            direction=direction,
        ),
    )


@app.get("/api/money-flows-unique-hits")
def money_flows_unique_hits_api(
    rail_type: str = Query(..., pattern="^(upi|bank_account|crypto_wallet)$"),
    rail_id: str = Query(..., min_length=1, max_length=256),
    source: str = Query("object", pattern="^(|object|reference)$"),
    since: str = Query("", max_length=32),
    until: str = Query("", max_length=32),
    folder: str = Query("", pattern="^(|all|deposit|withdrawal|bankingqr|other)$"),
    direction: str = Query("both", pattern="^(deposit|withdrawal|both)$"),
    limit: int = Query(500, ge=1, le=2000),
    offset: int = Query(0, ge=0),
    _=Depends(auth),
):
    """Every origin object for one unique Date-range address (closable expander)."""
    cache_key = f"flows-u-hits:{rail_type}:{rail_id}:{source}:{since}:{until}:{folder}:{direction}:{limit}:{offset}"
    return cached(
        cache_key,
        30.0,
        lambda: db.money_flows_unique_hits(
            rail_type=rail_type,
            rail_id=rail_id,
            source=source,
            since=since,
            until=until,
            folder=folder,
            direction=direction,
            limit=limit,
            offset=offset,
        ),
    )


@app.get("/api/export/money-flows-unique")
def export_money_flows_unique(
    rail_type: str = Query("", pattern="^(|upi|bank_account|crypto_wallet|unknown)$"),
    q: str = "",
    source: str = Query("object", pattern="^(|object|reference)$"),
    since: str = Query("", max_length=32),
    until: str = Query("", max_length=32),
    folder: str = Query("", pattern="^(|all|deposit|withdrawal|bankingqr|other)$"),
    direction: str = Query("both", pattern="^(deposit|withdrawal|both)$"),
    _=Depends(auth),
):
    """Unique address CSV — one VPA / account / wallet per row (not every slip)."""

    def build():
        rows = db.money_flows_unique_export_rows(
            rail_type=rail_type,
            q=q,
            source=source,
            since=since,
            until=until,
            folder=folder,
            direction=direction,
        )
        out = []
        for r in rows:
            key = r.get("sample_key") or ""
            out.append(
                {
                    "rail_type": r.get("rail_type") or "",
                    "rail_id": r.get("rail_id") or "",
                    "rail_quality": r.get("rail_quality") or "",
                    "utrs": r.get("utrs") or "",
                    "utr_count": r.get("utr_count") or 0,
                    "account_holder": r.get("account_holder") or "",
                    "ifsc": r.get("ifsc") or "",
                    "network": r.get("network") or "",
                    "slips": r.get("slips") or 0,
                    "image_hits": r.get("image_hits") or 0,
                    "amount_inr": r.get("amount_inr")
                    if r.get("amount_inr") is not None
                    else "",
                    "first_seen_utc": r.get("first_seen") or "",
                    "last_seen_utc": r.get("last_seen") or "",
                    "sample_object_key": key,
                    "dashboard_source_link": _source_link(key)
                    if key and not str(key).startswith("reference:")
                    else "",
                    "case": config.CASE_ID,
                    "auth_ref": config.AUTH_REF,
                    "fact_type": "fact",
                }
            )
        return out

    cache_key = (
        f"export-flows-u:{rail_type}:{q}:{source}:{since}:{until}:{folder}:{direction}"
    )
    rows = cached(cache_key, 30.0, build)
    tag = f"{folder or 'all'}-{(rail_type or 'all')}-{direction}-{since or 'start'}-to-{until or 'now'}"
    tag = re.sub(r"[^A-Za-z0-9._-]+", "_", tag)[:80]
    return _export_response(rows, _UNIQUE_EXPORT_COLUMNS, f"officer-unique-rails-{tag}")


def _source_link(key: str) -> str:
    """Absolute officer-verifiable link to the exact evidence object.

    Opens the sha256-verified object on the live dashboard so an officer can
    manually confirm any CSV row against the source screenshot/QR/file.
    Carries ?token= so the link opens in a fresh tab without re-unlocking
    (2026-10-03: pasted tokenless links 401'd despite an active session —
    new tabs send no Authorization header and each port is a separate
    origin). Treat exhibit CSVs as key-bearing material accordingly.
    """
    from urllib.parse import quote

    return (
        f"{config.PUBLIC_URL}/api/object?key={quote(key, safe='')}&download=1"
        f"&token={quote(config.API_TOKEN, safe='')}"
    )


_BUCKET_KEY_RE = re.compile(
    r"(?:^|[+\s])("
    r"(?:deposit|withdrawal|chat_file|bankingqr|qrcode|paymentgateway|payment|"
    r"csv-files|uploads|voice_notes|voice)/[^\s,|+]+"
    r")",
    re.I,
)


def _looks_like_bucket_key(value: str) -> bool:
    s = (value or "").strip()
    if not s or " " in s or s.startswith("http") or "/api/" in s.lower():
        return False
    return "/" in s and not s.lower().startswith("dashboard")


def _source_folder(key: str) -> str:
    s = (key or "").strip()
    if "/" not in s:
        return ""
    return s.split("/", 1)[0]


def _officer_source_path(
    proof_key: str, source_ref: str, live_keys: list[str] | None = None
) -> tuple[str, str, list[str]]:
    """Officer-facing bucket path (deposit/withdrawal/chat/…), never /api/wallets."""
    live = [k for k in (live_keys or []) if _looks_like_bucket_key(k)]
    proof = proof_key if _looks_like_bucket_key(proof_key) else ""
    extracted = ""
    m = _BUCKET_KEY_RE.search(source_ref or "")
    if m:
        extracted = m.group(1)
    path = proof or (live[0] if live else "") or extracted
    folder = _source_folder(path)
    all_keys = list(dict.fromkeys(([path] if path else []) + live))
    return path, folder, all_keys


def _iter_export_csv(columns: tuple, rows: list) -> Any:
    """Yield a full officer exhibit CSV in chunks (bounded memory)."""
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(columns)
    yield "\ufeff" + buf.getvalue()
    buf.seek(0)
    buf.truncate(0)
    for row in rows:
        writer.writerow([row.get(col, "") for col in columns])
        yield buf.getvalue()
        buf.seek(0)
        buf.truncate(0)


def _export_response(rows: list, columns: tuple, filename: str) -> StreamingResponse:
    stamp = time.strftime("%Y-%m-%d", time.gmtime())
    return StreamingResponse(
        _iter_export_csv(columns, rows),
        media_type="text/csv; charset=utf-8",
        headers={
            "Content-Disposition": f'attachment; filename="{filename}-{stamp}.csv"',
            "X-Content-Type-Options": "nosniff",
            "Cache-Control": "no-store",
        },
    )


_UPI_EXPORT_COLUMNS = (
    "vpa",
    "payee_name",
    "amount_inr",
    "status",
    "source_file",
    "source_folder",
    "qr_uploaded_utc",
    "seen_utc",
    "dashboard_source_link",
    "case",
    "auth_ref",
    "fact_type",
)


@app.get("/api/export/upis")
def export_upis(_=Depends(auth)):
    """Full decoded-VPA exhibit (ALL rows, old + new): every QR-decoded VPA with
    payee, amount, NEW/known status, source file + absolute dashboard link per
    row for officer download and manual verification. Opens in Excel."""

    def build():
        master = downloads_master()
        rows = []
        # Full exhibit keeps sender-side rows too (evidence completeness) —
        # the tab hides them by default via hide_senders.
        for it in db.upi_feed(10**9, hide_senders=False):
            vpa = it.get("vpa") or ""
            known = bool(vpa and vpa.lower() in master)
            key = it.get("key") or ""
            folder = key.split("/")[0] + "/" if "/" in key else "(root)"
            rows.append(
                {
                    "vpa": vpa,
                    "payee_name": it.get("payee_name") or "",
                    "amount_inr": it.get("amount_inr")
                    if it.get("amount_inr") is not None
                    else "",
                    "status": "known" if known else "NEW",
                    "source_file": key,
                    "source_folder": folder,
                    "qr_uploaded_utc": it.get("last_modified") or "",
                    "seen_utc": it.get("seen_utc") or "",
                    "dashboard_source_link": _source_link(key) if key else "",
                    "case": config.CASE_ID,
                    "auth_ref": config.AUTH_REF,
                    "fact_type": "fact",
                }
            )
        return rows

    return _export_response(
        cached("export-upis:full", 60.0, build),
        _UPI_EXPORT_COLUMNS,
        "decoded-vpas-full",
    )


_WALLET_HIT_EXPORT_COLUMNS = (
    "address",
    "network",
    "label",
    "source_folder",
    "source_file",
    "last_modified_utc",
    "amount",
    "amount_currency",
    "confidence",
    "checksum",
    "dashboard_source_link",
    "arkham_url",
    "tronscan_url",
    "case",
    "auth_ref",
    "fact_type",
)

_WALLET_DISTINCT_EXPORT_COLUMNS = (
    "address",
    "network",
    "hits",
    "first_seen_utc",
    "last_seen_utc",
    "source_folder",
    "sample_source_file",
    "all_source_files",
    "label",
    "amount",
    "amount_currency",
    "confidence",
    "checksum",
    "known_operator",
    "dashboard_source_link",
    "all_dashboard_links",
    "arkham_url",
    "tronscan_url",
    "case",
    "auth_ref",
    "fact_type",
)

_WALLET_REF_EXPORT_COLUMNS = (
    "address",
    "network",
    "role",
    "label",
    "attribution",
    "le_master",
    "case_ids",
    "notes",
    "source_folder",
    "source_path",
    "all_source_files",
    "source_ref",
    "proof_s3_key",
    "in_live",
    "first_seen_utc",
    "last_seen_utc",
    "arkham_url",
    "tronscan_url",
    "fact_type",
    "case",
    "auth_ref",
)


def _wallet_source_keys() -> dict[str, list[str]]:
    """All bucket object keys per distinct wallet address (newest first)."""
    out: dict[str, list[str]] = {}
    try:
        rows = (
            db.connect()
            .execute(
                "SELECT address, key FROM wallet_hits"
                " WHERE address!='' AND key!=''"
                " ORDER BY last_modified DESC"
            )
            .fetchall()
        )
    except Exception:
        return out
    for r in rows:
        addr = r["address"] or ""
        key = r["key"] or ""
        if not addr or not key:
            continue
        out.setdefault(addr, []).append(key)
    return out


def _wallet_explorer_urls(addr: str) -> tuple[str, str]:
    from urllib.parse import quote

    a = (addr or "").strip()
    if not a:
        return "", ""
    q = quote(a, safe="")
    return (
        f"https://platform.arkhamintelligence.com/explorer/address/{q}",
        f"https://tronscan.org/#/address/{q}",
    )


@app.get("/api/export/wallets")
def export_wallets(
    kind: str = Query("distinct", pattern="^(hits|distinct|reference)$"),
    _=Depends(auth),
):
    """Officer wallet CSVs matching the Crypto wallets cards / tables.

    hits — every bucket-decoded image×address row (the 410 card).
    distinct — one row per live address with all source files (the 132 card).
    reference — identified census, LE master operator rows flagged (2nd table).
    """
    from .evidence import load_master_wallet_rows, load_reference_wallets

    kind = (kind or "distinct").strip().lower()

    def build_hits():
        rows = []
        cur = db.connect().execute(
            "SELECT address, network, standard, confidence, checksum, kind,"
            " key, last_modified, amount_inr, amount_value, amount_currency"
            " FROM wallet_hits WHERE address!='' ORDER BY last_modified DESC"
        )
        for r in cur:
            addr = r["address"] or ""
            key = r["key"] or ""
            arkham, tron = _wallet_explorer_urls(addr)
            amt = (
                r["amount_value"] if r["amount_value"] is not None else r["amount_inr"]
            )
            rows.append(
                {
                    "address": addr,
                    "network": r["network"] or "",
                    "label": r["standard"] or "",
                    "source_folder": _source_folder(key),
                    "source_file": key,
                    "last_modified_utc": r["last_modified"] or "",
                    "amount": amt if amt is not None else "",
                    "amount_currency": r["amount_currency"] or "",
                    "confidence": r["confidence"] or "",
                    "checksum": r["checksum"] or "",
                    "dashboard_source_link": _source_link(key) if key else "",
                    "arkham_url": arkham,
                    "tronscan_url": tron,
                    "case": config.CASE_ID,
                    "auth_ref": config.AUTH_REF,
                    "fact_type": "ai_lead",
                }
            )
        return rows

    def build_distinct():
        keys_by_addr = _wallet_source_keys()
        master = {r["address_lower"] for r in load_master_wallet_rows()}
        rows = []
        off = 0
        while True:
            chunk = db.wallet_distinct(2000, off)
            if not chunk:
                break
            for it in chunk:
                addr = it.get("address") or ""
                key = it.get("key") or ""
                all_keys = keys_by_addr.get(addr) or ([key] if key else [])
                arkham, tron = _wallet_explorer_urls(addr)
                amt = it.get("amount_value")
                if amt is None:
                    amt = it.get("amount_inr")
                rows.append(
                    {
                        "address": addr,
                        "network": it.get("network") or "",
                        "hits": it.get("hits") or len(all_keys) or 1,
                        "first_seen_utc": it.get("first_seen") or "",
                        "last_seen_utc": it.get("last_seen") or "",
                        "source_folder": _source_folder(
                            key or (all_keys[0] if all_keys else "")
                        ),
                        "label": it.get("standard") or "",
                        "sample_source_file": key or (all_keys[0] if all_keys else ""),
                        "amount": amt if amt is not None else "",
                        "amount_currency": it.get("amount_currency") or "",
                        "confidence": it.get("confidence") or "",
                        "checksum": it.get("checksum") or "",
                        "known_operator": "yes" if addr.lower() in master else "no",
                        "dashboard_source_link": _source_link(key)
                        if key
                        else (_source_link(all_keys[0]) if all_keys else ""),
                        "all_source_files": " | ".join(all_keys),
                        "all_dashboard_links": " | ".join(
                            _source_link(k) for k in all_keys
                        ),
                        "arkham_url": arkham,
                        "tronscan_url": tron,
                        "case": config.CASE_ID,
                        "auth_ref": config.AUTH_REF,
                        "fact_type": "ai_lead",
                    }
                )
            off += len(chunk)
            if len(chunk) < 2000:
                break
        return rows

    def build_reference():
        live = _live_wallet_set()
        master = {r["address_lower"]: r for r in load_master_wallet_rows()}
        ref_rows = load_reference_wallets()
        times = db.wallet_times_by_address([r.get("address") or "" for r in ref_rows])
        keys_by_addr = _wallet_source_keys()
        rows = []
        for r in ref_rows:
            addr = r.get("address") or ""
            m = master.get(addr.lower())
            t = times.get(addr) or {}
            arkham, tron = _wallet_explorer_urls(addr)
            path, folder, all_keys = _officer_source_path(
                r.get("proof_s3_key") or "",
                r.get("source_ref") or "",
                keys_by_addr.get(addr) or [],
            )
            rows.append(
                {
                    "address": addr,
                    "network": r.get("network") or "",
                    "role": r.get("role") or "",
                    "label": (m or {}).get("label") or "",
                    "attribution": r.get("attribution") or "",
                    "le_master": "yes" if m else "no",
                    "case_ids": (m or {}).get("case_ids") or "",
                    "notes": (m or {}).get("notes") or "",
                    "source_folder": folder,
                    "source_path": path,
                    "all_source_files": " | ".join(all_keys),
                    "source_ref": r.get("source_ref") or "",
                    "proof_s3_key": r.get("proof_s3_key") or "",
                    "in_live": "yes" if addr in live else "no",
                    "first_seen_utc": t.get("first_seen") or "",
                    "last_seen_utc": t.get("last_seen") or "",
                    "arkham_url": arkham,
                    "tronscan_url": tron,
                    "fact_type": r.get("fact_type") or "fact",
                    "case": config.CASE_ID,
                    "auth_ref": config.AUTH_REF,
                }
            )
        rows.sort(key=lambda x: (0 if x["le_master"] == "yes" else 1, x["address"]))
        return rows

    if kind == "hits":
        return _export_response(
            cached("export-wallets:hits-v2", 30.0, build_hits),
            _WALLET_HIT_EXPORT_COLUMNS,
            "crypto-wallet-hits",
        )
    if kind == "reference":
        return _export_response(
            cached("export-wallets:reference-v3", 30.0, build_reference),
            _WALLET_REF_EXPORT_COLUMNS,
            "crypto-wallets-identified",
        )
    return _export_response(
        cached("export-wallets:distinct-v2", 30.0, build_distinct),
        _WALLET_DISTINCT_EXPORT_COLUMNS,
        "crypto-wallets-distinct",
    )


_PAYOUT_EXPORT_COLUMNS = (
    "account_holder",
    "account_number",
    "ifsc",
    "sample_txid",
    "amount_inr",
    "source_export_file",
    "source_row_no",
    "dashboard_source_link",
    "case",
    "auth_ref",
    "note",
)


@app.get("/api/export/payout-accounts")
def export_payout_accounts(_=Depends(auth)):
    """Full payout bank-account exhibit: every gateway payout row (holder,
    account, IFSC, txid, amount) with its source export file + absolute
    dashboard link. Observed accounts, not attributions. Opens in Excel."""

    def build():
        cur = db.connect().execute(
            "SELECT key,row_no,holder,account,ifsc,txid,amount FROM gateway_rows"
            " WHERE kind='gateway_payout' AND is_test=0 AND account!=''"
            " ORDER BY amount DESC"
        )
        return [
            {
                "account_holder": r["holder"] or "",
                "account_number": r["account"] or "",
                "ifsc": r["ifsc"] or "",
                "sample_txid": r["txid"] or "",
                "amount_inr": r["amount"] if r["amount"] is not None else "",
                "source_export_file": r["key"] or "",
                "source_row_no": r["row_no"],
                "dashboard_source_link": _source_link(r["key"]) if r["key"] else "",
                "case": config.CASE_ID,
                "auth_ref": config.AUTH_REF,
                "note": "observed bank account, not criminal attribution",
            }
            for r in cur.fetchall()
        ]

    return _export_response(
        cached("export-payouts:full", 60.0, build),
        _PAYOUT_EXPORT_COLUMNS,
        "payout-bank-accounts-full",
    )


@app.get("/api/extraction/status")
def extraction_status(_=Depends(auth)):
    """Explicit extraction/backfill status so sync and OCR progress are not conflated."""
    return {
        **db.enrichment_progress(),
        "payment_rows": db.payment_row_count(),
        "workers": config.ENRICH_WORKERS,
        "batch": config.ENRICH_BATCH,
        "historical_scope_hours": config.ENRICH_MAX_AGE_HOURS,
        "historical_scope": "all" if config.ENRICH_MAX_AGE_HOURS <= 0 else "bounded",
    }


@app.get("/api/upis")
def upis_api(
    limit: int = 500,
    offset: int = 0,
    distinct: bool = False,
    new_only: bool = False,
    hide_senders: bool = True,
    _=Depends(auth),
):
    """Paginated UPI tab (2026-09-11: newest-500 cap hid ~10k VPAs — the tab
    now pages through the full set; new_only filters server-side BEFORE
    limit/offset so pages stay full; hide_senders drops sender-only VPAs —
    innocent gambler counterparties — from items AND totals, same place.
    2026-09-16: the two upi_counts aggregates (up to ~4 full-table scans over
    ~1.5M rows) are cached 120s via singleflight+stale — an uncached tab load
    exceeded the 25s frontend timeout and rendered as all-zeros. Items stay
    live per request (one paged query); counts tolerate bounded staleness."""
    master = cached("upis:master", 300.0, downloads_master)
    exclude = master if new_only else set()
    limit = max(1, min(limit, 2000))
    offset = max(0, offset)
    if distinct:
        items = db.upi_distinct(limit, offset, exclude, hide_senders=hide_senders)
    else:
        items = db.upi_feed(limit, offset, exclude, hide_senders=hide_senders)
    for it in items:
        vpa = (it.get("vpa") or "").lower()
        it["known_mule"] = bool(vpa and vpa in master)
    totals = cached(
        f"upis:counts:{'new' if new_only else 'all'}:{1 if distinct else 0}:{1 if hide_senders else 0}",
        120.0,
        lambda: db.upi_counts(
            exclude, which="distinct" if distinct else "feed", hide_senders=hide_senders
        ),
    )
    total = totals["distinct_total"] if distinct else totals["feed_total"]
    new_total = (
        total
        if new_only
        # Same view grain as the tab (distinct vs feed) — a feed-grist NEW
        # count beside a unique-VPA tab was a stale mismatch (2026-09-11).
        else cached(
            f"upis:counts:new:{1 if distinct else 0}:{1 if hide_senders else 0}",
            120.0,
            lambda: db.upi_counts(
                master,
                which="distinct" if distinct else "feed",
                hide_senders=hide_senders,
            ),
        )["distinct_total" if distinct else "feed_total"]
    )
    return {
        "master_size": len(master),
        "items": items,
        "new_not_in_master": sum(1 for i in items if not i.get("known_mule")),
        "new_total": new_total,
        "distinct": distinct,
        "hide_senders": hide_senders,
        "limit": limit,
        "offset": offset,
        "total": total,
    }


def downloads_master() -> set[str]:
    from .evidence import load_master_vpas

    return load_master_vpas()


# ---- wallet full-scan (GCS s3-mirror source, not investigator files) ----
_wallet_scan_state: dict = {
    "running": False,
    "scanned": 0,
    "found": 0,
    "errors": 0,
    "started_utc": None,
    "last_key": None,
    "limit": 0,
    "rate_per_sec": 0.0,
}


def _wallet_full_scan_bg(limit: int, concurrency: int):
    import time
    from concurrent.futures import ThreadPoolExecutor

    from .enrich import EnrichmentWorker

    start = time.time()
    _wallet_scan_state["running"] = True
    _wallet_scan_state["scanned"] = 0
    _wallet_scan_state["found"] = 0
    _wallet_scan_state["errors"] = 0
    _wallet_scan_state["started_utc"] = time.strftime(
        "%Y-%m-%dT%H:%M:%SZ", time.gmtime()
    )
    _wallet_scan_state["limit"] = limit
    _wallet_scan_state["last_key"] = None
    scanned = 0
    try:
        # Master sets for badging inside worker
        try:
            from .evidence import load_master_vpas, load_master_wallets

            mv = load_master_vpas()
            mw = load_master_wallets()
        except Exception:
            mv, mw = set(), set()

        def _process_one(k: str):
            w = EnrichmentWorker(object_store, mv, bus.publish if bus else None, mw)
            try:
                before = db.wallet_stats()["wallet_hits"]
                w.process_key(k, full_scan=True)
                after = db.wallet_stats()["wallet_hits"]
                return (1, max(0, after - before), None)
            except Exception as e:
                return (1, 0, str(e)[:120])

        # Prioritised queue: bankingqr/qrcode/paymentgateway first, then deposit/withdrawal, then chat_file bulk
        remaining = limit
        batch = max(24, concurrency * 4)
        while remaining > 0 and _wallet_scan_state["running"]:
            keys = db.pending_wallet_candidates(min(batch, remaining))
            if not keys:
                break
            # Process this batch in parallel (GCS fetch + tesseract bound)
            with ThreadPoolExecutor(
                max_workers=max(1, concurrency), thread_name_prefix="wallet-scan"
            ) as pool:
                results = list(pool.map(_process_one, keys))
            for (c, f, e), k in zip(results, keys):
                scanned += c
                _wallet_scan_state["scanned"] = scanned
                _wallet_scan_state["last_key"] = k
                _wallet_scan_state["found"] += f
                if e:
                    _wallet_scan_state["errors"] += 1
                elapsed = max(1, time.time() - start)
                _wallet_scan_state["rate_per_sec"] = round(scanned / elapsed, 1)
                if bus:
                    try:
                        bus.publish(
                            {"type": "wallet_scan_progress", **dict(_wallet_scan_state)}
                        )
                    except Exception:
                        pass
            remaining -= len(keys)
            # refresh stats cache
            try:
                bus.publish(
                    {"type": "enriched"}
                )  # invalidates cached counts on frontend
            except Exception:
                pass
            if len(keys) < batch:
                break
    finally:
        _wallet_scan_state["running"] = False
        try:
            if bus:
                bus.publish({"type": "wallet_scan_done", **dict(_wallet_scan_state)})
        except Exception:
            pass


@app.post("/api/wallets/full-scan")
def wallets_full_scan(
    limit: int = Query(50000, ge=100, le=500000),
    concurrency: int = Query(6, ge=1, le=16),
    _=Depends(auth),
):
    """Trigger an efficient full-bucket wallet scan directly against the GCS s3-mirror.

    Source is live bucket objects via `gs://cbtf-case-evidence-518405669914/s3-mirror/`
    (the preserved copy, never the investigator's LE-SUBMISSION-MASTER files). Results
    are enriched rows whose `key` is the exact GCS object key — every hit carries its
    live source file link in the dashboard. Master CSV is only used to badge known_operator.
    """
    if _wallet_scan_state["running"]:
        return {
            "ok": False,
            "error": "scan already running",
            **dict(_wallet_scan_state),
        }
    import threading

    t = threading.Thread(
        target=_wallet_full_scan_bg, args=(limit, concurrency), daemon=True
    )
    t.start()
    return {"ok": True, "started": True, **dict(_wallet_scan_state)}


@app.get("/api/wallets/full-scan/status")
def wallets_full_scan_status(_=Depends(auth)):
    # live counts: pending wallet queue size + scan progress
    try:
        pending_full = db.pending_enrichment_count(full_scan=True)
        pending_live = db.pending_enrichment_count(full_scan=False)
    except Exception:
        pending_full = pending_live = 0
    return {
        **dict(_wallet_scan_state),
        "pending_full_scan": pending_full,
        "pending_live": pending_live,
        "source": str(config.SOURCE_ID),
        "gcs_bucket": config.GCS_BUCKET,
        "gcs_prefix": config.GCS_PREFIX,
    }


@app.get("/api/wallets")
def wallets_api(
    limit: int = Query(300, le=2000),
    offset: int = Query(0, ge=0),
    distinct: bool = False,
    q: str = Query("", max_length=128),
    network: str = Query("", max_length=32),
    sort: str = Query("recent", pattern="^(recent|hits|amount)$"),
    _=Depends(auth),
):
    """Crypto-wallet exhibit, bucket-native: every row is one TRON/ERC20/BTC
    address decoded from a QR / deposit-slip / crypto chat image in the live
    GCS bucket. Each item carries its exact source object key, so every row
    in the Crypto tab has view-proof. No external reference lists.
    2026-09-16: paginated (limit/offset) + distinct mode — the tab pages
    through the FULL set so old wallets never fall off the newest-N window.
    2026-09-17: q (address/source substring) + network filters and distinct
    sort (recent/hits/amount); distinct rows carry the newest hit's
    amount/txids/checksum so the Detail column is populated."""
    limit = max(1, min(limit, 2000))
    offset = max(0, offset)
    if distinct:
        items = db.wallet_distinct(limit, offset, q=q, network=network, sort=sort)
    else:
        items = db.wallet_feed(limit, offset, q=q, network=network)
    stats = db.wallet_stats()
    totals = cached(
        f"wallets:counts:{q}:{network}", 120.0, lambda: db.wallet_counts(q, network)
    )
    total = totals["distinct_total"] if distinct else totals["feed_total"]
    # Source stats for officer: enrichment backlog explains nil hits.
    # pending = HONEST total undecoded (live queue + older slip backlog that
    # holds most wallet-bearing deposit/withdrawal images) — never the
    # live-queue-only figure that reads 0 while 500k+ slips await decode.
    try:
        enr = db.enrichment_progress()
        _live = enr.get("pending_now") or 0
        _slip = enr.get("slip_backlog") or 0
        extraction_status = {
            "enriched": enr.get("enriched_done"),
            "pending": _live + _slip,
            "pending_live": _live,
            "slip_backlog": _slip,
            "objects": enr.get("total"),
            "pending_full_scan": db.pending_enrichment_count(full_scan=True),
        }
    except Exception:
        extraction_status = {}
    from .evidence import load_master_wallet_rows

    master_rows = load_master_wallet_rows()
    master_set = {r["address_lower"] for r in master_rows}
    for it in items:
        addr = (it.get("address") or "").lower()
        it["known_operator"] = bool(addr) and addr in master_set
    try:
        live_addrs = _live_wallet_set()
        new_not_in_master = sum(1 for a in live_addrs if a.lower() not in master_set)
    except Exception:
        new_not_in_master = sum(1 for it in items if not it.get("known_operator"))
    master_name = Path(config.MASTER_WALLET_CSV).name
    return {
        "items": items,
        "wallet_hits_total": stats["wallet_hits"],
        "distinct_wallets": stats["distinct_wallets"],
        "by_network": stats["by_network"],
        "extraction_status": extraction_status,
        "source": str(config.SOURCE_ID),
        "distinct": distinct,
        "limit": limit,
        "offset": offset,
        "total": total,
        "q": q,
        "network": network,
        "sort": sort,
        "master_size": len(master_rows),
        "master_rows": master_rows,
        "master_source": f"LE-SUBMISSION-MASTER/{master_name}"
        if master_rows
        else master_name,
        "new_not_in_master": new_not_in_master,
    }


def _live_wallet_set() -> set[str]:
    return cached("wallets:live-set", 120.0, db.live_wallet_addresses)


@app.get("/api/wallets/reference")
def wallets_reference(_=Depends(auth)):
    """All previously-identified wallets (reference census, 87 rows).

    Source rule (strict): the ONLY source is the true original object key in
    the GCS bucket (synced from S3: deposit / bankingqr / chat_file / voice
    notes / csv-files). `proof_s3_key` carries that key; `in_mirror` tells
    whether THIS host's mirror holds it right now. False = the object was
    deleted at the source before capture (deposit JSON + withdrawal proofs,
    verified 404) — the row keeps its true key and is badged deleted, never
    relinked to investigator-created files. `in_live` flags live-decoded
    coverage (identified vs decoded). Attribution labels are report
    attributions, NOT proof of ownership.
    """
    from .evidence import load_master_wallet_rows, load_reference_wallets

    live = _live_wallet_set()
    master = {r["address_lower"]: r for r in load_master_wallet_rows()}
    ref_rows = load_reference_wallets()
    present = db.keys_in_mirror([r.get("proof_s3_key") or "" for r in ref_rows])
    keys_by_addr = _wallet_source_keys()
    items = []
    for r in ref_rows:
        addr = r.get("address") or ""
        pk = r.get("proof_s3_key") or ""
        m = master.get(addr.lower())
        path, folder, all_keys = _officer_source_path(
            pk, r.get("source_ref") or "", keys_by_addr.get(addr) or []
        )
        items.append(
            {
                **r,
                "in_live": addr in live,
                "in_mirror": pk in present if pk else None,
                "le_master": bool(m),
                "label": (m or {}).get("label") or "",
                "case_ids": (m or {}).get("case_ids") or "",
                "notes": (m or {}).get("notes") or "",
                "le_confidence": (m or {}).get("confidence") or "",
                "le_fact_type": (m or {}).get("fact_type") or "",
                "source_path": path,
                "source_folder": folder,
                "all_source_files": all_keys,
            }
        )
    items.sort(key=lambda i: (0 if i.get("le_master") else 1, i.get("address") or ""))
    times = db.wallet_times_by_address([i.get("address") or "" for i in items])
    for i in items:
        t = times.get(i.get("address") or "") or {}
        i["first_seen"] = t.get("first_seen") or ""
        i["last_seen"] = t.get("last_seen") or ""
    identified = len(items)
    decoded = sum(1 for i in items if i["in_live"])
    return {
        "items": items,
        "identified_total": identified,
        "live_decoded": decoded,
        "missing_from_live": identified - decoded,
        "le_master_total": sum(1 for i in items if i.get("le_master")),
        "fact_type": "fact",
        "assessment_note": "attribution labels are report attributions, not proof of ownership/control",
    }


# ---------------- downloads / evidence ----------------
class DLReq(BaseModel):
    keys: list[str]
    label: str = ""


@app.post("/api/download")
def download(req: DLReq, _=Depends(auth)):
    if not req.keys or len(req.keys) > 5000:
        raise HTTPException(400, "keys required (max 5000 per job)")
    job_id = downloads.enqueue(req.keys, req.label)
    return {"job_id": job_id, "outdir": downloads.jobs[job_id]["outdir"]}


@app.get("/api/download/jobs")
def jobs(_=Depends(auth)):
    return {"jobs": list(downloads.jobs.values())[::-1]}


@app.get("/api/evidence/log")
def evidence_log(tail: int = 50, _=Depends(auth)):
    try:
        lines = open(config.EVIDENCE_LOG, encoding="utf-8").read().splitlines()
    except OSError:
        lines = []
    monitor_lines = [
        ln
        for ln in lines[-2000:]
        if "monitor_" in ln or "evidence_download" in ln or "evidence_batch" in ln
    ]
    return {
        "tail": [
            json.loads(x) for x in monitor_lines[-tail:] if x.strip().startswith("{")
        ]
    }


@app.get("/api/evidence/exhibit")
def evidence_exhibit(_=Depends(auth)):
    """JSON exhibit manifest for chain-of-custody CSV/API consumers (no UI tab)."""
    now = datetime.utcnow()
    try:
        lines = open(config.EVIDENCE_LOG, encoding="utf-8").read().splitlines()
    except OSError:
        lines = []
    events = [x for x in lines if x.strip().startswith("{")]
    kinds: dict[str, int] = {}
    hashed = 0
    for ln in events[-5000:]:
        try:
            e = json.loads(ln)
        except Exception:
            continue
        kinds[str(e.get("event") or "unknown")] = (
            kinds.get(str(e.get("event") or "unknown"), 0) + 1
        )
        if e.get("artifact_sha256"):
            hashed += 1
    jobs = list(downloads.jobs.values())
    return {
        "fact_type": "fact",
        "case_id": config.CASE_ID,
        "authorization_ref": config.AUTH_REF,
        "generated_utc": now.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "generated_ist": (now + timedelta(hours=5, minutes=30)).strftime(
            "%d %b %Y, %H:%M IST"
        ),
        "custody_events_total": len(events),
        "custody_events_by_type": kinds,
        "artifacts_sha256": hashed,
        "download_jobs": len(jobs),
        "download_jobs_complete": sum(1 for j in jobs if j.get("status") == "complete"),
        "objects_indexed": db.get_meta("objects_total") or 0,
        "app_version": app.version,
    }


@app.get("/api/healthz")
def healthz():
    return {"ok": True}


class _CheckpointState(dict):
    pass


_checkpoint_state: dict = {"running": False, "last": None}


@app.post("/api/maintenance/checkpoint")
def maintenance_checkpoint(truncate: int = Query(1, ge=0, le=1), _=Depends(auth)):
    """Reclaim the SQLite WAL (hang fix 2026-09-07).

    A reader-pinned 128MB WAL made every drain persist + every dashboard
    read slower and slower until the app hung. PASSIVE checkpoints run after
    every drain but cannot proceed while readers hold old frames; a TRUNCATE
    checkpoint under paused drains resets the file to zero. Runs in a
    background thread (never blocks the HTTP worker): pauses the drain loops
    by holding their locks, checkpoints, resumes. Safe to call anytime.
    """
    if _checkpoint_state.get("running"):
        return {"accepted": False, "reason": "already running"}
    _checkpoint_state["running"] = True

    def _bg():
        locks = []
        try:
            try:
                w = collector_holder["collector"].enricher
                if w is not None:
                    locks = [w._drain_lock, w._specific_lock]
            except Exception:
                locks = []
            for lk in locks:
                lk.acquire()
            try:
                res = db.checkpoint(truncate=bool(truncate))
            finally:
                for lk in reversed(locks):
                    try:
                        lk.release()
                    except Exception:
                        pass
            _checkpoint_state["last"] = res
            try:
                append_collection_log(
                    {
                        "event": "monitor_wal_checkpoint",
                        "method": "TRUNCATE checkpoint under paused drains",
                        "counts": res if isinstance(res, dict) else {"raw": res},
                    }
                )
            except Exception:
                pass
            print(f"[maintenance] checkpoint done: {res}", flush=True)
        except Exception as e:
            _checkpoint_state["last"] = {"error": str(e)[:200]}
        finally:
            _checkpoint_state["running"] = False

    threading.Thread(target=_bg, daemon=True, name="wal-checkpoint").start()
    return {"accepted": True}


@app.get("/api/maintenance/checkpoint/status")
def maintenance_checkpoint_status(_=Depends(auth)):
    return {
        "running": bool(_checkpoint_state.get("running")),
        "last": _checkpoint_state.get("last"),
    }


# ---- SPA fallback: serve index.html for any non-/api route when dist exists ----
@app.get("/{full_path:path}")
def spa_fallback(full_path: str):
    # let /api/* 404 naturally
    if full_path.startswith("api/"):
        from fastapi import HTTPException as _HE

        raise _HE(404, "not found")
    idx = _FRONTEND_DIST / "index.html" if "_FRONTEND_DIST" in dir() else None
    # resolve via global
    try:
        dist = Path(__file__).resolve().parents[1] / "frontend" / "dist" / "index.html"
        if dist.exists():
            # No-store: after each deploy the bundle hash changes, and a
            # cached index.html (verified 2026-09-11: stale "0 pending" UI)
            # is worse than one extra 304-checked fetch per load.
            return FileResponse(str(dist), headers={"Cache-Control": "no-store"})
    except Exception:
        pass
    from fastapi import HTTPException as _HE2

    raise _HE2(404, "frontend not built")
