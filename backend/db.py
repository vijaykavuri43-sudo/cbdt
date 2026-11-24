"""SQLite manifest of all bucket objects (WAL, per-thread connections, writer lock)."""

import json
import csv
import fcntl
import hashlib
import math
import os
import re
import sqlite3
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Collection, Optional

from . import config

MAX_AMOUNT_INR = config.MAX_AMOUNT_INR

# CASE/CAST remove JSON result subtypes so SQLite can read these expressions
# directly from an index instead of re-parsing every payload. Preserve numeric
# JSON types (including booleans), rather than coercing strings into amounts.
_FLOW_KIND_SQL = (
    "CASE WHEN json_valid(extract_json) "
    "THEN CAST(json_extract(extract_json,'$.kind') AS TEXT) END"
)
_FLOW_AMOUNT_SQL = (
    "CASE WHEN json_valid(extract_json) THEN CASE "
    "WHEN json_type(extract_json,'$.amount_inr') IN ('integer','real','true','false') "
    "THEN json_extract(extract_json,'$.amount_inr') END END"
)


def _sanitize_amount(value) -> float | None:
    """Plausible single-object INR amount, or None.

    UPI deposits realistically top out in the low lakhs; even a large single
    crypto deposit won't exceed ₹1 crore. Values beyond that (or NaN/strings)
    are OCR/QR mis-parses — keep the evidence row, exclude from money sums.
    Accepts floats, ints, or string forms like "=1,000" / "1 000" / "Rs 500"
    (OCR rupee-glyph artefacts).
    """
    if value is None:
        return None
    if isinstance(value, (int, float)):
        num = float(value)
    else:
        s = re.sub(r"[^\d.]", "", str(value))
        if not s or s.count(".") > 1:
            return None
        try:
            num = float(s)
        except ValueError:
            return None
    if math.isnan(num) or math.isinf(num) or not (0 < num <= MAX_AMOUNT_INR):
        return None
    return num


class _WriterLock:
    """Thread- AND process-wide writer serialization.

    uvicorn runs 2 worker processes; a threading.RLock only serialized writes
    inside one process, so the two workers raced for SQLite's single WAL writer
    slot. When one lost the race it could return "database is locked" and, with
    no rollback, leave an open transaction that pinned the WAL forever (live:
    19 GB WAL, main DB uncheckpointed for 4 days, 113 lock errors/day). An
    advisory file lock on the persistent volume serializes writers across
    processes; the RLock keeps nested writes (ledger -> wallets) re-entrant.

    RLock (not Lock): writer paths nest — e.g. ledger.sync_ledger holds this
    while merge_object_wallets re-acquires it. A plain Lock deadlocks there
    (verified 2026-09-16: sync hung forever). Same-thread re-entry is safe:
    connections are thread-local, so the same conn is reused.
    """

    def __init__(self):
        self._rlock = threading.RLock()
        self._fd = None
        self._depth = 0

    def __enter__(self):
        self._rlock.acquire()
        try:
            if self._depth == 0:
                path = Path(str(config.DB_PATH) + ".wlock")
                path.parent.mkdir(parents=True, exist_ok=True)
                fd = os.open(str(path), os.O_CREAT | os.O_RDWR, 0o600)
                try:
                    fcntl.flock(fd, fcntl.LOCK_EX)
                except BaseException:
                    os.close(fd)
                    raise
                self._fd = fd
            self._depth += 1
        except BaseException:
            self._rlock.release()
            raise
        return self

    def __exit__(self, exc_type, exc, tb):
        try:
            self._depth -= 1
            if self._depth == 0 and self._fd is not None:
                try:
                    fcntl.flock(self._fd, fcntl.LOCK_UN)
                finally:
                    os.close(self._fd)
                    self._fd = None
        finally:
            self._rlock.release()
        return False


class _SafeConnection(sqlite3.Connection):
    """Roll back on statement failure so no transaction is ever left pinned.

    Python's sqlite3 opens an implicit transaction before DML. If a write fails
    (e.g. SQLITE_BUSY from a concurrent writer) and the caller does not
    commit/rollback, that connection keeps its transaction and its WAL snapshot
    open. In WAL mode a pinned snapshot prevents checkpointing, so the WAL grows
    without bound and every later write fails with "database is locked".
    """

    def _safe_rollback(self):
        try:
            super().rollback()
        except Exception:
            pass

    def execute(self, *args, **kwargs):
        try:
            return super().execute(*args, **kwargs)
        except sqlite3.OperationalError:
            self._safe_rollback()
            raise

    def executemany(self, *args, **kwargs):
        try:
            return super().executemany(*args, **kwargs)
        except sqlite3.OperationalError:
            self._safe_rollback()
            raise

    def executescript(self, *args, **kwargs):
        try:
            return super().executescript(*args, **kwargs)
        except sqlite3.OperationalError:
            self._safe_rollback()
            raise

    def commit(self):
        try:
            return super().commit()
        except sqlite3.OperationalError:
            self._safe_rollback()
            raise


_writer_lock = _WriterLock()
_local = threading.local()
_schema_lock = threading.Lock()
_schema_ready: set[str] = set()


def connect() -> sqlite3.Connection:
    conn = getattr(_local, "conn", None)
    if conn is None:
        config.DB_PATH.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(
            str(config.DB_PATH), timeout=120.0, factory=_SafeConnection
        )
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        conn.execute("PRAGMA busy_timeout=120000")
        # Read-heavy 1M+ row dashboard on a 16GB host: keep hot index/B-tree
        # pages in RAM instead of re-reading them per navigation (2026-09-03).
        conn.execute("PRAGMA cache_size=-262144")  # 256MB page cache
        conn.execute("PRAGMA mmap_size=1073741824")  # 1GB mmap for index scans
        conn.execute("PRAGMA temp_store=MEMORY")  # ORDER BY spill goes to RAM
        # 2026-09-05: cap WAL growth — an uncheckpointed multi-GB WAL made
        # every drain's persist phase take 60-76s (live measurement). Persistent
        # setting; passive checkpoints after each drain do the rest.
        try:
            conn.execute("PRAGMA journal_size_limit=134217728")  # 128MB
        except Exception:
            pass
        # One-time schema/migration writes are serialized across uvicorn
        # workers and the sync container (same data dir, same flock). On a
        # large live DB a concurrent bulk writer can hold SQLite's write lock
        # past the busy timeout; retry instead of failing app startup.
        #
        # Only the first connection in this process pays this cost. Running
        # CREATE TABLE/INDEX IF NOT EXISTS on every new thread connection made
        # each API request queue behind the live sync writer (12GB DB: /api
        # requests hung for 90s+ after the chain tables were added).
        db_key = str(config.DB_PATH)
        with _schema_lock:
            if db_key not in _schema_ready:
                with _writer_lock:
                    for attempt in range(8):
                        try:
                            conn.executescript(SCHEMA)
                            _migrate(conn)
                            break
                        except sqlite3.OperationalError as e:
                            if "locked" not in str(e).lower() or attempt == 7:
                                raise
                            time.sleep(2 + attempt * 2)
                _schema_ready.add(db_key)
        _local.conn = conn
    return conn


def checkpoint(truncate: bool = False) -> dict:
    """Checkpoint the WAL so it cannot grow into the multi-GB drag seen live.

    PASSIVE (default): never blocks readers/writers; back-pressures the WAL
    file as long as no connection holds old frames. Called after every drain
    persist — ~ms when the WAL is small. TRUNCATE: maintenance path (deploy/
    manual) that reclaims the file down to zero; briefly takes the writer lock.
    """
    mode = "TRUNCATE" if truncate else "PASSIVE"
    try:
        if truncate:
            with _writer_lock:
                row = connect().execute(f"PRAGMA wal_checkpoint({mode})").fetchone()
        else:
            # PASSIVE never blocks readers/writers, so it must not queue behind
            # the global writer lock. Called after EVERY drain; taking the lock
            # here let a startup backfill stall the 24h slip loop at its final
            # checkpoint (observed 2026-11-08).
            row = connect().execute(f"PRAGMA wal_checkpoint({mode})").fetchone()
        if row:
            return {"mode": mode, "busy": row[0], "frames": row[1], "done": row[2]}
    except Exception as e:
        return {"mode": mode, "error": str(e)[:120]}
    return {"mode": mode}


def _migrate(conn: sqlite3.Connection):
    """Guarded one-time migrations (safe to run on every connect)."""
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(objects)").fetchall()}
    if "ext" not in cols:
        conn.execute("ALTER TABLE objects ADD COLUMN ext TEXT NOT NULL DEFAULT ''")
    for name, ddl in (
        ("enrich_attempts", "INTEGER NOT NULL DEFAULT 0"),
        ("enrich_error", "TEXT"),
        ("enrich_retry_utc", "TEXT"),
        ("enriched_utc", "TEXT"),
    ):
        if name not in cols:
            conn.execute(f"ALTER TABLE objects ADD COLUMN {name} {ddl}")
    payment_cols = {
        r["name"]
        for r in conn.execute("PRAGMA table_info(payment_transactions)").fetchall()
    }
    for name, ddl in (
        ("rail_role", "TEXT NOT NULL DEFAULT 'unknown'"),
        ("role_confidence", "TEXT NOT NULL DEFAULT 'low'"),
        # Tronscan verification (2026-11-20): canonical wallet + on-chain tx.
        ("rail_id_raw", "TEXT NOT NULL DEFAULT ''"),
        ("wallet_status", "TEXT NOT NULL DEFAULT ''"),
        ("txid", "TEXT NOT NULL DEFAULT ''"),
        ("txids", "TEXT NOT NULL DEFAULT ''"),
        ("tx_status", "TEXT NOT NULL DEFAULT ''"),
        ("tx_method", "TEXT NOT NULL DEFAULT ''"),
        ("tx_confidence", "TEXT NOT NULL DEFAULT ''"),
        ("tx_time", "TEXT NOT NULL DEFAULT ''"),
        ("tx_from", "TEXT NOT NULL DEFAULT ''"),
        ("tx_to", "TEXT NOT NULL DEFAULT ''"),
        ("tx_amount", "REAL"),
        ("tx_token", "TEXT NOT NULL DEFAULT ''"),
        ("tx_contract_ret", "TEXT NOT NULL DEFAULT ''"),
        ("tx_candidates", "TEXT NOT NULL DEFAULT ''"),
        ("chain_checked_utc", "TEXT NOT NULL DEFAULT ''"),
    ):
        if name not in payment_cols:
            conn.execute(f"ALTER TABLE payment_transactions ADD COLUMN {name} {ddl}")
    wallet_cols = {
        r["name"] for r in conn.execute("PRAGMA table_info(wallet_hits)").fetchall()
    }
    for name, ddl in (
        ("raw_address", "TEXT NOT NULL DEFAULT ''"),
        ("wallet_status", "TEXT NOT NULL DEFAULT ''"),
        ("txid", "TEXT NOT NULL DEFAULT ''"),
        ("tx_status", "TEXT NOT NULL DEFAULT ''"),
        ("chain_checked_utc", "TEXT NOT NULL DEFAULT ''"),
    ):
        if name not in wallet_cols:
            conn.execute(f"ALTER TABLE wallet_hits ADD COLUMN {name} {ddl}")
    try:
        wr_cols = {
            r["name"]
            for r in conn.execute("PRAGMA table_info(wallet_resolutions)").fetchall()
        }
        if "status" not in wr_cols:
            conn.execute(
                "ALTER TABLE wallet_resolutions ADD COLUMN status TEXT NOT NULL DEFAULT ''"
            )
    except sqlite3.OperationalError:
        pass
    try:
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_pay_txid ON payment_transactions(txid)"
            " WHERE txid != ''"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_pay_txstatus ON payment_transactions(tx_status)"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_wh_txid ON wallet_hits(txid)"
            " WHERE txid != ''"
        )
    except sqlite3.OperationalError:
        pass
    try:
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_obj_ext ON objects(ext) WHERE ext != ''"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_obj_enrich_fast ON objects(top_prefix,ext,last_modified DESC) "
            "WHERE enriched = 0 AND extract_json IS NULL"
        )
    except sqlite3.OperationalError:
        pass


def file_ext(key: str) -> str:
    base = key.rsplit("/", 1)[-1]
    dot = base.rfind(".")
    if dot <= 0:
        return ""
    e = base[dot + 1 :].lower()
    return "." + e if len(e) <= 8 else ""


def normalize_object_ts(ts: Optional[str]) -> str:
    """Normalize object timestamps to ISO-8601 UTC ('YYYY-MM-DDTHH:MM:SSZ').

    Handles S3/GCS ISO-8601 (with or without fractional seconds) AND RFC-1123
    HTTP-dates ('Wed, 31 Jul 2024 21:45:23 GMT') leaked through the GCS
    mirror's custom ``last_modified`` metadata. This matters because the
    manifest sorts lexicographically: mixed ISO/RFC formats corrupt every
    ``order=-modified`` view (RFC strings sort above all ISO strings, so
    2024 objects appear on top of newest-first feeds). Unparseable values
    are returned unchanged so evidence is never silently dropped.
    """
    if not ts:
        return ""
    try:
        dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
        return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    except ValueError:
        pass
    try:
        from email.utils import parsedate_to_datetime

        return (
            parsedate_to_datetime(ts)
            .astimezone(timezone.utc)
            .strftime("%Y-%m-%dT%H:%M:%SZ")
        )
    except (TypeError, ValueError):
        return ts


SCHEMA = """
CREATE TABLE IF NOT EXISTS objects(
  key TEXT PRIMARY KEY,
  top_prefix TEXT NOT NULL DEFAULT '',
  size INTEGER NOT NULL DEFAULT 0,
  last_modified TEXT DEFAULT '',
  etag TEXT DEFAULT '',
  seen_utc TEXT DEFAULT '',
  enriched INTEGER NOT NULL DEFAULT 0,
  extract_json TEXT,
  ext TEXT NOT NULL DEFAULT '',
  enrich_attempts INTEGER NOT NULL DEFAULT 0,
  enrich_error TEXT,
  enrich_retry_utc TEXT,
  enriched_utc TEXT
);
CREATE INDEX IF NOT EXISTS idx_obj_top ON objects(top_prefix);
CREATE INDEX IF NOT EXISTS idx_obj_lm ON objects(last_modified DESC);
CREATE INDEX IF NOT EXISTS idx_obj_seen ON objects(seen_utc DESC);
-- Composite index for pending-enrichment queries (covers WHERE clause exactly)
CREATE INDEX IF NOT EXISTS idx_obj_enrich_pending ON objects(enriched, extract_json, top_prefix)
  WHERE enriched = 0 AND extract_json IS NULL;
-- Index for deposit/upi queries via JSON extract
CREATE INDEX IF NOT EXISTS idx_obj_extract_kind ON objects(extract_json)
  WHERE extract_json IS NOT NULL;
-- Dashboard aggregates must not scan multi-GB OCR payloads. SQLite keeps
-- these compact projections in sync on every insert, re-OCR and deletion.
CREATE INDEX IF NOT EXISTS idx_obj_inventory ON objects(top_prefix,key,size,ext);
CREATE INDEX IF NOT EXISTS idx_obj_progress ON objects(
  enriched, (extract_json IS NOT NULL), (enrich_error IS NOT NULL), enriched_utc
);
-- Index for search (LIKE queries benefit from prefix matching via top_prefix)
CREATE INDEX IF NOT EXISTS idx_obj_prefix_key ON objects(top_prefix, key);
-- Composite index for newest-first folder browsing: tight range scan per top dir
CREATE INDEX IF NOT EXISTS idx_obj_top_lm ON objects(top_prefix, last_modified DESC, key DESC);
-- Bucket-root files are a handful of ancient rows; without this the root view
-- walks the whole last_modified index on every visit.
CREATE INDEX IF NOT EXISTS idx_obj_root_lm ON objects(last_modified DESC, key DESC)
  WHERE instr(key, '/') = 0;
-- Pending-pick fast path (2026-09-03): the 2s enrich drain sorts ~1M pending
-- rows newest-first on every cycle (~600ms). (top_prefix, seen_utc) serves the
-- fresh-head query as a tight per-prefix range scan; the bulk fill below uses
-- rowid order (no sort at all) since backlog drain order is irrelevant.
CREATE INDEX IF NOT EXISTS idx_obj_enrich_pick ON objects(top_prefix, seen_utc DESC)
  WHERE enriched = 0 AND extract_json IS NULL;
CREATE TABLE IF NOT EXISTS meta(k TEXT PRIMARY KEY, v TEXT);
-- Materialized crypto-wallet index (2026-09-16): json_each scans over 700k+
-- payloads made /api/wallets time out (60s+ on the VM). One row per
-- object x wallet hit, written through on every payload persist + one-time
-- backfill. All wallet reads serve from here (indexed, ms).
CREATE TABLE IF NOT EXISTS wallet_hits(
  address TEXT NOT NULL,
  key TEXT NOT NULL,
  last_modified TEXT DEFAULT '',
  seen_utc TEXT DEFAULT '',
  network TEXT DEFAULT '',
  standard TEXT DEFAULT '',
  confidence TEXT DEFAULT '',
  checksum TEXT DEFAULT '',
  kind TEXT DEFAULT '',
  amount_inr REAL,
  amount_value REAL,
  amount_currency TEXT DEFAULT '',
  txids TEXT DEFAULT '',
  PRIMARY KEY(address, key)
);
CREATE INDEX IF NOT EXISTS idx_wh_lm ON wallet_hits(last_modified DESC);
CREATE INDEX IF NOT EXISTS idx_wh_addr_lm ON wallet_hits(address, last_modified DESC);
-- Tronscan verification (2026-11-20): raw OCR address -> canonical address,
-- one row per distinct raw string. Never overwrites the source evidence.
CREATE TABLE IF NOT EXISTS wallet_resolutions(
  raw_address TEXT PRIMARY KEY,
  resolved_address TEXT NOT NULL DEFAULT '',
  network TEXT NOT NULL DEFAULT 'TRON',
  method TEXT NOT NULL DEFAULT '',
  confidence TEXT NOT NULL DEFAULT '',
  status TEXT NOT NULL DEFAULT '',
  tronscan_exists INTEGER NOT NULL DEFAULT 0,
  first_seen_utc TEXT NOT NULL DEFAULT '',
  checked_utc TEXT NOT NULL DEFAULT ''
);
-- Cached Tronscan transaction details (immutable; status may be refreshed).
CREATE TABLE IF NOT EXISTS chain_tx_cache(
  txid TEXT PRIMARY KEY,
  network TEXT NOT NULL DEFAULT 'TRON',
  from_address TEXT NOT NULL DEFAULT '',
  to_address TEXT NOT NULL DEFAULT '',
  amount REAL,
  token TEXT NOT NULL DEFAULT '',
  token_contract TEXT NOT NULL DEFAULT '',
  block_ts TEXT NOT NULL DEFAULT '',
  contract_ret TEXT NOT NULL DEFAULT '',
  confirmed INTEGER NOT NULL DEFAULT 0,
  raw_json TEXT NOT NULL DEFAULT '',
  response_sha256 TEXT NOT NULL DEFAULT '',
  fetched_utc TEXT NOT NULL DEFAULT ''
);
-- Audit trail: every txid candidate considered for a slip, not just the winner.
CREATE TABLE IF NOT EXISTS chain_links(
  source_key TEXT NOT NULL,
  rail_id TEXT NOT NULL,
  txid TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT '',
  method TEXT NOT NULL DEFAULT '',
  confidence TEXT NOT NULL DEFAULT '',
  match_score REAL NOT NULL DEFAULT 0,
  from_address TEXT NOT NULL DEFAULT '',
  to_address TEXT NOT NULL DEFAULT '',
  amount REAL,
  token TEXT NOT NULL DEFAULT '',
  block_ts TEXT NOT NULL DEFAULT '',
  contract_ret TEXT NOT NULL DEFAULT '',
  candidates_json TEXT NOT NULL DEFAULT '',
  checked_utc TEXT NOT NULL DEFAULT '',
  PRIMARY KEY(source_key, rail_id, txid)
);
CREATE INDEX IF NOT EXISTS idx_chain_links_key ON chain_links(source_key);
-- Object-level discovery queue: enriched objects whose OCR text mentions a
-- Tronscan transaction/hash but which never became a payment row or wallet
-- hit (truncated address, chat screenshot, …). Scanned once by rowid cursor.
CREATE TABLE IF NOT EXISTS chain_source_queue(
  key TEXT PRIMARY KEY,
  reason TEXT NOT NULL DEFAULT '',
  queued_utc TEXT NOT NULL DEFAULT '',
  checked_utc TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_chain_queue_pending
  ON chain_source_queue(checked_utc, queued_utc);
CREATE TABLE IF NOT EXISTS upi_hits(
  key TEXT PRIMARY KEY,
  vpa TEXT NOT NULL,
  last_modified TEXT DEFAULT '',
  seen_utc TEXT DEFAULT '',
  kind TEXT DEFAULT '',
  sender_only INTEGER DEFAULT 0,
  has_bene INTEGER DEFAULT 0,
  has_sender INTEGER DEFAULT 0,
  amount_inr REAL,
  payee_name TEXT DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_uh_lm ON upi_hits(last_modified DESC);
CREATE INDEX IF NOT EXISTS idx_uh_vpa_lm ON upi_hits(vpa, last_modified DESC);
CREATE TABLE IF NOT EXISTS utr_identity(
  utr TEXT PRIMARY KEY,
  gambler TEXT DEFAULT '',
  amount_inr REAL,
  day TEXT DEFAULT '',
  vpa TEXT DEFAULT '',
  source_reference TEXT DEFAULT ''
);
CREATE TABLE IF NOT EXISTS payment_transactions(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  source_key TEXT NOT NULL,
  direction TEXT NOT NULL,
  gambler TEXT DEFAULT '',
  amount_inr REAL,
  utr TEXT DEFAULT '',
  rail_type TEXT NOT NULL,
  rail_id TEXT NOT NULL DEFAULT '',
  account_holder TEXT DEFAULT '',
  ifsc TEXT DEFAULT '',
  network TEXT DEFAULT '',
  confidence TEXT DEFAULT '',
  rail_role TEXT NOT NULL DEFAULT 'unknown',
  role_confidence TEXT NOT NULL DEFAULT 'low',
  last_modified TEXT DEFAULT '',
  evidence_json TEXT,
  UNIQUE(source_key, rail_type, rail_id)
);
CREATE INDEX IF NOT EXISTS idx_pay_direction_lm ON payment_transactions(direction,last_modified DESC);
CREATE INDEX IF NOT EXISTS idx_pay_direction_source ON payment_transactions(direction,source_key);
CREATE INDEX IF NOT EXISTS idx_pay_direction_rail ON payment_transactions(direction,rail_type,rail_id);
CREATE INDEX IF NOT EXISTS idx_pay_rail_lm_direction ON payment_transactions(rail_type,last_modified DESC,direction);
CREATE INDEX IF NOT EXISTS idx_pay_lm ON payment_transactions(last_modified DESC);
CREATE TABLE IF NOT EXISTS slip_fingerprints(
  source_key TEXT PRIMARY KEY, etag TEXT NOT NULL, size INTEGER NOT NULL,
  sha256 TEXT NOT NULL, pixel_sha256 TEXT NOT NULL, updated_utc TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_pay_gambler ON payment_transactions(gambler) WHERE gambler != '';
CREATE INDEX IF NOT EXISTS idx_pay_rail ON payment_transactions(rail_type,rail_id) WHERE rail_id != '';
CREATE INDEX IF NOT EXISTS idx_pay_utr ON payment_transactions(utr) WHERE utr != '';
CREATE INDEX IF NOT EXISTS idx_pay_railid ON payment_transactions(rail_id) WHERE rail_id != '';
CREATE INDEX IF NOT EXISTS idx_pay_holder ON payment_transactions(account_holder) WHERE account_holder != '';
CREATE INDEX IF NOT EXISTS idx_pay_ifsc ON payment_transactions(ifsc) WHERE ifsc != '';
CREATE INDEX IF NOT EXISTS idx_pay_amount ON payment_transactions(amount_inr);
CREATE TABLE IF NOT EXISTS payment_flow_totals(
  direction TEXT NOT NULL,
  rail_filter TEXT NOT NULL DEFAULT '',
  transactions INTEGER NOT NULL DEFAULT 0,
  sum_amount_inr REAL NOT NULL DEFAULT 0,
  distinct_rails INTEGER NOT NULL DEFAULT 0,
  distinct_gamblers INTEGER NOT NULL DEFAULT 0,
  linked_transactions INTEGER NOT NULL DEFAULT 0,
  rows_total INTEGER NOT NULL DEFAULT 0,
  refreshed_utc TEXT NOT NULL,
  PRIMARY KEY(direction,rail_filter)
);
CREATE TABLE IF NOT EXISTS payment_rail_summary(
  direction TEXT NOT NULL,
  rail_type TEXT NOT NULL,
  rail_id TEXT NOT NULL,
  account_holder TEXT NOT NULL DEFAULT '',
  ifsc TEXT NOT NULL DEFAULT '',
  network TEXT NOT NULL DEFAULT '',
  transactions INTEGER NOT NULL DEFAULT 0,
  amount_inr REAL NOT NULL DEFAULT 0,
  gamblers INTEGER NOT NULL DEFAULT 0,
  refreshed_utc TEXT NOT NULL,
  PRIMARY KEY(direction,rail_type,rail_id,account_holder,ifsc,network)
);
CREATE INDEX IF NOT EXISTS idx_pay_summary_rank ON payment_rail_summary(direction,rail_type,transactions DESC,amount_inr DESC);
CREATE TABLE IF NOT EXISTS ledger_files(
  key TEXT PRIMARY KEY,
  etag TEXT DEFAULT '',
  size INTEGER NOT NULL DEFAULT 0,
  last_modified TEXT DEFAULT '',
  kind TEXT DEFAULT '',
  rows INTEGER NOT NULL DEFAULT 0,
  status TEXT DEFAULT '',
  synced_utc TEXT DEFAULT ''
);
CREATE TABLE IF NOT EXISTS ledger_rows(
  key TEXT NOT NULL,
  row_no INTEGER NOT NULL DEFAULT 0,
  type TEXT DEFAULT '',
  sender TEXT DEFAULT '',
  gambler TEXT DEFAULT '',
  amount REAL,
  bonus REAL NOT NULL DEFAULT 0,
  cash REAL,
  utr TEXT DEFAULT '',
  remark TEXT DEFAULT '',
  ts TEXT DEFAULT '',
  PRIMARY KEY(key,row_no)
);
CREATE INDEX IF NOT EXISTS idx_ledger_type ON ledger_rows(type);
CREATE INDEX IF NOT EXISTS idx_ledger_utr ON ledger_rows(utr) WHERE utr != '';
CREATE INDEX IF NOT EXISTS idx_ledger_gambler ON ledger_rows(gambler) WHERE gambler != '';
CREATE INDEX IF NOT EXISTS idx_ledger_sender ON ledger_rows(sender) WHERE sender != '';
CREATE TABLE IF NOT EXISTS gateway_files(
  key TEXT PRIMARY KEY,
  etag TEXT DEFAULT '',
  size INTEGER NOT NULL DEFAULT 0,
  last_modified TEXT DEFAULT '',
  kind TEXT DEFAULT '',
  rows INTEGER NOT NULL DEFAULT 0,
  test_rows INTEGER NOT NULL DEFAULT 0,
  status TEXT DEFAULT '',
  synced_utc TEXT DEFAULT ''
);
CREATE TABLE IF NOT EXISTS gateway_rows(
  key TEXT NOT NULL,
  row_no INTEGER NOT NULL DEFAULT 0,
  kind TEXT DEFAULT '',
  customer TEXT DEFAULT '',
  amount REAL,
  utr TEXT DEFAULT '',
  website TEXT DEFAULT '',
  client TEXT DEFAULT '',
  actionby TEXT DEFAULT '',
  receipt TEXT DEFAULT '',
  holder TEXT DEFAULT '',
  account TEXT DEFAULT '',
  ifsc TEXT DEFAULT '',
  txid TEXT DEFAULT '',
  is_test INTEGER NOT NULL DEFAULT 0,
  ts TEXT DEFAULT '',
  PRIMARY KEY(key,row_no)
);
CREATE INDEX IF NOT EXISTS idx_gateway_kind ON gateway_rows(kind);
CREATE INDEX IF NOT EXISTS idx_gateway_website ON gateway_rows(website) WHERE website != '';
CREATE INDEX IF NOT EXISTS idx_gateway_utr ON gateway_rows(utr) WHERE utr != '';
CREATE INDEX IF NOT EXISTS idx_gateway_account ON gateway_rows(account) WHERE account != '';
CREATE INDEX IF NOT EXISTS idx_gateway_customer ON gateway_rows(customer) WHERE customer != '';
CREATE TABLE IF NOT EXISTS prefix_stats_cache(
  p TEXT PRIMARY KEY,
  n INTEGER NOT NULL DEFAULT 0,
  s INTEGER NOT NULL DEFAULT 0,
  newest TEXT DEFAULT '',
  refreshed_utc TEXT DEFAULT ''
);
"""
SCHEMA += (
    f"CREATE INDEX IF NOT EXISTS idx_obj_flow_totals ON objects({_FLOW_KIND_SQL}, {_FLOW_AMOUNT_SQL}) "
    "WHERE extract_json IS NOT NULL;"
)


def close():
    conn = getattr(_local, "conn", None)
    if conn:
        conn.close()
        _local.conn = None


def set_meta(k: str, v: str):
    with _writer_lock:
        c = connect()
        c.execute(
            "INSERT INTO meta(k,v) VALUES(?,?) ON CONFLICT(k) DO UPDATE SET v=excluded.v",
            (k, v),
        )
        c.commit()


def get_meta(k: str, default=None):
    row = connect().execute("SELECT v FROM meta WHERE k=?", (k,)).fetchone()
    return row["v"] if row else default


def reset_collection_state(source_id: str):
    """Force one complete sweep when the backing object store changes."""
    with _writer_lock:
        c = connect()
        previous = c.execute("SELECT v FROM meta WHERE k='manifest_source'").fetchone()
        if previous and previous["v"] == source_id:
            return False
        c.execute("DELETE FROM meta WHERE k='sweep_complete_utc' OR k LIKE 'wm:%'")
        c.execute(
            "INSERT INTO meta(k,v) VALUES('manifest_source',?) "
            "ON CONFLICT(k) DO UPDATE SET v=excluded.v",
            (source_id,),
        )
        c.commit()
        return True


def repair_non_iso_timestamps(force: bool = False) -> dict:
    """One-time repair: rewrite non-ISO last_modified rows to ISO-8601 UTC.

    The GCS mirror's custom metadata historically carried RFC-1123 HTTP-dates
    ('Wed, 31 Jul 2024 21:45:23 GMT'). Lexicographic -modified sorting then put
    every RFC row above every ISO row, so 'newest-first' feeds showed 2024
    objects on top. Unparseable values are left untouched, never dropped.

    2026-11-08: the `NOT LIKE` predicate cannot use an index, so this was a
    2.47M-row full scan taken under the global writer lock at EVERY startup —
    a measured 5+ minute stall that held back the 24h enrichment drain after
    each restart. Scan on a read connection (WAL readers never block writers),
    take the writer lock only for the fixes, and record completion so later
    startups skip the scan. Ingestion normalizes timestamps, so a completed
    scan stays valid; pass force=True to re-run after a restore.
    """
    if not force and get_meta("ts_normalize_scan_v1") == "done":
        return {"checked": 0, "normalized": 0, "unparseable": 0, "skipped": True}
    rows = connect().execute(
        "SELECT key, last_modified FROM objects "
        "WHERE last_modified != '' AND last_modified NOT LIKE '____-__-__T__:__:__Z'"
    ).fetchall()
    if not rows:
        set_meta("ts_normalize_scan_v1", "done")
        return {"checked": 0, "normalized": 0, "unparseable": 0}
    updates: list[tuple[str, str, str]] = []
    unparseable = 0
    for r in rows:
        fixed = normalize_object_ts(r["last_modified"])
        if fixed != r["last_modified"] and fixed.startswith("20"):
            updates.append((fixed, r["key"], r["last_modified"]))
        else:
            unparseable += 1
    if updates:
        with _writer_lock:
            c = connect()
            # Optimistic guard: a concurrent sweep/enrichment may have already
            # rewritten the row; never clobber a newer value with our snapshot.
            for fixed, key, old in updates:
                c.execute(
                    "UPDATE objects SET last_modified=? WHERE key=? AND last_modified=?",
                    (fixed, key, old),
                )
            c.execute(
                "INSERT INTO meta(k,v) VALUES('ts_normalize_utc',?) "
                "ON CONFLICT(k) DO UPDATE SET v=excluded.v",
                (time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),),
            )
            c.commit()
    set_meta("ts_normalize_scan_v1", "done")
    return {
        "checked": len(rows),
        "normalized": len(updates),
        "unparseable": unparseable,
    }


