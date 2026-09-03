"""GCS evidence monitor API for the preserved CASE-2026-CBTF object mirror."""

import asyncio
import json
import mimetypes
import os
import threading
import time
from pathlib import Path

from fastapi import Depends, FastAPI, HTTPException, Query, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, PlainTextResponse, StreamingResponse
from pydantic import BaseModel

from . import config, db
from .collector import Collector, bus, collector_holder
from .evidence import DownloadManager, append_collection_log
from .s3client import S3PublicClient
from .gcsclient import GCSMirrorClient

_stats_cache: dict[str, tuple[float, dict]] = {}


def cached(key: str, ttl: float, builder):
    global _stats_cache
    now = time.monotonic()
    ent = _stats_cache.get(key)
    if ent and now - ent[0] < ttl:
        return ent[1]
    val = builder()
    _stats_cache[key] = (now, val)
    return val


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
                # status/stats aggregates depend on counts that just changed
                _stats_cache.pop("status", None)
                _stats_cache.pop("stats", None)
                _stats_cache.pop("types", None)
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

    threading.Thread(target=_repair, daemon=True).start()

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
    if prefix and not prefix.endswith("/"):
        prefix += "/"
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
        return {
            "live": True,
            "latency_ms": ms,
            "checked_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "bucket_url": config.SOURCE_ID,
            "object_source": config.OBJECT_SOURCE,
            "effective_source": _actual_source(),
        }
    except Exception as e:
        return {
            "live": False,
            "error": str(e)[:200],
            "latency_ms": int((time.monotonic() - t0) * 1000),
            "checked_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
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
        "status_prefix": "s3-mirror-sync/",
        "authorization_ref": config.AUTH_REF,
    }
    try:
        from .gcsclient import GCSMirrorClient as _GCS

        client = object_store.client if isinstance(object_store, _GCS) else None
        if client is None:
            raise RuntimeError("GCS client unavailable (object_source not gcs)")
        bucket = client.bucket(config.GCS_BUCKET)
        blob = bucket.get_blob("s3-mirror-sync/status.json")
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


def _build_status():
    st = dict(collector.state)
    st["objects_total"] = db.total_count()
    st["bucket_url"] = config.SOURCE_ID
    st["object_source"] = config.OBJECT_SOURCE
    st["effective_source"] = _actual_source()
    st["case"] = config.CASE_ID
    st["auth_ref"] = config.AUTH_REF
    st["prefixes"] = (
        db.get_meta("prefixes") or "\n".join(config.DEFAULT_PREFIXES)
    ).splitlines()
    st["enrichment"] = db.enrichment_progress()
    st["gemini_enabled"] = config.GEMINI_ENABLED
    st["gemini_model"] = config.GEMINI_MODEL if config.GEMINI_ENABLED else None
    st["max_amount_inr"] = config.MAX_AMOUNT_INR
    return st


@app.get("/api/types")
def types(_=Depends(auth)):
    """Census: what data exists in the bucket, by category."""
    return cached("types", 60.0, db.type_census)


@app.get("/api/stats")
def stats(_=Depends(auth)):
    return cached("stats", 10.0, _build_stats)


def _build_stats():
    import time as _t

    today = _t.strftime("%Y-%m-%d", _t.gmtime())
    dep = db.deposits(limit=1)
    wd = db.deposits(kind="withdrawal", limit=1)
    return {
        "objects_total": db.total_count(),
        "per_prefix": db.prefix_stats(),
        "day_histogram": db.day_histogram(14),
        "deposits": {"count": dep["total"], "sum_amount_inr": dep["sum_amount_inr"]},
        "withdrawals": {"count": wd["total"], "sum_amount_inr": wd["sum_amount_inr"]},
        "today_utc": today,
    }


# ---------------- browsing ----------------
@app.get("/api/tree")
def tree(prefix: str = Query(""), _=Depends(auth)):
    if prefix and not prefix.endswith("/"):
        prefix += "/"
    return {"prefix": prefix, **db.subfolders(prefix)}


