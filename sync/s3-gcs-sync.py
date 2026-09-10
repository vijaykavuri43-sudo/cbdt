"""Continuous S3 -> GCS evidence mirror sync daemon.

CASE-2026-CBTF · STANDING-JUDICIAL-AUTH-ILLEGAL-BETTING · READ-ONLY source access

Why this exists (Defect A, LIVE-CHAT-STALENESS-REVIEW-2026-09-02):
the s3lm dashboard reads gs://cbtf-case-evidence-991096649643/s3-mirror/,
but the preservation VMs were decommissioned on 2026-09-01, freezing the
mirror at ~Aug 22. The dashboard can never be real-time while the source is
frozen, regardless of collector fixes. This daemon runs ON the s3lm VM as a
second container and keeps the mirror continuously in sync:

  1. One-time: pre-populate the local state DB from a GCS listing (key, size,
     s3_etag from custody metadata) — so the initial catch-up only transfers
     the delta instead of re-copying the 1.4M objects already preserved.
  2. Each cycle: LIST the live S3 bucket per top-level prefix (read-only
     ListObjectsV2, rate-limited) + root-level files via delimiter listing.
  3. Diff S3 keys against the state DB (batched PK lookups, no giant RAM set).
  4. GET each missing/changed object (rate-limited, streamed, sha256 in
     flight) and upload to gs://<gcs_bucket>/s3-mirror/<original key> with
     chain-of-custody custom metadata (source_key, source_bucket, s3_etag,
     sha256, last_modified, case_id, authorization_ref). last_modified is
     stored as ISO-8601 UTC so dashboard lexicographic sorts stay correct
     (Defect B fixed at source).
  5. Loop forever; publishes gs://<bucket>/s3-mirror-sync/status.json each
     cycle for the /api/mirror-sync dashboard endpoint.

Cost control: unchanged objects are skipped via size/etag check (no GET);
LISTs are throttled (S3SYNC_LIST_RATE/s); GETs are throttled
(S3SYNC_RATE_PER_SEC/s); objects over S3SYNC_MAX_BYTES are skipped (the
preservation effort already captured them).

Safety: read-only against S3 (LIST+GET only); GCS bucket has versioning ON
+ 365d retention LOCKED — uploads append new evidence, never delete.
"""

import argparse
import hashlib
import json
import mimetypes
import os
import queue
import sqlite3
import sys
import threading
import time
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import httpx
from google.cloud import storage

# ---------- config via env ----------

S3_BUCKET_URL = os.environ.get(
    "S3SYNC_BUCKET_URL", "https://bx-s3-dev-001.s3.ap-southeast-1.amazonaws.com"
)
GCS_BUCKET = os.environ.get("S3SYNC_GCS_BUCKET", "cbtf-case-evidence-991096649643")
GCS_PREFIX = os.environ.get("S3SYNC_GCS_PREFIX", "s3-mirror/").strip("/") + "/"
# Heartbeat/status live OUTSIDE the mirror prefix: s3-mirror/ must stay a
# byte-exact evidence copy of the S3 bucket (a synthetic heartbeat inside it
# would surface in the dashboard manifest as a fake root object).
SYNC_STATUS_PREFIX = (
    os.environ.get("S3SYNC_STATUS_PREFIX", "s3-mirror-sync/").strip("/") + "/"
)
STATE_DB = os.environ.get(
    "S3SYNC_STATE_DB",
    str(Path.home() / ".local" / "share" / "s3-sync" / "state.db"),
)
RATE_PER_SEC = float(os.environ.get("S3SYNC_RATE_PER_SEC", "8"))
LIST_RATE_PER_SEC = float(os.environ.get("S3SYNC_LIST_RATE_PER_SEC", "10"))
WORKERS = int(os.environ.get("S3SYNC_WORKERS", "4"))
CYCLE_SEC = float(os.environ.get("S3SYNC_CYCLE_SEC", "60"))
CATCHUP_CYCLE_SEC = float(os.environ.get("S3SYNC_CATCHUP_CYCLE_SEC", "10"))
PREFIXES_ENV = os.environ.get("S3SYNC_PREFIXES", "")  # csv, empty = auto-discover
# Hot prefixes (money + chat paths) are re-listed EVERY cycle; the rest are
# verified round-robin one per cycle. Matches the collector's HOT_PREFIXES.
HOT_PREFIXES = [
    p.strip()
    for p in os.environ.get(
        "S3SYNC_HOT_PREFIXES",
        "chat_file/,deposit/,withdrawal/,bankingqr/,qrcode/,payment/,csv-files/,uploads/",
    ).split(",")
    if p.strip()
]
CASE_ID = os.environ.get("S3LM_CASE", "CASE-2026-CBTF")
AUTH_REF = os.environ.get("S3LM_AUTH", "STANDING-JUDICIAL-AUTH-ILLEGAL-BETTING")
SOURCE_BUCKET_LABEL = os.environ.get("S3SYNC_SOURCE_LABEL", "bx-s3-dev-001")
# Objects larger than this skip the GET-based sync (log + record only; the
# preservation effort already captured them; re-copying 1GB logs wastes money)
MAX_SYNC_BYTES = int(os.environ.get("S3SYNC_MAX_BYTES", str(512 * 1024 * 1024)))
AUDIT_LOG = Path(
    os.environ.get("S3SYNC_AUDIT_LOG", "/app/intelligence/audit/collection-log.jsonl")
)
MONITOR_URL = os.environ.get("S3SYNC_MONITOR_URL", "http://s3lm:8787").rstrip("/")
MONITOR_TOKEN_FILE = Path(
    os.environ.get(
        "S3SYNC_MONITOR_TOKEN_FILE",
        "/root/.local/share/s3-live-monitor/access-token.txt",
    )
)