def upsert_objects(rows: list[dict]) -> int:
    """rows: key,size,last_modified(raw iso),etag. Returns count inserted."""
    if not rows:
        return 0
    now = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    data = [
        (
            r["key"],
            r.get("top_prefix", ""),
            r["size"],
            normalize_object_ts(r.get("last_modified")),
            r.get("etag", ""),
            now,
            file_ext(r["key"]),
        )
        for r in rows
    ]
    updates = [
        (top, size, lm, etag, ext, key, size, etag, lm)
        for key, top, size, lm, etag, _seen, ext in data
    ]
    # The sync daemon calls this for every mirrored object (~24/s on bucket 2).
    # Under momentary contention SQLite can still return SQLITE_BUSY after the
    # busy timeout; retry briefly instead of surfacing a 500 to the daemon and
    # letting the object wait for the next cold crawl.
    last_err: sqlite3.OperationalError | None = None
    for attempt in range(4):
        try:
            with _writer_lock:
                c = connect()
                cur = c.executemany(
                    "INSERT OR IGNORE INTO objects(key,top_prefix,size,last_modified,etag,seen_utc,ext)"
                    " VALUES(?,?,?,?,?,?,?)",
                    data,
                )
                inserted = cur.rowcount if cur.rowcount and cur.rowcount > 0 else 0
                # Preserve first-seen time and completed extraction when content
                # is unchanged. An overwritten key is a new evidentiary object:
                # refresh metadata and put it back on the enrichment queue.
                c.executemany(
                    "UPDATE objects SET top_prefix=?,size=?,last_modified=?,etag=?,ext=?,"
                    " enriched=0,extract_json=NULL,enrich_attempts=0,enrich_error=NULL,enrich_retry_utc=NULL,enriched_utc=NULL"
                    " WHERE key=? AND (size<>? OR etag<>? OR last_modified<>?)",
                    updates,
                )
                c.commit()
                return inserted
        except sqlite3.OperationalError as e:
            last_err = e
            msg = str(e).lower()
            if "locked" not in msg and "busy" not in msg:
                raise
            time.sleep(0.25 * (2**attempt))
    assert last_err is not None
    raise last_err


def object_versions(keys: list[str]) -> dict[str, tuple[int, str, str]]:
    """Manifest versions for keys as ``key -> (size, etag, last_modified)``.

    A key alone is not sufficient evidence that an object is unchanged: S3/GCS
    objects can be overwritten in place. The wrapping crawl compares all three
    fields before skipping a page, so replacements are re-indexed and requeued
    for enrichment.
    """
    if not keys:
        return {}
    out: dict[str, tuple[int, str, str]] = {}
    c = connect()
    for i in range(0, len(keys), 900):
        chunk = keys[i : i + 900]
        ph = ",".join("?" * len(chunk))
        try:
            rows = c.execute(
                f"SELECT key,size,etag,last_modified FROM objects WHERE key IN ({ph})",
                chunk,
            ).fetchall()
            out.update(
                {
                    r["key"]: (
                        int(r["size"] or 0),
                        r["etag"] or "",
                        r["last_modified"] or "",
                    )
                    for r in rows
                }
            )
        except sqlite3.Error:
            pass
    return out


def total_count() -> int:
    return connect().execute("SELECT COUNT(*) c FROM objects").fetchone()["c"]


def prefix_stats() -> list[dict]:
    cached = (
        connect()
        .execute("SELECT p,n,s,newest FROM prefix_stats_cache ORDER BY n DESC")
        .fetchall()
    )
    if cached:
        return [dict(r) for r in cached]
    return _prefix_stats_live()


def refresh_prefix_stats() -> int:
    """Materialize per-prefix inventory (called ~minutely; served by prefix_stats)."""
    # Aggregate off-lock (read-only), then take the writer lock only for the
    # atomic cache replacement. Holding the lock across the GROUP BY scan let
    # this minutely job add to write starvation on the 2.47M-row table.
    rows = (
        connect()
        .execute(
            "SELECT top_prefix p,COUNT(*) n,SUM(size) s,MAX(last_modified) newest,"
            " strftime('%Y-%m-%dT%H:%M:%SZ','now') refreshed_utc"
            " FROM objects GROUP BY top_prefix"
        )
        .fetchall()
    )
    data = [tuple(r) for r in rows]
    with _writer_lock:
        c = connect()
        c.execute("DELETE FROM prefix_stats_cache")
        c.executemany(
            "INSERT INTO prefix_stats_cache(p,n,s,newest,refreshed_utc)"
            " VALUES(?,?,?,?,?)",
            data,
        )
        c.commit()
        return c.execute("SELECT COUNT(*) n FROM prefix_stats_cache").fetchone()["n"]


def _prefix_stats_live() -> list[dict]:
    rows = (
        connect()
        .execute(
            "SELECT top_prefix p, COUNT(*) n, SUM(size) s, MAX(last_modified) newest"
            " FROM objects GROUP BY top_prefix ORDER BY n DESC"
        )
        .fetchall()
    )
    return [dict(r) for r in rows]


def subfolders(prefix: str) -> dict:
    """Immediate child 'folders' of a prefix with counts+bytes."""
    now = time.monotonic()
    hit = _tree_cache.get(prefix)
    if hit is not None and now - hit[0] < _TREE_TTL:
        return hit[1]
    if not prefix:
        # Root fast path (2026-11-11): the old substr/LIKE '%' scan walked all
        # 758k+ objects on every cold /api/tree, which on a busy host exceeded
        # the Explorer's 20s budget. prefix_stats_cache already holds exactly
        # this aggregation (top_prefix -> count/bytes) for the minutely
        # materialized view, so root browsing becomes a small metadata read.
        rows = connect().execute(
            "SELECT p,n,s FROM prefix_stats_cache"
        ).fetchall()
        if not rows:
            rows = _prefix_stats_live()
        dirs = []
        files_n = 0
        files_s = 0
        for r in rows:
            p = r["p"] or ""
            n = int(r["n"] or 0)
            s = int(r["s"] or 0)
            if p.endswith("/") and p.strip("/"):
                dirs.append({"seg": p.strip("/"), "n": n, "size": s})
            else:
                # "(root)" (collector label) or "" = files directly at root.
                files_n += n
                files_s += s
        dirs.sort(key=lambda d: d["seg"])
        out = {"dirs": dirs, "files_n": files_n, "files_size": files_s}
        if len(_tree_cache) > 500:
            _tree_cache.clear()
        _tree_cache[prefix] = (time.monotonic(), out)
        return out
    plen = len(prefix)
    like = prefix.replace("%", r"\%").replace("_", r"\_") + "%"
    rows = (
        connect()
        .execute(
            "SELECT seg, is_dir, COUNT(*) n, SUM(sz) s FROM ("
            " SELECT substr(rest,1,CASE WHEN instr(rest,'/')>0 THEN instr(rest,'/')-1 ELSE length(rest) END) seg,"
            "        instr(rest,'/')>0 is_dir, size sz"
            " FROM (SELECT substr(key,?) rest, size FROM objects WHERE key LIKE ? ESCAPE '\\')"
            ") GROUP BY seg,is_dir ORDER BY seg",
            (plen + 1, like),
        )
        .fetchall()
    )
    dirs = [dict(seg=r["seg"], n=r["n"], size=r["s"] or 0) for r in rows if r["is_dir"]]
    files_n = sum(r["n"] for r in rows if not r["is_dir"])
    files_s = sum(r["s"] or 0 for r in rows if not r["is_dir"])
    out = {"dirs": dirs, "files_n": files_n, "files_size": files_s}
    if len(_tree_cache) > 500:
        _tree_cache.clear()
    _tree_cache[prefix] = (time.monotonic(), out)
    return out


# TTL cache for list totals: COUNT(*) over a 100K+ row prefix range is the
# slowest part of folder browsing; exact totals drift slowly so a stale total
# is fine for pagination UI (page windows always query live rows). Explicitly
# invalidated on sync/verify/notice ingestion (2026-09-03 cache review).
_LIST_TOTAL_TTL = 300.0
_list_total_cache: dict = {}
# Folder-tree cache: subfolders() substr-scans a whole prefix (1.3s on
# deposit/). Trees drift slowly; 5-min TTL, cleared with list totals.
_TREE_TTL = 300.0
_tree_cache: dict = {}


def invalidate_list_totals(prefix: str = ""):
    """Drop cached COUNTs for a top_prefix ('' = all) after new arrivals."""
    _tree_cache.clear()
    if not prefix:
        _list_total_cache.clear()
        return
    # cache keys store the normalized prefix ("banner/" or "" for root)
    top = prefix if prefix.endswith("/") else prefix.split("/", 1)[0] + "/"
    for k in [k for k in _list_total_cache if k[0] == top or k[0] == ""]:
        _list_total_cache.pop(k, None)


def _cached_list_total(cache_key, where, args) -> int:
    now = time.monotonic()
    hit = _list_total_cache.get(cache_key)
    if hit is not None and now - hit[0] < _LIST_TOTAL_TTL:
        return hit[1]
    conn = connect()
    total = conn.execute(f"SELECT COUNT(*) c FROM objects {where}", args).fetchone()[
        "c"
    ]
    if len(_list_total_cache) > 2000:
        _list_total_cache.clear()
    _list_total_cache[cache_key] = (now, total)
    return total


def list_objects(
    prefix: str,
    limit=200,
    offset=0,
    order="key",
    q: str = "",
    exts: Optional[list[str]] = None,
    since: str = "",
    until: str = "",
    flat: bool = False,
) -> dict:
    conds = []
    args: list = []
    if prefix:
        # top_prefix equality narrows to a tight index range (idx_obj_top_lm /
        # idx_obj_prefix_key) before the LIKE + instr() residual filters run.
        top = prefix.split("/", 1)[0] + "/"
        conds.append("top_prefix = ?")
        args.append(top)
        like = prefix.replace("%", r"\%").replace("_", r"\_") + "%"
        conds.append("key LIKE ? ESCAPE '\\'")
        args.append(like)
        if not flat:
            # direct children only: exclude keys with further '/'
            conds.append("instr(substr(key, ?), '/') = 0")
            args.append(len(prefix) + 1)
    elif not flat:
        conds.append("instr(key,'/') = 0")
    if q:
        conds.append("key LIKE ? ESCAPE '\\'")
        args.append("%" + q.replace("%", r"\%").replace("_", r"\_") + "%")
    if exts:
        conds.append("(" + " OR ".join(["lower(key) LIKE ?"] * len(exts)) + ")")
        args.extend("%" + e for e in exts)
    since_n = _bound_ts(since, end=False)
    until_n = _bound_ts(until, end=True)
    if since_n:
        conds.append("last_modified >= ?")
        args.append(since_n)
    if until_n:
        conds.append("last_modified <= ?")
        args.append(until_n)
    where = ("WHERE " + " AND ".join(conds)) if conds else ""
    cache_key = (prefix, q, tuple(exts or []), since_n, until_n, flat)
    total = _cached_list_total(cache_key, where, list(args))
    # Empty folder (root on this bucket contains 0 direct files): never run
    # the ORDER BY page scan. On a 758k-row table cold caches made this page
    # exceed the Explorer's 20s budget while showing "Nothing found" anyway.
    if total == 0:
        return {"total": 0, "items": []}
    conn = connect()
    # Root file fast path: with only a handful of direct-at-root keys, the
    # generic `ORDER BY key` plan scans the entire 2.5M-key primary index to
    # find them (17.5s measured live 2026-11-11 — the Explorer's 20s timeout).
    # idx_obj_root_lm stores exactly these rows; fetch them in index order and
    # sort/slice in Python. Fall back to SQL for pathological roots.
    if (
        not prefix
        and not flat
        and not q
        and not exts
        and not since_n
        and not until_n
        and total <= 5000
    ):
        rows = conn.execute(
            "SELECT key,size,last_modified,etag,seen_utc,enriched,extract_json"
            " FROM objects WHERE instr(key,'/') = 0"
            " ORDER BY last_modified DESC, key DESC"
        ).fetchall()
        if order == "key":
            rows.sort(key=lambda r: r["key"])
        elif order == "-modified":
            rows.sort(
                key=lambda r: (r["last_modified"] or "", r["key"] or ""),
                reverse=True,
            )
        elif order == "-seen":
            rows.sort(
                key=lambda r: (r["seen_utc"] or "", r["key"] or ""), reverse=True
            )
        else:  # size DESC, key ASC
            rows.sort(key=lambda r: (-int(r["size"] or 0), r["key"] or ""))
        return {
            "total": total,
            "items": [_row_pub(r) for r in rows[offset : offset + limit]],
        }
    # Stable newest-first: secondary key breaks ties where many objects share same timestamp.
    order_sql = {
        "key": "key ASC",
        "-modified": "last_modified DESC, key DESC",
        "-seen": "seen_utc DESC, key DESC",
        "size": "size DESC, key ASC",
    }[order]
    rows = conn.execute(
        f"SELECT key,size,last_modified,etag,seen_utc,enriched,extract_json FROM objects"
        f" {where} ORDER BY {order_sql} LIMIT ? OFFSET ?",
        args + [limit, offset],
    ).fetchall()
    return {"total": total, "items": [_row_pub(r) for r in rows]}


def search(
    q: str = "",
    ext: str = "",
    since: str = "",
    prefix: str = "",
    min_size: int = 0,
    limit: int = 500,
) -> list[dict]:
    conds = []
    args: list = []
    if q:
        conds.append("key LIKE ? ESCAPE '\\'")
        args.append("%" + q.replace("%", r"\%").replace("_", r"\_") + "%")
    if ext:
        for e in [x.strip().lower() for x in ext.split(",") if x.strip()]:
            conds.append("lower(key) LIKE ?")
            args.append("%" + e)
    if prefix:
        # Canonicalize like app._norm_prefix: collapse '//' so a malformed
        # prefix can never silently match zero keys (2026-09-11 explorer bug).
        p = re.sub(r"/+", "/", prefix.strip("/")) + "/"
        conds.append("key LIKE ? ESCAPE '\\'")
        args.append(p.replace("%", r"\%").replace("_", r"\_") + "%")
    if since:
        if len(since) == 10:
            since = since + "T00:00:00Z"
        conds.append("last_modified >= ?")
        args.append(since)
    if min_size:
        conds.append("size >= ?")
        args.append(min_size)
    where = ("WHERE " + " AND ".join(conds)) if conds else ""
    sql = (
        f"SELECT key,size,last_modified,seen_utc,enriched,extract_json FROM objects {where}"
        " ORDER BY last_modified DESC LIMIT ?"
    )
    args.append(limit)
    return [_row_pub(r) for r in connect().execute(sql, args).fetchall()]


def get_object_row(key: str) -> Optional[dict]:
    r = (
        connect()
        .execute(
            "SELECT key,size,last_modified,etag,seen_utc,enriched,extract_json FROM objects WHERE key=?",
            (key,),
        )
        .fetchone()
    )
    return _row_pub(r) if r else None


def get_object_sizes(keys: list[str]) -> dict[str, dict]:
    """Batch size/last_modified lookup for SSE event enrichment.

    The `enriched` event used to carry key+payload only, so the Live Feed
    modal received sizeless rows and rendered real images as 0-byte folder
    markers (verified 2026-09-11: 336KB slip shown as empty). One indexed
    query serves the whole batch.
    """
    out: dict[str, dict] = {}
    keys = [k for k in dict.fromkeys(keys) if k]
    if not keys:
        return out
    for i in range(0, len(keys), 500):
        chunk = keys[i : i + 500]
        q = "SELECT key,size,last_modified FROM objects WHERE key IN (%s)" % ",".join(
            "?" * len(chunk)
        )
        for r in connect().execute(q, chunk).fetchall():
            out[r["key"]] = {"size": r["size"], "last_modified": r["last_modified"]}
    return out


# ---------------------------------------------------------------------------
# Tronscan chain verification helpers (2026-11-20)
# ---------------------------------------------------------------------------

_canon_lock = threading.Lock()
_canon_cache: dict[tuple[str, str], dict] = {}


def _canonical_wallet(raw: Any, network: str = "TRON") -> dict:
    """Cached no-network TRON address canonicalisation (lazy import: chain→db)."""
    raw_s = str(raw or "").strip()
    net = str(network or "TRON").strip()
    key = (raw_s, net.upper())
    with _canon_lock:
        hit = _canon_cache.get(key)
    if hit is not None:
        return dict(hit)
    try:
        from . import chain

        res = chain.canonicalize_address_light(raw_s, net)
    except Exception:
        res = {
            "raw": raw_s,
            "address": raw_s,
            "status": "unresolved",
            "method": "",
            "confidence": "",
        }
    with _canon_lock:
        if len(_canon_cache) > 20000:
            _canon_cache.clear()
        _canon_cache[key] = dict(res)
    return res


def _wallet_resolution_lookup(
    c: sqlite3.Connection, raws: Collection[str]
) -> dict[str, dict]:
    """Look up previously verified raw→canonical mappings (one query/chunk)."""
    out: dict[str, dict] = {}
    clean = [str(r or "").strip() for r in raws]
    clean = [r for r in dict.fromkeys(clean) if r]
    for i in range(0, len(clean), 400):
        chunk = clean[i : i + 400]
        ph = ",".join("?" * len(chunk))
        try:
            rows = c.execute(
                "SELECT raw_address,resolved_address,network,method,confidence,status"
                f" FROM wallet_resolutions WHERE raw_address IN ({ph})",
                chunk,
            ).fetchall()
        except sqlite3.OperationalError:
            return out
        for r in rows:
            if r["resolved_address"]:
                out[r["raw_address"]] = {
                    "raw": r["raw_address"],
                    "address": r["resolved_address"],
                    "status": r["status"] or "repaired",
                    "method": r["method"] or "resolution",
                    "confidence": r["confidence"] or "",
                    "network": r["network"] or "TRON",
                }
    return out


def _full_txid(value: Any) -> str:
    """First complete 64-hex transaction hash in a list/JSON/comma string."""
    if not value:
        return ""
    items: list = []
    if isinstance(value, (list, tuple, set)):
        items = list(value)
    else:
        s = str(value).strip()
        try:
            parsed = json.loads(s)
            items = parsed if isinstance(parsed, list) else [parsed]
        except Exception:
            items = re.split(r"[,|\s]+", s)
    for item in items:
        h = str(item or "").strip().lower()
        if re.fullmatch(r"[0-9a-f]{64}", h):
            return h
    return ""


def _sync_wallet_hits(c: sqlite3.Connection, key: str, payload: dict | None) -> int:
    """Write-through: mirror payload wallets[] into wallet_hits (no clobber issue).

    Caller must hold _writer_lock. Deletes this key's rows then re-inserts
    from the current payload, so repairs/drops/recoveries stay in sync.
    Wallet addresses are canonicalised through wallet_resolutions / Tronscan
    repair; the raw OCR string is preserved in raw_address and previously
    verified chain data survives a re-sync. Returns rows inserted.
    """
    # Preserve chain verification across the delete/re-insert cycle.
    prior: dict[str, dict] = {}
    for r in c.execute(
        "SELECT address,raw_address,txid,tx_status,chain_checked_utc"
        " FROM wallet_hits WHERE key=?",
        (key,),
    ).fetchall():
        d = dict(r)
        prior.setdefault(str(r["address"]), d)
        if r["raw_address"]:
            prior.setdefault(str(r["raw_address"]), d)
    c.execute("DELETE FROM wallet_hits WHERE key=?", (key,))
    if not isinstance(payload, dict):
        return 0
    wallets = payload.get("wallets") or []
    if not wallets:
        return 0
    meta = c.execute(
        "SELECT last_modified,seen_utc FROM objects WHERE key=?", (key,)
    ).fetchone()
    lm = (meta["last_modified"] if meta else "") or ""
    seen = (meta["seen_utc"] if meta else "") or ""
    kind = str(payload.get("kind") or "")
    payload_txid = _full_txid(payload.get("txids"))
    resolved = _wallet_resolution_lookup(
        c, [str(w.get("address") or "") for w in wallets if isinstance(w, dict)]
    )
    seen_addrs: set[str] = set()
    inserted = 0
    for w in wallets:
        if not isinstance(w, dict) or not w.get("address"):
            continue
        raw_addr = str(w["address"]).strip()
        net = str(w.get("network") or w.get("standard") or "TRON")
        res = resolved.get(raw_addr) or _canonical_wallet(raw_addr, net)
        addr = str(res.get("address") or raw_addr).strip()
        if not addr or addr in seen_addrs:
            continue
        seen_addrs.add(addr)
        txids = w.get("txids", payload.get("txids"))
        if isinstance(txids, list):
            txids = json.dumps(txids)
        old = prior.get(addr) or prior.get(raw_addr) or {}
        txid = _full_txid(txids) or str(old.get("txid") or "") or payload_txid
        try:
            c.execute(
                "INSERT OR REPLACE INTO wallet_hits(address,key,last_modified,"
                "seen_utc,network,standard,confidence,checksum,kind,amount_inr,"
                "amount_value,amount_currency,txids,raw_address,wallet_status,"
                "txid,tx_status,chain_checked_utc)"
                " VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    addr,
                    key,
                    lm,
                    seen,
                    net,
                    str(w.get("standard") or ""),
                    str(w.get("confidence") or ""),
                    str(w.get("checksum") or ""),
                    kind,
                    payload.get("amount_inr"),
                    payload.get("amount_value"),
                    str(payload.get("amount_currency") or ""),
                    str(txids or ""),
                    raw_addr,
                    str(res.get("status") or ""),
                    txid,
                    str(old.get("tx_status") or ""),
                    str(old.get("chain_checked_utc") or ""),
                ),
            )
            inserted += 1
        except Exception:
            continue
    return inserted


def _sync_upi_hits(
    c: sqlite3.Connection,
    key: str,
    payload: dict | None,
    last_modified: str | None = None,
    seen_utc: str | None = None,
) -> int:
    """Write-through UPI index; caller holds _writer_lock."""
    c.execute("DELETE FROM upi_hits WHERE key=?", (key,))
    if not isinstance(payload, dict):
        return 0
    kind = str(payload.get("kind") or "")
    vpa = str(payload.get("vpa") or "").strip().lower()
    if kind not in ("qr", "deposit") or not vpa:
        return 0
    has_sender = has_bene = 0
    for d in payload.get("vpa_details") or []:
        if not isinstance(d, dict):
            continue
        if str(d.get("vpa") or "").strip().lower() != vpa:
            continue
        role = str(d.get("role") or "")
        if role == "sender":
            has_sender = 1
        elif role == "beneficiary":
            has_bene = 1
    if last_modified is None or seen_utc is None:
        meta = c.execute(
            "SELECT last_modified,seen_utc FROM objects WHERE key=?", (key,)
        ).fetchone()
        last_modified = (meta["last_modified"] if meta else "") or ""
        seen_utc = (meta["seen_utc"] if meta else "") or ""
    amount = payload.get("amount_inr")
    if not isinstance(amount, (int, float)):
        amount = None
    c.execute(
        "INSERT OR REPLACE INTO upi_hits(key,vpa,last_modified,seen_utc,kind,"
        "sender_only,has_bene,has_sender,amount_inr,payee_name)"
        " VALUES(?,?,?,?,?,?,?,?,?,?)",
        (
            key,
            vpa,
            last_modified or "",
            seen_utc or "",
            kind,
            1 if (has_sender and not has_bene) else 0,
            has_bene,
            has_sender,
            amount,
            str(payload.get("payee_name") or ""),
        ),
    )
    return 1


def backfill_upi_hits(batch: int = 20000) -> int:
    """One-time newest-first seed of upi_hits; write-through keeps it live."""
    set_meta("upi_index_v1", "building")
    total = 0
    last_lm: str | None = None
    last_key: str | None = None
    select = (
        "SELECT key,last_modified,seen_utc,extract_json FROM objects"
        " WHERE extract_json IS NOT NULL"
        " AND json_extract(extract_json,'$.kind') IN ('qr','deposit')"
        " AND json_extract(extract_json,'$.vpa') IS NOT NULL"
    )
    while True:
        c = connect()
        if last_lm is None:
            rows = c.execute(
                select + " ORDER BY last_modified DESC,key DESC LIMIT ?", (batch,)
            ).fetchall()
        else:
            rows = c.execute(
                select
                + " AND (last_modified < ? OR (last_modified = ? AND key < ?))"
                " ORDER BY last_modified DESC,key DESC LIMIT ?",
                (last_lm, last_lm, last_key, batch),
            ).fetchall()
        if not rows:
            break
        last_lm = rows[-1]["last_modified"]
        last_key = rows[-1]["key"]
        items: list[tuple[sqlite3.Row, dict]] = []
        for r in rows:
            try:
                p = json.loads(r["extract_json"])
            except Exception:
                continue
            if p.get("kind") not in ("qr", "deposit") or not p.get("vpa"):
                continue
            items.append((r, p))
        if items:
            with _writer_lock:
                wc = connect()
                for r, p in items:
                    try:
                        total += (
                            _sync_upi_hits(
                                wc,
                                r["key"],
                                p,
                                r["last_modified"],
                                r["seen_utc"],
                            )
                            or 0
                        )
                    except Exception:
                        continue
                wc.commit()
        if len(rows) < batch:
            break
        time.sleep(0.05)
    set_meta("upi_index_v1", "ready")
    return total


def upi_index_state() -> str:
    try:
        return str(get_meta("upi_index_v1") or "")
    except Exception:
        return ""


def backfill_wallet_hits(batch: int = 20000) -> int:
    """One-time seed of wallet_hits from existing payloads (batched by rowid).

    Only rows whose payload actually contains wallets[] are parsed; the scan
    touches each payload once. Idempotent (per-key delete+reinsert).
    """
    total = 0
    last_rowid = 0
    while True:
        with _writer_lock:
            c = connect()
            rows = c.execute(
                "SELECT rowid,key,extract_json FROM objects"
                " WHERE rowid > ? AND extract_json IS NOT NULL"
                " AND json_array_length(extract_json,'$.wallets') > 0"
                " ORDER BY rowid LIMIT ?",
                (last_rowid, batch),
            ).fetchall()
            if not rows:
                break
            for r in rows:
                try:
                    p = json.loads(r["extract_json"]) if r["extract_json"] else {}
                except Exception:
                    continue
                try:
                    total += _sync_wallet_hits(c, r["key"], p) or 0
                except Exception:
                    continue
                last_rowid = r["rowid"]
            c.commit()
            if len(rows) < batch:
                break
    return total


def wallet_index_ready() -> bool:
    try:
        return bool(get_meta("wallet_index_v1"))
    except Exception:
        return False


def mark_wallet_index_ready():
    set_meta("wallet_index_v1", time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()))


def mark_enriched(key: str, payload: dict | None):
    """Persist an enrichment payload; amounts are sanity-checked at ingestion.

    A 12-digit UTR/account number misread as the money value historically leaked
    into extract_json and skewed the Overview totals (which sum that column
    directly). We keep the row and flag the raw value, but never store an
    implausible amount as a real one.
    """
    if payload and payload.get("amount_inr") is not None:
        raw = payload.get("amount_inr")
        clean = _sanitize_amount(raw)
        if clean is None:
            payload["amount_rejected_inr"] = raw
            payload["amount_inr"] = None
        else:
            payload["amount_inr"] = clean
            payload.pop("amount_rejected_inr", None)
    with _writer_lock:
        c = connect()
        c.execute(
            "UPDATE objects SET enriched=1,extract_json=?,enrich_error=NULL,enrich_retry_utc=NULL,enriched_utc=? WHERE key=?",
            (
                json.dumps(payload, ensure_ascii=False) if payload else None,
                time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                key,
            ),
        )
        if payload:
            _replace_payment_rows(c, key, payload)
            _sync_wallet_hits(c, key, payload)
            _sync_upi_hits(c, key, payload)
        c.commit()


def persist_enriched_batch(items: list[tuple[str, dict | None]]) -> int:
    """Persist a whole drain's payloads in ONE transaction (2026-09-05).

    Live measurement on the 8-vCPU burn: per-key mark_enriched commits made
    the persist phase take 167-280s per ~100-row drain (lock convoy +
    per-commit fsync on balanced PD). One commit per drain cuts that to ~s.
    Same writes as mark_enriched, minus the per-row commit. Returns rows done.
    """
    if not items:
        return 0
    now = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    done = 0
    with _writer_lock:
        c = connect()
        for key, payload in items:
            if payload and payload.get("amount_inr") is not None:
                raw = payload.get("amount_inr")
                clean = _sanitize_amount(raw)
                if clean is None:
                    payload["amount_rejected_inr"] = raw
                    payload["amount_inr"] = None
                else:
                    payload["amount_inr"] = clean
                    payload.pop("amount_rejected_inr", None)
            c.execute(
                "UPDATE objects SET enriched=1,extract_json=?,enrich_error=NULL,"
                "enrich_retry_utc=NULL,enriched_utc=? WHERE key=?",
                (
                    json.dumps(payload, ensure_ascii=False) if payload else None,
                    now,
                    key,
                ),
            )
            if payload:
                _replace_payment_rows(c, key, payload)
                _sync_wallet_hits(c, key, payload)
                _sync_upi_hits(c, key, payload)
            done += 1
        c.commit()
    return done


# Fetch-miss errors (object not yet uploaded to the GCS mirror — the s3sync
# bulk backlog runs hours behind S3) must NEVER go terminal on the same
# 5-attempt budget as decode errors: attempt 5 lands ~15min after first
# sighting, long before the mirror catches up, and enriched=1 then drops
# the evidence forever (live-pipeline review 2026-09-16). Fetch misses get
# ~72 hourly-capped attempts (~3 days of mirror lag) before going terminal.
_FETCH_MISS_PREFIXES = ("fetch:", "fetch-pool:")
_FETCH_MISS_TERMINAL_ATTEMPTS = 72
_DECODE_TERMINAL_ATTEMPTS = 5


def mark_enrichment_error(key: str, error: str):
    """Retry transient fetch failures with bounded exponential backoff.

    Fetch-miss errors (mirror lag) retry ~72x (~3 days, hourly-capped);
    decode/drain errors go terminal after 5 attempts. Terminal rows keep
    enrich_error recorded for forensics.
    """
    with _writer_lock:
        c = connect()
        row = c.execute(
            "SELECT enrich_attempts FROM objects WHERE key=?", (key,)
        ).fetchone()
        attempts = int(row["enrich_attempts"] or 0) + 1 if row else 1
        fetch_miss = (error or "").startswith(_FETCH_MISS_PREFIXES)
        limit = (
            _FETCH_MISS_TERMINAL_ATTEMPTS if fetch_miss else _DECODE_TERMINAL_ATTEMPTS
        )
        terminal = attempts >= limit
        delay = min(3600, 30 * (2 ** min(attempts, 7)))
        retry = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() + delay))
        c.execute(
            "UPDATE objects SET enriched=?,enrich_attempts=?,enrich_error=?,enrich_retry_utc=?,enriched_utc=? WHERE key=?",
            (
                1 if terminal else 0,
                attempts,
                error[:500],
                retry,
                time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
                if terminal
                else None,
                key,
            ),
        )
        c.commit()


def requeue_fetch_misses(limit: int = 200000, force: bool = False) -> dict:
    """Revive rows poisoned by the pre-fix 5-attempt fetch budget.

    Rows with enriched=1, no payload, and a fetch-miss error went terminal
    while the GCS mirror was still catching up — the bytes are (now)
    fetchable but no queue will ever select them. Reset them to pending
    with a fresh attempt budget so the live/slip loops pick them up.

    2026-11-08: `enrich_error LIKE` is not indexable, so the startup UPDATE
    full-scanned 2.47M rows while holding the global writer lock (another
    4+ minute enrichment stall). Select candidates on a read connection,
    take the lock only for the rowid updates, and record completion so later
    startups skip it. The manual endpoint passes force=True.
    """
    if not force and get_meta("fetch_requeue_v1") == "done":
        return {"requeued": 0, "limit": limit, "skipped": True}
    rowids = [
        r["rowid"]
        for r in connect().execute(
            "SELECT rowid FROM objects WHERE enriched=1 "
            "AND extract_json IS NULL AND enrich_error LIKE 'fetch%' "
            "LIMIT ?",
            (limit,),
        ).fetchall()
    ]
    requeued = 0
    if rowids:
        with _writer_lock:
            c = connect()
            for start in range(0, len(rowids), 1000):
                chunk = rowids[start : start + 1000]
                marks = ",".join("?" * len(chunk))
                cur = c.execute(
                    "UPDATE objects SET enriched=0, enrich_attempts=0, "
                    f"enrich_retry_utc=NULL WHERE rowid IN ({marks})",
                    chunk,
                )
                requeued += cur.rowcount
            c.commit()
    set_meta("fetch_requeue_v1", "done")
    return {"requeued": requeued, "limit": limit}


def pending_enrichment(limit: int = 20, full_scan: bool = False) -> list[str]:
    where, args = _pending_where(full_scan=full_scan)
    c = connect()
    # Phase 1 — fresh head: evidence seen in the last hour, newest first.
    # Tight per-prefix range scan on idx_obj_enrich_pick (~ms). Fresh slips
    # must never wait behind the million-row historical backlog.
    fresh_cut = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() - 3600))
    head = [
        r["key"]
        for r in c.execute(
            f"SELECT key FROM objects WHERE {where} AND seen_utc >= ? "
            "ORDER BY seen_utc DESC LIMIT ?",
            args + [fresh_cut, limit],
        ).fetchall()
    ]
    if len(head) >= limit:
        return head
    # Phase 2 — bulk fill in index order (NO sort: backlog drain order is
    # irrelevant, and sorting ~1M rows cost ~600ms every 2s drain cycle).
    fill = [
        r["key"]
        for r in c.execute(
            f"SELECT key FROM objects WHERE {where} LIMIT ?",
            args + [limit],
        ).fetchall()
    ]
    seen = set(head)
    return head + [k for k in fill if k not in seen][: limit - len(head)]