@app.get("/api/list")
def list_objs(
    prefix: str = "",
    q: str = "",
    limit: int = Query(200, le=1000),
    offset: int = 0,
    order: str = Query("key", pattern="^(key|-modified|size)$"),
    ext: str = Query("", description="comma list e.g. .jpg,.png"),
    since: str = Query("", description="YYYY-MM-DD or ISO ts"),
    flat: int = 0,
    _=Depends(auth),
):
    if prefix and not prefix.endswith("/"):
        prefix += "/"
    exts = [e.strip().lower() for e in ext.split(",") if e.strip()]
    return db.list_objects(
        prefix,
        limit=limit,
        offset=offset,
        order=order,
        q=q,
        exts=exts,
        since=since,
        flat=bool(flat),
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
        if prefix and not prefix.endswith("/"):
            prefix += "/"
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
        c.state["objects_total"] = db.total_count()
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
        c.state["new_since_start"] += 1
        c.state["last_poll_utc"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        c.state["objects_total"] = db.total_count()
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
    return {
        "new_by_prefix": found,
        "new_root_files": root_new,
        "new_top_level_folders": new_pfx,
    }


# ---------------- object proxy (preview + download) ----------------
@app.get("/api/object")
def get_object(key: str, download: int = 0, _=Depends(auth)):
    try:
        resp, headers = object_store.get_object_stream(key)
    except RuntimeError as e:
        raise HTTPException(404, str(e))
    mt = (
        headers.get("content_type")
        or mimetypes.guess_type(key)[0]
        or "application/octet-stream"
    )
    filename = key.rsplit("/", 1)[-1] or "object"
    media = mt.split(";")[0].strip()
    if download:
        media = "application/octet-stream"
    else:
        # avoid browsers executing anything
        if media in ("text/html", "application/xhtml+xml", "image/svg+xml"):
            media = "text/plain"
    return StreamingResponse(
        resp.iter_bytes(65536),
        media_type=media,
        headers={
            "Content-Disposition": f'{"attachment" if download else "inline"}; filename="{filename}"',
            "Cache-Control": "private, max-age=300",
            "X-Content-Type-Options": "nosniff",
        },
    )


@app.post("/api/enrich")
def enrich_now(key: str, gemini: int = 0, _=Depends(auth)):
    """Re-run enrichment for a single object; gemini=1 forces Gemini Vision when configured."""
    from .enrich import (
        EnrichmentWorker,
        classify,
        decode_qr_image,
        fetch_bytes,
        gemini_ocr_slip,
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
        # Forced Gemini path: bypass worker heuristic and call gemini_ocr_slip directly
        try:
            data = fetch_bytes(object_store, key)
        except Exception as e:
            raise HTTPException(404, f"object not found: {e}")
        kind = classify(key)
        if kind in ("deposit", "withdrawal"):
            payload = gemini_ocr_slip(data)
            payload["kind"] = kind
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
    direction: str = Query("deposit", pattern="^(deposit|withdrawal)$"),
    rail_type: str = Query("", pattern="^(|upi|bank_account|crypto_wallet|unknown)$"),
    q: str = "",
    limit: int = Query(200, ge=1, le=1000),
    offset: int = Query(0, ge=0),
    _=Depends(auth),
):
    """Normalized gambler↔payment-rail flows from OCR, QR, and case references."""
    cache_key = f"flows:{direction}:{rail_type}:{q}:{limit}:{offset}"
    return cached(
        cache_key,
        5.0,
        lambda: db.money_flows(
            direction=direction,
            rail_type=rail_type,
            q=q,
            limit=limit,
            offset=offset,
        ),
    )


@app.get("/api/mule-rails")
def mule_rails_api(
    rail_type: str = Query("", pattern="^(|upi|bank_account|crypto_wallet)$"),
    q: str = "",
    priority: str = Query("", pattern="^(|high|medium|lead)$"),
    limit: int = Query(200, ge=1, le=1000),
    offset: int = Query(0, ge=0),
    _=Depends(auth),
):
    """Rank observed payment rails for corroboration—not automatic attribution."""
    from .evidence import load_master_vpas, load_master_wallets

    cache_key = f"mule-rails:{rail_type}:{q}:{priority}:{limit}:{offset}"

    def build():
        # Rank the complete filtered set before pagination so page order and
        # priority totals remain stable.
        raw = db.mule_rails(rail_type=rail_type, q=q, limit=10000, offset=0)
        master_vpas = load_master_vpas()
        master_wallets = load_master_wallets()
        for item in raw["items"]:
            rail_id = str(item.get("rail_id") or "")
            known = (
                rail_id.lower() in master_vpas
                if item["rail_type"] == "upi"
                else rail_id.lower() in master_wallets
                if item["rail_type"] == "crypto_wallet"
                else False
            )
            both = bool(
                item["deposit_transactions"] and item["withdrawal_transactions"]
            )
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
        all_items = sorted(
            raw["items"],
            key=lambda item: (
                item["candidate_score"],
                item["deposit_transactions"] > 0
                and item["withdrawal_transactions"] > 0,
                item["transactions"],
                item.get("last_seen") or "",
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
        raw["total"] = len(all_items)
        raw["items"] = all_items[offset : offset + limit]
        raw["scoring_note"] = (
            "Heuristic triage score uses both-direction activity, linked gamblers, "
            "transaction frequency, object evidence, role evidence, and master indicators."
        )
        return raw

    return cached(cache_key, 15.0, build)


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
def upis_api(limit: int = 300, _=Depends(auth)):
    master = downloads_master()
    items = db.upi_feed(limit)
    for it in items:
        vpa = (it.get("vpa") or "").lower()
        it["known_mule"] = bool(vpa and vpa in master)
    new = [i for i in items if not i.get("known_mule")]
    return {"master_size": len(master), "items": items, "new_not_in_master": len(new)}


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

    Source is live bucket objects via `gs://cbtf-case-evidence-991096649643/s3-mirror/`
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
def wallets_api(limit: int = Query(300, le=2000), _=Depends(auth)):
    """Live crypto-wallet feed: TRON/ERC20/BTC addresses extracted from QRs,
    deposit slips and crypto chat screenshots; cross-checked against the LE
    submission master (17-CRYPTO-WALLETS.csv) — known operator wallets badged."""
    master = downloads_wallet_master()
    items = db.wallet_feed(limit)
    stats = db.wallet_stats()
    for it in items:
        addr = (it.get("address") or "").lower()
        it["known_operator"] = bool(addr and addr in master)
    new = [i for i in items if not i.get("known_operator")]
    # mastering: include provenance rows so dashboard stays useful even when
    # live extraction has not yet drained the 1M+ backlog (hits currently nil).
    try:
        from .evidence import load_master_wallet_rows

        master_rows = load_master_wallet_rows()
        try:
            master_rel = str(config.MASTER_WALLET_CSV.relative_to(config.REPO_ROOT))
        except Exception:
            master_rel = str(config.MASTER_WALLET_CSV)
    except Exception:
        master_rows = []
        master_rel = "LE-SUBMISSION-MASTER/17-CRYPTO-WALLETS.csv"
    # Source stats for officer: enrichment backlog explains nil hits.
    try:
        enr = db.enrichment_progress()
        extraction_status = {
            "enriched": enr.get("enriched_done"),
            "pending": enr.get("pending_now"),
            "objects": enr.get("total"),
            "pending_full_scan": db.pending_enrichment_count(full_scan=True),
        }
    except Exception:
        extraction_status = {}
    return {
        "master_size": len(master),
        "master_rows": master_rows,
        "master_source": master_rel,
        "items": items,
        "new_not_in_master": len(new),
        "wallet_hits_total": stats["wallet_hits"],
        "distinct_wallets": stats["distinct_wallets"],
        "by_network": stats["by_network"],
        "extraction_status": extraction_status,
        "source": str(config.SOURCE_ID),
    }


def downloads_wallet_master() -> set[str]:
    from .evidence import load_master_wallets

    return load_master_wallets()


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


@app.get("/api/healthz")
def healthz():
    return {"ok": True}


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
            return FileResponse(str(dist))
    except Exception:
        pass
    from fastapi import HTTPException as _HE2

    raise _HE2(404, "frontend not built")