_log_lock = threading.Lock()

XMLNS = "{http://s3.amazonaws.com/doc/2006-03-01/}"


def log(msg: str):
    line = f"{datetime.now(timezone.utc).strftime('%H:%M:%S')} {msg}"
    with _log_lock:
        print(line, flush=True)


# Degraded write-blocked mode (review 2026-09-08): when GCS writes fail
# (e.g. project billing delinquent -> 403 on every upload), the daemon must
# NOT die and docker-restart-loop. It keeps LISTing S3 + local inventory +
# status publishes, pauses all downloads/uploads, and re-probes write
# capability each cycle so it self-heals the moment billing is fixed.
WRITE_STATE = {"blocked": False, "reason": ""}


def probe_writes(bucket, hb) -> tuple:
    """One tiny heartbeat upload. Returns (ok, err); never raises."""
    try:
        hb.metadata = {"case_id": CASE_ID, "authorization_ref": AUTH_REF}
        hb.upload_from_string(
            f"{utcnow()} alive case={CASE_ID}\n",
            content_type="text/plain",
        )
        return True, ""
    except Exception as e:
        return False, str(e)[:200]


def uploads_paused() -> bool:
    """True while GCS writes are known-blocked (degraded read-only mode)."""
    return WRITE_STATE["blocked"]


def _audit(summary: str, fact_type: str = "fact", ref: str = ""):
    """Append a chain-of-custody fact to intelligence/audit/collection-log.jsonl.

    Local best-effort mirror of the AGENTS.md persistence rule (the daemon runs
    in a container without the repo checkout — the durable GCS copy is the
    authoritative one; this line keeps the local audit trail complete too).
    """
    try:
        AUDIT_LOG.parent.mkdir(parents=True, exist_ok=True)
        rec = {
            "utc": utcnow(),
            "case": CASE_ID,
            "authorization_ref": AUTH_REF,
            "method": "s3-gcs-sync-deletion-proofing",
            "fact_type": fact_type,
            "summary": summary[:400],
        }
        if ref:
            rec["ref"] = ref
        with open(AUDIT_LOG, "a", encoding="utf-8") as f:
            f.write(json.dumps(rec) + "\n")
    except Exception:
        pass


# set in main(): bucket for s3-mirror-sync/ bookkeeping objects (status,
# heartbeat, deletion evidence) — separate from the evidence mirror prefix
bucket_del = None  # type: ignore


def utcnow() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def normalize_ts(rfc_or_iso: str) -> str:
    """RFC-1123/ISO -> ISO-8601 UTC (same parser family as backend.db)."""
    if not rfc_or_iso:
        return ""
    try:
        dt = datetime.fromisoformat(rfc_or_iso.replace("Z", "+00:00"))
        return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    except ValueError:
        pass
    try:
        from email.utils import parsedate_to_datetime

        return (
            parsedate_to_datetime(rfc_or_iso)
            .astimezone(timezone.utc)
            .strftime("%Y-%m-%dT%H:%M:%SZ")
        )
    except (TypeError, ValueError):
        return ""


# ---------- state ----------


def open_state() -> sqlite3.Connection:
    Path(STATE_DB).parent.mkdir(parents=True, exist_ok=True)
    c = sqlite3.connect(STATE_DB, timeout=120)
    c.execute("PRAGMA journal_mode=WAL")
    c.execute("PRAGMA synchronous=NORMAL")
    c.execute(
        """CREATE TABLE IF NOT EXISTS synced(
            key TEXT PRIMARY KEY, size INTEGER, etag TEXT, sha256 TEXT,
            synced_utc TEXT, gcs_generation TEXT)"""
    )
    # Tombstones for objects that existed in S3 but could not be captured
    # (deleted from the source between our LIST and GET, transient S3 errors).
    # Review 2026-09-02: previously such objects left only an error log line —
    # no durable record, no retry — violating the "nothing reaching S3 may be
    # lost even if deleted at source" evidence bar. fail_count>0 rows are
    # retried every cycle; GET->404 preserves the deletion evidence and is
    # escalated to the chain-of-custody audit log.
    c.execute(
        """CREATE TABLE IF NOT EXISTS sync_failures(
            key TEXT PRIMARY KEY, size INTEGER, etag TEXT,
            first_seen_utc TEXT, last_error TEXT, last_attempt_utc TEXT,
            fail_count INTEGER DEFAULT 0, terminal INTEGER DEFAULT 0)"""
    )
    failure_cols = {
        r[1] for r in c.execute("PRAGMA table_info(sync_failures)").fetchall()
    }
    if "terminal" not in failure_cols:
        c.execute("ALTER TABLE sync_failures ADD COLUMN terminal INTEGER DEFAULT 0")
    c.execute(
        """CREATE TABLE IF NOT EXISTS prefix_inventory(
            prefix TEXT PRIMARY KEY, object_count INTEGER NOT NULL,
            bytes INTEGER NOT NULL, newest_utc TEXT DEFAULT '',
            completed_utc TEXT NOT NULL, cycle INTEGER NOT NULL)"""
    )
    c.commit()
    return c