def pending_slips(limit: int = 24) -> list[str]:
    """Gambler-paid slip queue for the background slip_loop drain.

    deposit/withdrawal/payment images, newest first — these carry the
    receiving-rail VPAs/UTRs the QR-tier queue never sees. Same two-phase
    pattern as pending_wallets: fresh-hour head, then unsorted bulk fill.
    Prefix set is config-driven (_slip_prefixes) so bucket-specific slip
    prefixes (e.g. payment-proofs/) drain instead of idling forever.
    """
    slip_clause, slip_args = _slip_prefix_clause()
    where = (
        "(enriched=0 AND extract_json IS NULL) "
        "AND (enrich_retry_utc IS NULL "
        "OR enrich_retry_utc <= strftime('%Y-%m-%dT%H:%M:%SZ','now')) "
        f"AND {_IMG_CLAUSE} "
        f"AND {slip_clause}"
    )
    c = connect()
    fresh_cut = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() - 3600))
    head = [
        r["key"]
        for r in c.execute(
            f"SELECT key FROM objects WHERE {where} AND seen_utc >= ? "
            "ORDER BY last_modified DESC, seen_utc DESC LIMIT ?",
            (slip_args + [fresh_cut, limit]),
        ).fetchall()
    ]
    if len(head) >= limit:
        return head
    # Bulk fill MUST stay newest-first too. Unsorted fill returned the oldest
    # pending rows (index order), so after the WAL lock storm left a 28K slip
    # backlog the live 24h briefing cards kept showing 10-33h-old evidence
    # while yesterday's 21K deposits waited behind history. The 28K backlog
    # sorts in milliseconds via the enrich partial index.
    fill = [
        r["key"]
        for r in c.execute(
            f"SELECT key FROM objects WHERE {where}"
            " ORDER BY last_modified DESC LIMIT ?",
            (slip_args + [limit]),
        ).fetchall()
    ]
    seen = set(head)
    return head + [k for k in fill if k not in seen][: limit - len(head)]


def _slip_prefixes() -> list:
    """Prefixes owned by slip_loop. Config-driven (2026-09-21 b3 review:
    hardcoded deposit/withdrawal/payment orphaned payment-proofs/ and
    payment-proof/ — 8.6k images invisible to every queue). payment/ stays
    for pre-env back-compat (live queue also covers it; the overlap term
    in enrichment_progress keeps counts honest)."""
    return list(dict.fromkeys(list(config.OCR_PREFIXES) + ["payment/"]))


def _slip_prefix_clause() -> tuple[str, list]:
    prefs = _slip_prefixes()
    return "top_prefix IN (" + ",".join(["?"] * len(prefs)) + ")", prefs


def pending_wallet_candidates(limit: int = 200) -> list[str]:
    """Wallet-prioritised pending keys for POST /api/wallets/full-scan.

    Always full_scan=True (every chat_file image), most wallet-likely first.
    """
    return pending_enrichment(limit, full_scan=True)


# Mirror of enrich.crypto_filename_hint as SQL (chat wallet/USDT screenshots).
_WALLET_HINT_LIKES = " OR ".join(
    f"lower(key) LIKE '%{t}%'"
    for t in (
        "trustapp",
        "trust_wallet",
        "usdt",
        "tron",
        "wallet",
        "tether",
        "binance",
        "crypto",
    )
)


def pending_wallets(limit: int = 96) -> list[str]:
    """Wallet-priority queue for the background wallet_loop drain.

    Picks pending (a) ALL bankingqr/qrcode/paymentgateway/payment images,
    (b) hint-named chat_file images, and (c) any chat_file image seen in the
    last 24h even without a crypto filename — so generic chat screenshots
    still get wallet OCR. Never the full 287K chat backlog. Deposit and
    withdrawal slips are owned by slip_loop (ocr_slip + QR also extracts
    wallets).
    """
    where = (
        "(enriched=0 AND extract_json IS NULL) "
        "AND (enrich_retry_utc IS NULL "
        "OR enrich_retry_utc <= strftime('%Y-%m-%dT%H:%M:%SZ','now')) "
        f"AND {_IMG_CLAUSE} "
        "AND (top_prefix IN ('bankingqr/','qrcode/','paymentgateway/','payment/') "
        f"OR (top_prefix='chat_file/' AND ({_WALLET_HINT_LIKES}))) "
    )
    c = connect()
    fresh_cut = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() - 3600))
    day_cut = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() - 86400))
    head = [
        r["key"]
        for r in c.execute(
            f"SELECT key FROM objects WHERE {where} AND seen_utc >= ? "
            "ORDER BY seen_utc DESC LIMIT ?",
            (fresh_cut, limit),
        ).fetchall()
    ]
    seen = set(head)
    if len(head) < limit:
        chat_where = (
            "(enriched=0 AND extract_json IS NULL) "
            "AND (enrich_retry_utc IS NULL "
            "OR enrich_retry_utc <= strftime('%Y-%m-%dT%H:%M:%SZ','now')) "
            f"AND {_IMG_CLAUSE} AND top_prefix='chat_file/' AND seen_utc >= ?"
        )
        for r in c.execute(
            f"SELECT key FROM objects WHERE {chat_where} "
            "ORDER BY seen_utc DESC LIMIT ?",
            (day_cut, limit),
        ).fetchall():
            if r["key"] not in seen:
                head.append(r["key"])
                seen.add(r["key"])
            if len(head) >= limit:
                break
    if len(head) >= limit:
        return head
    fill = [
        r["key"]
        for r in c.execute(
            f"SELECT key FROM objects WHERE {where} LIMIT ?",
            (limit,),
        ).fetchall()
    ]
    return head + [k for k in fill if k not in seen][: limit - len(head)]


_IMG_CLAUSE = "(ext IN ('.jpg','.jpeg','.png','.webp','.gif','.bmp','.tiff','.tif','.heic','.heif','.avif','.jfif') OR (ext='' AND (lower(key) LIKE '%.jpg' OR lower(key) LIKE '%.jpeg' OR lower(key) LIKE '%.png' OR lower(key) LIKE '%.webp' OR lower(key) LIKE '%.gif' OR lower(key) LIKE '%.bmp' OR lower(key) LIKE '%.tiff' OR lower(key) LIKE '%.tif' OR lower(key) LIKE '%.heic' OR lower(key) LIKE '%.heif' OR lower(key) LIKE '%.avif' OR lower(key) LIKE '%.jfif')))"


def _pending_where(full_scan: bool = False) -> tuple[str, list]:
    """WHERE clause selecting enrichable objects.

    Default live queue (full_scan=False):
    - qr/deposit/withdrawal prefixes: all images
    - crypto chat screenshots remain available for on-demand enrichment; the
      automatic queue stays focused on payment evidence so live slips do not
      wait behind hundreds of thousands of chat images
    - last_modified within ENRICH_MAX_AGE_HOURS (stops the historical OCR flood;
      old objects stay browsable and can be enriched on demand via POST /api/enrich)

    full_scan=True (POST /api/wallets/full-scan):
    - also includes *every* chat_file image — efficient wallet hunt across the
      287k chat images, not just hint-named ones. Still prioritised (QR/deposit
      first, chat last) in the ORDER BY so hot evidence wins.
    - last_modified within ENRICH_MAX_AGE_HOURS (stops the historical OCR flood;
      old objects stay browsable and can be enriched on demand via POST /api/enrich)
    """
    prefixes = list(config.QR_PREFIXES + config.OCR_PREFIXES)
    if getattr(config, "SLIP_OCR", False):
        # Burn mode: the dedicated slip_loop owns deposit/withdrawal OCR.
        # Keeping them in the live pending queue too made all three loops
        # fetch the same keys (live: ~35/384 fresh per batch, pool idle at
        # load 2 on 8 vCPU). Pending keeps QR prefixes + fresh chat; slips
        # drain via slip_loop newest-first.
        prefixes = [p for p in prefixes if p not in config.OCR_PREFIXES]
    if full_scan or config.WALLET_FULL_SCAN:
        prefixes = list(dict.fromkeys(prefixes + ["chat_file/"]))
    prefix_clause = "top_prefix IN (" + ",".join(["?"] * len(prefixes)) + ")"
    conds = [
        "(enriched=0 AND extract_json IS NULL)",
        "(enrich_retry_utc IS NULL OR enrich_retry_utc <= strftime('%Y-%m-%dT%H:%M:%SZ','now'))",
        prefix_clause,
        _IMG_CLAUSE,
    ]
    args: list = list(prefixes)
    if config.ENRICH_MAX_AGE_HOURS > 0:
        cutoff = time.strftime(
            "%Y-%m-%dT%H:%M:%SZ",
            time.gmtime(time.time() - config.ENRICH_MAX_AGE_HOURS * 3600),
        )
        conds.append("last_modified >= ?")
        args.append(cutoff)
    return "(" + " AND ".join(conds) + ")", args


def _pending_sql(limit: int, full_scan: bool = False) -> tuple[str, list]:
    where, args = _pending_where(full_scan=full_scan)
    # Wallet-rich prefixes first — bankingqr/qrcode most likely to contain
    # addresses, then deposit/withdrawal slips, then chat_file bulk.
    # Within each tier newest seen_utc first.
    order = "CASE WHEN top_prefix IN ('bankingqr/','qrcode/','paymentgateway/') THEN 0 WHEN top_prefix IN ('deposit/','withdrawal/','payment/') THEN 1 ELSE 2 END, seen_utc DESC"
    sql = f"SELECT key FROM objects WHERE {where} ORDER BY {order} LIMIT ?"
    return sql, args + [limit]


_pending_cache: tuple[float, int] = (0.0, 0)
_PENDING_TTL = 60.0  # heavy LIKE scan over 1M+ rows — serve stale up to 60s


def pending_enrichment_count(full_scan: bool = False) -> int:
    # Separate caches for live vs full-scan counts (different WHERE)
    cache_key = "_pending_cache_full" if full_scan else "_pending_cache"
    cached = globals().get(cache_key)
    # legacy single-cache compat
    if not full_scan:
        global _pending_cache
        now = time.time()
        if now - _pending_cache[0] < _PENDING_TTL:
            return _pending_cache[1]
        where, args = _pending_where(full_scan=False)
        row = (
            connect()
            .execute(f"SELECT COUNT(*) c FROM objects WHERE {where}", args)
            .fetchone()
        )
        _pending_cache = (now, row["c"])
        return row["c"]
    else:
        # full-scan count is expensive (chat 287k) — compute without short TTL
        where, args = _pending_where(full_scan=True)
        row = (
            connect()
            .execute(f"SELECT COUNT(*) c FROM objects WHERE {where}", args)
            .fetchone()
        )
        return row["c"]


def recent_new(hours: float = 24, limit: int = 100) -> list[dict]:
    cutoff = time.strftime(
        "%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() - hours * 3600)
    )
    rows = (
        connect()
        .execute(
            "SELECT key,size,last_modified,seen_utc,enriched,extract_json FROM objects"
            " WHERE seen_utc >= ? ORDER BY seen_utc DESC LIMIT ?",
            (cutoff, limit),
        )
        .fetchall()
    )
    return [_row_pub(r) for r in rows]


def day_histogram(days: int = 14) -> list[dict]:
    out = []
    for i in range(days - 1, -1, -1):
        day = time.strftime("%Y-%m-%d", time.gmtime(time.time() - i * 86400))
        nxt = time.strftime("%Y-%m-%d", time.gmtime(time.time() - (i - 1) * 86400))
        row = (
            connect()
            .execute(
                "SELECT COUNT(*) n, SUM(size) s FROM objects WHERE last_modified >= ? AND last_modified < ?",
                (day, nxt),
            )
            .fetchone()
        )
        out.append({"day": day, "objects": row["n"], "bytes": row["s"] or 0})
    return out


def backfill_ext(batch: int = 20000, max_batches: int = 0, force: bool = False) -> int:
    """Fill ext for rows inserted before the column existed.

    Runs in a background thread at startup; each batch is one indexed UPDATE.
    Returns number of rows stamped. max_batches=0 -> until done.

    2026-11-08: `ext=''` cannot use the partial idx_obj_ext (WHERE ext != ''),
    so the final empty SELECT full-scanned 2.47M rows/10GB WHILE HOLDING the
    global writer lock — every restart blocked persist, sync notices, poll
    upserts and the slip checkpoint for 10+ minutes. Read candidates off-lock,
    write only real rows under the lock, and record one completed pass so later
    startups skip the scan entirely (force=True re-runs after a restore).
    """
    if not force and get_meta("ext_backfill_v1") == "done":
        return 0
    total = 0
    batches = 0
    while True:
        c = connect()
        rows = c.execute(
            "SELECT key FROM objects WHERE ext = '' AND key LIKE '%.%' AND key NOT LIKE '%/' LIMIT ?",
            (batch,),
        ).fetchall()
        if not rows:
            break
        # "none" (not "") for odd keys such as a trailing dot: stamping an
        # empty ext would re-select the same row forever.
        data = [(file_ext(r["key"]) or "none", r["key"]) for r in rows]
        if data:
            with _writer_lock:
                w = connect()
                w.executemany("UPDATE objects SET ext=? WHERE key=?", data)
                w.commit()
        total += len(data)
        batches += 1
        if max_batches and batches >= max_batches:
            break
    set_meta("ext_backfill_v1", "done")
    return total


def type_census() -> dict:
    """SQL-aggregated type census — avoids loading all rows into Python.
    Handles ~1M+ objects efficiently via SQLite CASE/GROUP BY."""
    conn = connect()
    cats_def = {
        "deposit_withdrawal_slips": ("Images — payment slips",),
        "qr_codes": ("QR code images",),
        "chat_screenshots": ("Chat screenshots",),
        "voice_notes": ("Voice notes (wav)",),
        "video": ("Video",),
        "spreadsheets": ("Spreadsheets (agent ledgers)",),
        "documents": ("Documents (PDF etc.)",),
        "apps": ("APK installers",),
        "logs": ("Server logs",),
        "other": ("Other",),
    }
    # Single query with CASE expressions — SQLite handles this in C
    sql = """
    SELECT
      CASE
        WHEN top_prefix IN ('deposit/', 'withdrawal/') THEN 'deposit_withdrawal_slips'
        WHEN top_prefix IN ('bankingqr/', 'qrcode/') THEN 'qr_codes'
        WHEN top_prefix = 'chat_file/' AND lower(key) NOT LIKE '%.wav' AND lower(key) NOT LIKE '%.mp3' AND lower(key) NOT LIKE '%.amr' THEN 'chat_screenshots'
        WHEN (top_prefix = 'chat_file/' AND (lower(key) LIKE '%.wav' OR lower(key) LIKE '%.mp3' OR lower(key) LIKE '%.amr')) OR lower(key) LIKE '%.wav' THEN 'voice_notes'
        WHEN lower(key) LIKE '%.mp4' OR lower(key) LIKE '%.mov' OR lower(key) LIKE '%.webm' OR lower(key) LIKE '%.avi' THEN 'video'
        WHEN lower(key) LIKE '%.xlsx' OR lower(key) LIKE '%.xls' OR lower(key) LIKE '%.csv' THEN 'spreadsheets'
        WHEN lower(key) LIKE '%.pdf' OR lower(key) LIKE '%.doc' OR lower(key) LIKE '%.docx' THEN 'documents'
        WHEN lower(key) LIKE '%.apk' THEN 'apps'
        WHEN lower(key) LIKE '%.log' OR lower(key) LIKE '%.gz' THEN 'logs'
        WHEN key LIKE '%/' THEN 'other'
        ELSE 'other'
      END AS cat,
      COUNT(*) AS n,
      COALESCE(SUM(size), 0) AS s
    FROM objects INDEXED BY idx_obj_inventory
    GROUP BY cat
    """
    rows = conn.execute(sql).fetchall()
    counts = {k: 0 for k in cats_def}
    bytes_by = {k: 0 for k in cats_def}
    for r in rows:
        cat = r["cat"]
        if cat in counts:
            counts[cat] = r["n"]
            bytes_by[cat] = r["s"]

    # Extension counts — indexed ext column (instant GROUP BY). Falls back to a
    # capped Python scan only if backfill hasn't stamped anything yet.
    ext_rows = conn.execute(
        "SELECT ext e, COUNT(*) n FROM objects WHERE ext != '' GROUP BY ext ORDER BY n DESC LIMIT 25"
    ).fetchall()
    if ext_rows:
        sorted_exts = [(r["e"], r["n"]) for r in ext_rows]
    else:
        key_rows = conn.execute(
            "SELECT key FROM objects WHERE key LIKE '%.%' AND key NOT LIKE '%/' LIMIT 300000"
        ).fetchall()
        ext_counts: dict[str, int] = {}
        for r in key_rows:
            e = file_ext(r["key"])
            if e:
                ext_counts[e] = ext_counts.get(e, 0) + 1
        sorted_exts = sorted(ext_counts.items(), key=lambda x: -x[1])[:25]

    return {
        "categories": [
            {"id": cid, "label": label, "count": counts[cid], "bytes": bytes_by[cid]}
            for cid, (label,) in cats_def.items()
        ],
        "extensions": sorted_exts,
    }


def flow_kind_totals() -> dict:
    """Exact slip totals from the compact kind/amount index, not OCR payloads."""
    out = {}
    for kind in ("deposit", "withdrawal"):
        r = connect().execute(
            "SELECT COUNT(*) c,"
            f" SUM(CASE WHEN ({_FLOW_AMOUNT_SQL}) BETWEEN 0.01 AND ?"
            f" THEN ({_FLOW_AMOUNT_SQL}) END) amt"
            " FROM objects INDEXED BY idx_obj_flow_totals WHERE extract_json IS NOT NULL"
            f" AND ({_FLOW_KIND_SQL})=?",
            (MAX_AMOUNT_INR, kind),
        ).fetchone()
        out[kind] = {"total": r["c"], "sum_amount_inr": r["amt"] or 0}
    return out


def deposits(limit: int = 300, offset: int = 0, kind: str = "deposit") -> dict:
    conn = connect()
    total = conn.execute(
        "SELECT COUNT(*) c FROM objects WHERE extract_json IS NOT NULL"
        " AND json_extract(extract_json,'$.kind')=?",
        (kind,),
    ).fetchone()["c"]
    agg = conn.execute(
        "SELECT SUM(CASE WHEN json_extract(extract_json,'$.amount_inr') IS NOT NULL"
        " AND json_extract(extract_json,'$.amount_inr') BETWEEN 0.01 AND ? THEN"
        " json_extract(extract_json,'$.amount_inr') END) amt,"
        " COUNT(DISTINCT json_extract(extract_json,'$.utr')) utrs,"
        " COUNT(DISTINCT json_extract(extract_json,'$.vpa')) vpas"
        " FROM objects WHERE extract_json IS NOT NULL AND json_extract(extract_json,'$.kind')=?",
        (MAX_AMOUNT_INR, kind),
    ).fetchone()
    rows = conn.execute(
        "SELECT key,size,last_modified,seen_utc,extract_json FROM objects"
        " WHERE extract_json IS NOT NULL AND json_extract(extract_json,'$.kind')=?"
        " ORDER BY last_modified DESC LIMIT ? OFFSET ?",
        (kind, limit, offset),
    ).fetchall()
    items = []
    for r in rows:
        d = {
            "key": r["key"],
            "last_modified": r["last_modified"],
            "seen_utc": r["seen_utc"],
            **(json.loads(r["extract_json"]) if r["extract_json"] else {}),
        }
        items.append(d)
    return {
        "total": total,
        "sum_amount_inr": agg["amt"] or 0,
        "distinct_utrs": agg["utrs"] or 0,
        "distinct_vpas": agg["vpas"] or 0,
        "items": items,
    }


def _identity_for_utr(c: sqlite3.Connection, utr: str) -> dict:
    if not utr:
        return {}
    row = c.execute(
        "SELECT gambler,amount_inr,day,vpa,source_reference FROM utr_identity WHERE utr=?",
        (utr,),
    ).fetchone()
    return dict(row) if row else {}


def _replace_payment_rows(c: sqlite3.Connection, key: str, payload: dict):
    direction = payload.get("kind") or ""
    if direction not in ("deposit", "withdrawal"):
        return
    obj = c.execute("SELECT last_modified FROM objects WHERE key=?", (key,)).fetchone()
    last_modified = obj["last_modified"] if obj else ""
    utr = str(payload.get("utr") or "").strip()
    identity = _identity_for_utr(c, utr)
    inferred_gambler = (
        payload.get("sender_name")
        if direction == "deposit"
        else payload.get("beneficiary_name")
    )
    gambler = str(
        payload.get("gambler") or identity.get("gambler") or inferred_gambler or ""
    ).strip()
    amount = payload.get("amount_inr")
    if amount is None:
        amount = identity.get("amount_inr")
    # Legacy OCR sometimes promoted a 12-digit UTR/account number to amount.
    # Retain the transaction but exclude impossible values from financial sums.
    amount = _sanitize_amount(amount)
    inferred_holder = (
        payload.get("beneficiary_name") or payload.get("payee_name")
        if direction == "deposit"
        else payload.get("sender_name")
    )
    holder = str(payload.get("account_holder") or inferred_holder or "").strip()

    rails: list[dict] = []
    desired_role = "beneficiary" if direction == "deposit" else "sender"

    def relevant(items: list[dict]) -> list[dict]:
        """Keep explicitly relevant rails; retain unknowns only as low-confidence leads."""
        matching = [item for item in items if item.get("role") == desired_role]
        if matching:
            return matching
        return [item for item in items if item.get("role", "unknown") == "unknown"]

    vpa_details = payload.get("vpa_details") or []
    if not vpa_details:
        vpas = payload.get("vpas") or (
            [payload.get("vpa")] if payload.get("vpa") else []
        )
        vpa_details = [
            {"vpa": v, "role": "unknown", "role_confidence": "low"} for v in vpas if v
        ]
    if not vpa_details and identity.get("vpa"):
        vpa_details = [
            {
                "vpa": identity["vpa"],
                "role": "beneficiary",
                "role_confidence": "medium",
            }
        ]
    seen_vpas: set[str] = set()
    for detail in relevant(vpa_details):
        vpa = str(detail.get("vpa") or "").lower().strip()
        if not vpa or vpa in seen_vpas:
            continue
        seen_vpas.add(vpa)
        rails.append(
            {
                "rail_type": _canonical_rail_type("upi", vpa),
                "rail_id": vpa,
                "account_holder": holder,
                "rail_role": detail.get("role", "unknown"),
                "role_confidence": detail.get("role_confidence", "low"),
            }
        )
    account_items: list[dict] = []
    for account in payload.get("bank_accounts") or []:
        if isinstance(account, str):
            account = {"account_number": account}
        account_items.append(account)
    for account in relevant(account_items):
        rail_id = str(
            account.get("account_number") or account.get("masked_account") or ""
        ).strip()
        if rail_id:
            rails.append(
                {
                    "rail_type": _canonical_rail_type("bank_account", rail_id),
                    "rail_id": rail_id,
                    "account_holder": str(account.get("holder") or holder),
                    "ifsc": str(account.get("ifsc") or payload.get("ifsc") or ""),
                    "rail_role": account.get("role", "unknown"),
                    "role_confidence": account.get("role_confidence", "low"),
                }
            )
    wallet_items = relevant(payload.get("wallets") or [])
    resolved = _wallet_resolution_lookup(
        c,
        [str(w.get("address") or "") for w in wallet_items if isinstance(w, dict)],
    )
    for wallet in wallet_items:
        if not isinstance(wallet, dict):
            continue
        rail_raw = str(wallet.get("address") or "").strip()
        if not rail_raw:
            continue
        net = str(wallet.get("network") or wallet.get("standard") or "TRON")
        res = resolved.get(rail_raw) or _canonical_wallet(rail_raw, net)
        rail_id = str(res.get("address") or rail_raw).strip()
        if rail_id:
            rails.append(
                {
                    "rail_type": _canonical_rail_type("crypto_wallet", rail_id),
                    "rail_id": rail_id,
                    "rail_id_raw": rail_raw,
                    "wallet_status": str(res.get("status") or ""),
                    "network": net,
                    "account_holder": holder,
                    "rail_role": wallet.get("role", "unknown"),
                    "role_confidence": wallet.get("role_confidence", "low"),
                }
            )
    # Canonicalisation can collapse OCR variants (TB4…gyE / TB4…gyF) into one
    # on-chain address; payment_transactions is unique per source+rail, so
    # keep the first (strongest) variant rather than INSERT OR REPLACE churn.
    deduped: list[dict] = []
    seen_rails: set[tuple[str, str]] = set()
    for rail in rails:
        k = (str(rail.get("rail_type") or ""), str(rail.get("rail_id") or ""))
        if k in seen_rails:
            continue
        seen_rails.add(k)
        deduped.append(rail)
    rails = deduped
    if not rails:
        rails = [
            {
                "rail_type": "unknown",
                "rail_id": "",
                "account_holder": holder,
                "rail_role": "unknown",
                "role_confidence": "low",
            }
        ]

    # Preserve Tronscan verification across a rebuild: payment rows are
    # delete/re-inserted, but the chain result is immutable evidence.
    prior_chain: dict[tuple[str, str], dict] = {}
    for pr in c.execute(
        "SELECT rail_type,rail_id,rail_id_raw,wallet_status,txid,txids,"
        "tx_status,tx_method,tx_confidence,tx_time,tx_from,tx_to,tx_amount,"
        "tx_token,tx_contract_ret,tx_candidates,chain_checked_utc"
        " FROM payment_transactions WHERE source_key=?",
        (key,),
    ).fetchall():
        d = dict(pr)
        prior_chain.setdefault((str(d["rail_type"]), str(d["rail_id"])), d)
        if d.get("rail_id_raw"):
            prior_chain.setdefault(
                (str(d["rail_type"]), str(d["rail_id_raw"])), d
            )

    c.execute("DELETE FROM payment_transactions WHERE source_key=?", (key,))
    evidence_json = json.dumps(payload, ensure_ascii=False)
    for rail in rails:
        chain: dict = {}
        if rail["rail_type"] == "crypto_wallet":
            chain = (
                prior_chain.get((rail["rail_type"], rail["rail_id"]))
                or prior_chain.get(
                    (rail["rail_type"], rail.get("rail_id_raw") or "")
                )
                or {}
            )
        c.execute(
            "INSERT OR REPLACE INTO payment_transactions("
            "source_key,direction,gambler,amount_inr,utr,rail_type,rail_id,"
            "rail_id_raw,account_holder,ifsc,network,confidence,rail_role,"
            "role_confidence,last_modified,evidence_json,wallet_status,txid,"
            "txids,tx_status,tx_method,tx_confidence,tx_time,tx_from,tx_to,"
            "tx_amount,tx_token,tx_contract_ret,tx_candidates,chain_checked_utc"
            ") VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                key,
                direction,
                gambler,
                amount,
                utr,
                rail["rail_type"],
                rail["rail_id"],
                rail.get("rail_id_raw", ""),
                rail.get("account_holder", ""),
                rail.get("ifsc", ""),
                rail.get("network", ""),
                str(payload.get("confidence") or ""),
                rail.get("rail_role", "unknown"),
                rail.get("role_confidence", "low"),
                last_modified,
                evidence_json,
                rail.get("wallet_status", "")
                or str(chain.get("wallet_status") or ""),
                str(chain.get("txid") or ""),
                str(chain.get("txids") or ""),
                str(chain.get("tx_status") or ""),
                str(chain.get("tx_method") or ""),
                str(chain.get("tx_confidence") or ""),
                str(chain.get("tx_time") or ""),
                str(chain.get("tx_from") or ""),
                str(chain.get("tx_to") or ""),
                chain.get("tx_amount"),
                str(chain.get("tx_token") or ""),
                str(chain.get("tx_contract_ret") or ""),
                str(chain.get("tx_candidates") or ""),
                str(chain.get("chain_checked_utc") or ""),
            ),
        )


def import_reference_flows(force: bool = False) -> dict:
    """Import the verified five-day UTR↔gambler↔VPA join when available."""
    imported = 0
    gambler_path = Path(config.REFERENCE_UTR_GAMBLER)
    vpa_path = Path(config.REFERENCE_UTR_VPA)
    signature_parts = []
    for path in (gambler_path, vpa_path):
        try:
            stat = path.stat()
            signature_parts.append(f"{path}:{stat.st_size}:{stat.st_mtime_ns}")
        except OSError:
            signature_parts.append(f"{path}:missing")
    signature = "|".join(signature_parts)
    c = connect()
    existing = c.execute("SELECT COUNT(*) n FROM utr_identity").fetchone()["n"]
    prior_signature = get_meta("reference_flows_signature")
    # Existing deployments predate the signature marker but already imported
    # the same mounted case references. Adopt the marker once instead of
    # blocking live ingestion with another full idempotent upsert pass.
    if not force and existing and (not prior_signature or prior_signature == signature):
        if not prior_signature:
            set_meta("reference_flows_signature", signature)
        return {"reference_rows": existing, "reused": True}
    with _writer_lock:
        c = connect()
        if gambler_path.exists():
            with gambler_path.open(
                encoding="utf-8", errors="replace", newline=""
            ) as handle:
                for row in csv.DictReader(handle, delimiter="\t"):
                    utr = (row.get("utr") or "").strip()
                    if not utr:
                        continue
                    try:
                        amount = float(row.get("amount") or 0) or None
                    except ValueError:
                        amount = None
                    c.execute(
                        "INSERT INTO utr_identity(utr,gambler,amount_inr,day,source_reference) VALUES(?,?,?,?,?)"
                        " ON CONFLICT(utr) DO UPDATE SET gambler=excluded.gambler,amount_inr=COALESCE(excluded.amount_inr,utr_identity.amount_inr),day=excluded.day,source_reference=excluded.source_reference",
                        (
                            utr,
                            (row.get("gambler") or "").strip(),
                            amount,
                            (row.get("day") or "").strip(),
                            str(gambler_path),
                        ),
                    )
                    imported += 1
        if vpa_path.exists():
            with vpa_path.open(
                encoding="utf-8", errors="replace", newline=""
            ) as handle:
                for row in csv.DictReader(handle, delimiter="\t"):
                    utr = (row.get("utr") or "").strip()
                    if not utr:
                        continue
                    try:
                        amount = float(row.get("amount") or 0) or None
                    except ValueError:
                        amount = None
                    c.execute(
                        "INSERT INTO utr_identity(utr,gambler,amount_inr,day,vpa,source_reference) VALUES(?,?,?,?,?,?)"
                        " ON CONFLICT(utr) DO UPDATE SET gambler=excluded.gambler,amount_inr=COALESCE(excluded.amount_inr,utr_identity.amount_inr),day=excluded.day,vpa=excluded.vpa,source_reference=excluded.source_reference",
                        (
                            utr,
                            (row.get("gambler") or "").strip(),
                            amount,
                            (row.get("day") or "").strip(),
                            (row.get("vpa") or "").strip().lower(),
                            str(vpa_path),
                        ),
                    )
        # Materialize reference joins even when the related OCR file has not yet
        # been processed. They remain clearly identified as reference rows.
        c.execute(
            """
            INSERT OR REPLACE INTO payment_transactions(
              source_key,direction,gambler,amount_inr,utr,rail_type,rail_id,
              account_holder,ifsc,network,confidence,rail_role,role_confidence,last_modified,evidence_json
            )
            SELECT 'reference:utr:'||utr,'deposit',gambler,amount_inr,utr,
                   CASE WHEN vpa!='' THEN 'upi' ELSE 'unknown' END,vpa,
                   '','','','medium','beneficiary','medium',day||'T00:00:00Z',
                   json_object('source_reference',source_reference,'fact_type','fact','join_method','UTR')
            FROM utr_identity
            """
        )
        c.execute(
            "UPDATE payment_transactions SET gambler=(SELECT gambler FROM utr_identity u WHERE u.utr=payment_transactions.utr)"
            " WHERE gambler='' AND utr IN (SELECT utr FROM utr_identity WHERE gambler!='')"
        )
        c.execute(
            "INSERT INTO meta(k,v) VALUES('reference_flows_signature',?) "
            "ON CONFLICT(k) DO UPDATE SET v=excluded.v",
            (signature,),
        )
        c.commit()
    return {"reference_rows": imported}


PAYMENT_INDEX_VERSION = "2-role-aware"


def rebuild_payment_index(force: bool = False) -> dict:
    """Rebuild normalized money flows once per schema/logic version.

    Work is committed in small chunks so live sync notifications and dashboard
    reads are not starved behind a multi-minute global writer lock.
    """
    if not force and get_meta("payment_index_version") == PAYMENT_INDEX_VERSION:
        return {
            "rebuilt_objects": 0,
            "payment_rows": payment_row_count(),
            "index_version": PAYMENT_INDEX_VERSION,
            "reused": True,
        }
    import_reference_flows()
    rebuilt = 0
    c = connect()
    rows = c.execute(
        "SELECT key,extract_json FROM objects WHERE extract_json IS NOT NULL"
        " AND json_extract(extract_json,'$.kind') IN ('deposit','withdrawal')"
    ).fetchall()
    batch_size = 100
    for start in range(0, len(rows), batch_size):
        with _writer_lock:
            c = connect()
            for row in rows[start : start + batch_size]:
                try:
                    payload = json.loads(row["extract_json"])
                except Exception:
                    continue
                _replace_payment_rows(c, row["key"], payload)
                rebuilt += 1
            c.commit()
        # Explicitly yield between chunks to keep HTTP and sync fast paths live.
        time.sleep(0.02)
    set_meta("payment_index_version", PAYMENT_INDEX_VERSION)
    summary = refresh_payment_summaries()
    return {
        "rebuilt_objects": rebuilt,
        "payment_rows": payment_row_count(),
        "index_version": PAYMENT_INDEX_VERSION,
        **summary,
    }


def payment_row_count() -> int:
    return (
        connect().execute("SELECT COUNT(*) n FROM payment_transactions").fetchone()["n"]
    )


def sanitize_stored_amounts(force: bool = False) -> dict:
    """One-time repair of amounts already persisted by pre-fix extraction.

    The QR `am=` path had no plausibility bound, so UTR/reference values got
    stored as amounts and inflated the Overview totals. We null those in
    place (keeping the raw value flagged for audit), rebuild the payment rows,
    and re-materialize the money-flow summaries.

    2026-11-08: this json_extract sweep ran under the global writer lock at
    every startup. Read/decide off-lock, write only the fixes under the lock
    with an optimistic extract_json guard, then record completion so later
    startups skip the sweep (force=True re-runs it after a restore).
    """
    if not force and get_meta("amount_sanitize_v1") == "done":
        return {"impossible_amounts_cleared": 0, "skipped": True}
    rows = connect().execute(
        "SELECT key, extract_json FROM objects WHERE extract_json IS NOT NULL"
        " AND json_extract(extract_json,'$.kind') IN ('deposit','withdrawal')"
        " AND json_extract(extract_json,'$.amount_inr') IS NOT NULL"
        " AND NOT (json_extract(extract_json,'$.amount_inr') BETWEEN 0.01 AND ?)",
        (MAX_AMOUNT_INR,),
    ).fetchall()
    fixes: list[tuple[str, dict, str]] = []
    for r in rows:
        try:
            p = json.loads(r["extract_json"])
        except Exception:
            continue
        raw = p.get("amount_inr")
        if _sanitize_amount(raw) is not None:
            continue
        p["amount_rejected_inr"] = raw
        p["amount_inr"] = None
        fixes.append((r["key"], p, r["extract_json"]))
    if fixes:
        with _writer_lock:
            c = connect()
            for key, payload, original in fixes:
                cur = c.execute(
                    "UPDATE objects SET extract_json=? "
                    "WHERE key=? AND extract_json=?",
                    (json.dumps(payload, ensure_ascii=False), key, original),
                )
                if cur.rowcount:
                    _replace_payment_rows(c, key, payload)
                    _sync_upi_hits(c, key, payload)
            c.commit()
    set_meta("amount_sanitize_v1", "done")
    if not fixes:
        return {"impossible_amounts_cleared": 0, "skipped": True}
    totals = refresh_payment_summaries()
    return {"impossible_amounts_cleared": len(fixes), **totals}


def repair_twin_artifacts(dry_run: bool = True, limit: int = 20000) -> dict:
    """Batch repair persisted twin-misread amounts (₹ eaten into leading digit).

    Suspects: tesseract 5-digit amounts at medium/low confidence. The stored
    text_sample is only the first 400 chars, so the twin signature (X repeated
    + shorter sibling Y) must be visible there; undecidable rows are counted
    as need_reocr (re-OCR when Vision quota returns) and left untouched.
    2026-09-11: range extended to 6 digits — the lakh-eaten case (₹12,000 →
    '212,000', officer-verified) repairs via the same signature.
    """
    from .enrich import correct_rupee_eaten  # lazy: enrich imports db

    scanned = repaired = need_reocr = 0
    samples: list[dict] = []
    now = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    # Suspect scan runs on a READ connection with NO writer lock (repair-twins
    # used to hold _writer_lock across a full-table json_extract scan — on the
    # loaded VM that stalled the endpoint past 280s). Candidates come from the
    # indexed payment_transactions.amount_inr (idx_pay_amount) so only
    # small-candidate JSON guards run, not a 1M-row json_extract sweep.
    c = connect()
    rows = c.execute(
        "SELECT o.key AS key, o.extract_json AS extract_json FROM objects o"
        " JOIN (SELECT DISTINCT source_key FROM payment_transactions"
        "  WHERE amount_inr BETWEEN 10000 AND 999999) p ON p.source_key=o.key"
        " WHERE o.extract_json IS NOT NULL"
        " AND json_extract(o.extract_json,'$.kind') IN ('deposit','withdrawal')"
        " AND json_extract(o.extract_json,'$.engine')='tesseract'"
        " AND json_extract(o.extract_json,'$.twin_artifact') IS NULL"
        " AND json_extract(o.extract_json,'$.rupee_eaten') IS NULL"
        " LIMIT ?",
        (limit,),
    ).fetchall()
    # Phase 1 (no lock): decide. Phase 2 (live only, locked): write.
    fixes: list[tuple[str, dict]] = []
    for r in rows:
        try:
            p = json.loads(r["extract_json"])
        except Exception:
            continue
        scanned += 1
        hit_amt = correct_rupee_eaten(p.get("text_sample") or "", p.get("amount_inr"))
        if hit_amt is None:
            need_reocr += 1
            continue
        repaired += 1
        if len(samples) < 10:
            samples.append(
                {"key": r["key"], "from": p.get("amount_inr"), "to": hit_amt}
            )
        if dry_run:
            continue
        p["amount_tesseract_raw"] = p.get("amount_inr")
        p["amount_inr"] = hit_amt
        p["amount_value"] = hit_amt
        p["rupee_eaten"] = True
        p["twin_artifact"] = {
            "artifact": str(int(p["amount_tesseract_raw"])),
            "true_amount": int(hit_amt),
            "repaired_utc": now,
            "repaired_from": "batch",
        }
        fixes.append((r["key"], p))
    if fixes:
        with _writer_lock:
            wc = connect()
            for key, p in fixes:
                wc.execute(
                    "UPDATE objects SET extract_json=? WHERE key=?",
                    (json.dumps(p, ensure_ascii=False), key),
                )
                _replace_payment_rows(wc, key, p)
                _sync_upi_hits(wc, key, p)
            wc.commit()
    totals = {} if dry_run else refresh_payment_summaries()
    return {
        "scanned": scanned,
        "repaired": repaired,
        "need_reocr": need_reocr,
        "dry_run": dry_run,
        "samples": samples,
        **totals,
    }


_MALFORMED_NUM_RE = re.compile(r"\d[\d,]*\.?\d*")
_MALFORMED_REF_RE = re.compile(
    r"(?:\bref\b\.?|\breference\b|\brrn\b|ref\.?\s*(?:no|number|num))\s*"
    r"[a-z]*\s*[«»:=~#\-]?\s*=?\s*$",
    re.IGNORECASE,
)
_MALFORMED_AD_RE = re.compile(
    r"up\s*to|upto|getupto|postpaid|pre[\s-]?approved"
    r"|instant\s+(loan|credit|approval)|credit\s+(limit|line|on\s+upi)"
    r"|buy\s*now|\bemi\b|discount|cashback",
    re.IGNORECASE,
)
# Round-2 (verified 2026-09-11 top-tail audit):
# - IMPS receipts print the authoritative principal as 'Transfer Amount X'
#   while a nearby RRN/ref run gets stored ('RRNNumber « = 6252477' over
#   TRUE 1,500; 'Transfer Amount 500.00' over stored 2,537,316).
_MALFORMED_TRANSFER_RE = re.compile(
    r"transfer\s+amount\s+([\d,]+(?:\.\d+)?)", re.IGNORECASE
)
# - Bill reminders show an amount DUE, never a completed transfer
#   ('due tomorrow', 'Pay Postpaid', masked 'Credit Card - 1668';
#   8× ₹13,28,572.62 rows on credit-card bills).
_MALFORMED_BILL_RE = re.compile(
    r"due\s+tomorrow|pay\s+postpaid|credit\s+card\s*-\s*\d", re.IGNORECASE
)
# - Timestamp-fragment concat: spaced run glued to HH:MM
#   ('1:05 1 111 202' stored 1,111,202 over TRUE 200).
_MALFORMED_TSFRAG_RE = re.compile(r"(\d{1,2}:\d{2})\s+((?:\d{1,3}\s+)+\d{1,3})")
# - Targeted corrections: audited keys whose TRUE value is visible in the
#   stored sample but no generic rule can decide (Navi slips print the paid
#   amount as malformed '71,0000' while the 20L loan ad poisons the winner).
#   Values verified against text_sample 2026-09-11; never guessed.
TARGETED_AMOUNT_FIXES: dict[str, tuple[Optional[float], str]] = {
    "deposit/1788195062817.jpg": (71000.0, "sample shows paid 71,0000; 20L is loan ad"),
    "deposit/1788572774877.jpg": (72000.0, "sample shows paid 72,0000; 20L is loan ad"),
    "deposit/1788025253342.jpg": (71000.0, "sample shows paid 71,0000; 20L is loan ad"),
    "deposit/1788113559421.jpg": (71000.0, "sample shows paid 71,0000; 20L is loan ad"),
    "deposit/1788083689003.jpg": (75000.0, "sample shows paid 75000; 20L is loan ad"),
    # Round-2 audit (2026-09-11): audited junk, true principal unknown -> null
    # for Gemini re-adjudication (None correct = null, needs_gemini True).
    "withdrawal/1787741592963.jpeg": (
        None,
        "IMPS ref id picked as amount; true principal unknown",
    ),
    "withdrawal/1787915028867.jpeg": (
        500.0,
        "audited: true principal 500; stored 2.5M is a ref/id run",
    ),
    "withdrawal/1788022477775.png": (
        None,
        "credit-card-bill context (Pay Postpaid/due tomorrow); not a transfer",
    ),
    "withdrawal/1788019852731.png": (
        None,
        "credit-card-bill context (Pay Postpaid/due tomorrow); not a transfer",
    ),
    "withdrawal/1788020007524.png": (
        None,
        "credit-card-bill context (Pay Postpaid/due tomorrow); not a transfer",
    ),
    "withdrawal/1788106904937.png": (
        None,
        "credit-card-bill context (Pay Postpaid/due tomorrow); not a transfer",
    ),
    "withdrawal/1788106995331.png": (
        None,
        "credit-card-bill context (Pay Postpaid/due tomorrow); not a transfer",
    ),
    "deposit/1788969025602.jpg": (
        None,
        "stored 920188 unaudited vs ~500 split-expense; null for Vision re-read",
    ),
    "deposit/1788800046008.jpg": (
        None,
        "stored 1M matches ref/scratch id pattern; true principal unknown",
    ),
}


def _malformed_evidence(sample: str, stored: float) -> Optional[dict]:
    """Point at the culprit token for a suspect stored amount.

    Returns {rule, evidence} when the digit string matching `stored` is found
    in the text sample inside a known-decoy context, else None (undecidable —
    needs a Vision re-read of the pixels). A {rule: transfer-amount/bill,
    correct: value} dict corrects instead of nulling when the sample names
    the authoritative principal.
    """
    # Authoritative principal on IMPS receipts takes precedence: when the
    # sample prints 'Transfer Amount X' and X != stored, the stored run is
    # a nearby RRN/ref id, never the principal.
    tm = _MALFORMED_TRANSFER_RE.search(sample)
    if tm:
        try:
            true_val = float(tm.group(1).replace(",", ""))
        except ValueError:
            true_val = None
        if true_val is not None and true_val != stored:
            return {
                "rule": "transfer-amount",
                "evidence": tm.group(0)[:40],
                "correct": true_val,
            }
    for m in _MALFORMED_NUM_RE.finditer(sample):
        tok = m.group(0)
        try:
            val = float(tok.replace(",", ""))
        except ValueError:
            continue
        if val != stored:
            continue
        s, e = m.start(), m.end()
        before = sample[max(0, s - 40) : s]
        after = sample[e : e + 25]
        before_low = before.lower()
        if _MALFORMED_BILL_RE.search(before_low[-40:]):
            return {"rule": "bill-context", "evidence": tok}
        if "," in tok:
            from .enrich import detect_bad_grouping  # lazy: enrich imports db

            if detect_bad_grouping(tok):
                return {"rule": "bad-comma", "evidence": tok}
        if _MALFORMED_REF_RE.search(before_low[-35:]):
            return {"rule": "ref-number", "evidence": tok}
        if _MALFORMED_AD_RE.search(before_low[-40:]):
            return {"rule": "ad-context", "evidence": tok}
        if after.lower().lstrip("~ ").startswith("scratch"):
            return {"rule": "scratch-promo", "evidence": tok}
        if re.match(r"[\w.\-]*@", sample[e : e + 12]):
            return {"rule": "vpa-digits", "evidence": tok}
        # '=' echo: long comma-less run right after '=' while the sample holds
        # another validly-grouped amount ('/21,000 = 8460704' stored 8.46M
        # over TRUE 21,000; verified 2026-09-11).
        digits = tok.replace(",", "").split(".")[0]
        if "," not in tok and len(digits) >= 7 and re.search(r"=\s*$", before):
            others = [
                t for t in _MALFORMED_NUM_RE.findall(sample) if "," in t and t != tok
            ]
            if others:
                return {"rule": "ref-echo", "evidence": tok}
    # Truncated ref ids: extractor keeps ≤9 int digits, so a stored amount can
    # be a PREFIX of a longer ref run ('Ref. No: 613726905' stored 6137269;
    # 'Ref. No: 313103306197' stored 3131033; verified 2026-09-11).
    stored_digits = str(int(stored))
    for m in re.finditer(r"\d[\d,]*", sample):
        tok_digits = m.group(0).replace(",", "")
        if len(tok_digits) <= len(stored_digits):
            continue
        if not tok_digits.startswith(stored_digits):
            continue
        s = m.start()
        before_low = sample[max(0, s - 40) : s].lower()
        if _MALFORMED_REF_RE.search(before_low[-35:]) or re.search(
            r"=\s*$", sample[max(0, s - 3) : s]
        ):
            return {"rule": "ref-number", "evidence": m.group(0)}
        if sample[m.end() : m.end() + 25].lower().lstrip("~ ").startswith("scratch"):
            return {"rule": "scratch-promo", "evidence": m.group(0)}
    # Timestamp-fragment concat: spaced run glued to HH:MM whose glued value
    # equals the stored amount ('1:05 1 111 202' → 1,111,202 over TRUE 200).
    for m in _MALFORMED_TSFRAG_RE.finditer(sample):
        glued = m.group(2).replace(" ", "")
        try:
            if float(glued) == stored:
                return {"rule": "timestamp-frag", "evidence": m.group(0)[:40]}
        except ValueError:
            continue
    return None


def repair_malformed_amounts(dry_run: bool = True, limit: int = 20000) -> dict:
    """Batch repair persisted OCR-decoy amounts (verified 2026-09-11 top-30
    audit: bad-comma 10-100x inflation, loan/postpaid ad picks, ref/RRN ids,
    scratch ids, VPA-digit runs — ~Rs.10.3cr of inflation in the top 30
    rows alone).

    Suspects: tesseract amounts >= 100000. Decidable only when the culprit
    digit string is visible in the stored text sample inside a decoy context
    (else need_reocr). Decided rows are nulled for Gemini re-adjudication —
    the true value is NOT guessed. Mirrors repair_twin_artifacts (indexed
    candidate scan, two-phase decide/write).
    """
    scanned = repaired = need_reocr = 0
    samples: list[dict] = []
    now = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    c = connect()
    rows = c.execute(
        "SELECT o.key AS key, o.extract_json AS extract_json FROM objects o"
        " JOIN (SELECT DISTINCT source_key FROM payment_transactions"
        "  WHERE amount_inr >= 100000) p ON p.source_key=o.key"
        " WHERE o.extract_json IS NOT NULL"
        " AND json_extract(o.extract_json,'$.kind') IN ('deposit','withdrawal')"
        " AND json_extract(o.extract_json,'$.engine')='tesseract'"
        " AND json_extract(o.extract_json,'$.malformed_amount') IS NULL"
        " LIMIT ?",
        (limit,),
    ).fetchall()
    # Phase 1 (no lock): decide. Phase 2 (live only, locked): write.
    fixes: list[tuple[str, dict, dict]] = []
    for r in rows:
        try:
            p = json.loads(r["extract_json"])
        except Exception:
            continue
        stored = p.get("amount_inr")
        if stored is None:
            continue
        scanned += 1
        hit = _malformed_evidence(p.get("text_sample") or "", float(stored))
        if not hit and r["key"] in TARGETED_AMOUNT_FIXES:
            correct, note = TARGETED_AMOUNT_FIXES[r["key"]]
            if correct is None:
                # Audited junk, true principal unknown -> null for Gemini
                hit = {"rule": "targeted-audit", "evidence": note}
            else:
                hit = {"rule": "targeted-audit", "evidence": note, "correct": correct}
        if not hit:
            need_reocr += 1
            continue
        repaired += 1
        if len(samples) < 15:
            samples.append({"key": r["key"], "from": stored, **hit})
        if dry_run:
            continue
        p["amount_tesseract_raw"] = stored
        if "correct" in hit:
            # Authoritative principal named in the sample (Transfer Amount
            # label, targeted audit) — set it, no Gemini round-trip needed.
            p["amount_inr"] = hit["correct"]
            p["amount_corrected_from"] = stored
            p["needs_gemini"] = False
        else:
            p["amount_inr"] = None
            p["needs_gemini"] = True
        p["malformed_amount"] = {**hit, "repaired_utc": now, "repaired_from": "batch"}
        fixes.append((r["key"], p, hit))
    if fixes:
        with _writer_lock:
            wc = connect()
            for key, p, _hit in fixes:
                wc.execute(
                    "UPDATE objects SET extract_json=? WHERE key=?",
                    (json.dumps(p, ensure_ascii=False), key),
                )
                _replace_payment_rows(wc, key, p)
                _sync_upi_hits(wc, key, p)
            wc.commit()
    totals = {} if dry_run else refresh_payment_summaries()
    return {
        "scanned": scanned,
        "repaired": repaired,
        "corrected": sum(1 for _k, _p, h in fixes if "correct" in h),
        "need_reocr": need_reocr,
        "dry_run": dry_run,
        "samples": samples,
        **totals,
    }


def repair_txid_wallets(
    dry_run: bool = True, limit: int = 20000, force: bool = False
) -> dict:
    """Batch-remove stored transaction-id fragments kept as crypto wallets.

    force=True reprocesses rows already stamped by an earlier repair run
    (2026-09-21: the OCR-recovery graph was extended, so stamped rows with
    still-invalid TRON strings deserve a second pass with the new code).
    Stamp records the code path so repeats stay auditable.

    Verified 2026-09-11: a UPI 'Transaction ID …NEF54¢3985c…' fragment was
    stored as a BITCOIN wallet and a truncated-display '…111bcedb…' tail as
    another — txids are evidence, never wallets. Drops a stored wallet only
    with positive fragment evidence (txid/ref label gap, ellipsis truncation,
    long-hex run). A failed TRON base58check is NOT drop evidence — it means
    lookalike OCR degradation (5/S, o/0) one variant away from the true
    on-chain address — so those are checksum-recovered when possible and
    otherwise kept flagged (confidence low, needs_gemini) for Vision re-OCR,
    never dropped. Wallets live only inside extract_json (wallet_feed /
    wallet_stats read it live), so no payment-row rebuild is needed. Mirrors
    repair_twin_artifacts (two-phase decide/write).
    """
    from .enrich import (  # lazy: enrich imports db
        _after_ellipsis,
        _in_long_hex_run,
        _labeled_as_txid,
        _labeled_as_txid_loose,
        _recover_tron_ocr,
        _tron_checksum_ok,
        infer_rail_role,
        recover_tron_ocr,
    )

    scanned = repaired = need_reocr = dropped_total = 0
    recovered_total = flagged_total = relabeled_total = 0
    samples: list[dict] = []
    now = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    c = connect()
    stamp_filter = (
        ""
        if force
        else " AND json_extract(o.extract_json,'$.txid_wallets_repaired') IS NULL"
    )
    rows = c.execute(
        "SELECT o.key AS key, o.extract_json AS extract_json FROM objects o"
        " WHERE o.extract_json IS NOT NULL"
        " AND json_array_length(o.extract_json,'$.wallets') > 0"
        + stamp_filter
        + " LIMIT ?",
        (limit,),
    ).fetchall()
    fixes: list[tuple[str, dict]] = []
    for r in rows:
        try:
            p = json.loads(r["extract_json"])
        except Exception:
            continue
        wallets = p.get("wallets") or []
        if not wallets:
            continue
        scanned += 1
        sample = p.get("text_sample") or ""
        kept: list[dict] = []
        dropped: list[dict] = []
        recovered: list[dict] = []
        relabeled: list[str] = []
        undecided = False
        for w in wallets:
            addr = str(w.get("address") or "")
            if not addr:
                continue
            if (
                w.get("network") == "TRON"
                and w.get("checksum") != "valid"
                and _tron_checksum_ok(addr)
            ):
                # Math is the authority: a TRON-charset string passing
                # base58check was only ever mislabeled (rows extracted
                # before the checksum field existed). Upgrade the label,
                # change nothing else. No assumption, no recovery guess.
                w = {**w, "checksum": "valid"}
                relabeled.append(addr)
                kept.append(w)
                continue
            reason = None
            if (
                w.get("network") == "TRON"
                and w.get("checksum") != "valid"
                and not _tron_checksum_ok(addr)
            ):
                # Degraded lead, not proven junk: recover the true address
                # when lookalike variants validate, else keep flagged for
                # Vision re-OCR. Never drop — an officer lead must survive.
                fixed = recover_tron_ocr(addr)
                pos = sample.find(addr)
                if pos >= 0:
                    role, rc = infer_rail_role(sample, pos, pos + len(addr))
                else:
                    role, rc = "unknown", "low"
                if fixed:
                    w = {
                        **w,
                        "address": fixed,
                        "confidence": "medium",
                        "checksum": "valid",
                        "ocr_recovered": True,
                        "role": role,
                        "role_confidence": rc,
                    }
                    recovered.append({"from": addr, "to": fixed})
                else:
                    w = {
                        **w,
                        "confidence": "low",
                        "checksum": "invalid",
                        "role": role,
                        "role_confidence": rc,
                    }
                    p["needs_gemini"] = True
                kept.append(w)
                continue
            else:
                pos = sample.find(addr)
                if pos < 0:
                    undecided = True
                    kept.append(w)
                    continue
                end = pos + len(addr)
                if _in_long_hex_run(sample, pos, end):
                    reason = "long_hex_run"
                elif _labeled_as_txid(sample, pos):
                    reason = "txid_label"
                elif _labeled_as_txid_loose(sample, pos, end):
                    reason = "txid_label_gap"
                elif _after_ellipsis(sample, pos, end):
                    reason = "ellipsis_truncation"
            if reason:
                dropped.append({"address": addr, "reason": reason})
            else:
                kept.append(w)
        if undecided:
            need_reocr += 1
        # Recovered/relabeled/flagged wallets also modify the payload (address
        # fix, checksum label, or needs_gemini), so they must be written even
        # with zero drops.
        touched = bool(dropped or recovered or relabeled or p.get("needs_gemini"))
        if not touched:
            # Nothing to change, but mark scanned so rescan skips settled rows.
            p["txid_wallets_repaired"] = {
                "repaired_utc": now,
                "repaired_from": "batch-force" if force else "batch",
                "dropped": [],
                "kept": len(kept),
            }
            fixes.append((r["key"], p))
            continue
        repaired += 1
        dropped_total += len(dropped)
        recovered_total += len(recovered)
        relabeled_total += len(relabeled)
        if len(samples) < 15:
            samples.append(
                {"key": r["key"], "dropped": dropped, "recovered": recovered}
            )
        if dry_run:
            continue
        p["wallets"] = kept
        p["txid_wallets_repaired"] = {
            "repaired_utc": now,
            "repaired_from": "batch-force" if force else "batch",
            "dropped": dropped,
            "recovered": recovered,
            "relabeled": relabeled,
            "kept": len(kept),
        }
        fixes.append((r["key"], p))
    if fixes and not dry_run:
        with _writer_lock:
            wc = connect()
            for key, p in fixes:
                wc.execute(
                    "UPDATE objects SET extract_json=? WHERE key=?",
                    (json.dumps(p, ensure_ascii=False), key),
                )
                _replace_payment_rows(wc, key, p)
                _sync_wallet_hits(wc, key, p)
                _sync_upi_hits(wc, key, p)
            wc.commit()
    return {
        "scanned": scanned,
        "repaired": repaired,
        "need_reocr": need_reocr,
        "wallets_dropped": dropped_total,
        "wallets_recovered": recovered_total,
        "wallets_relabeled": relabeled_total,
        "dry_run": dry_run,
        "samples": samples,
    }


def apply_source_wallet_reads(corrections: list[dict], dry_run: bool = True) -> dict:
    """Apply officer-verified source-image reads to stored wallet rows.

    STRICT contract (2026-09-21: never assert from a guess):
    - replace {from,to}: `from` must already be in the row's wallets[];
      `to` must be a TRON-charset 34-char string that PASSES base58check —
      or an EVM case-restore (0x+40hex, same hex ignoring case, mandatory
      provenance: only the stored casing is corrected, no new address).
    - add [addr]: same math proof required for each addr; skips addrs
      already present.
    - drop [{address,reason,evidence}]: `address` must already be in the
      row; `evidence` (QR payload / txid context proving non-wallet) is
      mandatory; reason in {qr_fragment, txid_fragment, upi_payload}.
    Anything failing validation is reported in `skipped`, never written.
    Provenance is stamped per key (source_wallet_reads) so every change is
    auditable back to the image read. wallet_hits mirrors the write.
    """
    from .enrich import _tron_checksum_ok  # lazy: enrich imports db

    _TRON_STRICT = set("123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz")
    _DROP_REASONS = {"qr_fragment", "txid_fragment", "upi_payload"}
    now = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    applied: list[dict] = []
    skipped: list[dict] = []
    fixes: list[tuple[str, dict]] = []
    n_replaced = n_added = n_dropped = 0
    c = connect()
    for corr in corrections or []:
        key = str((corr or {}).get("key") or "")
        prov = str((corr or {}).get("provenance") or "")
        if not key:
            skipped.append({"key": key, "reason": "missing key"})
            continue
        r = c.execute("SELECT extract_json FROM objects WHERE key=?", (key,)).fetchone()
        if not r or not r["extract_json"]:
            skipped.append({"key": key, "reason": "row or payload missing"})
            continue
        try:
            p = json.loads(r["extract_json"])
        except Exception:
            skipped.append({"key": key, "reason": "payload unparsable"})
            continue
        wallets = p.get("wallets") or []
        by_addr = {
            str(w.get("address")): i
            for i, w in enumerate(wallets)
            if isinstance(w, dict) and w.get("address")
        }
        log: dict = {"key": key, "replaced": [], "added": [], "dropped": []}
        ok = True
        for rep in corr.get("replace") or []:
            frm, to = str(rep.get("from") or ""), str(rep.get("to") or "")
            if frm not in by_addr:
                skipped.append(
                    {
                        "key": key,
                        "reason": f"replace from-addr not in row: {frm}",
                        "row_wallets": sorted(by_addr)[:25],
                    }
                )
                ok = False
                break
            if (
                len(to) != 34
                or not to.startswith("T")
                or any(ch not in _TRON_STRICT for ch in to)
                or not _tron_checksum_ok(to)
            ):
                import re as _re

                if not (
                    _re.fullmatch(r"0x[0-9a-fA-F]{40}", to)
                    and frm.lower() == to.lower()
                    and prov
                ):
                    skipped.append(
                        {
                            "key": key,
                            "reason": f"replace to-addr fails checksum: {to}",
                        }
                    )
                    ok = False
                    break
                # EVM case-restore: same hex address ignoring case, restoring
                # the source-observed (EIP-55) casing from a unanimous
                # multi-pass image read. No new address is asserted — only
                # the stored casing is corrected, so no checksum math applies.
                # Provenance is mandatory.
                idx = by_addr[frm]
                old = wallets[idx]
                wallets[idx] = {
                    **old,
                    "address": to,
                    "confidence": "high",
                    "source_read": True,
                    "evm_case_restored": True,
                    "source_provenance": prov,
                }
                del by_addr[frm]
                by_addr[to] = idx
                log["replaced"].append({"from": frm, "to": to})
                n_replaced += 1
                continue
            idx = by_addr[frm]
            old = wallets[idx]
            wallets[idx] = {
                **old,
                "address": to,
                "confidence": "high",
                "checksum": "valid",
                "source_read": True,
                "source_provenance": prov,
            }
            del by_addr[frm]
            by_addr[to] = idx
            log["replaced"].append({"from": frm, "to": to})
            n_replaced += 1
        if not ok:
            continue
        for addr in corr.get("add") or []:
            addr = str(addr or "")
            if addr in by_addr:
                continue
            if (
                len(addr) != 34
                or not addr.startswith("T")
                or any(ch not in _TRON_STRICT for ch in addr)
                or not _tron_checksum_ok(addr)
            ):
                skipped.append(
                    {"key": key, "reason": f"add addr fails checksum: {addr}"}
                )
                ok = False
                break
            wallets.append(
                {
                    "address": addr,
                    "network": "TRON",
                    "standard": "TRC-20",
                    "confidence": "high",
                    "checksum": "valid",
                    "source_read": True,
                    "source_provenance": prov,
                    "role": "unknown",
                    "role_confidence": "low",
                }
            )
            by_addr[addr] = len(wallets) - 1
            log["added"].append(addr)
            n_added += 1
        if not ok:
            continue
        for d in corr.get("drop") or []:
            addr = str((d or {}).get("address") or "")
            reason = str((d or {}).get("reason") or "")
            evidence = str((d or {}).get("evidence") or "")
            if addr not in by_addr:
                skipped.append({"key": key, "reason": f"drop addr not in row: {addr}"})
                ok = False
                break
            if reason not in _DROP_REASONS or not evidence:
                skipped.append(
                    {
                        "key": key,
                        "reason": f"drop needs reason+evidence: {addr}",
                    }
                )
                ok = False
                break
            idx = by_addr[addr]
            wallets[idx] = None  # type: ignore[assignment]
            log["dropped"].append(
                {"address": addr, "reason": reason, "evidence": evidence[:300]}
            )
            n_dropped += 1
        if not ok:
            continue
        wallets = [w for w in wallets if w is not None]
        p["wallets"] = wallets
        p["source_wallet_reads"] = {
            "applied_utc": now,
            "provenance": prov,
            "replaced": log["replaced"],
            "added": log["added"],
            "dropped": log["dropped"],
        }
        applied.append(log)
        fixes.append((key, p))
    if fixes and not dry_run:
        with _writer_lock:
            wc = connect()
            for key, p in fixes:
                wc.execute(
                    "UPDATE objects SET extract_json=? WHERE key=?",
                    (json.dumps(p, ensure_ascii=False), key),
                )
                _replace_payment_rows(wc, key, p)
                _sync_wallet_hits(wc, key, p)
                _sync_upi_hits(wc, key, p)
            wc.commit()
    return {
        "corrections": len(corrections or []),
        "applied": len(applied),
        "skipped": skipped,
        "wallets_replaced": n_replaced,
        "wallets_added": n_added,
        "wallets_dropped": n_dropped,
        "dry_run": dry_run,
        "log": applied,
    }