def record_prefix_inventory(
    state: sqlite3.Connection,
    prefix: str,
    object_count: int,
    total_bytes: int,
    newest_utc: str,
    cycle: int,
):
    """Persist the latest complete source listing for one prefix."""
    with state:
        state.execute(
            """INSERT INTO prefix_inventory(
                    prefix,object_count,bytes,newest_utc,completed_utc,cycle
                ) VALUES(?,?,?,?,?,?)
                ON CONFLICT(prefix) DO UPDATE SET
                    object_count=excluded.object_count,
                    bytes=excluded.bytes,
                    newest_utc=excluded.newest_utc,
                    completed_utc=excluded.completed_utc,
                    cycle=excluded.cycle""",
            (prefix, object_count, total_bytes, newest_utc, utcnow(), cycle),
        )


def source_inventory_summary(
    state: sqlite3.Connection, expected_prefixes: list[str]
) -> dict:
    rows = state.execute(
        "SELECT prefix,object_count,bytes,newest_utc,completed_utc,cycle "
        "FROM prefix_inventory"
    ).fetchall()
    by_prefix = {r[0]: r for r in rows}
    expected = list(dict.fromkeys(["(root)"] + expected_prefixes))
    present = [by_prefix[p] for p in expected if p in by_prefix]
    completed = [r[4] for r in present if r[4]]
    return {
        "source_inventory_objects": sum(int(r[1] or 0) for r in present),
        "source_inventory_bytes": sum(int(r[2] or 0) for r in present),
        "source_inventory_prefixes_scanned": len(present),
        "source_inventory_prefixes_total": len(expected),
        "source_inventory_complete": len(present) == len(expected),
        "source_inventory_oldest_scan_utc": min(completed) if completed else None,
        "source_inventory_newest_scan_utc": max(completed) if completed else None,
    }


def s3_escape(key: str) -> str:
    from urllib.parse import quote

    return quote(key, safe="/-._~")


class Throttle:
    """Simple monotonic-clock rate limiter shared across threads."""

    def __init__(self, rate_per_sec: float):
        self._min_interval = 1.0 / max(0.1, rate_per_sec)
        self._last = 0.0
        self._lock = threading.Lock()

    def wait(self):
        with self._lock:
            now = time.monotonic()
            delay = self._min_interval - (now - self._last)
            if delay > 0:
                time.sleep(delay)
                self._last = time.monotonic()
            else:
                self._last = now


class S3Lister:
    """ListObjectsV2 (read-only, anonymous) for the evidence bucket."""

    def __init__(self, bucket_url: str, client: httpx.Client, throttle: Throttle):
        self.url = bucket_url.rstrip("/")
        self.client = client
        self.throttle = throttle

    def _get(self, params: dict) -> ET.Element:
        self.throttle.wait()
        r = self.client.get(self.url + "/", params=params, timeout=60.0)
        r.raise_for_status()
        return ET.fromstring(r.text)

    def iter_prefix(self, prefix: str = ""):
        """Yield (key, size, etag, last_modified) for every object under prefix."""
        token: Optional[str] = None
        while True:
            params = {"list-type": "2", "max-keys": "1000", "prefix": prefix}
            if token:
                params["continuation-token"] = token
            root = self._get(params)
            for c in root.iter(f"{XMLNS}Contents"):
                yield (
                    c.findtext(f"{XMLNS}Key") or "",
                    int(c.findtext(f"{XMLNS}Size") or "0"),
                    (c.findtext(f"{XMLNS}ETag") or "").strip('"'),
                    c.findtext(f"{XMLNS}LastModified") or "",
                )
            t = root.findtext(f"{XMLNS}NextContinuationToken")
            truncated = (root.findtext(f"{XMLNS}IsTruncated") or "").lower() == "true"
            if not truncated or not t:
                return
            token = t

    def iter_root_files(self):
        """Root-level keys (no '/'): a delimiter listing returns them as Contents."""
        root = self._get({"list-type": "2", "max-keys": "1000", "delimiter": "/"})
        for c in root.iter(f"{XMLNS}Contents"):
            yield (
                c.findtext(f"{XMLNS}Key") or "",
                int(c.findtext(f"{XMLNS}Size") or "0"),
                (c.findtext(f"{XMLNS}ETag") or "").strip('"'),
                c.findtext(f"{XMLNS}LastModified") or "",
            )

    def top_prefixes(self) -> list[str]:
        root = self._get({"list-type": "2", "max-keys": "1000", "delimiter": "/"})
        out = [
            (p.findtext(f"{XMLNS}Prefix") or "")
            for p in root.iter(f"{XMLNS}CommonPrefixes")
        ]
        return sorted(p for p in out if p and p != "/")