def refresh_payment_summaries() -> dict:
    """Materialize common dashboard aggregates; live rows refresh every minute."""
    refreshed = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    filters = ("", "upi", "bank_account", "crypto_wallet", "unknown")
    # Aggregation is read-heavy. In WAL mode it can run alongside live writes,
    # so do not hold the process-wide writer lock while scanning. Only the final
    # atomic replacement below needs serialization.
    c = connect()
    total_rows: list[tuple] = []
    for direction in ("deposit", "withdrawal"):
        for rail_filter in filters:
            where = "direction=?" + (" AND rail_type=?" if rail_filter else "")
            args = [direction] + ([rail_filter] if rail_filter else [])
            tx = c.execute(
                f"SELECT COUNT(*) transactions,COALESCE(SUM(amount_inr),0) amount FROM ("
                f"SELECT source_key,MAX(amount_inr) amount_inr FROM payment_transactions WHERE {where} GROUP BY source_key)",
                args,
            ).fetchone()
            coverage = c.execute(
                f"SELECT COUNT(DISTINCT CASE WHEN gambler!='' THEN source_key END) linked_transactions,"
                f"COUNT(DISTINCT CASE WHEN rail_id!='' THEN rail_type||':'||rail_id END) distinct_rails,"
                f"COUNT(DISTINCT NULLIF(gambler,'')) distinct_gamblers,COUNT(*) rows_total "
                f"FROM payment_transactions WHERE {where}",
                args,
            ).fetchone()
            total_rows.append(
                (
                    direction,
                    rail_filter,
                    tx["transactions"] or 0,
                    tx["amount"] or 0,
                    coverage["distinct_rails"] or 0,
                    coverage["distinct_gamblers"] or 0,
                    coverage["linked_transactions"] or 0,
                    coverage["rows_total"] or 0,
                    refreshed,
                )
            )
    summary_rows = c.execute(
        "SELECT direction,rail_type,rail_id,account_holder,ifsc,network,COUNT(DISTINCT source_key),"
        "COALESCE(SUM(amount_inr),0),COUNT(DISTINCT NULLIF(gambler,'')),? "
        "FROM payment_transactions WHERE rail_id!='' "
        "GROUP BY direction,rail_type,rail_id,account_holder,ifsc,network",
        (refreshed,),
    ).fetchall()
    with _writer_lock:
        c = connect()
        c.execute("DELETE FROM payment_flow_totals")
        c.executemany(
            "INSERT INTO payment_flow_totals VALUES(?,?,?,?,?,?,?,?,?)", total_rows
        )
        c.execute("DELETE FROM payment_rail_summary")
        c.executemany(
            "INSERT INTO payment_rail_summary VALUES(?,?,?,?,?,?,?,?,?,?)",
            [tuple(row) for row in summary_rows],
        )
        c.commit()
    return {"summary_rails": len(summary_rows), "summary_refreshed_utc": refreshed}


def _bound_ts(ts: str, *, end: bool = False) -> str:
    """Normalize YYYY-MM-DD or ISO into ISO-8601 UTC for lexicographic compare.

    End-of-day bounds use T23:59:59Z so a single calendar day is inclusive.
    """
    if not ts:
        return ""
    ts = str(ts).strip()
    if len(ts) == 10 and ts[4:5] == "-" and ts[7:8] == "-":
        return ts + ("T23:59:59Z" if end else "T00:00:00Z")
    if len(ts) == 19 and "T" in ts and not ts.endswith("Z"):
        return ts + "Z"
    return ts


# Officer Date-range tab: filter by bucket folder (where the slip lives),
# not by OCR "flow type". bankingqr/ + qrcode/ are collection QRs.
_FOLDER_KEY_SQL = {
    "deposit": ("source_key LIKE 'deposit/%'", []),
    "withdrawal": ("source_key LIKE 'withdrawal/%'", []),
    "bankingqr": (
        "(source_key LIKE 'bankingqr/%' OR source_key LIKE 'qrcode/%')",
        [],
    ),
    "other": (
        "source_key NOT LIKE 'deposit/%' AND source_key NOT LIKE 'withdrawal/%' "
        "AND source_key NOT LIKE 'bankingqr/%' AND source_key NOT LIKE 'qrcode/%' "
        "AND source_key NOT LIKE 'reference:%'",
        [],
    ),
}


def _canonical_rail_type(declared: str, rail_id: str) -> str:
    """OCR often stores a bank account in the VPA field (no @) or a TRON
    address as an 'account'. Shape of the identifier wins over the label."""
    s = (rail_id or "").strip()
    if not s:
        return declared or "unknown"
    if "@" in s:
        return "upi"
    if s.startswith("0x") and len(s) >= 40:
        return "crypto_wallet"
    if s[:1] == "T" and 26 <= len(s) <= 40 and s.isalnum():
        return "crypto_wallet"
    digits = re.sub(r"\D", "", s)
    if len(digits) >= 8 and len(digits) >= 0.5 * max(len(s), 1):
        return "bank_account"
    return declared or "unknown"


# Known-good UPI handle suffixes: PSP/bank handles with live evidence behind
# them. 'pty'/'ubl'-style truncations are deliberately absent — anything
# else on a readable local is partial, never countable as new.
_KNOWN_UPI_HANDLES = frozenset(
    "okbizaxis okaxis oksbi okhdfcbank okhdfc okicici ybl paytm phonepe upi "
    "ptyes navi naviaxis uboi kotakbank kotak hdfcbank icici sbi "
    "airtel jio mbk axisbank".split()
)

_MASK_RE = re.compile(r"[xX]{2,}")


def rail_quality(rail_type: str, rail_id: str) -> str:
    """Classify an observed rail string: full / masked / partial / garble.

    2026-10-03: distinct-string counts were presented as distinct VPAs, but
    only ~6% are complete handles — the rest are masked phones, truncated
    fragments, or OCR garble. Novelty/identity claims must use full only.
    """
    if (rail_type or "") != "upi":
        if (rail_type or "") == "bank_account":
            # 2026-10-03: dashboard listed masked fragments ('XX0609',
            # 'XXXXXXXX0502') and short tails ('01414700') as accounts.
            rid = (rail_id or "").strip()
            if not rid:
                return "garble"
            if "x" in rid.lower():
                return "masked"
            if re.search(r"[a-zA-Z]", rid):
                return "garble"
            digits = re.sub(r"\D", "", rid)
            if 9 <= len(digits) <= 18 and len(digits) == len(rid):
                return "full"
            return "partial"
        return "full" if (rail_id or "").strip() else "garble"
    rid = (rail_id or "").strip()
    if "@" not in rid:
        return "garble"
    local, _, handle = rid.partition("@")
    hl = handle.lower()
    if (
        not local
        or not hl
        or local.startswith(("-", "."))
        or local.endswith(".")
        or ".." in rid
        or "..." in rid
        or len(rid) > 64
    ):
        return "garble"
    if _MASK_RE.search(local):
        return "masked"
    if re.match(r"[0x]{4,}", local, re.IGNORECASE):
        # Leading zero/x runs ('0000008301-q251', 'xxxxxx7922'): the slip
        # itself masks the number — partial identity, never countable as new.
        return "masked"
    if re.match(r"0[0-9.\-]{5,}$", local):
        # Zero-leading numeric fragment ('0001559', '0097-1', '0048272'):
        # truncated phone, never a complete VPA. (Short tails like '0767'
        # stay partial — plain truncation, no mask run.)
        return "masked"
    _mask_lead = re.match(r"[0xXeE]+", local)
    if (
        _mask_lead
        and (len(_mask_lead.group(0)) >= 3 or len(local) >= 5)
        and re.match(r"[0xXeE]+\d", local)
    ):
        # Mask-char runs incl. OCR 'e'-for-'x' reads ('x8211', '0e3679',
        # '00e1925', '000e6139') and masked tail codes ('021-xf2b').
        # Short tails like '0767' stay partial (plain truncation, no mask).
        return "masked"
    if re.match(r"[0xXecEcC]{3,}", local):
        # OCR also reads mask blocks as 'e'/'c' ('eeecee7922', 'ecceee7145'):
        # a 3+ run of mask-confusable chars can never open a real VPA.
        return "masked"
    if re.match(r"[0soxXeEcC]{3,}[\dq\-]", local):
        # s/o-prefixed mask reads ('seeeee4944', 'oeeeee3202', 'sss-1172',
        # 'seeeeq904' — trailing q is itself a mask misread).
        return "masked"
    if re.match(r"^e{2,}[a-z]{0,3}\d+", local):
        # Leading e-run mask reads ('eea280', 'een091', 'eett74', 'eetb12').
        return "masked"
    if re.match(r"^[0-9a-z]{1,3}e{2,}\d", local, re.IGNORECASE):
        # Digit-open mask runs ('200eee8634', '2ceeee8634'). Real names
        # never open with digits-then-e-runs ('neel12' stays full).
        return "masked"
    if re.match(r"[0soxXeEcC]{3,}[a-z]?[\dq\-]", local):
        # Mask run + one substituted letter + digits ('oeeeeea384').
        return "masked"
    if re.match(r"[a-z0-9]{1,2}e{2,}\d", local, re.IGNORECASE):
        # Mask runs after a short open ('2ee1559', 'eea280', 'een091',
        # 'eett74', 'eg6940', 'es2888'): e-runs never sit inside real names.
        return "masked"
    if re.match(r"^[a-z]-\d", local, re.IGNORECASE):
        # Single-letter mask prefix ('e-2068', 's-9526355975' pattern).
        return "masked"
    if re.match(r"^[a-z]{1,2}[ex0][ex0\d\-q]*\d$", local, re.IGNORECASE):
        # Alpha-open mask garble ('se9e01910', 'se0ee9989', 'seeeeq904').
        # Real names never look like this ('sonu123' stays full).
        return "masked"
    if re.match(r"^[oq]{1,2}-?\d", local, re.IGNORECASE) or re.match(
        r"^(stk|spay|epay|qpay|qypay|goay|qgpay)-", local, re.IGNORECASE
    ):
        # Truncated/misread prefixes ('o120-1', 'stk-7778854270-1' for
        # '7778854270'; 'epay-' for 'gpay-'). Digit-run linkage proved these.
        return "masked"
    if re.match(r"[0soxXeEcC]{3,}[0-9]", local):
        # Mask blocks misread with 's'/'o' prefixes ('seeeee4944',
        # 'oeeeee3202', 'ooeeee2686', 'see9e9987'): s/o + e-run + digits.
        # Short names ('sonu123', 'se7en') never match: run must be 3+.
        return "masked"
    if re.search(r"-[a-z]+[a-z0-9]*$", local, re.IGNORECASE) and not re.fullmatch(
        r"\d{10}-\d{1,2}", local
    ):
        # Lettered tail codes ('1021-xf2b', '8301-q251') are masked slots;
        # 10-digit +-1/-2 dual-SIM tags stay (real VPAs).
        return "masked"
    if (
        re.fullmatch(r"\d+", local)
        and len(local) > 10
        and not (len(local) == 12 and local.startswith("91"))
    ):
        # Long digit blobs (11+ digits, not 91+10-digit mobiles) are OCR
        # artifacts, not valid mobiles. Short numeric tails ('0767') stay
        # partial — plain truncation, kept as leads.
        return "masked"
    if re.fullmatch(r"[0x]+", local, re.IGNORECASE) and len(local) > 6:
        return "masked"
    if hl not in _KNOWN_UPI_HANDLES:
        return "partial"
    if re.fullmatch(r"\d{10}", local):
        return "full"
    if re.fullmatch(r"[a-z0-9._\-]{6,}", local, re.IGNORECASE):
        return "full"
    return "partial"


def _rail_id_shape_sql(rail_type: str) -> str:
    """Extra WHERE so Bank accounts never returns @upi rows (and vice versa)."""
    if rail_type == "upi":
        return "instr(rail_id,'@')>0"
    if rail_type == "bank_account":
        return (
            "instr(rail_id,'@')=0 AND rail_id NOT LIKE '0x%' "
            "AND NOT (substr(rail_id,1,1)='T' AND length(rail_id) BETWEEN 26 AND 40)"
        )
    if rail_type == "crypto_wallet":
        return "(rail_id LIKE '0x%' OR (substr(rail_id,1,1)='T' AND length(rail_id) BETWEEN 26 AND 40))"
    return ""


def _folder_clause(column: str, folder: str) -> tuple[str, list]:
    """Prefix filter copied from _FOLDER_KEY_SQL onto wallet_hits.key etc."""
    folder = (folder or "").strip().lower()
    if folder in ("", "all") or folder not in _FOLDER_KEY_SQL:
        return "", []
    sql, extra = _FOLDER_KEY_SQL[folder]
    return sql.replace("source_key", column), extra


def _wallet_hit_window_filters(
    q: str = "", since: str = "", until: str = "", folder: str = ""
) -> tuple[str, list]:
    conds = ["address!=''"]
    args: list = []
    fsql, fextra = _folder_clause("key", folder)
    if fsql:
        conds.append(fsql)
        args.extend(fextra)
    since_n = _bound_ts(since, end=False)
    until_n = _bound_ts(until, end=True)
    if since_n:
        conds.append("last_modified>=?")
        args.append(since_n)
    if until_n:
        conds.append("last_modified<=?")
        args.append(until_n)
    if q:
        like = "%" + q.replace("%", r"\%").replace("_", r"\_") + "%"
        conds.append(
            "(address LIKE ? ESCAPE '\\' OR raw_address LIKE ? ESCAPE '\\'"
            " OR key LIKE ? ESCAPE '\\')"
        )
        args.extend([like, like, like])
    return " AND ".join(conds), args


def _pipe_join_csv(raw: str) -> str:
    """Turn SQLite GROUP_CONCAT output into a stable 'a | b | c' officer list."""
    seen: list[str] = []
    found: set[str] = set()
    for part in (raw or "").replace("|", ",").split(","):
        token = part.strip()
        if token and token not in found:
            found.add(token)
            seen.append(token)
    return " | ".join(seen)


def _tx_status_filter(tx_status: str) -> tuple[str, list]:
    """Chain-verification filter shared by flow/unique/export queries."""
    s = (tx_status or "").strip().lower()
    if not s:
        return "", []
    if s == "unchecked":
        return "tx_status=''", []
    if s in ("verified", "derived", "ambiguous", "unresolved"):
        return "tx_status=?", [s]
    return "", []


def _money_flow_filters(
    direction: str = "deposit",
    rail_type: str = "",
    q: str = "",
    source: str = "",
    since: str = "",
    until: str = "",
    folder: str = "",
    tx_status: str = "",
) -> tuple[str, list]:
    """Shared WHERE for money_flows browse + officer CSV export."""
    conds: list[str] = []
    args: list = []
    direction = (direction or "deposit").strip().lower()
    if direction and direction != "both":
        conds.append("direction=?")
        args.append(direction)
    else:
        conds.append("direction IN ('deposit','withdrawal')")
    folder = (folder or "").strip().lower()
    if folder in _FOLDER_KEY_SQL:
        sql, extra = _FOLDER_KEY_SQL[folder]
        conds.append(sql)
        args.extend(extra)
    if rail_type:
        conds.append("rail_type=?")
        args.append(rail_type)
        shape = _rail_id_shape_sql(rail_type)
        if shape:
            conds.append(shape)
    chain_sql, chain_args = _tx_status_filter(tx_status)
    if chain_sql:
        conds.append(chain_sql)
        args.extend(chain_args)
    if source == "object":
        conds.append("source_key NOT LIKE 'reference:%'")
    elif source == "reference":
        conds.append("source_key LIKE 'reference:%'")
    since_n = _bound_ts(since, end=False)
    until_n = _bound_ts(until, end=True)
    if since_n:
        conds.append("last_modified>=?")
        args.append(since_n)
    if until_n:
        conds.append("last_modified<=?")
        args.append(until_n)
    if q:
        like = "%" + q.replace("%", r"\%").replace("_", r"\_") + "%"
        conds.append(
            "(gambler LIKE ? ESCAPE '\\' OR utr LIKE ? ESCAPE '\\' OR rail_id LIKE ? ESCAPE '\\' OR account_holder LIKE ? ESCAPE '\\' OR ifsc LIKE ? ESCAPE '\\')"
        )
        args.extend([like] * 5)
    where = " AND ".join(conds)
    if q:
        # Exact-identifier fast path (officer pastes a full VPA / account /
        # UTR / holder / IFSC): equality hits idx_pay_* instead of a 5-column
        # %LIKE% full scan. Direction-scoped when not "both".
        c = connect()
        # These identifier indexes are partial (WHERE field!=''). SQLite does
        # not infer that predicate from field=?; spell it out so the OR uses
        # the five indexes instead of scanning the entire payment table.
        exact_sql = "(" + " OR ".join(
            f"({field}!='' AND {field}=?)"
            for field in ("rail_id", "gambler", "utr", "account_holder", "ifsc")
        ) + ")"
        if direction and direction != "both":
            hit = c.execute(
                f"SELECT 1 FROM payment_transactions WHERE direction=? AND {exact_sql} LIMIT 1",
                (direction, q, q, q, q, q),
            ).fetchone()
        else:
            hit = c.execute(
                f"SELECT 1 FROM payment_transactions WHERE {exact_sql} LIMIT 1",
                (q, q, q, q, q),
            ).fetchone()
        if hit:
            conds.append(exact_sql)
            args.extend([q] * 5)
            where = " AND ".join(conds)
    return where, args


def _attach_seen_utc(items: list[dict]) -> list[dict]:
    """Join objects.seen_utc (mirror index stamp) onto payment rows."""
    keys = [
        it["source_key"]
        for it in items
        if it.get("source_key") and not str(it["source_key"]).startswith("reference:")
    ]
    if not keys:
        for it in items:
            it.setdefault("seen_utc", "")
        return items
    seen: dict[str, str] = {}
    c = connect()
    for i in range(0, len(keys), 400):
        chunk = keys[i : i + 400]
        ph = ",".join("?" * len(chunk))
        for r in c.execute(
            f"SELECT key, seen_utc FROM objects WHERE key IN ({ph})", chunk
        ):
            seen[r["key"]] = r["seen_utc"] or ""
    for it in items:
        it["seen_utc"] = seen.get(it.get("source_key") or "", "")
    return items


def money_flows(
    direction: str = "deposit",
    rail_type: str = "",
    q: str = "",
    limit: int = 200,
    offset: int = 0,
    source: str = "",
    since: str = "",
    until: str = "",
    folder: str = "",
    tx_status: str = "",
) -> dict:
    direction = (direction or "deposit").strip().lower()
    where, args = _money_flow_filters(
        direction=direction,
        rail_type=rail_type,
        q=q,
        source=source,
        since=since,
        until=until,
        folder=folder,
        tx_status=tx_status,
    )
    c = connect()
    materialized = None
    # Materialized totals are per single direction and ignore date bounds.
    if (
        not q
        and not source
        and not since
        and not until
        and not folder
        and direction in ("deposit", "withdrawal")
    ):
        materialized = c.execute(
            "SELECT * FROM payment_flow_totals WHERE direction=? AND rail_filter=?",
            (direction, rail_type),
        ).fetchone()
    if materialized:
        top_where = "direction=?" + (" AND rail_type=?" if rail_type else "")
        top_args = [direction] + ([rail_type] if rail_type else [])
        top = c.execute(
            f"SELECT rail_type,rail_id,account_holder,ifsc,network,transactions,amount_inr,gamblers "
            f"FROM payment_rail_summary WHERE {top_where} ORDER BY transactions DESC,amount_inr DESC LIMIT 100",
            top_args,
        ).fetchall()
    else:
        tx = c.execute(
            f"SELECT COUNT(*) transactions,COALESCE(SUM(amount_inr),0) amount FROM ("
            f"SELECT source_key,MAX(amount_inr) amount_inr FROM payment_transactions WHERE {where} GROUP BY source_key)",
            args,
        ).fetchone()
        coverage = c.execute(
            f"SELECT COUNT(DISTINCT CASE WHEN gambler!='' THEN source_key END) linked_gamblers,"
            f"COUNT(DISTINCT CASE WHEN rail_id!='' THEN rail_type||':'||rail_id END) distinct_rails,"
            f"COUNT(DISTINCT NULLIF(gambler,'')) distinct_gamblers,COUNT(*) rows_total FROM payment_transactions WHERE {where}",
            args,
        ).fetchone()
        top = c.execute(
            f"SELECT rail_type,rail_id,account_holder,ifsc,network,COUNT(DISTINCT source_key) transactions,"
            f"COALESCE(SUM(amount_inr),0) amount_inr,COUNT(DISTINCT NULLIF(gambler,'')) gamblers "
            f"FROM payment_transactions WHERE {where} AND rail_id!='' "
            f"GROUP BY rail_type,rail_id,account_holder,ifsc,network ORDER BY transactions DESC,amount_inr DESC LIMIT 100",
            args,
        ).fetchall()
    rows = c.execute(
        f"SELECT source_key,direction,gambler,amount_inr,utr,rail_type,rail_id,"
        f"rail_id_raw,account_holder,ifsc,network,confidence,rail_role,role_confidence,"
        f"wallet_status,txid,txids,tx_status,tx_method,tx_confidence,tx_time,"
        f"tx_from,tx_to,tx_amount,tx_token,tx_contract_ret,tx_candidates,"
        f"chain_checked_utc,last_modified,evidence_json "
        f"FROM payment_transactions WHERE {where} ORDER BY last_modified DESC,id DESC LIMIT ? OFFSET ?",
        args + [limit, offset],
    ).fetchall()
    total_rows = materialized["rows_total"] if materialized else coverage["rows_total"]
    if direction and direction != "both":
        src_new = c.execute(
            "SELECT MAX(last_modified) m FROM payment_transactions "
            "WHERE direction=? AND source_key NOT LIKE 'reference:%'",
            (direction,),
        ).fetchone()
    else:
        src_new = c.execute(
            "SELECT MAX(last_modified) m FROM payment_transactions "
            "WHERE source_key NOT LIKE 'reference:%'",
        ).fetchone()
    window_new = c.execute(
        f"SELECT MAX(last_modified) m FROM payment_transactions WHERE {where}",
        args,
    ).fetchone()
    items = []
    for row in rows:
        item = dict(row)
        try:
            item["evidence"] = json.loads(item.pop("evidence_json") or "{}")
        except Exception:
            item["evidence"] = {}
        item["has_object"] = not item["source_key"].startswith("reference:")
        items.append(item)
    _attach_seen_utc(items)
    return {
        "direction": direction,
        "folder": folder,
        "source": source,
        "since": _bound_ts(since, end=False),
        "until": _bound_ts(until, end=True),
        "transactions": (
            materialized["transactions"] if materialized else tx["transactions"]
        )
        or 0,
        "sum_amount_inr": (
            materialized["sum_amount_inr"] if materialized else tx["amount"]
        )
        or 0,
        "distinct_rails": (
            materialized["distinct_rails"]
            if materialized
            else coverage["distinct_rails"]
        )
        or 0,
        "distinct_gamblers": (
            materialized["distinct_gamblers"]
            if materialized
            else coverage["distinct_gamblers"]
        )
        or 0,
        "gambler_linked_transactions": (
            materialized["linked_transactions"]
            if materialized
            else coverage["linked_gamblers"]
        )
        or 0,
        "rows_total": total_rows,
        "src_newest": src_new["m"] if src_new else None,
        "window_newest": window_new["m"] if window_new else None,
        "summary_refreshed_utc": materialized["refreshed_utc"]
        if materialized
        else None,
        "top_rails": [dict(r) for r in top],
        "items": items,
    }


def money_flows_export_rows(
    direction: str = "both",
    rail_type: str = "",
    q: str = "",
    source: str = "object",
    since: str = "",
    until: str = "",
    folder: str = "",
    max_rows: int = 200000,
    tx_status: str = "",
) -> list[dict]:
    """All matching payment rows for officer CSV (Excel-openable), capped."""
    where, args = _money_flow_filters(
        direction=direction,
        rail_type=rail_type,
        q=q,
        source=source,
        since=since,
        until=until,
        folder=folder,
        tx_status=tx_status,
    )
    c = connect()
    rows = c.execute(
        f"SELECT source_key,direction,gambler,amount_inr,utr,rail_type,rail_id,"
        f"rail_id_raw,account_holder,ifsc,network,confidence,rail_role,role_confidence,"
        f"wallet_status,txid,txids,tx_status,tx_method,tx_confidence,tx_time,"
        f"tx_from,tx_to,tx_amount,tx_token,tx_contract_ret,tx_candidates,"
        f"chain_checked_utc,last_modified "
        f"FROM payment_transactions WHERE {where} "
        f"ORDER BY last_modified DESC,id DESC LIMIT ?",
        args + [max_rows],
    ).fetchall()
    items = [dict(r) for r in rows]
    _attach_seen_utc(items)
    for it in items:
        it["has_object"] = not str(it.get("source_key") or "").startswith("reference:")
    return items


def money_flows_unique(
    rail_type: str = "",
    q: str = "",
    source: str = "object",
    since: str = "",
    until: str = "",
    folder: str = "",
    direction: str = "both",
    limit: int = 200,
    offset: int = 0,
    tx_status: str = "",
) -> dict:
    """One row per UPI/VPA, bank account, or wallet in the date+folder window."""
    if (rail_type or "").strip().lower() == "crypto_wallet":
        return _money_flows_unique_crypto(
            q=q,
            source=source,
            since=since,
            until=until,
            folder=folder,
            direction=direction,
            limit=limit,
            offset=offset,
            tx_status=tx_status,
        )
    where, args = _money_flow_filters(
        direction=direction,
        rail_type=rail_type,
        q=q,
        source=source,
        since=since,
        until=until,
        folder=folder,
        tx_status=tx_status,
    )
    where = f"{where} AND rail_id!=''"
    # Parity with mule_rails(): OCR template junk (receipt boilerplate
    # misread as a VPA) must not top officer unique lists/CSVs either.
    # Plus amount artifacts like '000.00@tess' (digits-dot-digits handle —
    # never a real VPA) observed live on b2/b3 2026-10-02.
    # _UNIQUE_EXTRA_JUNK: template-word handles seen live 2026-10-02
    # ('receipt@contact', 'paidto@curvieunani', 'groceries@others',
    # 'imps@operator', 'share@need', 'vasu@prenge', 'safs@code').
    # Kept local to the unique path (not the shared mule tuple): these are
    # display-hygiene exclusions, while mule_rails scores them down instead.
    _UNIQUE_EXTRA_JUNK = (
        "paidto@%",
        "%@paidto",
        "balance@%",
        "account@%",
        "alerts@%",
        "beauty@%",
        "electronics@%",
        "bhatree@upi",
        "help@quicktransfer%",
        "mode@transfert",
        "receipt@%",
        "groceries@%",
        "successfully@%",
        "payment@sbi",
        "repayment@sbi",
        "payments@havingissues",
        "kumar@transferring",
        "details@%",
        "help@quicktransfer",
        "successitily@kushank",
        "from@%",
        "%@share",
        "%@snare",
        "%@shar",
        "%@view",
        "%@nickname",
        "%@sent%",
        "%@paid",
        "%@having",
        "%@your",
        "%@you",
        "%@five",
        "%@oo",
        "%@du",
        "%@sb",
        "%@contact",
        "%@operator",
        "%@others",
        "%@need",
        "%@prenge",
        "%@code",
        "%@state",
        "%@ref",
        "%@bank",
    )
    _all_junk = [p.lower() for p in _VPA_JUNK_PATTERNS] + list(_UNIQUE_EXTRA_JUNK)
    # Time fragments ('04pm@kotak', '06am@state'): receipt timestamps misread
    # as VPAs. A real VPA local never starts with digits and ends in am/pm.
    # (@kotak/@sbi alone stay: real bank handles.)
    where = (
        f"{where} AND NOT ("
        + " OR ".join(["lower(rail_id) LIKE ?"] * len(_all_junk))
        + ") AND rail_id NOT GLOB '[0-9]*.[0-9]*@*'"
        + " AND lower(rail_id) NOT GLOB '[0-9]*am@*'"
        + " AND lower(rail_id) NOT GLOB '[0-9]*pm@*'"
    )
    args = args + _all_junk
    c = connect()
    sums = c.execute(
        f"SELECT COUNT(DISTINCT source_key) slips, COALESCE(SUM(amount_inr),0) amount, "
        f"COUNT(DISTINCT rail_type||':'||rail_id) rails FROM payment_transactions WHERE {where}",
        args,
    ).fetchone()
    rows = c.execute(
        f"SELECT rail_type,rail_id,"
        f"GROUP_CONCAT(DISTINCT NULLIF(account_holder,'')) account_holder,"
        f"GROUP_CONCAT(DISTINCT NULLIF(ifsc,'')) ifsc,"
        f"GROUP_CONCAT(DISTINCT NULLIF(network,'')) network,"
        f"GROUP_CONCAT(DISTINCT NULLIF(utr,'')) utrs,"
        f"COUNT(DISTINCT NULLIF(utr,'')) utr_count,"
        f"COUNT(DISTINCT source_key) slips,"
        f"COALESCE(SUM(amount_inr),0) amount_inr,"
        f"MIN(last_modified) first_seen,"
        f"MAX(last_modified) last_seen,"
        f"MIN(source_key) sample_key "
        f"FROM payment_transactions WHERE {where} "
        f"GROUP BY rail_type,rail_id ORDER BY slips DESC,amount_inr DESC LIMIT ? OFFSET ?",
        args + [limit, offset],
    ).fetchall()
    items = [dict(r) for r in rows]
    for it in items:
        it["utrs"] = _pipe_join_csv(it.get("utrs") or "")
        it["utr_count"] = int(it.get("utr_count") or 0)
        it["has_object"] = not str(it.get("sample_key") or "").startswith("reference:")
        it["rail_quality"] = rail_quality(
            it.get("rail_type") or "", it.get("rail_id") or ""
        )
    return {
        "folder": folder,
        "rail_type": rail_type,
        "since": _bound_ts(since, end=False),
        "until": _bound_ts(until, end=True),
        "rows_total": sums["rails"] or 0,
        "transactions": sums["slips"] or 0,
        "sum_amount_inr": sums["amount"] or 0,
        "distinct_rails": sums["rails"] or 0,
        "items": items,
    }


def _money_flows_unique_crypto(
    q: str = "",
    source: str = "object",
    since: str = "",
    until: str = "",
    folder: str = "",
    direction: str = "both",
    limit: int = 200,
    offset: int = 0,
    tx_status: str = "",
) -> dict:
    """Unique crypto freeze list = decoded image wallets ∪ OCR payment rails.

    Date range used to count only payment_transactions (67) while the Crypto
    wallets tab counted wallet_hits (132). Officers need one census: every
    address decoded from images in the window, plus any slip-only rails.
    """
    limit = max(1, min(int(limit), 1000))
    offset = max(0, int(offset))
    # Image-decoded wallets carry no slip direction, so a bare UNION would
    # show the same wallets on both money sides. Scope wallet_hits to the
    # side's folders when a side is selected (folder param ANDs on top).
    _SIDE_FOLDERS = {
        "deposit": ("deposit/", "bankingqr/", "qrcode/"),
        "withdrawal": ("withdrawal/", "payment-proof/", "payment-proofs/"),
    }
    wh_where, wh_args = _wallet_hit_window_filters(
        q=q, since=since, until=until, folder=folder
    )
    chain_sql, chain_args = _tx_status_filter(tx_status)
    if chain_sql:
        wh_where = f"{wh_where} AND {chain_sql}"
        wh_args = wh_args + list(chain_args)
    if (direction or "both").strip().lower() in _SIDE_FOLDERS:
        prefixes = _SIDE_FOLDERS[(direction or "both").strip().lower()]
        wh_where = (
            f"{wh_where} AND (" + " OR ".join(["key LIKE ?"] * len(prefixes)) + ")"
        )
        wh_args = wh_args + [f"{p}%" for p in prefixes]
    pt_where, pt_args = _money_flow_filters(
        direction=direction,
        rail_type="crypto_wallet",
        q=q,
        source=source,
        since=since,
        until=until,
        folder=folder,
        tx_status=tx_status,
    )
    pt_where = f"{pt_where} AND rail_id!=''"
    c = connect()
    # Distinct slips cannot be summed per wallet: one slip may name several
    # addresses. Keep this count separate, but derive every other total and the
    # page from shared, materialized per-address aggregates below. Previously
    # the UNION/counts/joins repeatedly scanned both base tables (four times).
    slip_row = c.execute(
        f"SELECT COUNT(DISTINCT source_key) slips "
        f"FROM payment_transactions WHERE {pt_where}",
        pt_args,
    ).fetchone()
    rows = c.execute(
        f"WITH w AS ("
        f" SELECT address, COUNT(*) hits, MIN(last_modified) first_wh, MAX(last_modified) last_wh,"
        f" GROUP_CONCAT(DISTINCT NULLIF(network,'')) network,"
        f" MAX(key) sample_wh, MAX(NULLIF(raw_address,'')) raw_address,"
        f" MAX(NULLIF(wallet_status,'')) wallet_status, MAX(NULLIF(txid,'')) txid,"
        f" MAX(NULLIF(tx_status,'')) tx_status, MAX(NULLIF(chain_checked_utc,'')) chain_checked_utc "
        f" FROM wallet_hits WHERE {wh_where} GROUP BY address"
        f"), p AS ("
        f" SELECT rail_id, COUNT(DISTINCT source_key) slips, COALESCE(SUM(amount_inr),0) amount_inr,"
        f" GROUP_CONCAT(DISTINCT NULLIF(utr,'')) utrs, COUNT(DISTINCT NULLIF(utr,'')) utr_count,"
        f" MIN(source_key) sample_pt, MIN(last_modified) first_pt, MAX(last_modified) last_pt,"
        f" GROUP_CONCAT(DISTINCT NULLIF(rail_id_raw,'')) rail_ids_raw,"
        f" MAX(NULLIF(wallet_status,'')) wallet_status, MAX(NULLIF(txid,'')) txid,"
        f" GROUP_CONCAT(DISTINCT NULLIF(txids,'')) txids, MAX(NULLIF(tx_status,'')) tx_status,"
        f" MAX(NULLIF(tx_method,'')) tx_method, MAX(NULLIF(tx_confidence,'')) tx_confidence,"
        f" MAX(NULLIF(tx_time,'')) tx_time, MAX(tx_amount) tx_amount, MAX(NULLIF(tx_token,'')) tx_token,"
        f" MAX(NULLIF(chain_checked_utc,'')) chain_checked_utc "
        f" FROM payment_transactions WHERE {pt_where} GROUP BY rail_id"
        f"), u AS (SELECT address AS rail_id FROM w UNION SELECT rail_id FROM p),"
        f" totals AS (SELECT (SELECT COUNT(*) FROM u) total,"
        f" (SELECT COALESCE(SUM(hits),0) FROM w) hits,"
        f" (SELECT COUNT(*) FROM w) addrs), page AS ("
        f"SELECT u.rail_id,"
        f"COALESCE(w.hits,0) image_hits,"
        f"COALESCE(p.slips,0) slips,"
        f"p.amount_inr amount_inr,"
        f"p.utrs utrs,"
        f"p.utr_count utr_count,"
        f"w.network network,"
        f"COALESCE(w.first_wh, p.first_pt) first_seen,"
        f"CASE WHEN IFNULL(w.last_wh,'') >= IFNULL(p.last_pt,'') THEN w.last_wh ELSE p.last_pt END last_seen,"
        f"COALESCE(w.sample_wh, p.sample_pt) sample_key,"
        f"COALESCE(p.rail_ids_raw, w.raw_address, '') rail_id_raw,"
        f"COALESCE(p.wallet_status, w.wallet_status, '') wallet_status,"
        f"COALESCE(p.txid, w.txid, '') txid,"
        f"COALESCE(p.txids, '') txids,"
        f"COALESCE(p.tx_status, w.tx_status, '') tx_status,"
        f"COALESCE(p.tx_method, '') tx_method,"
        f"COALESCE(p.tx_confidence, '') tx_confidence,"
        f"COALESCE(p.tx_time, '') tx_time,"
        f"COALESCE(p.tx_amount, NULL) tx_amount,"
        f"COALESCE(p.tx_token, '') tx_token,"
        f"COALESCE(p.chain_checked_utc, w.chain_checked_utc, '') chain_checked_utc "
        f"FROM u LEFT JOIN w ON w.address=u.rail_id "
        f"LEFT JOIN p ON p.rail_id=u.rail_id "
        f"ORDER BY (COALESCE(w.hits,0)+COALESCE(p.slips,0)) DESC, u.rail_id "
        f"LIMIT ? OFFSET ?) "
        f"SELECT totals.*, page.* FROM totals LEFT JOIN page ON 1=1 "
        f"ORDER BY (COALESCE(page.image_hits,0)+COALESCE(page.slips,0)) DESC, page.rail_id",
        list(wh_args) + list(pt_args) + [limit, offset],
    ).fetchall()
    # LEFT JOIN retains totals even when the window/page has no addresses.
    totals = rows[0]
    items = []
    for r in rows:
        if r["rail_id"] is None:
            continue
        it = dict(r)
        for field in ("total", "hits", "addrs"):
            it.pop(field)
        it["rail_type"] = "crypto_wallet"
        it["account_holder"] = ""
        it["ifsc"] = ""
        it["utrs"] = _pipe_join_csv(it.get("utrs") or "")
        it["utr_count"] = int(it.get("utr_count") or 0)
        it["image_hits"] = int(it.get("image_hits") or 0)
        it["slips"] = int(it.get("slips") or 0)
        it["txids"] = _txids_of(it.get("txids"))
        it["has_object"] = not str(it.get("sample_key") or "").startswith("reference:")
        it["rail_quality"] = rail_quality(
            it.get("rail_type") or "", it.get("rail_id") or ""
        )
        items.append(it)
    return {
        "folder": folder,
        "rail_type": "crypto_wallet",
        "since": _bound_ts(since, end=False),
        "until": _bound_ts(until, end=True),
        "rows_total": totals["total"] or 0,
        "transactions": slip_row["slips"] or 0,
        "image_hits": totals["hits"] or 0,
        "decoded_wallets": totals["addrs"] or 0,
        "sum_amount_inr": 0,
        "distinct_rails": totals["total"] or 0,
        "items": items,
    }


def money_flows_unique_export_rows(
    rail_type: str = "",
    q: str = "",
    source: str = "object",
    since: str = "",
    until: str = "",
    folder: str = "",
    direction: str = "both",
    max_rows: int = 200000,
    tx_status: str = "",
) -> list[dict]:
    out = money_flows_unique(
        rail_type=rail_type,
        q=q,
        source=source,
        since=since,
        until=until,
        folder=folder,
        direction=direction,
        limit=max_rows,
        offset=0,
        tx_status=tx_status,
    )
    return out["items"]


def _key_top_folder(key: str) -> str:
    """First path segment of a bucket key: deposit / withdrawal / chat_file / …"""
    s = (key or "").split("?", 1)[0].lstrip("/")
    if "/" in s:
        return s.split("/", 1)[0]
    return s


def _origin_label(origins: set[str]) -> str:
    order = (("image_hit", "image hit"), ("ocr_slip", "OCR slip"))
    parts = [label for key, label in order if key in origins]
    return " + ".join(parts) if parts else "source"


def money_flows_unique_hits(
    rail_type: str,
    rail_id: str,
    source: str = "object",
    since: str = "",
    until: str = "",
    folder: str = "",
    direction: str = "both",
    limit: int = 500,
    offset: int = 0,
    tx_status: str = "",
    deduplicate: bool = False,
) -> dict:
    """Every origin object for one unique address in the Date range window.

    Crypto unions wallet_hits (decoded QR/image) with OCR payment slips,
    deduped by bucket key so officers never miss a source file. UPI / bank
    rows expand to every matching payment_transactions object.
    """
    rail_type = (rail_type or "").strip().lower()
    rail_id = (rail_id or "").strip()
    limit = max(1, min(int(limit), 2000))
    offset = max(0, int(offset))
    empty = {
        "rail_type": rail_type,
        "rail_id": rail_id,
        "folder": folder,
        "since": _bound_ts(since, end=False),
        "until": _bound_ts(until, end=True),
        "rows_total": 0,
        "items": [],
    }
    if not rail_id or rail_type not in ("upi", "bank_account", "crypto_wallet"):
        return empty

    by_key: dict[str, dict] = {}
    c = connect()
    where, args = _money_flow_filters(
        direction=direction,
        rail_type=rail_type,
        q="",
        source=source,
        since=since,
        until=until,
        folder=folder,
        tx_status=tx_status,
    )
    where = f"{where} AND rail_id=?"
    pt_args = list(args) + [rail_id]
    for r in c.execute(
        "SELECT source_key,direction,gambler,amount_inr,utr,rail_type,rail_id,"
        "rail_id_raw,account_holder,ifsc,network,wallet_status,txid,txids,"
        "tx_status,tx_method,tx_confidence,tx_time,tx_amount,tx_token,"
        "tx_contract_ret,chain_checked_utc,last_modified "
        f"FROM payment_transactions WHERE {where} "
        "ORDER BY last_modified DESC, id DESC",
        pt_args,
    ):
        key = r["source_key"] or ""
        if not key:
            continue
        existing = by_key.get(key)
        if existing:
            existing["origins"].add("ocr_slip")
            if not existing.get("utr") and r["utr"]:
                existing["utr"] = r["utr"]
            if existing.get("amount_inr") is None and r["amount_inr"] is not None:
                existing["amount_inr"] = r["amount_inr"]
            if not existing.get("txid") and r["txid"]:
                existing["txid"] = r["txid"]
                existing["tx_status"] = r["tx_status"] or ""
                existing["tx_method"] = r["tx_method"] or ""
                existing["tx_confidence"] = r["tx_confidence"] or ""
                existing["tx_time"] = r["tx_time"] or ""
                existing["tx_amount"] = r["tx_amount"]
                existing["tx_token"] = r["tx_token"] or ""
            continue
        by_key[key] = {
            "source_key": key,
            "folder": _key_top_folder(key),
            "direction": r["direction"] or "",
            "amount_inr": r["amount_inr"],
            "utr": r["utr"] or "",
            "rail_type": r["rail_type"] or rail_type,
            "rail_id": r["rail_id"] or rail_id,
            "rail_id_raw": r["rail_id_raw"] or "",
            "account_holder": r["account_holder"] or "",
            "ifsc": r["ifsc"] or "",
            "network": r["network"] or "",
            "wallet_status": r["wallet_status"] or "",
            "txid": r["txid"] or "",
            "txids": r["txids"] or "",
            "tx_status": r["tx_status"] or "",
            "tx_method": r["tx_method"] or "",
            "tx_confidence": r["tx_confidence"] or "",
            "tx_time": r["tx_time"] or "",
            "tx_amount": r["tx_amount"],
            "tx_token": r["tx_token"] or "",
            "tx_contract_ret": r["tx_contract_ret"] or "",
            "chain_checked_utc": r["chain_checked_utc"] or "",
            "last_modified": r["last_modified"] or "",
            "origins": {"ocr_slip"},
            "has_object": not str(key).startswith("reference:"),
        }

    if rail_type == "crypto_wallet":
        wh_where, wh_args = _crypto_image_filters(
            q="", since=since, until=until, folder=folder,
            direction=direction, tx_status=tx_status, source=source,
        )
        wh_where = f"{wh_where} AND address=?"
        for r in c.execute(
            "SELECT address,key,last_modified,seen_utc,network,kind,"
            "raw_address,wallet_status,txid,tx_status,chain_checked_utc "
            f"FROM wallet_hits WHERE {wh_where} ORDER BY last_modified DESC",
            list(wh_args) + [rail_id],
        ):
            key = r["key"] or ""
            if not key:
                continue
            existing = by_key.get(key)
            if existing:
                existing["origins"].add("image_hit")
                if not existing.get("network") and r["network"]:
                    existing["network"] = r["network"]
                if not existing.get("last_modified") and r["last_modified"]:
                    existing["last_modified"] = r["last_modified"]
                if not existing.get("txid") and r["txid"]:
                    existing["txid"] = r["txid"]
                    existing["tx_status"] = r["tx_status"] or ""
                continue
            by_key[key] = {
                "source_key": key,
                "folder": _key_top_folder(key),
                "direction": "",
                "amount_inr": None,
                "utr": "",
                "rail_type": "crypto_wallet",
                "rail_id": rail_id,
                "rail_id_raw": r["raw_address"] or "",
                "account_holder": "",
                "ifsc": "",
                "network": r["network"] or "",
                "wallet_status": r["wallet_status"] or "",
                "txid": r["txid"] or "",
                "tx_status": r["tx_status"] or "",
                "chain_checked_utc": r["chain_checked_utc"] or "",
                "last_modified": r["last_modified"] or "",
                "seen_utc": r["seen_utc"] or "",
                "origins": {"image_hit"},
                "has_object": not str(key).startswith("reference:"),
            }

    items = list(by_key.values())
    items.sort(key=lambda it: it.get("last_modified") or "", reverse=True)
    raw_total = len(items)
    if deduplicate:
        items = deduplicated_evidence(items)
    total = len(items)
    page = items[offset : offset + limit]
    for it in page:
        origins = it.pop("origins")
        it["origins"] = sorted(origins)
        it["origin_label"] = _origin_label(origins)
    _attach_seen_utc(page)
    return {
        "rail_type": rail_type,
        "rail_id": rail_id,
        "folder": folder,
        "since": _bound_ts(since, end=False),
        "until": _bound_ts(until, end=True),
        "rows_total": total,
        "source_files_total": raw_total,
        "duplicates_removed": raw_total - total,
        "items": page,
    }


def mule_rails(
    rail_type: str = "",
    q: str = "",
    limit: int = 200,
    offset: int = 0,
    source: str = "",
    new_since: str = "",
    active_since: str = "",
) -> dict:
    """Aggregate observed gambler↔rail flows for analyst triage.

    The result intentionally calls these candidate rails. Criminal attribution is
    an analyst conclusion requiring corroboration beyond a single identifier.
    """
    conds = ["rail_id!=''", "rail_type!='unknown'"]
    args: list = []
    # OCR template junk (receipt boilerplate Tesseract misreads as a VPA,
    # e.g. 'only@food', 'kumar@transfer'): feed/distinct/exports excluded it
    # since 2026-09-11 but this aggregate missed the filter, so junk topped
    # the rails (quoted as #1 "mule" by the AI investigator 2026-09-18).
    # Same patterns as _VPA_JUNK_PATTERNS, applied to rail_id ('@' in the
    # patterns means non-VPA rail types are unaffected).
    conds.append(
        "NOT (" + " OR ".join(["lower(rail_id) LIKE ?"] * len(_VPA_JUNK_PATTERNS)) + ")"
    )
    args.extend([p.lower() for p in _VPA_JUNK_PATTERNS])
    if source == "object":
        conds.append("source_key NOT LIKE 'reference:%'")
    elif source == "reference":
        conds.append("source_key LIKE 'reference:%'")
    if rail_type:
        conds.append("rail_type=?")
        args.append(rail_type)
        shape = _rail_id_shape_sql(rail_type)
        if shape:
            conds.append(shape)
    if q:
        like = "%" + q.replace("%", r"\%").replace("_", r"\_") + "%"
        conds.append(
            "(rail_id LIKE ? ESCAPE '\\' OR account_holder LIKE ? ESCAPE '\\' "
            "OR ifsc LIKE ? ESCAPE '\\' OR network LIKE ? ESCAPE '\\' OR gambler LIKE ? ESCAPE '\\' "
            "OR utr LIKE ? ESCAPE '\\')"
        )
        args.extend([like] * 6)
    where = " AND ".join(conds)
    having_bits: list[str] = []
    having_args: list = []
    if new_since:
        having_bits.append("MIN(last_modified)>=?")
        having_args.append(new_since)
    if active_since:
        # Rails that received a deposit in the window — not merely first-seen
        # then. Officers need 24h receiving UPIs / wallets / accounts.
        having_bits.append(
            "SUM(CASE WHEN direction='deposit' AND last_modified>=? THEN 1 ELSE 0 END)>0"
        )
        having_args.append(active_since)
    having_sql = (" HAVING " + " AND ".join(having_bits)) if having_bits else ""
    window_cols = ""
    window_args: list = []
    if active_since:
        # Window columns are 24h (or since) inflow — not lifetime totals.
        # Overview ranks by these; lifetime sums stay on the Mule rails tab.
        window_cols = """,
          COALESCE(SUM(CASE WHEN direction='deposit' AND last_modified>=? THEN amount_inr ELSE 0 END),0) window_deposit_amount_inr,
          COUNT(DISTINCT CASE WHEN direction='deposit' AND last_modified>=? THEN source_key END) window_deposit_transactions,
          COUNT(DISTINCT CASE WHEN direction='deposit' AND last_modified>=? THEN NULLIF(gambler,'') END) window_deposit_gamblers,
          MAX(CASE WHEN last_modified>=? THEN last_modified END) window_last_seen
        """
        window_args = [active_since] * 4
    aggregate = f"""
        SELECT rail_type,rail_id,
          GROUP_CONCAT(DISTINCT NULLIF(account_holder,'')) account_holders,
          GROUP_CONCAT(DISTINCT NULLIF(ifsc,'')) ifscs,
          GROUP_CONCAT(DISTINCT NULLIF(network,'')) networks,
          GROUP_CONCAT(DISTINCT NULLIF(utr,'')) utrs,
          COUNT(DISTINCT NULLIF(utr,'')) utr_count,
          COUNT(DISTINCT source_key) transactions,
          COUNT(DISTINCT CASE WHEN direction='deposit' THEN source_key END) deposit_transactions,
          COUNT(DISTINCT CASE WHEN direction='withdrawal' THEN source_key END) withdrawal_transactions,
          COALESCE(SUM(CASE WHEN direction='deposit' THEN amount_inr ELSE 0 END),0) deposit_amount_inr,
          COALESCE(SUM(CASE WHEN direction='withdrawal' THEN amount_inr ELSE 0 END),0) withdrawal_amount_inr,
          COUNT(DISTINCT NULLIF(gambler,'')) gamblers,
          COUNT(DISTINCT CASE WHEN direction='deposit' THEN NULLIF(gambler,'') END) deposit_gamblers,
          COUNT(DISTINCT CASE WHEN direction='withdrawal' THEN NULLIF(gambler,'') END) withdrawal_gamblers,
          SUM(CASE WHEN role_confidence='high' THEN 1 ELSE 0 END) high_role_evidence,
          SUM(CASE WHEN role_confidence='medium' THEN 1 ELSE 0 END) medium_role_evidence,
          SUM(CASE WHEN role_confidence='low' OR role_confidence='' THEN 1 ELSE 0 END) low_role_evidence,
          SUM(CASE WHEN source_key NOT LIKE 'reference:%' THEN 1 ELSE 0 END) object_evidence_rows,
          SUM(CASE WHEN source_key LIKE 'reference:%' THEN 1 ELSE 0 END) reference_rows,
          MIN(last_modified) first_seen,
          MAX(last_modified) last_seen
          {window_cols}
        FROM payment_transactions WHERE {where}
        GROUP BY rail_type,rail_id
        {having_sql}
    """
    # SELECT window ?s appear before WHERE in the SQL text.
    qargs = window_args + args + having_args
    c = connect()
    # One grouped scan supplies rows and totals. The old implementation ran the
    # same aggregate three times, causing avoidable multi-second API latency.
    all_items = [dict(row) for row in c.execute(aggregate, qargs).fetchall()]
    for row in all_items:
        row["utrs"] = _pipe_join_csv(row.get("utrs") or "")
        row["utr_count"] = int(row.get("utr_count") or 0)
    if active_since:
        all_items.sort(
            key=lambda row: (
                float(row.get("window_deposit_amount_inr") or 0),
                int(row.get("window_deposit_transactions") or 0),
                row.get("window_last_seen") or "",
            ),
            reverse=True,
        )
    else:
        all_items.sort(
            key=lambda row: (
                row["transactions"],
                bool(row["deposit_transactions"] and row["withdrawal_transactions"]),
                row.get("last_seen") or "",
            ),
            reverse=True,
        )
    summary = {
        "candidate_rails": len(all_items),
        "receiving_rails": sum(bool(row["deposit_transactions"]) for row in all_items),
        "sending_rails": sum(bool(row["withdrawal_transactions"]) for row in all_items),
        "both_direction_rails": sum(
            bool(row["deposit_transactions"] and row["withdrawal_transactions"])
            for row in all_items
        ),
        "by_rail_type": {
            "upi": sum(1 for row in all_items if row.get("rail_type") == "upi"),
            "crypto_wallet": sum(
                1 for row in all_items if row.get("rail_type") == "crypto_wallet"
            ),
            "bank_account": sum(
                1 for row in all_items if row.get("rail_type") == "bank_account"
            ),
        },
    }
    return {
        "fact_type": "fact",
        "assessment_fact_type": "analyst_conclusion",
        "scope_note": "Observed links are evidence leads; mule/criminal attribution requires corroboration.",
        "total": len(all_items),
        "summary": summary,
        "items": all_items[offset : offset + limit],
    }


def mule_rails_window(active_since: str = "", source: str = "") -> dict:
    """Sub-second receiving-rail summary for a live window (Overview cards).

    Root cause 2026-10-13: the Overview briefing polled /api/mule-rails with
    a per-second active_since. That endpoint runs a FULL-table GROUP BY +
    HAVING (measured 34.7s on 94K rows; the VM holds ~1M) on every poll —
    per-second cache keys plus per-event cache invalidation meant zero cache
    reuse — so every response died in the 25s frontend timeout and the
    Receiving-rails / 24h-mix cards sat at -- / 0-0-0 while window-bounded
    money-flows stayed fast. This function bounds the scan to the window in
    WHERE (same indexed pattern as money_flows) and returns only the summary
    the cards need. Counts must match mule_rails(active_since=...) totals.
    """
    since_n = _bound_ts(active_since, end=False)
    conds = [
        "direction='deposit'",
        "rail_id!=''",
        "rail_type!='unknown'",
        # Same OCR-template-junk exclusion as mule_rails() so the briefing
        # counts never include receipt-boilerplate VPAs.
        "NOT ("
        + " OR ".join(["lower(rail_id) LIKE ?"] * len(_VPA_JUNK_PATTERNS))
        + ")",
    ]
    args: list = [p.lower() for p in _VPA_JUNK_PATTERNS]
    if since_n:
        conds.append("last_modified>=?")
        args.append(since_n)
    if source == "object":
        conds.append("source_key NOT LIKE 'reference:%'")
    elif source == "reference":
        conds.append("source_key LIKE 'reference:%'")
    where = " AND ".join(conds)
    c = connect()
    rows = c.execute(
        f"SELECT rail_type, COUNT(DISTINCT rail_id) n,"
        f" MAX(last_modified) newest FROM payment_transactions"
        f" WHERE {where} GROUP BY rail_type",
        args,
    ).fetchall()
    by_type = {"upi": 0, "crypto_wallet": 0, "bank_account": 0}
    newest: str | None = None
    for row in rows:
        rt = row["rail_type"]
        if rt in by_type:
            by_type[rt] = int(row["n"] or 0)
        if row["newest"] and (newest is None or row["newest"] > newest):
            newest = row["newest"]
    return {
        "fact_type": "fact",
        "active_since": since_n,
        "source": source,
        "total": sum(by_type.values()),
        "summary": {"by_rail_type": by_type},
        "window_newest": newest,
    }


# OCR template junk: receipt boilerplate Tesseract misreads as a VPA
# (verified 2026-09-11 live: 1,166 distinct junk VPAs / 13,127 slips, e.g.
# 'store@completed', 'only@food', 'kumar@transfer'). These are not payment
# rails — exclude from every mule-VPA surface (feed, distinct, exports).
_VPA_JUNK_PATTERNS = (
    "%@completed",
    "only@%",
    "merchant@%",
    "restaurant@%",
    "store@%",
    "%@transfer",
    "%@entertainment",
    # 2026-09-22 live b3: super.money receipt boilerplate
    # ("super.money @ Where is my cashback?") + 'photos@on' glue token.
    # No PSP handle is 'where'/'photos' — exclude from every mule surface.
    "%@where",
    "photos@%",
)


def _vpa_junk_clause() -> str:
    return "".join(
        " AND lower(json_extract(extract_json,'$.vpa')) NOT LIKE '%s'" % p
        for p in _VPA_JUNK_PATTERNS
    )


def _vpa_exclude_clause(exclude: Collection[str] = ()) -> tuple[str, tuple]:
    """NOT IN clause for already-known mules (NEW-only tab filter).

    2026-09-11: the UPI tab paginates 10k+ VPAs server-side, so the NEW-only
    filter must apply BEFORE limit/offset — client-side filtering would show
    half-empty pages. Master lists are small (hundreds); well under the
    SQLite variable limit.
    """
    exc = sorted({str(v).lower() for v in exclude if v})
    if not exc:
        return "", ()
    return (
        " AND LOWER(json_extract(extract_json,'$.vpa')) NOT IN ("
        + ",".join("?" for _ in exc)
        + ")",
        tuple(exc),
    )


def _vpa_role_row_expr(role: str) -> str:
    """Row-level check: this row's primary VPA is tagged `role` in its own
    vpa_details[] (role provenance from extract_vpa_details / QR decode).

    2026-09-11: sender-tagged VPAs are gambler/user counterparties ("Paid by
    ..."), not mule rails — the UPI tab hides sender-only VPAs by default.
    Rows without vpa_details[] match neither role (kept as unknown/triage).
    """
    return (
        "EXISTS(SELECT 1 FROM json_each(json_extract(extract_json,'$.vpa_details')) d"
        " WHERE LOWER(json_extract(d.value,'$.vpa'))=LOWER(json_extract(extract_json,'$.vpa'))"
        f" AND json_extract(d.value,'$.role')='{role}')"
    )


def _vpa_sender_only_row_expr() -> str:
    """Positive row expression: primary VPA tagged sender and never
    beneficiary in this row's details."""
    return (
        f"({_vpa_role_row_expr('sender')} AND NOT {_vpa_role_row_expr('beneficiary')})"
    )


def _vpa_sender_only_row_filter() -> str:
    """Row predicate (NOT sender-only here): primary VPA tagged sender and
    never beneficiary in this row's details. Applied in WHERE so hidden rows
    never consume page slots — LIMIT/OFFSET stay full."""
    return f"NOT {_vpa_sender_only_row_expr()}"


def _vpa_junk_index_clause() -> tuple[str, list]:
    return (
        "".join(" AND lower(vpa) NOT LIKE ?" for _ in _VPA_JUNK_PATTERNS),
        [p.lower() for p in _VPA_JUNK_PATTERNS],
    )


def _vpa_exclude_index_clause(exclude: Collection[str] = ()) -> tuple[str, list]:
    exc = sorted({str(v).lower() for v in exclude if v})
    if not exc:
        return "", []
    return (" AND vpa NOT IN (" + ",".join("?" for _ in exc) + ")", exc)


def _upi_counts_index(
    exclude: Collection[str] = (), which: str = "both", hide_senders: bool = True
) -> dict:
    c = connect()
    jclause, jargs = _vpa_junk_index_clause()
    xclause, xargs = _vpa_exclude_index_clause(exclude)
    base = " FROM upi_hits WHERE 1=1" + jclause + xclause
    args = jargs + xargs
    feed_total = distinct_total = 0
    if which in ("both", "feed"):
        rowf = " AND sender_only=0" if hide_senders else ""
        feed_total = c.execute("SELECT COUNT(*)" + base + rowf, args).fetchone()[0]
    if which in ("both", "distinct"):
        if hide_senders:
            distinct_total = c.execute(
                "SELECT COUNT(*) FROM (SELECT vpa"
                + base
                + " GROUP BY vpa HAVING NOT (SUM(has_sender)>0 AND SUM(has_bene)=0))",
                args,
            ).fetchone()[0]
        else:
            distinct_total = c.execute(
                "SELECT COUNT(DISTINCT vpa)" + base, args
            ).fetchone()[0]
    return {"feed_total": feed_total, "distinct_total": distinct_total}


def _upi_distinct_index(
    limit: int = 300,
    offset: int = 0,
    exclude: Collection[str] = (),
    hide_senders: bool = True,
) -> list[dict]:
    jclause, jargs = _vpa_junk_index_clause()
    xclause, xargs = _vpa_exclude_index_clause(exclude)
    hclause = (
        " AND NOT (has_sender=1 AND COALESCE(has_bene,0)=0)"
        if hide_senders
        else ""
    )
    rows = (
        connect()
        .execute(
            f"""
        SELECT vpa, slips, total_inr, with_amount, first_seen, last_seen,
               key, payee_name, amount_inr, last_modified,
               CASE WHEN has_qr=1 AND has_dep=1 THEN 'qr+deposit'
                    WHEN has_qr=1 THEN 'qr' ELSE 'deposit' END AS origin,
               has_bene, has_sender
        FROM (
          SELECT vpa, key, payee_name, amount_inr, last_modified,
                 ROW_NUMBER() OVER (PARTITION BY vpa ORDER BY last_modified DESC) rn,
                 COUNT(*) OVER (PARTITION BY vpa) slips,
                 SUM(amount_inr) OVER (PARTITION BY vpa) total_inr,
                 SUM(CASE WHEN amount_inr IS NOT NULL THEN 1 ELSE 0 END)
                   OVER (PARTITION BY vpa) with_amount,
                 MIN(last_modified) OVER (PARTITION BY vpa) first_seen,
                 MAX(last_modified) OVER (PARTITION BY vpa) last_seen,
                 MAX(CASE WHEN kind='qr' THEN 1 ELSE 0 END)
                   OVER (PARTITION BY vpa) has_qr,
                 MAX(CASE WHEN kind='deposit' THEN 1 ELSE 0 END)
                   OVER (PARTITION BY vpa) has_dep,
                 MAX(has_bene) OVER (PARTITION BY vpa) has_bene,
                 MAX(has_sender) OVER (PARTITION BY vpa) has_sender
          FROM upi_hits WHERE 1=1{jclause}{xclause}
        ) WHERE rn=1{hclause}
        ORDER BY last_seen DESC LIMIT ? OFFSET ?
        """,
            (*jargs, *xargs, limit, offset),
        )
        .fetchall()
    )
    return [dict(r) for r in rows]


def upi_feed(
    limit: int = 300,
    offset: int = 0,
    exclude: Collection[str] = (),
    hide_senders: bool = True,
) -> list[dict]:
    """Mule-side VPAs only: collection QR images + deposit-slip recipients
    (money paid TO this VPA). Withdrawal-slip VPAs are excluded — those are
    gambler payout accounts, not mules. 2026-09-11: sender-only rows (a
    gambler's own VPA under "Paid by", ~1% of rows) are hidden by default —
    they are innocent counterparties, not rails; pass hide_senders=False for
    the full exhibit (export does this)."""
    xclause, xargs = _vpa_exclude_clause(exclude)
    hclause = (" AND " + _vpa_sender_only_row_filter()) if hide_senders else ""
    rows = (
        connect()
        .execute(
            "SELECT key,last_modified,seen_utc,extract_json,"
            f" CASE WHEN {_vpa_sender_only_row_expr()} THEN 1 ELSE 0 END AS sender_only"
            " FROM objects"
            " WHERE extract_json IS NOT NULL AND json_extract(extract_json,'$.kind') IN ('qr','deposit')"
            " AND json_extract(extract_json,'$.vpa') IS NOT NULL"
            + _vpa_junk_clause()
            + xclause
            + hclause
            + " ORDER BY last_modified DESC LIMIT ? OFFSET ?",
            (*xargs, limit, offset),
        )
        .fetchall()
    )
    out = []
    for r in rows:
        try:
            payload = json.loads(r["extract_json"]) if r["extract_json"] else {}
        except Exception:
            payload = {}
        out.append(
            {
                "key": r["key"],
                "last_modified": r["last_modified"],
                "seen_utc": r["seen_utc"],
                **payload,
                "sender_only": bool(r["sender_only"]),
            }
        )
    return out


def upi_distinct(
    limit: int = 300,
    offset: int = 0,
    exclude: Collection[str] = (),
    hide_senders: bool = True,
) -> list[dict]:
    """One row per unique VPA (case-insensitive): slip count, summed amount,
    first/last seen, plus the newest slip's key/payee as the sample evidence.
    Mule-side only (collection QRs + deposit-slip recipients; withdrawal-slip
    VPAs are gambler payout accounts, excluded). Origin tells which evidence
    kinds a VPA appears in. Single-pass window query — no per-VPA lookups.
    2026-09-11: VPAs seen ONLY as sender (someone's "Paid by" VPA — innocent
    counterparties) are hidden by default via the has_sender/has_bene window
    flags; has_sender is also returned so mixed rows badge "also seen as
    sender". Unknowns (no role tags) are kept for triage."""
    if upi_index_state() in ("building", "ready"):
        return _upi_distinct_index(limit, offset, exclude, hide_senders)
    xclause, xargs = _vpa_exclude_clause(exclude)
    part = "PARTITION BY LOWER(json_extract(extract_json,'$.vpa'))"
    rows = (
        connect()
        .execute(
            f"""
        SELECT vpa, slips, total_inr, with_amount, first_seen, last_seen,
               key, payee_name, amount_inr, last_modified, origin,
               has_bene, has_sender
        FROM (
          SELECT LOWER(json_extract(extract_json,'$.vpa')) AS vpa,
                  key, last_modified,
                  json_extract(extract_json,'$.payee_name') AS payee_name,
                  CAST(json_extract(extract_json,'$.amount_inr') AS REAL) AS amount_inr,"""
            + f"""
                  MAX(CASE WHEN {_vpa_role_row_expr("beneficiary")} THEN 1 ELSE 0 END) OVER (
                    {part}) AS has_bene,
                  MAX(CASE WHEN {_vpa_role_row_expr("sender")} THEN 1 ELSE 0 END) OVER (
                    {part}) AS has_sender,"""
            + """
                  CASE WHEN MAX(CASE WHEN json_extract(extract_json,'$.kind')='qr' THEN 1 ELSE 0 END) OVER ("""
            + part
            + """) = 1
                       AND MAX(CASE WHEN json_extract(extract_json,'$.kind')='deposit' THEN 1 ELSE 0 END) OVER ("""
            + part
            + """) = 1
                   THEN 'qr+deposit'
                   WHEN MAX(CASE WHEN json_extract(extract_json,'$.kind')='qr' THEN 1 ELSE 0 END) OVER ("""
            + part
            + """) = 1
                   THEN 'qr' ELSE 'deposit' END AS origin,
                  ROW_NUMBER() OVER ("""
            + part
            + """
                    ORDER BY last_modified DESC) AS rn,
                  COUNT(*) OVER ("""
            + part
            + """) AS slips,
                  SUM(CAST(json_extract(extract_json,'$.amount_inr') AS REAL)) OVER ("""
            + part
            + """) AS total_inr,
                  SUM(CASE WHEN json_extract(extract_json,'$.amount_inr') IS NOT NULL
                           THEN 1 ELSE 0 END) OVER ("""
            + part
            + """) AS with_amount,
                  MIN(last_modified) OVER ("""
            + part
            + """) AS first_seen,
                  MAX(last_modified) OVER ("""
            + part
            + """) AS last_seen
             FROM objects
             WHERE extract_json IS NOT NULL
               AND json_extract(extract_json,'$.kind') IN ('qr','deposit')
               AND json_extract(extract_json,'$.vpa') IS NOT NULL
               """
            + _vpa_junk_clause()
            + xclause
            + """
          ) WHERE rn = 1"""
            + (
                " AND NOT (has_sender = 1 AND COALESCE(has_bene, 0) = 0)"
                if hide_senders
                else ""
            )
            + """
        ORDER BY last_seen DESC LIMIT ? OFFSET ?
        """,
            (*xargs, limit, offset),
        )
        .fetchall()
    )
    return [dict(r) for r in rows]