class SyncWorker(threading.Thread):
    """GET from S3 (throttled) -> sha256 -> upload to GCS with custody metadata."""

    def __init__(
        self,
        wid: int,
        s3_client: httpx.Client,
        bucket,
        rate_q: "KeyQueue",
        stats,
        stop_evt,
        get_throttle: Throttle,
    ):
        super().__init__(daemon=True, name=f"s3sync-{wid}")
        self.s3 = s3_client
        self.bucket = bucket
        self.rate_q = rate_q
        self.stats = stats
        self.stop = stop_evt
        self.get_throttle = get_throttle
        self._db = None  # created inside run(): sqlite conns are thread-bound

    def run(self):
        # sqlite connections are thread-bound: open ours in THIS thread
        self._db = sqlite3.connect(STATE_DB, timeout=120, check_same_thread=False)
        self._db.execute("PRAGMA journal_mode=WAL")
        try:
            self._run_loop()
        finally:
            try:
                self._db.close()
            except Exception:
                pass

    def _run_loop(self):
        while not self.stop.is_set():
            try:
                item = self.rate_q.get(timeout=1.0)
            except queue.Empty:
                continue
            if item is None:
                return
            key = item[0]
            if self.stop.is_set():
                self.rate_q.task_done()
                return
            try:
                self.sync_one(item)
            except Exception as e:
                self.stats["errors"] += 1
                log(f"[w{self.name[-1]}] ERROR {str(key)[:100]}: {str(e)[:160]}")
                self._record_failure(
                    key,
                    item[1] if len(item) > 1 else None,
                    str(e),
                    etag=item[2] if len(item) > 2 else "",
                )
            finally:
                self.rate_q.task_done()

    def _record_failure(
        self, key, size, err: str, etag: str = "", terminal: bool = False
    ):
        """Durable tombstone for every failed capture (deletion-proofing).

        The row keeps the object on the retry list for the next cycle. When
        the S3 GET itself answered 404 the object was deleted at the source
        before we could copy it — record that explicitly (chain-of-custody
        fact, review 2026-09-02) so the loss is provable and bounded.
        """
        try:
            with self._db:
                self._db.execute(
                    """INSERT INTO sync_failures(key,size,etag,first_seen_utc,last_error,last_attempt_utc,fail_count,terminal)
                        VALUES(?,?,?,?,?,?,1,?)
                        ON CONFLICT(key) DO UPDATE SET
                        size=COALESCE(excluded.size, sync_failures.size),
                        etag=COALESCE(excluded.etag, sync_failures.etag),
                        last_error=excluded.last_error,
                        last_attempt_utc=excluded.last_attempt_utc,
                        fail_count=sync_failures.fail_count+1,
                        terminal=excluded.terminal""",
                    (
                        key,
                        size,
                        etag or None,
                        utcnow(),
                        err[:300],
                        utcnow(),
                        1 if terminal else 0,
                    ),
                )
        except sqlite3.Error as db_err:
            log(f"[{self.name}] tombstone write failed {str(key)[:80]}: {db_err}")

    def _clear_failure(self, key):
        try:
            with self._db:
                self._db.execute("DELETE FROM sync_failures WHERE key=?", (key,))
        except sqlite3.Error:
            pass

    def _record(self, key, size, etag, sha, generation):
        with self._db:
            self._db.execute(
                "INSERT INTO synced(key,size,etag,sha256,synced_utc,gcs_generation) "
                "VALUES(?,?,?,?,?,?) ON CONFLICT(key) DO UPDATE SET "
                "size=excluded.size, etag=excluded.etag, sha256=excluded.sha256, "
                "synced_utc=excluded.synced_utc, gcs_generation=excluded.gcs_generation",
                (key, size, etag, sha, utcnow(), str(generation)),
            )

    def _notify_dashboard(self, key: str, size: int, etag: str, last_modified: str):
        """Push an uploaded object into the manifest immediately.

        Object keys are user-prefixed rather than time-sorted, so a watermark
        poll cannot guarantee low-latency discovery. The version-aware cold
        crawl remains the fallback; this authenticated local notification is
        the fast path and contains metadata only (no evidence bytes).
        """
        if not MONITOR_URL:
            return
        try:
            token = MONITOR_TOKEN_FILE.read_text(encoding="utf-8").strip()
            headers = {"Authorization": f"Bearer {token}"} if token else {}
            payload = {
                "key": key,
                "size": size,
                "etag": etag,
                "last_modified": last_modified,
            }
            # Log review 2026-09-03: 10s timeout with no retry lost 68/87
            # notifies when the dashboard was busy (full-table COUNT(*) per
            # request). 30s + one retry; dashboard hot paths are fixed too.
            last_err: Exception | None = None
            for attempt in range(2):
                try:
                    r = self.s3.post(
                        MONITOR_URL + "/api/sync/object",
                        headers=headers,
                        json=payload,
                        timeout=30.0,
                    )
                    if r.status_code >= 300:
                        raise RuntimeError(f"HTTP {r.status_code}")
                    last_err = None
                    break
                except Exception as e:
                    last_err = e
            if last_err is not None:
                raise last_err
            self.stats["notified"] += 1
        except Exception as e:
            self.stats["notify_errors"] += 1
            n_err = self.stats["notify_errors"]
            if n_err <= 5 or n_err % 50 == 0:
                log(
                    f"dashboard notify deferred for {key[:80]}: {str(e)[:100]}"
                    + (f" ({n_err} total)" if n_err > 5 else "")
                )

    def sync_one(self, item: tuple):
        key, size_hint, etag_hint = item
        self.get_throttle.wait()
        url = f"{S3_BUCKET_URL.rstrip('/')}/{s3_escape(key)}"
        r = self.s3.send(self.s3.build_request("GET", url), stream=True)
        try:
            if r.status_code == 404:
                # Deleted from S3 between LIST and GET — the exact loss window
                # this daemon exists to close. Preserve the evidence: durable
                # tombstone, deletion log in GCS (s3-mirror-sync/deletions/),
                # and an audit-log fact. Nothing silently disappears.
                err = (
                    f"GET {key} -> 404 (deleted at source before capture; "
                    f"listed size={size_hint} etag={etag_hint})"
                )
                log(f"DELETED-AT-SOURCE {key[:100]} — tombstoned + logged")
                self._record_failure(key, size_hint, err, etag=etag_hint, terminal=True)
                try:
                    dl = bucket_del.blob(
                        SYNC_STATUS_PREFIX
                        + "deletions/"
                        + utcnow().replace(":", "")
                        + "-"
                        + s3_escape(key)[-120:]
                    )
                    dl.metadata = {
                        "source_key": key,
                        "source_bucket": SOURCE_BUCKET_LABEL,
                        "case_id": CASE_ID,
                        "authorization_ref": AUTH_REF,
                    }
                    dl.upload_from_string(
                        json.dumps(
                            {
                                "utc": utcnow(),
                                "key": key,
                                "size": size_hint,
                                "etag": etag_hint,
                                "event": "deleted_from_s3_before_capture",
                                "case": CASE_ID,
                                "authorization_ref": AUTH_REF,
                            },
                            indent=1,
                        ),
                        content_type="application/json",
                    )
                except Exception as gcs_err:
                    log(
                        f"deletion-log upload failed {str(key)[:80]}: {str(gcs_err)[:120]}"
                    )
                _audit(
                    f"SYNC-TOMBSTONE: {key} (size={size_hint}) was deleted from "
                    f"{SOURCE_BUCKET_LABEL} before mirror capture — evidence "
                    f"recorded in state DB + GCS deletion log",
                    fact_type="fact",
                    ref=f"{CASE_ID} deletion-proofing review 2026-09-02",
                )
                return
            if r.status_code != 200:
                raise RuntimeError(f"GET {key} -> {r.status_code}")
            etag = (r.headers.get("etag") or "").strip('"')
            last_modified = r.headers.get("last-modified") or ""
            blob = self.bucket.blob(GCS_PREFIX + key)
            h_sha = hashlib.sha256()
            n = 0
            import tempfile

            tmp = tempfile.NamedTemporaryFile(delete=False, suffix=".s3sync")
            try:
                for chunk in r.iter_bytes(1024 * 1024):
                    if self.stop.is_set():
                        raise RuntimeError("stop requested mid-transfer")
                    tmp.write(chunk)
                    h_sha.update(chunk)
                    n += len(chunk)
                tmp.flush()
                os.fsync(tmp.fileno())
                iso_ts = normalize_ts(last_modified) or utcnow()
                blob.metadata = {  # set before upload; not a kwarg
                    "source_key": key,
                    "source_bucket": SOURCE_BUCKET_LABEL,
                    "s3_etag": etag,
                    "sha256": h_sha.hexdigest(),
                    "last_modified": iso_ts,  # ISO-8601 — Defect B fixed at source
                    "case_id": CASE_ID,
                    "authorization_ref": AUTH_REF,
                }
                with open(tmp.name, "rb") as f:
                    # ext-first: the S3 source lies about content-type for some
                    # QR objects (text/plain for real JPEGs) — trust the key.
                    blob.upload_from_file(
                        f,
                        content_type=mimetypes.guess_type(key)[0]
                        or r.headers.get("content-type")
                        or "application/octet-stream",
                    )
                self._record(key, n, etag, h_sha.hexdigest(), blob.generation)
                self._clear_failure(key)
                self.stats["uploaded"] += 1
                self.stats["bytes"] += n
                self._notify_dashboard(key, n, etag, iso_ts)
                if self.stats["uploaded"] % 200 == 0:
                    log(
                        f"  … {self.stats['uploaded']} uploaded "
                        f"({self.stats['bytes'] / 1e6:.1f} MB) "
                        f"errors={self.stats['errors']}"
                    )
            finally:
                tmp.close()
                try:
                    os.unlink(tmp.name)
                except OSError:
                    pass
        finally:
            r.close()