def upi_counts(
    exclude: Collection[str] = (), which: str = "both", hide_senders: bool = True
) -> dict:
    """Exact totals backing UPI-tab pagination (same filters as the queries).

    2026-09-11: the tab shows only newest-500 without this — officers could
    not reach the other ~10k VPAs. feed_total counts slip rows, distinct_total
    counts unique VPAs; both honor the NEW-only exclusion. `which` limits the
    full-table scans (each is seconds on 1.5M rows): "feed", "distinct", "both".
    hide_senders=True (default) drops sender-only rows/VPAs from both totals
    so cards and pages agree with the filtered tab.
    """
    if upi_index_state() in ("building", "ready"):
        return _upi_counts_index(exclude, which, hide_senders)
    xclause, xargs = _vpa_exclude_clause(exclude)
    c = connect()
    base = (
        "FROM objects WHERE extract_json IS NOT NULL"
        " AND json_extract(extract_json,'$.kind') IN ('qr','deposit')"
        " AND json_extract(extract_json,'$.vpa') IS NOT NULL"
        + _vpa_junk_clause()
        + xclause
    )
    feed_total = distinct_total = 0
    rowf = (" AND " + _vpa_sender_only_row_filter()) if hide_senders else ""
    if which in ("both", "feed"):
        feed_total = c.execute("SELECT COUNT(*) " + base + rowf, xargs).fetchone()[0]
    if which in ("both", "distinct"):
        if not hide_senders:
            distinct_total = c.execute(
                "SELECT COUNT(DISTINCT LOWER(json_extract(extract_json,'$.vpa'))) "
                + base,
                xargs,
            ).fetchone()[0]
        else:
            # Exclude VPAs seen ONLY as sender across all their sightings.
            x2clause, x2args = _vpa_exclude_clause(exclude)
            base2 = (
                "FROM objects WHERE extract_json IS NOT NULL"
                " AND json_extract(extract_json,'$.kind') IN ('qr','deposit')"
                " AND json_extract(extract_json,'$.vpa') IS NOT NULL"
                + _vpa_junk_clause()
                + x2clause
            )
            sender_vpas = (
                "SELECT LOWER(json_extract(extract_json,'$.vpa')) "
                + base2
                + " GROUP BY 1 HAVING SUM(CASE WHEN "
                + _vpa_role_row_expr("sender")
                + " THEN 1 ELSE 0 END) > 0 AND SUM(CASE WHEN "
                + _vpa_role_row_expr("beneficiary")
                + " THEN 1 ELSE 0 END) = 0"
            )
            distinct_total = c.execute(
                "SELECT COUNT(DISTINCT LOWER(json_extract(extract_json,'$.vpa'))) "
                + base
                + " AND LOWER(json_extract(extract_json,'$.vpa')) NOT IN ("
                + sender_vpas
                + ")",
                (*xargs, *x2args),
            ).fetchone()[0]
    return {"feed_total": feed_total, "distinct_total": distinct_total}


def _txids_of(raw) -> Any:
    if not raw:
        return None
    try:
        v = json.loads(raw)
        return v
    except Exception:
        return raw


def _wallet_filter_clause(q: str = "", network: str = "") -> tuple[str, list]:
    """Shared WHERE fragment for wallet reads: substring match on address or
    source key, plus exact network match. LIKE wildcards in q are escaped."""
    clauses: list[str] = []
    args: list = []
    q = (q or "").strip()
    if q:
        like = (
            "%" + q.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"
        )
        clauses.append(
            "(address LIKE ? ESCAPE '\\' OR raw_address LIKE ? ESCAPE '\\'"
            " OR key LIKE ? ESCAPE '\\')"
        )
        args += [like, like, like]
    network = (network or "").strip()
    if network:
        clauses.append("network = ?")
        args.append(network)
    if not clauses:
        return "", []
    return " AND " + " AND ".join(clauses), args


def wallet_feed(
    limit: int = 300, offset: int = 0, q: str = "", network: str = ""
) -> list[dict]:
    """Every enriched object whose payload contains crypto wallet addresses.

    Served from the materialized wallet_hits index (ms); legacy json_each
    scan only until the one-time backfill completes.
    2026-09-16: offset added — the tab pages through the full hit set so old
    wallets never fall off the newest-N window (same fix as UPI tab 2026-09-11).
    """
    limit = max(1, min(limit, 2000))
    offset = max(0, offset)
    if not wallet_index_ready():
        return _wallet_feed_legacy(limit, offset)
    fclause, fargs = _wallet_filter_clause(q, network)
    rows = (
        connect()
        .execute(
            "SELECT address,key,last_modified,seen_utc,kind,network,standard,"
            "confidence,checksum,amount_inr,amount_value,amount_currency,txids,"
            "raw_address,wallet_status,txid,tx_status,chain_checked_utc"
            " FROM wallet_hits WHERE 1=1"
            + fclause
            + " ORDER BY last_modified DESC LIMIT ? OFFSET ?",
            (*fargs, limit, offset),
        )
        .fetchall()
    )
    return [
        {
            "key": r["key"],
            "last_modified": r["last_modified"],
            "seen_utc": r["seen_utc"],
            "kind": r["kind"],
            "address": r["address"],
            "raw_address": r["raw_address"] or "",
            "wallet_status": r["wallet_status"] or "",
            "network": r["network"],
            "standard": r["standard"],
            "confidence": r["confidence"],
            "checksum": r["checksum"],
            "amount_inr": r["amount_inr"],
            "amount_value": r["amount_value"],
            "amount_currency": r["amount_currency"],
            "txids": _txids_of(r["txids"]),
            "txid": r["txid"] or "",
            "tx_status": r["tx_status"] or "",
            "chain_checked_utc": r["chain_checked_utc"] or "",
        }
        for r in rows
    ]


def _wallet_feed_legacy(limit: int = 300, offset: int = 0) -> list[dict]:
    rows = (
        connect()
        .execute(
            """
        SELECT o.key, o.last_modified, o.seen_utc, je.value AS w,
               json_extract(o.extract_json,'$.kind') AS kind,
               json_extract(o.extract_json,'$.amount_inr') AS amount_inr,
               json_extract(o.extract_json,'$.amount_value') AS amount_value,
               json_extract(o.extract_json,'$.amount_currency') AS amount_currency,
               json_extract(o.extract_json,'$.txids') AS txids
        FROM objects o, json_each(o.extract_json,'$.wallets') je
        WHERE o.extract_json IS NOT NULL
        ORDER BY o.last_modified DESC LIMIT ? OFFSET ?
        """,
            (limit, offset),
        )
        .fetchall()
    )
    out = []
    for r in rows:
        try:
            w = json.loads(r["w"])
        except Exception:
            continue
        try:
            txids = json.loads(r["txids"]) if r["txids"] else None
        except Exception:
            txids = r["txids"]
        out.append(
            {
                "key": r["key"],
                "last_modified": r["last_modified"],
                "seen_utc": r["seen_utc"],
                "kind": r["kind"],
                **w,
                "amount_inr": r["amount_inr"],
                "amount_value": r["amount_value"],
                "amount_currency": r["amount_currency"],
                "txids": txids,
            }
        )
    return out


def wallet_stats() -> dict:
    """Aggregate counts for the Wallets dashboard cards (indexed, ms)."""
    if not wallet_index_ready():
        return _wallet_stats_legacy()
    c = connect()
    row = c.execute(
        "SELECT COUNT(*) n, COUNT(DISTINCT address) d FROM wallet_hits"
    ).fetchone()
    by_net = c.execute(
        "SELECT network net, COUNT(*) n FROM wallet_hits"
        " GROUP BY network ORDER BY n DESC"
    ).fetchall()
    chain = c.execute(
        "SELECT SUM(CASE WHEN txid!='' THEN 1 ELSE 0 END) with_tx,"
        " SUM(CASE WHEN chain_checked_utc!='' THEN 1 ELSE 0 END) checked,"
        " SUM(CASE WHEN wallet_status IN ('valid','repaired') THEN 1 ELSE 0 END) verified"
        " FROM wallet_hits"
    ).fetchone()
    return {
        "wallet_hits": row["n"] or 0,
        "distinct_wallets": row["d"] or 0,
        "wallets_with_tx": int(chain["with_tx"] or 0),
        "wallets_checked": int(chain["checked"] or 0),
        "wallets_verified": int(chain["verified"] or 0),
        "by_network": [{"network": r["net"], "count": r["n"]} for r in by_net],
    }


def _wallet_stats_legacy() -> dict:
    """Aggregate counts for the Wallets dashboard cards."""
    row = (
        connect()
        .execute(
            """
        SELECT COUNT(*) AS n, COUNT(DISTINCT json_extract(je.value,'$.address')) distinct_wallets
        FROM objects o, json_each(o.extract_json,'$.wallets') je
        WHERE o.extract_json IS NOT NULL
        """
        )
        .fetchone()
    )
    by_net = (
        connect()
        .execute(
            """
        SELECT json_extract(je.value,'$.network') net, COUNT(*) n
        FROM objects o, json_each(o.extract_json,'$.wallets') je
        WHERE o.extract_json IS NOT NULL GROUP BY net ORDER BY n DESC
        """
        )
        .fetchall()
    )
    return {
        "wallet_hits": row["n"] or 0,
        "distinct_wallets": row["distinct_wallets"] or 0,
        "by_network": [{"network": r["net"], "count": r["n"]} for r in by_net],
    }


_WALLET_DISTINCT_SORTS = {
    # NULLS: SQLite treats NULL as smallest, so DESC already sorts NULLs
    # last — no NULLS LAST syntax needed.
    "recent": "last_seen DESC",
    "hits": "hits DESC, last_seen DESC",
    "amount": "amount_inr DESC, last_seen DESC",
}


def wallet_distinct(
    limit: int = 300,
    offset: int = 0,
    q: str = "",
    network: str = "",
    sort: str = "recent",
) -> list[dict]:
    """One row per unique wallet address: hit count, first/last seen, plus the
    newest hit's key/network/label/amount/txids as the sample evidence.
    Officers use this to reach old single-hit wallets that drown in the
    newest-first hit feed. Served from the wallet_hits index (ms).
    Single-pass window query — no per-address lookups (2026-09-17: the old
    GROUP BY + N sample SELECTs did up to 501 queries per page)."""
    limit = max(1, min(limit, 2000))
    offset = max(0, offset)
    if not wallet_index_ready():
        return _wallet_distinct_legacy(limit, offset)
    fclause, fargs = _wallet_filter_clause(q, network)
    order = _WALLET_DISTINCT_SORTS.get(sort, _WALLET_DISTINCT_SORTS["recent"])
    c = connect()
    rows = c.execute(
        "SELECT address, hits, first_seen, last_seen,"
        " key, network, standard, confidence, checksum, kind, last_modified,"
        " amount_inr, amount_value, amount_currency, txids, raw_address,"
        " wallet_status, txid, tx_status, chain_checked_utc"
        " FROM ("
        " SELECT address, key, last_modified, network, standard, confidence,"
        " checksum, kind, amount_inr, amount_value, amount_currency, txids,"
        " raw_address, wallet_status, txid, tx_status, chain_checked_utc,"
        " ROW_NUMBER() OVER ("
        " PARTITION BY address ORDER BY last_modified DESC) AS rn,"
        " COUNT(*) OVER (PARTITION BY address) AS hits,"
        " MIN(last_modified) OVER (PARTITION BY address) AS first_seen,"
        " MAX(last_modified) OVER (PARTITION BY address) AS last_seen"
        " FROM wallet_hits WHERE 1=1" + fclause + ") WHERE rn = 1"
        " ORDER BY " + order + " LIMIT ? OFFSET ?",
        (*fargs, limit, offset),
    ).fetchall()
    return [
        {
            "address": r["address"],
            "hits": r["hits"],
            "first_seen": r["first_seen"],
            "last_seen": r["last_seen"],
            "key": r["key"],
            "network": r["network"],
            "standard": r["standard"],
            "confidence": r["confidence"],
            "checksum": r["checksum"],
            "kind": r["kind"],
            "last_modified": r["last_modified"],
            "amount_inr": r["amount_inr"],
            "amount_value": r["amount_value"],
            "amount_currency": r["amount_currency"],
            "txids": _txids_of(r["txids"]),
            "raw_address": r["raw_address"] or "",
            "wallet_status": r["wallet_status"] or "",
            "txid": r["txid"] or "",
            "tx_status": r["tx_status"] or "",
            "chain_checked_utc": r["chain_checked_utc"] or "",
        }
        for r in rows
    ]


def _wallet_distinct_legacy(limit: int = 300, offset: int = 0) -> list[dict]:
    """One row per unique wallet address: hit count, first/last seen, plus the
    newest hit's key/network/label as the sample evidence. Officers use this to
    reach old single-hit wallets that drown in the newest-first hit feed.
    Single-pass window query — no per-address lookups."""
    limit = max(1, min(limit, 2000))
    offset = max(0, offset)
    rows = (
        connect()
        .execute(
            """
        SELECT address, hits, first_seen, last_seen,
               key, last_modified, network, standard, confidence, kind,
               amount_inr, amount_value, amount_currency, txids, checksum
        FROM (
          SELECT json_extract(je.value,'$.address') AS address,
                 o.key AS key,
                 o.last_modified AS last_modified,
                 json_extract(je.value,'$.network') AS network,
                 json_extract(je.value,'$.standard') AS standard,
                 json_extract(je.value,'$.confidence') AS confidence,
                 json_extract(je.value,'$.checksum') AS checksum,
                 json_extract(extract_json,'$.kind') AS kind,
                 CAST(json_extract(extract_json,'$.amount_inr') AS REAL) AS amount_inr,
                 json_extract(extract_json,'$.amount_value') AS amount_value,
                 json_extract(extract_json,'$.amount_currency') AS amount_currency,
                 json_extract(extract_json,'$.txids') AS txids,
                 ROW_NUMBER() OVER (
                   PARTITION BY json_extract(je.value,'$.address')
                   ORDER BY last_modified DESC) AS rn,
                 COUNT(*) OVER (
                   PARTITION BY json_extract(je.value,'$.address')) AS hits,
                 MIN(last_modified) OVER (
                   PARTITION BY json_extract(je.value,'$.address')) AS first_seen,
                 MAX(last_modified) OVER (
                   PARTITION BY json_extract(je.value,'$.address')) AS last_seen
          FROM objects o, json_each(o.extract_json,'$.wallets') je
          WHERE o.extract_json IS NOT NULL
            AND json_extract(je.value,'$.address') IS NOT NULL
        ) WHERE rn = 1
        ORDER BY last_seen DESC LIMIT ? OFFSET ?
        """,
            (limit, offset),
        )
        .fetchall()
    )
    out = [dict(r) for r in rows]
    for r in out:
        r["txids"] = _txids_of(r.get("txids"))
    return out


def wallet_counts(q: str = "", network: str = "") -> dict:
    """Exact totals backing wallet-tab pagination (indexed, ms) — same
    filters as the queries so the page count matches the filtered list."""
    if not wallet_index_ready():
        return _wallet_counts_legacy()
    fclause, fargs = _wallet_filter_clause(q, network)
    row = (
        connect()
        .execute(
            "SELECT COUNT(*) n, COUNT(DISTINCT address) d FROM wallet_hits"
            " WHERE 1=1" + fclause,
            fargs,
        )
        .fetchone()
    )
    return {"feed_total": row["n"] or 0, "distinct_total": row["d"] or 0}


def _wallet_counts_legacy() -> dict:
    """Exact totals backing wallet-tab pagination (same filters as queries)."""
    c = connect()
    row = c.execute(
        "SELECT COUNT(*) n,"
        " COUNT(DISTINCT json_extract(je.value,'$.address')) d"
        " FROM objects o, json_each(o.extract_json,'$.wallets') je"
        " WHERE o.extract_json IS NOT NULL"
    ).fetchone()
    return {"feed_total": row["n"] or 0, "distinct_total": row["d"] or 0}


def wallet_times_by_address(addresses: list[str]) -> dict[str, dict[str, str]]:
    """First/last object last_modified per address from wallet_hits (indexed)."""
    out: dict[str, dict[str, str]] = {}
    addrs = [a for a in addresses if a]
    if not addrs or not wallet_index_ready():
        return out
    chunk = 400
    c = connect()
    for i in range(0, len(addrs), chunk):
        part = addrs[i : i + chunk]
        ph = ",".join("?" * len(part))
        for r in c.execute(
            "SELECT address, MIN(last_modified) first_seen, MAX(last_modified) last_seen "
            f"FROM wallet_hits WHERE address IN ({ph}) GROUP BY address",
            part,
        ).fetchall():
            out[r["address"]] = {
                "first_seen": r["first_seen"] or "",
                "last_seen": r["last_seen"] or "",
            }
    return out


def live_wallet_addresses() -> set[str]:
    """Distinct live-decoded wallet addresses (indexed, cached by callers)."""
    if not wallet_index_ready():
        return _live_wallet_addresses_legacy()
    rows = connect().execute("SELECT DISTINCT address a FROM wallet_hits").fetchall()
    return {str(r["a"]) for r in rows if r["a"]}


def _live_wallet_addresses_legacy() -> set[str]:
    """Distinct live-decoded wallet addresses (single scan, cached by callers)."""
    rows = (
        connect()
        .execute(
            "SELECT DISTINCT json_extract(je.value,'$.address') a"
            " FROM objects o, json_each(o.extract_json,'$.wallets') je"
            " WHERE o.extract_json IS NOT NULL"
            " AND json_extract(je.value,'$.address') IS NOT NULL"
        )
        .fetchall()
    )
    return {str(r["a"]) for r in rows if r["a"]}


def keys_in_mirror(keys: list[str]) -> set[str]:
    """Which of these keys exist in this host's mirror (one bounded query)."""
    keys = [k for k in keys if k]
    if not keys:
        return set()
    out = set()
    c = connect()
    for i in range(0, len(keys), 500):
        chunk = keys[i : i + 500]
        rows = c.execute(
            "SELECT key FROM objects WHERE key IN ("
            + ",".join("?" for _ in chunk)
            + ")",
            chunk,
        ).fetchall()
        out.update(r["key"] for r in rows)
    return out


def merge_object_wallets(key: str, wallets: list[dict], kind: str) -> int:
    """Merge sheet/config-decoded wallets into objects.extract_json (no clobber).

    2026-09-16 (root-cause fix): csv-files/ spreadsheets were ledger-indexed
    for money rows but their wallet-address cells were never scanned — the
    28 hawala wallets lived only in bank_data xlsx. Same for any future
    JSON/text rail config. This merges found wallets into the same
    extract_json wallets[] the Crypto tab reads, so sheet sources surface
    with view-proof like image decodes. Existing image payloads are only
    appended to (dedupe by address); never overwritten.
    Returns number of newly added wallets.
    """
    if not wallets:
        return 0
    now = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    with _writer_lock:
        c = connect()
        row = c.execute(
            "SELECT extract_json,last_modified FROM objects WHERE key=?", (key,)
        ).fetchone()
        if row is None:
            return 0
        try:
            payload = json.loads(row["extract_json"]) if row["extract_json"] else {}
        except Exception:
            payload = {}
        if not isinstance(payload, dict):
            payload = {}
        existing = {
            str(w.get("address"))
            for w in (payload.get("wallets") or [])
            if isinstance(w, dict) and w.get("address")
        }
        added = 0
        for w in wallets:
            if not isinstance(w, dict) or not w.get("address"):
                continue
            if str(w["address"]) in existing:
                continue
            (payload.setdefault("wallets", [])).append(w)
            existing.add(str(w["address"]))
            added += 1
        if added:
            payload["sheet_scan_utc"] = now
            payload.setdefault("kind", kind)
            c.execute(
                "UPDATE objects SET extract_json=?, enriched=1, enriched_utc=?,"
                " enrich_error=NULL, enrich_retry_utc=NULL WHERE key=?",
                (json.dumps(payload, ensure_ascii=False), now, key),
            )
            _replace_payment_rows(c, key, payload)
            _sync_wallet_hits(c, key, payload)
            _sync_upi_hits(c, key, payload)
            c.commit()
    return added


def pending_slip_count() -> int:
    """COUNT twin of pending_slips() — the slip backlog the live
    pending-enrichment queue never sees.

    With SLIP_OCR burn mode the live queue (_pending_where) excludes
    deposit/withdrawal/payment prefixes entirely; slips drain only via
    slip_loop, so pending_enrichment_count() reports 0 while hundreds of
    thousands of bulk-copied slips sit undecoded. This count makes the
    backlog visible so eligible/coverage/ETA stay honest. Served by the
    idx_obj_enrich_pending partial index — no full-table scan.
    """
    slip_clause, slip_args = _slip_prefix_clause()
    return (
        connect()
        .execute(
            "SELECT COUNT(*) FROM objects "
            "WHERE (enriched=0 AND extract_json IS NULL) "
            "AND (enrich_retry_utc IS NULL "
            "OR enrich_retry_utc <= strftime('%Y-%m-%dT%H:%M:%SZ','now')) "
            f"AND {_IMG_CLAUSE} "
            f"AND {slip_clause}",
            slip_args,
        )
        .fetchone()[0]
    )


def enrichment_progress() -> dict:
    r = (
        connect()
        .execute(
            "SELECT COUNT(*) total, SUM(enriched) done, "
            "SUM(extract_json IS NOT NULL) with_payload, "
            "SUM(enrich_error IS NOT NULL) errors, "
            "SUM(CASE WHEN enriched_utc >= strftime('%Y-%m-%dT%H:%M:%SZ','now','-1 hour') THEN 1 ELSE 0 END) processed_last_hour "
            "FROM objects INDEXED BY idx_obj_progress"
        )
        .fetchone()
    )
    pend = pending_enrichment_count()
    slip_backlog = pending_slip_count()
    # payment/ (and deposit//withdrawal/ when SLIP_OCR is off) images are
    # counted in BOTH pend and slip_backlog — the live and slip loops both
    # drain them. Subtract the intersection once so eligible/coverage/ETA
    # stay honest (2026-09-16). Intersect the live WHERE with the slip
    # prefixes so this holds under any SLIP_OCR setting.
    overlap_where, overlap_args = _pending_where(full_scan=False)
    slip_clause, slip_args = _slip_prefix_clause()
    overlap = (
        connect()
        .execute(
            "SELECT COUNT(*) c FROM objects WHERE (" + overlap_where + ") "
            f"AND {slip_clause}",
            overlap_args + slip_args,
        )
        .fetchone()["c"]
    )
    eligible = (r["done"] or 0) + pend + slip_backlog - overlap
    coverage = round(((r["done"] or 0) / eligible) * 100, 2) if eligible else 100.0
    hourly = r["processed_last_hour"] or 0
    eta_hours = round((pend + slip_backlog - overlap) / hourly, 1) if hourly else None
    return {
        "total": r["total"],
        "enriched_done": r["done"] or 0,
        "with_payload": r["with_payload"] or 0,
        "pending_now": pend,
        "slip_backlog": slip_backlog,
        "queue_overlap": overlap,
        "eligible_total": eligible,
        "coverage_pct": coverage,
        "processed_last_hour": hourly,
        "eta_hours": eta_hours,
        "errors": r["errors"] or 0,
    }


def _row_pub(r) -> dict:
    d = dict(r)
    if d.get("extract_json"):
        try:
            d["extract"] = json.loads(d.pop("extract_json"))
        except Exception:
            d["extract"] = None
    else:
        d["extract"] = None
        d.pop("extract_json", None)
    return d


# ---------------------------------------------------------------- agent network
# The operator/agent hierarchy is exported by the platform itself as
# csv-files/manager_*.xlsx (public bucket) and parsed into a roster artifact.
# Serving it from the artifact (not a DB scan) keeps the Agents tab cheap: the
# file is ~0.6 MB and read once per cache miss.
_AGENT_ROSTER_REL = (
    "analysis/CASE-2026-CBTF/agent-roster-attribution-2026-10-13/agent_roster.json"
)
_STAFF_FINDINGS_REL = (
    "analysis/CASE-2026-CBTF/staff-identity-negative-results-2026-10-13/"
    "staff_identity_findings.json"
)


def _intel_artifact(rel: str):
    """Locate an intelligence artifact in container (/app) or repo layout."""
    from pathlib import Path as _P

    here = _P(__file__).resolve().parent
    for root in (
        here.parent / "intelligence",
        here.parent.parent / "intelligence",
        _P("/app/intelligence"),
        _P("/opt/s3lm/intelligence"),
    ):
        p = root / rel
        if p.exists():
            try:
                return json.loads(p.read_text(encoding="utf-8"))
            except Exception:
                return None
    return None


def agent_network(limit: int = 200, offset: int = 0) -> dict:
    """Agent + operator identity surface for the dashboard Agents tab.

    Three evidence layers, kept separate so the officer never reads a
    correlation as an identity:
      1. operator_desks  — live gateway_rows.actionby (console usernames)
      2. agents          — platform-exported roster artifact (3,707 accounts)
      3. login_evidence  — the only agents with a mobile+IP in public exports

    Attribution guard: handles and contacts are evidence leads. Criminal-role
    attribution requires corroboration beyond a single indicator.
    """
    roster = _intel_artifact(_AGENT_ROSTER_REL) or {}
    findings = _intel_artifact(_STAFF_FINDINGS_REL) or {}
    agents = roster.get("agents") or []

    managers: dict = {}
    brands: dict = {}
    mobiles: dict = {}
    for a in agents:
        for m in a.get("upline_managers") or []:
            managers.setdefault(m, 0)
            managers[m] += 1
        for o in a.get("origins") or []:
            brands.setdefault(o, 0)
            brands[o] += 1
        for mo in a.get("mobiles") or []:
            mobiles.setdefault(mo, 0)
            mobiles[mo] += 1
    shared_mobiles = sum(1 for v in mobiles.values() if v > 1)

    # Operator consoles come from the live gateway tables, not the artifact.
    c = connect()
    desks = [
        dict(r)
        for r in c.execute(
            """
            SELECT actionby AS operator, COUNT(*) AS rows,
                   COUNT(DISTINCT client) AS clients,
                   COUNT(DISTINCT customer) AS customers,
                   ROUND(SUM(amount),2) AS deposit_amount_inr,
                   SUM(is_test) AS test_rows,
                   MIN(ts) AS first_seen, MAX(ts) AS last_seen
            FROM gateway_rows
            WHERE actionby IS NOT NULL AND TRIM(actionby)!=''
              AND kind='gateway_deposit'
            GROUP BY actionby ORDER BY rows DESC
            """
        ).fetchall()
    ]
    for d in desks:
        d["test_rows"] = int(d.get("test_rows") or 0)

    ranked_agents = sorted(
        agents,
        key=lambda a: (
            -len(a.get("mobiles") or []),
            -int(a.get("child_accounts") or 0),
            a.get("username") or "",
        ),
    )

    def top(d, n=25):
        return [
            {"name": k, "agents": v}
            for k, v in sorted(d.items(), key=lambda kv: (-kv[1], kv[0]))[:n]
        ]

    return {
        "fact_type": "fact",
        "assessment_fact_type": "analyst_conclusion",
        "scope_note": (
            "Handles, mobiles and IPs are evidence leads for account identity only. "
            "Mule/criminal attribution requires corroboration beyond a single indicator."
        ),
        "roster_source": roster.get("source") or "",
        "roster_collected_utc": roster.get("collected_utc") or "",
        "roster_note": roster.get("note") or "",
        "guard_rail": findings.get("guard_rail") or "",
        "summary": {
            "operator_consoles": len(desks),
            "agents": len(agents),
            "agents_with_mobile": sum(1 for a in agents if a.get("mobiles")),
            "distinct_mobiles": len(mobiles),
            "mobiles_shared_by_multiple_agents": shared_mobiles,
            "upline_managers": len(managers),
            "brand_origins": len(brands),
            "agents_with_login_evidence": len(
                findings.get("agents_with_login_records") or []
            ),
        },
        "operator_desks": desks,
        "top_upline_managers": top(managers),
        "top_brand_origins": top(brands),
        "login_evidence": findings.get("agents_with_login_records") or [],
        "staff_like_bettors": findings.get("staff_like_bettor_accounts_not_operators")
        or [],
        "negative_results": findings.get("negative_results") or [],
        "unmined_corpora": findings.get("unmined_corpora") or [],
        "items": ranked_agents[offset : offset + limit],
        "total": len(ranked_agents),
    }


# ---------------------------------------------------------------------------
# Tronscan chain verification: persistence + work queues (2026-11-20)
# ---------------------------------------------------------------------------

def chain_tx_cache_get(txid: str) -> Optional[dict]:
    """Cached Tronscan transaction, normalised for chain.resolve_source."""
    h = str(txid or "").strip().lower()
    if not h:
        return None
    r = (
        connect()
        .execute("SELECT * FROM chain_tx_cache WHERE txid=?", (h,))
        .fetchone()
    )
    if not r:
        return None
    try:
        raw = json.loads(r["raw_json"] or "{}")
    except Exception:
        raw = {}
    return {
        "txid": r["txid"],
        "network": r["network"] or "TRON",
        "from": r["from_address"] or "",
        "to": r["to_address"] or "",
        "amount": r["amount"],
        "token": r["token"] or "",
        "token_contract": r["token_contract"] or "",
        "block_ts": r["block_ts"] or "",
        "contract_ret": r["contract_ret"] or "",
        "confirmed": bool(r["confirmed"]),
        "raw": raw,
    }


def chain_tx_cache_put(info: dict) -> None:
    """Persist an immutable Tronscan response (raw JSON + response hash)."""
    if not isinstance(info, dict) or not info.get("txid"):
        return
    txid = str(info["txid"]).strip().lower()
    if not re.fullmatch(r"[0-9a-f]{64}", txid):
        return
    raw = info.get("raw") or {}
    raw_json = json.dumps(raw, ensure_ascii=False, sort_keys=True)
    digest = hashlib.sha256(raw_json.encode("utf-8", "replace")).hexdigest()
    now = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    try:
        with _writer_lock:
            c = connect()
            c.execute(
                "INSERT INTO chain_tx_cache(txid,network,from_address,to_address,"
                "amount,token,token_contract,block_ts,contract_ret,confirmed,"
                "raw_json,response_sha256,fetched_utc)"
                " VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)"
                " ON CONFLICT(txid) DO UPDATE SET"
                " from_address=excluded.from_address,to_address=excluded.to_address,"
                " amount=excluded.amount,token=excluded.token,"
                " token_contract=excluded.token_contract,block_ts=excluded.block_ts,"
                " contract_ret=excluded.contract_ret,confirmed=excluded.confirmed,"
                " raw_json=excluded.raw_json,response_sha256=excluded.response_sha256,"
                " fetched_utc=excluded.fetched_utc",
                (
                    txid,
                    str(info.get("network") or "TRON"),
                    str(info.get("from") or ""),
                    str(info.get("to") or ""),
                    info.get("amount"),
                    str(info.get("token") or ""),
                    str(info.get("token_contract") or ""),
                    str(info.get("block_ts") or ""),
                    str(info.get("contract_ret") or ""),
                    1 if info.get("confirmed") else 0,
                    raw_json,
                    digest,
                    now,
                ),
            )
            c.commit()
    except Exception:
        return


def chain_tx_cache_multi_transfer_txids() -> list[str]:
    """Cached transactions whose payload contains more than one TRC20 move.

    GasFree Permit Transfers carry a fee transfer plus the real transfer. An
    older normaliser took the first one (the fee); these are the rows that
    need a re-fetch after the parser fix.
    """
    out: list[str] = []
    for r in connect().execute("SELECT txid, raw_json FROM chain_tx_cache").fetchall():
        try:
            raw = json.loads(r["raw_json"] or "{}")
        except Exception:
            continue
        if len(raw.get("trc20TransferInfo") or []) > 1:
            out.append(str(r["txid"]))
    return out


def chain_sources_for_txids(txids: list[str]) -> list[str]:
    """Distinct crypto payment sources currently attached to these txids."""
    ids = [str(t) for t in (txids or []) if t]
    if not ids:
        return []
    q = ",".join("?" for _ in ids)
    rows = connect().execute(
        f"SELECT DISTINCT source_key FROM payment_transactions"
        f" WHERE rail_type='crypto_wallet' AND txid IN ({q})"
        f" ORDER BY source_key",
        ids,
    ).fetchall()
    return [str(r["source_key"]) for r in rows if r["source_key"]]