class KeyQueue(queue.Queue):
    """Work queue carrying (key, size, etag) tuples; task_done on completion."""

    pass


# ---------- main loop ----------


def preload_gcs(state: sqlite3.Connection, gcs_client: storage.Client) -> int:
    """Record what the mirror already holds, so catch-up only moves the delta.

    Reads name+size+s3_etag (custody metadata) per GCS blob and INSERTs them
    into the local state DB. Disk-backed, no giant RAM set.
    """
    n = 0
    batch = []
    for b in gcs_client.list_blobs(GCS_BUCKET, prefix=GCS_PREFIX):
        k = b.name[len(GCS_PREFIX) :]
        if not k:
            continue
        meta = b.metadata or {}
        batch.append((k, int(b.size or 0), meta.get("s3_etag") or ""))
        n += 1
        if len(batch) >= 5000:
            with state:
                state.executemany(
                    "INSERT OR IGNORE INTO synced(key,size,etag,sha256,synced_utc,gcs_generation) "
                    "VALUES(?,?,?,'', '', '')",
                    batch,
                )
            batch = []
            if n % 200000 == 0:
                log(f"  preload… {n:,} mirror objects recorded")
    if batch:
        with state:
            state.executemany(
                "INSERT OR IGNORE INTO synced(key,size,etag,sha256,synced_utc,gcs_generation) "
                "VALUES(?,?,?,'', '', '')",
                batch,
            )
    return n