def chain_tx_cache_delete(txids: list[str]) -> int:
    """Drop cached transactions so the fixed normaliser re-fetches them."""
    ids = [str(t) for t in (txids or []) if t]
    if not ids:
        return 0
    q = ",".join("?" for _ in ids)
    with _writer_lock:
        c = connect()
        cur = c.execute(f"DELETE FROM chain_tx_cache WHERE txid IN ({q})", ids)
        c.commit()
        return int(cur.rowcount or 0)


def chain_clear_unresolved_tx_fields() -> int:
    """Blank on-chain details on rows that never got a verified transaction.

    A candidate transaction may have been scored but rejected; its amount or
    timestamp must not appear in the officer CSV without a hash.
    """
    with _writer_lock:
        c = connect()
        cur = c.execute(
            "UPDATE payment_transactions SET tx_from='',tx_to='',tx_amount=NULL,"
            "tx_time='',tx_token='',tx_contract_ret='',tx_method='',"
            "tx_confidence=''"
            " WHERE rail_type='crypto_wallet'"
            " AND tx_status IN ('unresolved','ambiguous')"
            " AND (tx_from<>'' OR tx_to<>'' OR tx_amount IS NOT NULL"
            " OR tx_time<>'' OR tx_token<>'' OR tx_contract_ret<>''"
            " OR tx_method<>'' OR tx_confidence<>'')"
        )
        c.commit()
        return int(cur.rowcount or 0)


def chain_known_addresses(limit: int = 20000) -> list[str]:
    """Every case address already known from wallets, rails, or resolutions.

    Used only for deterministic truncated-address recovery (unique prefix /
    suffix match); the caller still verifies checksums before accepting.
    """
    limit = max(1, min(int(limit), 100000))
    c = connect()
    rows = c.execute(
        "SELECT address FROM wallet_hits WHERE address!=''"
        " UNION SELECT resolved_address FROM wallet_resolutions WHERE resolved_address!=''"
        " UNION SELECT rail_id FROM payment_transactions"
        "  WHERE rail_type='crypto_wallet' AND rail_id!=''"
        " LIMIT ?",
        (limit,),
    ).fetchall()
    out: list[str] = []
    seen: set[str] = set()
    for r in rows:
        a = str(r["address"] if "address" in r.keys() else r[0]).strip()
        if a and a not in seen:
            seen.add(a)
            out.append(a)
    return out


def chain_source_bundle(key: str) -> Optional[dict]:
    """Object payload + crypto payment rows for one source key."""
    key = str(key or "").strip()
    if not key:
        return None
    c = connect()
    obj = c.execute(
        "SELECT extract_json,last_modified FROM objects WHERE key=?", (key,)
    ).fetchone()
    rows = [
        dict(r)
        for r in c.execute(
            "SELECT id,rail_type,rail_id,rail_id_raw,network,direction,"
            "last_modified,evidence_json FROM payment_transactions"
            " WHERE source_key=?",
            (key,),
        ).fetchall()
    ]
    payload: dict = {}
    if obj and obj["extract_json"]:
        try:
            parsed = json.loads(obj["extract_json"])
            if isinstance(parsed, dict):
                payload = parsed
        except Exception:
            payload = {}
    if not payload and rows:
        try:
            parsed = json.loads(rows[0].get("evidence_json") or "{}")
            if isinstance(parsed, dict):
                payload = parsed
        except Exception:
            payload = {}
    if not payload and not rows:
        return None
    if obj and obj["last_modified"]:
        payload.setdefault("_last_modified", obj["last_modified"])
    if rows:
        payload.setdefault("_last_modified", rows[0].get("last_modified") or "")
    return {"payload": payload, "rows": rows}


def _chain_row_values(r: dict, checked: str) -> tuple:
    return (
        str(r.get("rail_id") or ""),
        str(r.get("rail_id_raw") or ""),
        str(r.get("wallet_status") or ""),
        str(r.get("txid") or ""),
        str(r.get("txids") or ""),
        str(r.get("tx_status") or ""),
        str(r.get("tx_method") or ""),
        str(r.get("tx_confidence") or ""),
        str(r.get("tx_time") or ""),
        str(r.get("tx_from") or ""),
        str(r.get("tx_to") or ""),
        r.get("tx_amount"),
        str(r.get("tx_token") or ""),
        str(r.get("tx_contract_ret") or ""),
        str(r.get("tx_candidates") or ""),
        checked,
    )


def chain_apply_source_result(source_key: str, result: dict) -> dict:
    """Persist a resolver result: canonical rail, tx details, wallet mapping.

    Additive and idempotent. The original OCR string stays in rail_id_raw /
    wallet_resolutions.raw_address; only the canonical rail_id is rewritten.
    """
    key = str(source_key or "").strip()
    if not key or not isinstance(result, dict):
        return {"updated": 0}
    checked = str(result.get("checked_utc") or "")
    if not checked:
        checked = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    wallets = [w for w in (result.get("wallets") or []) if isinstance(w, dict)]
    rows = [r for r in (result.get("rows") or []) if isinstance(r, dict)]
    links = [x for x in (result.get("links") or []) if isinstance(x, dict)]
    summary = result.get("summary") or {}

    # Canonical address -> row result, for attaching the right tx per wallet.
    row_by_addr = {
        str(r.get("rail_id") or ""): r for r in rows if r.get("rail_id")
    }
    primary_rail = next(
        (str(r.get("rail_id")) for r in rows if r.get("txid")),
        str((rows[0].get("rail_id") if rows else "") or ""),
    )
    tx_addresses = {
        str(r.get("rail_id") or "")
        for r in rows
        if r.get("txid") and r.get("rail_id")
    }

    updated = 0
    with _writer_lock:
        c = connect()
        # 1) Persist raw -> canonical mapping (never deletes evidence).
        now = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        for w in wallets:
            raw = str(w.get("raw") or "").strip()
            addr = str(w.get("address") or "").strip()
            if not raw:
                continue
            status = str(w.get("status") or "")
            resolved_addr = addr if status in ("valid", "repaired", "tronscan_confirmed") else ""
            exists = 1 if (addr and addr in tx_addresses) else 0
            c.execute(
                "INSERT INTO wallet_resolutions(raw_address,resolved_address,"
                "network,method,confidence,status,tronscan_exists,first_seen_utc,checked_utc)"
                " VALUES(?,?,?,?,?,?,?,?,?)"
                " ON CONFLICT(raw_address) DO UPDATE SET"
                " resolved_address=excluded.resolved_address,"
                " network=excluded.network,method=excluded.method,"
                " confidence=excluded.confidence,status=excluded.status,"
                " tronscan_exists=MAX(wallet_resolutions.tronscan_exists,excluded.tronscan_exists),"
                " checked_utc=excluded.checked_utc",
                (
                    raw,
                    resolved_addr,
                    str(w.get("network") or "TRON"),
                    str(w.get("method") or ""),
                    str(w.get("confidence") or ""),
                    status,
                    exists,
                    now,
                    checked,
                ),
            )

        # 2) Canonicalise + stamp each crypto payment row.
        for r in rows:
            rid = r.get("id")
            if rid is None:
                continue
            canonical = str(r.get("rail_id") or "").strip()
            raw = str(r.get("rail_id_raw") or "").strip()
            if canonical:
                dup = c.execute(
                    "SELECT id FROM payment_transactions WHERE source_key=?"
                    " AND rail_type='crypto_wallet' AND rail_id=? AND id<>?",
                    (key, canonical, rid),
                ).fetchone()
                if dup:
                    # OCR variants collapsed to one on-chain address: keep the
                    # canonical row, drop the duplicate, carry the tx onto it.
                    c.execute(
                        "DELETE FROM payment_transactions WHERE id=?", (rid,)
                    )
                    rid = dup["id"]
            c.execute(
                "UPDATE payment_transactions SET rail_id=?,rail_id_raw=?,"
                "wallet_status=?,txid=?,txids=?,tx_status=?,tx_method=?,"
                "tx_confidence=?,tx_time=?,tx_from=?,tx_to=?,tx_amount=?,"
                "tx_token=?,tx_contract_ret=?,tx_candidates=?,chain_checked_utc=?"
                " WHERE id=?",
                _chain_row_values(r, checked) + (rid,),
            )
            updated += 1

        # 2b) Object-only crypto screenshots (no OCR payment row): create the
        # exhibit row so the transaction appears in Officer Query and the CSV.
        created = 0
        if not rows:
            summary_txid = str(summary.get("txid") or "")
            subject = str(summary.get("subject_address") or "")
            if summary_txid and subject:
                src = summary.get("source") or {}
                direction = str(summary.get("direction") or "")
                if direction not in ("deposit", "withdrawal"):
                    direction = (
                        "deposit"
                        if subject == str(summary.get("tx_to") or "")
                        else "withdrawal"
                    )
                raw_subject = str(summary.get("subject_raw") or subject)
                wallet_status = str(
                    summary.get("subject_status") or "tronscan_confirmed"
                )
                tx_status = str(summary.get("tx_status") or "")
                method = str(summary.get("tx_method") or "")
                conf = str(summary.get("tx_confidence") or "")
                token = str(summary.get("tx_token") or "")
                amount = summary.get("tx_amount")
                tx_time = str(summary.get("tx_time") or "")
                tx_from = str(summary.get("tx_from") or "")
                tx_to = str(summary.get("tx_to") or "")
                contract_ret = str(summary.get("tx_contract_ret") or "")
                last_modified = str(src.get("last_modified") or "")
                evidence = {
                    "text_sample": src.get("text_sample") or "",
                    "amount_value": src.get("amount_value"),
                    "amount_currency": src.get("amount_currency") or "",
                    "wallets": wallets,
                    "chain_source": "object_queue",
                }
                role = "beneficiary" if direction == "deposit" else "sender"
                c.execute(
                    "INSERT OR IGNORE INTO payment_transactions("
                    "source_key,direction,gambler,amount_inr,utr,rail_type,rail_id,"
                    "account_holder,ifsc,network,confidence,rail_role,role_confidence,"
                    "last_modified,evidence_json) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (
                        key,
                        direction,
                        "",
                        None,
                        "",
                        "crypto_wallet",
                        subject,
                        "",
                        "",
                        "TRON",
                        f"chain_{tx_status or 'derived'}",
                        role,
                        conf or "medium",
                        last_modified,
                        json.dumps(evidence),
                    ),
                )
                row = c.execute(
                    "SELECT id FROM payment_transactions WHERE source_key=?"
                    " AND rail_type='crypto_wallet' AND rail_id=?",
                    (key, subject),
                ).fetchone()
                if row:
                    c.execute(
                        "UPDATE payment_transactions SET rail_id_raw=?,wallet_status=?,"
                        "txid=?,txids=?,tx_status=?,tx_method=?,tx_confidence=?,"
                        "tx_time=?,tx_from=?,tx_to=?,tx_amount=?,tx_token=?,"
                        "tx_contract_ret=?,tx_candidates=?,chain_checked_utc=? WHERE id=?",
                        (
                            raw_subject,
                            wallet_status,
                            summary_txid,
                            json.dumps([summary_txid]),
                            tx_status,
                            method,
                            conf,
                            tx_time,
                            tx_from,
                            tx_to,
                            amount,
                            token,
                            contract_ret,
                            "[]",
                            checked,
                            row["id"],
                        ),
                    )
                    created += 1
                    if not primary_rail:
                        primary_rail = subject
                    tx_addresses.add(subject)
                # Wallets tab: the recovered subject wallet must be visible.
                c.execute(
                    "INSERT OR IGNORE INTO wallet_hits(address,key,last_modified,"
                    "seen_utc,network,standard,confidence,checksum,kind,amount_inr,"
                    "amount_value,amount_currency,txids,raw_address,wallet_status,"
                    "txid,tx_status,chain_checked_utc)"
                    " VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (
                        subject,
                        key,
                        last_modified,
                        str(src.get("seen_utc") or ""),
                        "TRON",
                        "",
                        conf or "high",
                        "",
                        str(src.get("kind") or ""),
                        None,
                        src.get("amount_value"),
                        str(src.get("amount_currency") or ""),
                        json.dumps([summary_txid]),
                        raw_subject,
                        wallet_status,
                        summary_txid,
                        tx_status,
                        checked,
                    ),
                )

        # 3) Mirror canonical address + chain stamp into wallet_hits.
        wh_rows = c.execute(
            "SELECT rowid AS rid,address,raw_address FROM wallet_hits WHERE key=?",
            (key,),
        ).fetchall()
        for wh in wh_rows:
            raw_addr = str(wh["raw_address"] or wh["address"] or "")
            addr = str(wh["address"] or "")
            res = next(
                (
                    w
                    for w in wallets
                    if str(w.get("raw") or "") == raw_addr
                    or str(w.get("address") or "") == addr
                ),
                None,
            )
            canonical = str((res or {}).get("address") or addr)
            if canonical and canonical != addr:
                dup = c.execute(
                    "SELECT rowid FROM wallet_hits WHERE key=? AND address=?"
                    " AND rowid<>?",
                    (key, canonical, wh["rid"]),
                ).fetchone()
                if dup:
                    c.execute(
                        "DELETE FROM wallet_hits WHERE rowid=?", (wh["rid"],)
                    )
                    continue
            rr = row_by_addr.get(canonical) or {}
            if not rr and summary.get("txid"):
                tx_from = str(summary.get("tx_from") or "")
                tx_to = str(summary.get("tx_to") or "")
                if canonical and canonical in (tx_from, tx_to):
                    rr = {
                        "txid": summary.get("txid") or "",
                        "tx_status": summary.get("tx_status") or "",
                    }
            c.execute(
                "UPDATE wallet_hits SET address=?,raw_address=?,wallet_status=?,"
                "txid=?,tx_status=?,chain_checked_utc=? WHERE rowid=?",
                (
                    canonical,
                    raw_addr,
                    str((res or {}).get("status") or ""),
                    str(rr.get("txid") or ""),
                    str(rr.get("tx_status") or ""),
                    checked,
                    wh["rid"],
                ),
            )

        # 4) Audit every candidate transaction considered for this source.
        for link in links:
            txid = str(link.get("txid") or "").strip().lower()
            if not re.fullmatch(r"[0-9a-f]{64}", txid):
                continue
            c.execute(
                "INSERT OR REPLACE INTO chain_links(source_key,rail_id,txid,"
                "status,method,confidence,match_score,from_address,to_address,"
                "amount,token,block_ts,contract_ret,candidates_json,checked_utc)"
                " VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    key,
                    primary_rail,
                    txid,
                    str(link.get("status") or ""),
                    str(link.get("method") or ""),
                    str(link.get("confidence") or ""),
                    float(link.get("score") or 0),
                    str(link.get("from") or ""),
                    str(link.get("to") or ""),
                    link.get("amount"),
                    str(link.get("token") or ""),
                    str(link.get("block_ts") or ""),
                    str(link.get("contract_ret") or ""),
                    str(link.get("candidates_json") or ""),
                    checked,
                ),
            )
        c.commit()
    return {
        "updated": updated,
        "created": created,
        "tx_status": summary.get("tx_status") or "",
    }


def chain_pending_sources(limit: int = 8) -> list[str]:
    """Newest crypto slip sources not yet checked by the chain resolver."""
    limit = max(1, min(int(limit), 500))
    rows = (
        connect()
        .execute(
            "SELECT source_key FROM payment_transactions"
            " WHERE rail_type='crypto_wallet' AND source_key NOT LIKE 'reference:%'"
            " AND chain_checked_utc=''"
            " GROUP BY source_key ORDER BY MAX(last_modified) DESC LIMIT ?",
            (limit,),
        )
        .fetchall()
    )
    return [r["source_key"] for r in rows]


def chain_pending_wallet_keys(limit: int = 8) -> list[str]:
    """QR/image wallet hits that have no crypto payment row yet."""
    limit = max(1, min(int(limit), 500))
    rows = (
        connect()
        .execute(
            "SELECT key FROM wallet_hits"
            " WHERE address!='' AND chain_checked_utc=''"
            " AND (UPPER(network) LIKE '%TRON%' OR UPPER(network) IN ('TRC20','TRC-20','TRX') OR network='')"
            " AND key NOT IN (SELECT source_key FROM payment_transactions"
            "   WHERE rail_type='crypto_wallet')"
            " GROUP BY key ORDER BY MAX(last_modified) DESC LIMIT ?",
            (limit,),
        )
        .fetchall()
    )
    return [r["key"] for r in rows]


# OCR text markers for an on-chain transaction screenshot. Kept deliberately
# tight: Amazon Pay / UPI receipts also say "Transaction details" and "hash",
# but they never say TRONSCAN or carry a 64-hex txids field.
_CHAIN_OBJECT_WHERE = (
    "extract_json IS NOT NULL AND ("
    " lower(extract_json) LIKE '%tronscan%'"
    " OR extract_json LIKE '%\"txids\"%'"
    " OR (lower(extract_json) LIKE '%hash:%' AND ("
    "   lower(extract_json) LIKE '%usdt%'"
    "   OR lower(extract_json) LIKE '%trx%'"
    "   OR lower(extract_json) LIKE '%tron%'"
    " ))"
    ")"
)


def chain_queue_crypto_objects(batch: int = 5000, max_batches: int = 1) -> dict:
    """Discover crypto-transaction objects that never became payment/wallet rows.

    Scans ``objects.extract_json`` once by rowid cursor and enqueues every
    Tronscan/hash-bearing image. This is the safety net for slips whose wallet
    OCR failed completely (no payment row, no wallet_hit) but whose screenshot
    still shows a transaction hash/block.
    """
    batch = max(1, min(int(batch), 50000))
    max_batches = max(1, min(int(max_batches), 500))
    cursor = 0
    try:
        cursor = int(get_meta("chain_object_cursor", "0") or 0)
    except (TypeError, ValueError):
        cursor = 0
    queued = scanned = 0
    done = False
    now = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    for _ in range(max_batches):
        with _writer_lock:
            c = connect()
            rows = c.execute(
                "SELECT rowid,key,extract_json FROM objects"
                f" WHERE rowid>? AND {_CHAIN_OBJECT_WHERE}"
                " ORDER BY rowid LIMIT ?",
                (cursor, batch),
            ).fetchall()
            if not rows:
                done = True
                c.execute(
                    "INSERT INTO meta(k,v) VALUES('chain_object_cursor',?)"
                    " ON CONFLICT(k) DO UPDATE SET v=excluded.v",
                    (str(cursor),),
                )
                c.commit()
                break
            for r in rows:
                key = str(r["key"] or "")
                if not key:
                    continue
                blob = str(r["extract_json"] or "").lower()
                reason = (
                    "tronscan"
                    if "tronscan" in blob
                    else "txids"
                    if '"txids"' in blob
                    else "hash"
                )
                cur = c.execute(
                    "INSERT OR IGNORE INTO chain_source_queue(key,reason,queued_utc)"
                    " VALUES(?,?,?)",
                    (key, reason, now),
                )
                if cur.rowcount:
                    queued += 1
                scanned += 1
                cursor = int(r["rowid"])
            c.execute(
                "INSERT INTO meta(k,v) VALUES('chain_object_cursor',?)"
                " ON CONFLICT(k) DO UPDATE SET v=excluded.v",
                (str(cursor),),
            )
            c.commit()
        if len(rows) < batch:
            done = True
            break
    return {
        "queued": queued,
        "scanned": scanned,
        "cursor": cursor,
        "done": done,
    }


def chain_pending_queue_keys(limit: int = 8) -> list[str]:
    limit = max(1, min(int(limit), 2000))
    rows = (
        connect()
        .execute(
            "SELECT key FROM chain_source_queue WHERE checked_utc=''"
            " ORDER BY queued_utc DESC LIMIT ?",
            (limit,),
        )
        .fetchall()
    )
    return [r["key"] for r in rows]


def chain_queue_mark_checked(key: str) -> None:
    k = str(key or "").strip()
    if not k:
        return
    with _writer_lock:
        c = connect()
        c.execute(
            "UPDATE chain_source_queue SET checked_utc=? WHERE key=?",
            (time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), k),
        )
        c.commit()


def chain_queue_status() -> dict:
    c = connect()
    row = c.execute(
        "SELECT COUNT(*) total,"
        " SUM(CASE WHEN checked_utc='' THEN 1 ELSE 0 END) pending"
        " FROM chain_source_queue"
    ).fetchone()
    return {
        "total": int(row["total"] or 0),
        "pending": int(row["pending"] or 0),
        "cursor": get_meta("chain_object_cursor", "0"),
    }


def chain_backfill_sources(
    since: str = "", until: str = "", limit: int = 500, pending_only: bool = False
) -> list[str]:
    """All crypto sources in a window (checked or not) for a full backfill."""
    limit = max(1, min(int(limit), 100000))
    since_n = _bound_ts(since, end=False)
    until_n = _bound_ts(until, end=True)
    conds = ["source_key NOT LIKE 'reference:%'"]
    args: list = []
    if pending_only:
        conds.append("chain_checked_utc=''")
    if since_n:
        conds.append("last_modified>=?")
        args.append(since_n)
    if until_n:
        conds.append("last_modified<=?")
        args.append(until_n)
    where = " AND ".join(conds)
    args.append(limit)
    c = connect()
    rows = c.execute(
        "SELECT source_key FROM payment_transactions"
        " WHERE rail_type='crypto_wallet' AND " + where +
        " GROUP BY source_key ORDER BY MAX(last_modified) DESC LIMIT ?",
        args,
    ).fetchall()
    out = [r["source_key"] for r in rows]
    seen = set(out)
    # Wallet-only sources (decoded QR images with no OCR payment row).
    wconds = ["address!=''"]
    wargs: list = []
    if pending_only:
        wconds.append("chain_checked_utc=''")
    if since_n:
        wconds.append("last_modified>=?")
        wargs.append(since_n)
    if until_n:
        wconds.append("last_modified<=?")
        wargs.append(until_n)
    wargs.append(limit)
    for r in c.execute(
        "SELECT key FROM wallet_hits WHERE " + " AND ".join(wconds) +
        " AND (UPPER(network) LIKE '%TRON%' OR UPPER(network) IN ('TRC20','TRC-20','TRX') OR network='')"
        " GROUP BY key ORDER BY MAX(last_modified) DESC LIMIT ?",
        wargs,
    ).fetchall():
        k = r["key"]
        if k not in seen:
            seen.add(k)
            out.append(k)
    return out[:limit]


def chain_recheck_sources(limit: int = 500) -> list[str]:
    """Crypto sources whose previous chain result was not a transaction.

    Used after resolver/OCR fixes and for transient Tronscan failures: a
    checked source that stayed ``unresolved``/``ambiguous`` is retried without
    redoing every verified row.
    """
    limit = max(1, min(int(limit), 100000))
    rows = (
        connect()
        .execute(
            "SELECT source_key FROM payment_transactions"
            " WHERE rail_type='crypto_wallet' AND source_key NOT LIKE 'reference:%'"
            " AND (tx_status IN ('unresolved','ambiguous')"
            "      OR (tx_status='' AND chain_checked_utc!=''))"
            " GROUP BY source_key ORDER BY MAX(last_modified) DESC LIMIT ?",
            (limit,),
        )
        .fetchall()
    )
    return [r["source_key"] for r in rows]


def chain_recheck_queue_keys(limit: int = 500) -> list[str]:
    """Checked queue objects that never produced a transaction row."""
    limit = max(1, min(int(limit), 100000))
    rows = (
        connect()
        .execute(
            "SELECT q.key FROM chain_source_queue q"
            " WHERE q.checked_utc!='' AND NOT EXISTS ("
            "   SELECT 1 FROM payment_transactions p"
            "   WHERE p.source_key=q.key AND p.rail_type='crypto_wallet'"
            "   AND p.txid!='')"
            " ORDER BY q.queued_utc DESC LIMIT ?",
            (limit,),
        )
        .fetchall()
    )
    return [r["key"] for r in rows]


def chain_status_counts() -> dict:
    """Coverage counters for /api/chain/status and the dashboard badge."""
    c = connect()
    pay = c.execute(
        "SELECT COUNT(*) total,"
        " SUM(CASE WHEN chain_checked_utc!='' THEN 1 ELSE 0 END) checked,"
        " SUM(CASE WHEN txid!='' THEN 1 ELSE 0 END) with_tx,"
        " SUM(CASE WHEN wallet_status IN ('valid','repaired') THEN 1 ELSE 0 END) valid_wallet"
        " FROM payment_transactions WHERE rail_type='crypto_wallet'"
    ).fetchone()
    by_status = {
        str(r["tx_status"] or "unchecked"): int(r["n"] or 0)
        for r in c.execute(
            "SELECT tx_status,COUNT(*) n FROM payment_transactions"
            " WHERE rail_type='crypto_wallet' GROUP BY tx_status"
        ).fetchall()
    }
    wh = c.execute(
        "SELECT COUNT(*) total,"
        " SUM(CASE WHEN chain_checked_utc!='' THEN 1 ELSE 0 END) checked,"
        " SUM(CASE WHEN txid!='' THEN 1 ELSE 0 END) with_tx"
        " FROM wallet_hits"
    ).fetchone()
    res = c.execute(
        "SELECT COUNT(*) total,"
        " SUM(CASE WHEN resolved_address!='' AND resolved_address!=raw_address"
        " THEN 1 ELSE 0 END) repaired,"
        " SUM(CASE WHEN tronscan_exists=1 THEN 1 ELSE 0 END) onchain"
        " FROM wallet_resolutions"
    ).fetchone()
    cached = c.execute("SELECT COUNT(*) n FROM chain_tx_cache").fetchone()
    links = c.execute("SELECT COUNT(*) n FROM chain_links").fetchone()
    try:
        queue = chain_queue_status()
    except Exception:
        queue = {"total": 0, "pending": 0, "cursor": "0"}
    return {
        "crypto_rows": int(pay["total"] or 0),
        "crypto_checked": int(pay["checked"] or 0),
        "crypto_with_tx": int(pay["with_tx"] or 0),
        "crypto_valid_wallet": int(pay["valid_wallet"] or 0),
        "by_tx_status": by_status,
        "wallet_hits": int(wh["total"] or 0),
        "wallet_hits_checked": int(wh["checked"] or 0),
        "wallet_hits_with_tx": int(wh["with_tx"] or 0),
        "resolutions": int(res["total"] or 0),
        "resolutions_repaired": int(res["repaired"] or 0),
        "resolutions_onchain": int(res["onchain"] or 0),
        "cached_transactions": int(cached["n"] or 0),
        "audit_links": int(links["n"] or 0),
        "object_queue_total": int(queue.get("total") or 0),
        "object_queue_pending": int(queue.get("pending") or 0),
        "object_scan_cursor": queue.get("cursor") or "0",
    }


def _crypto_image_filters(q="", since="", until="", folder="", direction="both", tx_status="", source="object"):
    where, args = _wallet_hit_window_filters(q=q, since=since, until=until, folder=folder)
    if source == "reference":
        return "0", []
    chain_sql, chain_args = _tx_status_filter(tx_status)
    if chain_sql:
        where += " AND " + chain_sql
        args += list(chain_args)
    sides = {"deposit": ("deposit/", "bankingqr/", "qrcode/"),
             "withdrawal": ("withdrawal/", "payment-proof/", "payment-proofs/")}
    if direction in sides:
        where += " AND (" + " OR ".join("key LIKE ?" for _ in sides[direction]) + ")"
        args += [p + "%" for p in sides[direction]]
    return where, args


def attach_slip_identity(items):
    c = connect()
    by_key = {}
    keys = list({r["source_key"] for r in items if r.get("source_key")})
    for start in range(0, len(keys), 400):
        part = keys[start:start + 400]
        placeholders = ",".join("?" for _ in part)
        for row in c.execute(
            "SELECT o.key,o.etag,o.size,f.sha256,f.pixel_sha256 FROM objects o "
            "LEFT JOIN slip_fingerprints f ON f.source_key=o.key AND f.etag=o.etag AND f.size=o.size "
            f"WHERE o.key IN ({placeholders})", part,
        ):
            by_key[row["key"]] = row
    for item in items:
        meta = by_key.get(item.get("source_key"))
        if meta:
            item.update(object_etag=meta["etag"], object_size=meta["size"],
                        content_sha256=meta["sha256"] or "", pixel_sha256=meta["pixel_sha256"] or "")
    return items


def deduplicated_evidence(items):
    from .slip_dedup import deduplicate_slips
    return deduplicate_slips(attach_slip_identity(items))


def save_slip_fingerprints(rows):
    if not rows:
        return
    with _writer_lock:
        c = connect()
        c.executemany(
            "INSERT OR REPLACE INTO slip_fingerprints(source_key,etag,size,sha256,pixel_sha256,updated_utc) "
            "VALUES (?,?,?,?,?,?)", rows,
        )
        c.commit()


def crypto_slip_candidates(q="", source="object", since="", until="", folder="", direction="both", tx_status="", max_sources=None):
    """Same date/folder/side/status/search scope as crypto address results.

    Union OCR transactions and decoded images by source key, not by wallet.
    Limits raise explicitly; no silently truncated evidence packs.
    """
    where, args = _money_flow_filters(direction=direction, rail_type="crypto_wallet", q=q,
        source=source, since=since, until=until, folder=folder, tx_status=tx_status)
    where += " AND rail_id!=''"
    c = connect()
    by_key = {}
    for row in c.execute(
        "SELECT source_key,direction,gambler,amount_inr,utr,rail_type,rail_id,rail_id_raw,"
        "account_holder,ifsc,network,wallet_status,txid,txids,tx_status,tx_method,tx_confidence,"
        "tx_time,tx_amount,tx_token,tx_from,tx_to,tx_contract_ret,chain_checked_utc,last_modified "
        f"FROM payment_transactions WHERE {where} ORDER BY last_modified DESC,id DESC", args,
    ):
        item = dict(row)
        item.pop("evidence_json", None)
        key = item["source_key"]
        if key not in by_key:
            item.update(origins={"ocr_slip"}, rail_ids=[item["rail_id"]])
            by_key[key] = item
        elif item["rail_id"] not in by_key[key]["rail_ids"]:
            by_key[key]["rail_ids"].append(item["rail_id"])
        if max_sources and len(by_key) > max_sources:
            raise ValueError(f"More than {max_sources} source files match. Select a narrower date range for the PDF.")
    wh_where, wh_args = _crypto_image_filters(q=q, since=since, until=until, folder=folder,
        direction=direction, tx_status=tx_status, source=source)
    for row in c.execute(
        f"SELECT * FROM wallet_hits WHERE {wh_where} ORDER BY last_modified DESC", wh_args,
    ):
        r = dict(row)
        key = r["key"]
        if key in by_key:
            by_key[key]["origins"].add("image_hit")
            if r["address"] not in by_key[key]["rail_ids"]:
                by_key[key]["rail_ids"].append(r["address"])
        else:
            by_key[key] = dict(source_key=key, rail_type="crypto_wallet", rail_id=r["address"],
                rail_ids=[r["address"]], network=r.get("network") or "", txid=r.get("txid") or "",
                tx_status=r.get("tx_status") or "", last_modified=r["last_modified"],
                origins={"image_hit"}, gambler="", amount_inr=None, utr="")
        if max_sources and len(by_key) > max_sources:
            raise ValueError(f"More than {max_sources} source files match. Select a narrower date range for the PDF.")
    if max_sources and len(by_key) > max_sources:
        raise ValueError(f"More than {max_sources} source files match. Select a narrower date range for the PDF.")
    items = list(by_key.values())
    for item in items:
        item.update(folder=_key_top_folder(item["source_key"]), has_object=not item["source_key"].startswith("reference:"))
    return attach_slip_identity(items)


def crypto_deduplicated_flows(limit=200, offset=0, **filters):
    from .slip_dedup import deduplicate_slips
    raw = crypto_slip_candidates(**filters)
    items = deduplicate_slips(raw)
    _attach_seen_utc(items)
    return dict(rows_total=len(items), transactions=len(items), sum_amount_inr=0,
        distinct_rails=len({rail for r in items for rail in r.get("rail_ids", [])}),
        source_files_total=len(raw), duplicates_removed=len(raw) - len(items),
        items=items[offset:offset + limit], top_rails=[])


def crypto_unique_deduplicated(result, **filters):
    """Keep unique-address pagination, but report deduplicated evidence counts."""
    from .slip_dedup import deduplicate_slips
    raw = crypto_slip_candidates(**filters)
    groups = deduplicate_slips(raw)
    per_rail, source_counts = {}, {}
    for row in raw:
        for rail in row.get("rail_ids", [row["rail_id"]]):
            source_counts[rail] = source_counts.get(rail, 0) + 1
    for row in groups:
        for rail in row.get("rail_ids", [row["rail_id"]]):
            per_rail.setdefault(rail, []).append(row)
    for item in result["items"]:
        matches = per_rail.get(item["rail_id"], [])
        item["image_hits"] = sum("image_hit" in r.get("origins", []) for r in matches)
        item["slips"] = sum("ocr_slip" in r.get("origins", []) for r in matches)
        item["duplicate_count"] = max(0, source_counts.get(item["rail_id"], 0) - len(matches))
        if matches:
            item["sample_key"] = matches[0]["source_key"]
    result.update(transactions=sum("ocr_slip" in r.get("origins", []) for r in groups),
        image_hits=sum("image_hit" in r.get("origins", []) for r in groups),
        source_files_total=len(raw), duplicates_removed=len(raw) - len(groups))
    return result