def diff_against_state(state: sqlite3.Connection, entries) -> list[tuple]:
    """Return entries (key,size,etag) not present/unchanged in state DB."""
    out = []
    rows = [e for e in entries if e[0]]
    for i in range(0, len(rows), 900):
        chunk = rows[i : i + 900]
        ph = ",".join("?" * len(chunk))
        try:
            have = {
                r[0]: (r[1], r[2])
                for r in state.execute(
                    f"SELECT key,size,etag FROM synced WHERE key IN ({ph})",
                    [e[0] for e in chunk],
                ).fetchall()
            }
        except sqlite3.Error:
            have = {}
        for key, size, etag, lm in chunk:
            prev = have.get(key)
            if (
                prev
                and prev[0] == size
                and (not etag or not prev[1] or prev[1] == etag)
            ):
                continue  # unchanged
            out.append((key, size, etag, lm))
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--status", action="store_true", help="print sync state and exit")
    ap.add_argument("--once", action="store_true", help="run one cycle and exit")
    ap.add_argument(
        "--preload-only",
        action="store_true",
        help="populate state DB from GCS listing and exit",
    )
    args = ap.parse_args()

    state = open_state()
    if args.status:
        (n,) = state.execute("SELECT COUNT(*) FROM synced").fetchone()
        (last,) = state.execute("SELECT MAX(synced_utc) FROM synced").fetchone()
        print(f"state_db={STATE_DB}\nsynced={n}\nlast={last}")
        return 0

    gcs = storage.Client()
    bucket = gcs.bucket(GCS_BUCKET)
    global bucket_del
    bucket_del = bucket  # bookkeeping bucket (status/heartbeat/deletion log)
    s3_http = httpx.Client(timeout=120.0, follow_redirects=True)
    lister = S3Lister(S3_BUCKET_URL, s3_http, Throttle(LIST_RATE_PER_SEC))
    get_throttle = Throttle(RATE_PER_SEC)

    if args.preload_only:
        n = preload_gcs(state, gcs)
        print(f"preloaded {n} mirror objects into {STATE_DB}")
        return 0

    stats = {
        "uploaded": 0,
        "bytes": 0,
        "errors": 0,
        "scanned": 0,
        "notified": 0,
        "notify_errors": 0,
    }
    stop_evt = threading.Event()

    def handle_signal(sig, frame):
        log("stop signal received — draining…")
        stop_evt.set()

    import signal

    signal.signal(signal.SIGTERM, handle_signal)
    signal.signal(signal.SIGINT, handle_signal)

    # write-capability check early (VM SA must be able to create objects).
    # Degraded-mode review 2026-09-08: a failed probe (e.g. 403 billing
    # delinquent) must NOT kill the daemon into a docker restart loop.
    # Enter listing-only mode; the per-cycle probe below self-heals.
    hb = bucket.blob(SYNC_STATUS_PREFIX + "heartbeat.txt")
    ok, err = probe_writes(bucket, hb)
    if ok:
        log(f"started: S3 {S3_BUCKET_URL} -> gs://{GCS_BUCKET}/{GCS_PREFIX}")
    else:
        WRITE_STATE.update(blocked=True, reason=err)
        log(f"STARTUP DEGRADED — GCS writes blocked: {err[:160]}")
        log(
            "listing-only mode: S3 inventory + status continue, "
            "uploads paused until writes recover"
        )
        log(f"started (DEGRADED): S3 {S3_BUCKET_URL} -> gs://{GCS_BUCKET}/{GCS_PREFIX}")
    log(
        f"state={STATE_DB} get_rate={RATE_PER_SEC}/s list_rate={LIST_RATE_PER_SEC}/s "
        f"workers={WORKERS} cycle={CYCLE_SEC}s"
    )

    # Publish an immediate STARTING status (review 2026-09-02): /api/mirror-sync
    # previously had no status.json at all until the first full cycle finished,
    # which the dashboard reported as "NO SYNC YET — mirror may be frozen".
    try:
        status_blob = bucket.blob(SYNC_STATUS_PREFIX + "status.json")
        status_blob.metadata = {"case_id": CASE_ID, "authorization_ref": AUTH_REF}
        status_blob.upload_from_string(
            json.dumps(
                {
                    "utc": utcnow(),
                    "cycle": 0,
                    "state": "starting",
                    "queued_pending": 0,
                    "tombstones_pending": 0,
                    "s3_bucket": S3_BUCKET_URL,
                    "gcs_target": f"gs://{GCS_BUCKET}/{GCS_PREFIX}",
                    "case": CASE_ID,
                    "authorization_ref": AUTH_REF,
                },
                indent=1,
            ),
            content_type="application/json",
        )
        log("published startup status.json")
    except Exception as e:
        log(f"startup status publish failed: {str(e)[:120]}")

    (mirror_known,) = state.execute("SELECT COUNT(*) FROM synced").fetchone()
    if mirror_known < 100_000:
        log("pre-populating state from GCS mirror listing (one-time delta base)…")
        t0 = time.monotonic()
        n = preload_gcs(state, gcs)
        log(f"mirror holds {n:,} objects (preload took {time.monotonic() - t0:.0f}s)")
    else:
        log(f"state DB knows {mirror_known:,} mirror objects — skipping preload")

    rate_q: KeyQueue = KeyQueue()
    workers = [
        SyncWorker(i, s3_http, bucket, rate_q, stats, stop_evt, get_throttle)
        for i in range(WORKERS)
    ]
    for w in workers:
        w.start()

    cycle = 0
    try:
        while not stop_evt.is_set():
            cycle += 1
            t0 = time.monotonic()
            cycle_scanned = 0
            # Write-capability probe (degraded-mode review 2026-09-08):
            # one tiny upload per cycle; flips listing-only mode on/off so
            # the daemon survives billing outages and self-heals after.
            ok, err = probe_writes(bucket, hb)
            if ok:
                if WRITE_STATE["blocked"]:
                    WRITE_STATE.update(blocked=False, reason="")
                    log("GCS writes recovered — resuming uploads")
            else:
                if not WRITE_STATE["blocked"]:
                    log(f"GCS writes blocked ({err[:120]}) — listing-only mode")
                WRITE_STATE.update(blocked=True, reason=err)
            degraded = uploads_paused()
            if degraded:
                log(f"cycle {cycle}: DEGRADED — listing only, uploads paused")
            # discover prefixes each cycle (operators add folders)
            try:
                tops = lister.top_prefixes()
            except Exception as e:
                log(f"list prefixes failed: {str(e)[:120]}")
                tops = []
            fixed = [p.strip() for p in PREFIXES_ENV.split(",") if p.strip()]
            hot = [p for p in HOT_PREFIXES if p in (tops or []) or not tops]
            # round-robin one cold prefix per cycle so the full keyspace is
            # still re-verified hourly-ish, but each cycle's LIST cost stays
            # bounded (hot prefixes are the money/chat paths officers watch).
            cold = [p for p in (tops or []) if p not in HOT_PREFIXES]
            next_cold = cold[(cycle - 1) % len(cold)] if cold else None
            targets = fixed or (
                [p for p in hot if p] + ([next_cold] if next_cold else [])
            )
            queued = 0
            oversized = 0
            root_files = 0
            # Deletion-proofing: retry every tombstoned object each cycle
            # BEFORE new listings (review 2026-09-02). If the object is back
            # or was a transient failure it syncs and the tombstone clears;
            # a 404 now writes deletion evidence instead of a log line.
            try:
                (pending_retries,) = state.execute(
                    "SELECT COUNT(*) FROM sync_failures "
                    "WHERE fail_count>0 AND terminal=0"
                ).fetchone()
                (terminal_tombstones,) = state.execute(
                    "SELECT COUNT(*) FROM sync_failures WHERE terminal=1"
                ).fetchone()
            except sqlite3.Error:
                pending_retries = 0
                terminal_tombstones = 0
            if pending_retries and not degraded:
                log(f"retrying {pending_retries:,} tombstoned object(s)…")
                retried = 0
                for rk, rsize, retag in state.execute(
                    "SELECT key,size,etag FROM sync_failures "
                    "WHERE fail_count>0 AND terminal=0"
                ).fetchall():
                    if stop_evt.is_set() or retried >= 20000:
                        break
                    rate_q.put((rk, rsize or 0, retag or ""))
                    queued += 1
                    retried += 1
            if not fixed:
                root_bytes = 0
                root_newest = ""
                for key, size, etag, lm in lister.iter_root_files():
                    root_files += 1
                    root_bytes += size
                    root_newest = max(root_newest, normalize_ts(lm))
                    entries = [(key, size, etag, lm)]
                    for k2, s2, e2, l2 in diff_against_state(state, entries):
                        if s2 > MAX_SYNC_BYTES:
                            oversized += 1
                            continue
                        if degraded:
                            continue
                        rate_q.put((k2, s2, e2))
                        queued += 1
                record_prefix_inventory(
                    state, "(root)", root_files, root_bytes, root_newest, cycle
                )
            for pfx in targets:
                if stop_evt.is_set():
                    break
                page_buf: list[tuple] = []
                prefix_count = 0
                prefix_bytes = 0
                prefix_newest = ""
                try:
                    for key, size, etag, lm in lister.iter_prefix(pfx):
                        if stop_evt.is_set():
                            break
                        if not key or key.endswith("/"):
                            continue
                        stats["scanned"] += 1
                        cycle_scanned += 1
                        prefix_count += 1
                        prefix_bytes += size
                        prefix_newest = max(prefix_newest, normalize_ts(lm))
                        page_buf.append((key, size, etag, lm))
                        if len(page_buf) >= 1000:
                            for k2, s2, e2, _ in diff_against_state(state, page_buf):
                                if s2 > MAX_SYNC_BYTES:
                                    oversized += 1
                                    continue
                                if degraded:
                                    continue
                                rate_q.put((k2, s2, e2))
                                queued += 1
                            page_buf = []
                    if page_buf:
                        for k2, s2, e2, _ in diff_against_state(state, page_buf):
                            if s2 > MAX_SYNC_BYTES:
                                oversized += 1
                                continue
                            if degraded:
                                continue
                            rate_q.put((k2, s2, e2))
                            queued += 1
                    record_prefix_inventory(
                        state,
                        pfx,
                        prefix_count,
                        prefix_bytes,
                        prefix_newest,
                        cycle,
                    )
                except Exception as e:
                    log(f"list {pfx or '(root)'} failed: {str(e)[:120]}")
            log(
                f"cycle {cycle}: scanned_this_cycle={cycle_scanned:,} "
                f"scanned_total={stats['scanned']:,} queued={queued:,} "
                f"root_files={root_files} oversized_skipped={oversized}"
            )

            # Publish status BEFORE draining the queue (review 2026-09-02):
            # cycle 1 on a fresh state DB can drain for hours (3,595-object
            # catch-up took ~40 min). Publishing only after the drain left
            # status.json absent, and the dashboard reported "NO SYNC YET —
            # mirror may be frozen" during perfectly normal catch-up.
            #
            # Heartbeat refactor (review 2026-09-10): one builder serves the
            # pre-drain publish, the in-drain heartbeat, and the post-drain
            # snapshot, so status.json utc never goes stale during 30+ min
            # drains (832K-object listings kept the dashboard at STALE).
            def publish_cycle_status(state_label, queued_pending, took_sec):
                (mirror_n2,) = state.execute("SELECT COUNT(*) FROM synced").fetchone()
                (retr2,) = state.execute(
                    "SELECT COUNT(*) FROM sync_failures "
                    "WHERE fail_count>0 AND terminal=0"
                ).fetchone()
                (term2,) = state.execute(
                    "SELECT COUNT(*) FROM sync_failures WHERE terminal=1"
                ).fetchone()
                hb.metadata = {"case_id": CASE_ID, "authorization_ref": AUTH_REF}
                hb.upload_from_string(
                    f"{utcnow()} cycle={cycle} alive\n",
                    content_type="text/plain",
                )
                sb = bucket.blob(SYNC_STATUS_PREFIX + "status.json")
                sb.metadata = {
                    "case_id": CASE_ID,
                    "authorization_ref": AUTH_REF,
                }
                inv = source_inventory_summary(state, tops or targets)
                sb.upload_from_string(
                    json.dumps(
                        {
                            "utc": utcnow(),
                            "cycle": cycle,
                            "state": state_label,
                            "writes_blocked": WRITE_STATE["blocked"],
                            "writes_block_reason": WRITE_STATE["reason"][:160],
                            "queued_pending": queued_pending,
                            "tombstones_pending": retr2,
                            "tombstones_terminal": term2,
                            "cycle_took_sec": round(took_sec, 1),
                            "scanned_this_cycle": cycle_scanned,
                            "scanned_total": stats["scanned"],
                            "queued": queued,
                            "uploaded_total": stats["uploaded"],
                            "bytes_total": stats["bytes"],
                            "errors": stats["errors"],
                            "dashboard_notified": stats["notified"],
                            "dashboard_notify_errors": stats["notify_errors"],
                            "oversized_skipped": oversized,
                            "gcs_mirror_objects": mirror_n2,
                            "preserved_only_estimate": max(
                                0,
                                mirror_n2 - int(inv["source_inventory_objects"]),
                            )
                            if inv["source_inventory_complete"]
                            else None,
                            **inv,
                            "s3_bucket": S3_BUCKET_URL,
                            "gcs_target": f"gs://{GCS_BUCKET}/{GCS_PREFIX}",
                            "case": CASE_ID,
                            "authorization_ref": AUTH_REF,
                        },
                        indent=1,
                    ),
                    content_type="application/json",
                )

            try:
                publish_cycle_status(
                    "syncing" if queued else "idle", queued, time.monotonic() - t0
                )
            except Exception as e:
                log(f"status publish failed: {str(e)[:120]}")
            # drain queue with a 60s heartbeat: join() alone blocks for the
            # whole drain, so the loop polls unfinished_tasks and republishes
            # live queue depth instead of leaving status.json frozen.
            last_hb = time.monotonic()
            while rate_q.unfinished_tasks > 0 and not stop_evt.is_set():
                time.sleep(2)
                if time.monotonic() - last_hb >= 60:
                    last_hb = time.monotonic()
                    try:
                        publish_cycle_status(
                            "draining", rate_q.qsize(), time.monotonic() - t0
                        )
                    except Exception as e:
                        log(f"drain heartbeat failed: {str(e)[:120]}")
            took = time.monotonic() - t0
            # Publish a post-drain snapshot. Without this, the dashboard keeps
            # showing the pre-drain queue until the next multi-minute source
            # listing finishes, even though every object is already mirrored.
            try:
                publish_cycle_status("idle", 0, took)
            except Exception as e:
                log(f"post-drain status publish failed: {str(e)[:120]}")
            if args.once:
                break
            wait = CATCHUP_CYCLE_SEC if queued > 0 else CYCLE_SEC
            log(f"cycle {cycle} complete in {took:.0f}s — sleeping {wait}s")
            stop_evt.wait(wait)
    finally:
        stop_evt.set()
        for _ in workers:
            rate_q.put(None)
        for w in workers:
            w.join(timeout=30)
        log(
            f"final: uploaded={stats['uploaded']} bytes={stats['bytes']:,} "
            f"errors={stats['errors']}"
        )
        # Final status publish so /api/mirror-sync shows a precise verdict
        # ("STOPPED") instead of a stale-LIVE misread if the daemon exits.
        try:
            status_blob = bucket.blob(SYNC_STATUS_PREFIX + "status.json")
            status_blob.metadata = {"case_id": CASE_ID, "authorization_ref": AUTH_REF}
            status_blob.upload_from_string(
                json.dumps(
                    {
                        "utc": utcnow(),
                        "cycle": cycle,
                        "state": "stopped",
                        "uploaded_total": stats["uploaded"],
                        "bytes_total": stats["bytes"],
                        "errors": stats["errors"],
                        "s3_bucket": S3_BUCKET_URL,
                        "gcs_target": f"gs://{GCS_BUCKET}/{GCS_PREFIX}",
                        "case": CASE_ID,
                        "authorization_ref": AUTH_REF,
                    },
                    indent=1,
                ),
                content_type="application/json",
            )
        except Exception:
            pass
        try:
            state.close()
        except Exception:
            pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
