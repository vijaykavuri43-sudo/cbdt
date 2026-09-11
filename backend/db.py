"""SQLite manifest of all bucket objects (WAL, per-thread connections, writer lock)."""

import json
import csv
import math
import re
import sqlite3
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Collection, Optional

from . import config

MAX_AMOUNT_INR = config.MAX_AMOUNT_INR


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


_writer_lock = threading.Lock()
_local = threading.local()


def connect() -> sqlite3.Connection:
    conn = getattr(_local, "conn", None)
    if conn is None:
        config.DB_PATH.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(str(config.DB_PATH), timeout=30.0)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        conn.execute("PRAGMA busy_timeout=30000")
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
        conn.executescript(SCHEMA)
        _migrate(conn)
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
        with _writer_lock:
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
    ):
        if name not in payment_cols:
            conn.execute(f"ALTER TABLE payment_transactions ADD COLUMN {name} {ddl}")
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


def repair_non_iso_timestamps() -> dict:
    """One-time-ish repair: rewrite non-ISO last_modified rows to ISO-8601 UTC.

    The GCS mirror's custom metadata historically carried RFC-1123 HTTP-dates
    ('Wed, 31 Jul 2024 21:45:23 GMT'). Lexicographic -modified sorting then put
    every RFC row above every ISO row, so 'newest-first' feeds showed 2024
    objects on top. Runs at every backend startup (cheap when nothing to fix:
    the scan is a single LIKE-indexed count). Idempotent; unparseable values
    are left untouched and reported, never dropped.
    """
    with _writer_lock:
        c = connect()
        rows = c.execute(
            "SELECT key, last_modified FROM objects "
            "WHERE last_modified != '' AND last_modified NOT LIKE '____-__-__T__:__:__Z'"
        ).fetchall()
        if not rows:
            return {"checked": 0, "normalized": 0, "unparseable": 0}
        updates: list[tuple[str, str]] = []
        unparseable = 0
        for r in rows:
            fixed = normalize_object_ts(r["last_modified"])
            if fixed != r["last_modified"] and fixed.startswith("20"):
                updates.append((fixed, r["key"]))
            else:
                unparseable += 1
        if updates:
            c.executemany("UPDATE objects SET last_modified=? WHERE key=?", updates)
            c.execute(
                "INSERT INTO meta(k,v) VALUES('ts_normalize_utc',?) "
                "ON CONFLICT(k) DO UPDATE SET v=excluded.v",
                (time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),),
            )
            c.commit()
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
    with _writer_lock:
        c = connect()
        cur = c.executemany(
            "INSERT OR IGNORE INTO objects(key,top_prefix,size,last_modified,etag,seen_utc,ext)"
            " VALUES(?,?,?,?,?,?,?)",
            data,
        )
        inserted = cur.rowcount if cur.rowcount and cur.rowcount > 0 else 0
        # Preserve first-seen time and completed extraction when content is
        # unchanged. An overwritten key is a new evidentiary object: refresh
        # metadata and put it back on the enrichment queue.
        c.executemany(
            "UPDATE objects SET top_prefix=?,size=?,last_modified=?,etag=?,ext=?,"
            " enriched=0,extract_json=NULL,enrich_attempts=0,enrich_error=NULL,enrich_retry_utc=NULL,enriched_utc=NULL"
            " WHERE key=? AND (size<>? OR etag<>? OR last_modified<>?)",
            [
                (top, size, lm, etag, ext, key, size, etag, lm)
                for key, top, size, lm, etag, _seen, ext in data
            ],
        )
        c.commit()
        return inserted


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
    with _writer_lock:
        c = connect()
        c.execute("DELETE FROM prefix_stats_cache")
        c.execute(
            "INSERT INTO prefix_stats_cache(p,n,s,newest,refreshed_utc)"
            " SELECT top_prefix,COUNT(*),SUM(size),MAX(last_modified),"
            " strftime('%Y-%m-%dT%H:%M:%SZ','now') FROM objects GROUP BY top_prefix"
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
    if since:
        if len(since) == 10:
            since = since + "T00:00:00Z"
        conds.append("last_modified >= ?")
        args.append(since)
    where = ("WHERE " + " AND ".join(conds)) if conds else ""
    cache_key = (prefix, q, tuple(exts or []), since, flat)
    total = _cached_list_total(cache_key, where, list(args))
    conn = connect()
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
            done += 1
        c.commit()
    return done


def mark_enrichment_error(key: str, error: str):
    """Retry transient fetch failures with bounded exponential backoff."""
    with _writer_lock:
        c = connect()
        row = c.execute(
            "SELECT enrich_attempts FROM objects WHERE key=?", (key,)
        ).fetchone()
        attempts = int(row["enrich_attempts"] or 0) + 1 if row else 1
        delay = min(3600, 30 * (2 ** min(attempts, 7)))
        retry = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() + delay))
        c.execute(
            "UPDATE objects SET enriched=?,enrich_attempts=?,enrich_error=?,enrich_retry_utc=?,enriched_utc=? WHERE key=?",
            (
                1 if attempts >= 5 else 0,
                attempts,
                error[:500],
                retry,
                time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
                if attempts >= 5
                else None,
                key,
            ),
        )
        c.commit()


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
    """
    where = (
        "(enriched=0 AND extract_json IS NULL) "
        "AND (enrich_retry_utc IS NULL "
        "OR enrich_retry_utc <= strftime('%Y-%m-%dT%H:%M:%SZ','now')) "
        f"AND {_IMG_CLAUSE} "
        "AND top_prefix IN ('deposit/','withdrawal/','payment/')"
    )
    c = connect()
    fresh_cut = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() - 3600))
    head = [
        r["key"]
        for r in c.execute(
            f"SELECT key FROM objects WHERE {where} AND seen_utc >= ? "
            "ORDER BY seen_utc DESC LIMIT ?",
            (fresh_cut, limit),
        ).fetchall()
    ]
    if len(head) >= limit:
        return head
    fill = [
        r["key"]
        for r in c.execute(
            f"SELECT key FROM objects WHERE {where} LIMIT ?",
            (limit,),
        ).fetchall()
    ]
    seen = set(head)
    return head + [k for k in fill if k not in seen][: limit - len(head)]


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

    Picks pending (a) ALL bankingqr/qrcode/paymentgateway images and
    (b) hint-named chat_file images, newest first. Bounded to tens of
    thousands of rows — never the full 287K chat backlog at once.
    Two-phase like pending_enrichment: fresh head via idx_obj_seen range
    scan (~ms), bulk fill unsorted (a full ORDER BY over the filtered set
    cost ~5s on the 1.2M-row manifest).
    """
    where = (
        "(enriched=0 AND extract_json IS NULL) "
        "AND (enrich_retry_utc IS NULL "
        "OR enrich_retry_utc <= strftime('%Y-%m-%dT%H:%M:%SZ','now')) "
        f"AND {_IMG_CLAUSE} "
        "AND (top_prefix IN ('bankingqr/','qrcode/','paymentgateway/') "
        f"OR (top_prefix='chat_file/' AND ({_WALLET_HINT_LIKES}))) "
    )
    c = connect()
    fresh_cut = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() - 3600))
    head = [
        r["key"]
        for r in c.execute(
            f"SELECT key FROM objects WHERE {where} AND seen_utc >= ? "
            "ORDER BY seen_utc DESC LIMIT ?",
            (fresh_cut, limit),
        ).fetchall()
    ]
    if len(head) >= limit:
        return head
    fill = [
        r["key"]
        for r in c.execute(
            f"SELECT key FROM objects WHERE {where} LIMIT ?",
            (limit,),
        ).fetchall()
    ]
    seen = set(head)
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


def backfill_ext(batch: int = 20000, max_batches: int = 0) -> int:
    """Fill ext for rows inserted before the column existed.

    Runs in a background thread at startup; each batch is one indexed UPDATE.
    Returns number of rows stamped. max_batches=0 -> until done.
    """
    total = 0
    batches = 0
    while True:
        with _writer_lock:
            c = connect()
            rows = c.execute(
                "SELECT key FROM objects WHERE ext = '' AND key LIKE '%.%' AND key NOT LIKE '%/' LIMIT ?",
                (batch,),
            ).fetchall()
            if not rows:
                break
            data = [(file_ext(r["key"]), r["key"]) for r in rows]
            c.executemany("UPDATE objects SET ext=? WHERE key=?", data)
            c.commit()
        total += len(rows)
        batches += 1
        if max_batches and batches >= max_batches:
            break
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
    FROM objects
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
    """Deposit+withdrawal slip totals in ONE JSON scan     (stats endpoint was
    running the same 1M-row json_extract scan twice)."""
    rows = (
        connect()
        .execute(
            "SELECT json_extract(extract_json,'$.kind') k,COUNT(*) c,"
            " SUM(CASE WHEN json_extract(extract_json,'$.amount_inr') BETWEEN 0.01 AND ?"
            " THEN json_extract(extract_json,'$.amount_inr') END) amt"
            " FROM objects WHERE extract_json IS NOT NULL"
            " AND json_extract(extract_json,'$.kind') IN ('deposit','withdrawal')"
            " GROUP BY k",
            (MAX_AMOUNT_INR,),
        )
        .fetchall()
    )
    out = {
        "deposit": {"total": 0, "sum_amount_inr": 0},
        "withdrawal": {"total": 0, "sum_amount_inr": 0},
    }
    for r in rows:
        if r["k"] in out:
            out[r["k"]] = {"total": r["c"], "sum_amount_inr": r["amt"] or 0}
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
                "rail_type": "upi",
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
                    "rail_type": "bank_account",
                    "rail_id": rail_id,
                    "account_holder": str(account.get("holder") or holder),
                    "ifsc": str(account.get("ifsc") or payload.get("ifsc") or ""),
                    "rail_role": account.get("role", "unknown"),
                    "role_confidence": account.get("role_confidence", "low"),
                }
            )
    for wallet in relevant(payload.get("wallets") or []):
        rail_id = str(wallet.get("address") or "").strip()
        if rail_id:
            rails.append(
                {
                    "rail_type": "crypto_wallet",
                    "rail_id": rail_id,
                    "network": str(
                        wallet.get("network") or wallet.get("standard") or ""
                    ),
                    "account_holder": holder,
                    "rail_role": wallet.get("role", "unknown"),
                    "role_confidence": wallet.get("role_confidence", "low"),
                }
            )
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

    c.execute("DELETE FROM payment_transactions WHERE source_key=?", (key,))
    evidence_json = json.dumps(payload, ensure_ascii=False)
    for rail in rails:
        c.execute(
            "INSERT OR REPLACE INTO payment_transactions("
            "source_key,direction,gambler,amount_inr,utr,rail_type,rail_id,account_holder,ifsc,network,confidence,rail_role,role_confidence,last_modified,evidence_json"
            ") VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                key,
                direction,
                gambler,
                amount,
                utr,
                rail["rail_type"],
                rail["rail_id"],
                rail.get("account_holder", ""),
                rail.get("ifsc", ""),
                rail.get("network", ""),
                str(payload.get("confidence") or ""),
                rail.get("rail_role", "unknown"),
                rail.get("role_confidence", "low"),
                last_modified,
                evidence_json,
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


def sanitize_stored_amounts() -> dict:
    """One-time repair of amounts already persisted by pre-fix extraction.

    The QR `am=` path had no plausibility bound, so UTR/reference values got
    stored as amounts and inflated the Overview totals. We null those in
    place (keeping the raw value flagged for audit), rebuild the payment rows,
    and re-materialize the money-flow summaries.
    """
    cleared = 0
    with _writer_lock:
        c = connect()
        rows = c.execute(
            "SELECT key, extract_json FROM objects WHERE extract_json IS NOT NULL"
            " AND json_extract(extract_json,'$.kind') IN ('deposit','withdrawal')"
            " AND json_extract(extract_json,'$.amount_inr') IS NOT NULL"
            " AND NOT (json_extract(extract_json,'$.amount_inr') BETWEEN 0.01 AND ?)",
            (MAX_AMOUNT_INR,),
        ).fetchall()
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
            c.execute(
                "UPDATE objects SET extract_json=? WHERE key=?",
                (json.dumps(p, ensure_ascii=False), r["key"]),
            )
            _replace_payment_rows(c, r["key"], p)
            cleared += 1
        c.commit()
    totals = refresh_payment_summaries()
    return {"impossible_amounts_cleared": cleared, **totals}


def repair_twin_artifacts(dry_run: bool = True, limit: int = 20000) -> dict:
    """Batch repair persisted twin-misread amounts (₹ eaten into leading digit).

    Suspects: tesseract 5-digit amounts at medium/low confidence. The stored
    text_sample is only the first 400 chars, so the twin signature (X repeated
    + shorter sibling Y) must be visible there; undecidable rows are counted
    as need_reocr (re-OCR when Vision quota returns) and left untouched.
    2026-09-11: range extended to 6 digits — the lakh-eaten case (₹12,000 →
    '212,000', officer-verified) repairs via the same signature.
    """
    from .enrich import detect_twin_artifact  # lazy: enrich imports db

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
        " AND (json_extract(o.extract_json,'$.confidence') IS NULL"
        "  OR json_extract(o.extract_json,'$.confidence') IN ('low','medium'))"
        " AND json_extract(o.extract_json,'$.twin_artifact') IS NULL"
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
        hit = detect_twin_artifact(p.get("text_sample") or "", p.get("amount_inr"))
        if not hit:
            need_reocr += 1
            continue
        repaired += 1
        if len(samples) < 10:
            samples.append(
                {"key": r["key"], "from": hit["artifact"], "to": hit["true_amount"]}
            )
        if dry_run:
            continue
        p["amount_tesseract_raw"] = hit["artifact"]
        p["amount_inr"] = hit["true_amount"]
        p["twin_artifact"] = {**hit, "repaired_utc": now, "repaired_from": "batch"}
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


def _malformed_evidence(sample: str, stored: float) -> Optional[dict]:
    """Point at the culprit token for a suspect stored amount.

    Returns {rule, evidence} when the digit string matching `stored` is found
    in the text sample inside a known-decoy context, else None (undecidable —
    needs a Vision re-read of the pixels).
    """
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
        if not hit:
            need_reocr += 1
            continue
        repaired += 1
        if len(samples) < 15:
            samples.append({"key": r["key"], "from": stored, **hit})
        if dry_run:
            continue
        p["amount_tesseract_raw"] = stored
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


def repair_txid_wallets(dry_run: bool = True, limit: int = 20000) -> dict:
    """Batch-remove stored transaction-id fragments kept as crypto wallets.

    Verified 2026-09-11: a UPI 'Transaction ID …NEF54¢3985c…' fragment was
    stored as a BITCOIN wallet and a truncated-display '…111bcedb…' tail as
    another — txids are evidence, never wallets. Drops a stored wallet only
    with positive evidence (txid/ref label gap, ellipsis truncation, long-hex
    run, or failed TRON base58check); otherwise counts need_reocr. Wallets
    live only inside extract_json (wallet_feed/wallet_stats read it live),
    so no payment-row rebuild is needed. Mirrors repair_twin_artifacts
    (two-phase decide/write).
    """
    from .enrich import (  # lazy: enrich imports db
        _after_ellipsis,
        _in_long_hex_run,
        _labeled_as_txid,
        _labeled_as_txid_loose,
        _tron_checksum_ok,
    )

    scanned = repaired = need_reocr = dropped_total = 0
    samples: list[dict] = []
    now = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    c = connect()
    rows = c.execute(
        "SELECT o.key AS key, o.extract_json AS extract_json FROM objects o"
        " WHERE o.extract_json IS NOT NULL"
        " AND json_array_length(o.extract_json,'$.wallets') > 0"
        " AND json_extract(o.extract_json,'$.txid_wallets_repaired') IS NULL"
        " LIMIT ?",
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
        undecided = False
        for w in wallets:
            addr = str(w.get("address") or "")
            if not addr:
                continue
            reason = None
            if (
                w.get("network") == "TRON"
                and w.get("checksum") != "valid"
                and not _tron_checksum_ok(addr)
            ):
                reason = "tron_checksum_invalid"
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
        if not dropped:
            # Nothing to remove, but mark scanned so rescan skips settled rows.
            p["txid_wallets_repaired"] = {
                "repaired_utc": now,
                "repaired_from": "batch",
                "dropped": [],
                "kept": len(kept),
            }
            fixes.append((r["key"], p))
            continue
        repaired += 1
        dropped_total += len(dropped)
        if len(samples) < 15:
            samples.append({"key": r["key"], "dropped": dropped})
        if dry_run:
            continue
        p["wallets"] = kept
        p["txid_wallets_repaired"] = {
            "repaired_utc": now,
            "repaired_from": "batch",
            "dropped": dropped,
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
            wc.commit()
    return {
        "scanned": scanned,
        "repaired": repaired,
        "need_reocr": need_reocr,
        "wallets_dropped": dropped_total,
        "dry_run": dry_run,
        "samples": samples,
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


def money_flows(
    direction: str = "deposit",
    rail_type: str = "",
    q: str = "",
    limit: int = 200,
    offset: int = 0,
    source: str = "",
    since: str = "",
) -> dict:
    conds = ["direction=?"]
    args: list = [direction]
    if rail_type:
        conds.append("rail_type=?")
        args.append(rail_type)
    if source == "object":
        conds.append("source_key NOT LIKE 'reference:%'")
    elif source == "reference":
        conds.append("source_key LIKE 'reference:%'")
    if since:
        # UTC ISO cutoff, e.g. last-24h briefing window. last_modified is ISO Z.
        conds.append("last_modified>=?")
        args.append(since)
    if q:
        like = "%" + q.replace("%", r"\%").replace("_", r"\_") + "%"
        conds.append(
            "(gambler LIKE ? ESCAPE '\\' OR utr LIKE ? ESCAPE '\\' OR rail_id LIKE ? ESCAPE '\\' OR account_holder LIKE ? ESCAPE '\\' OR ifsc LIKE ? ESCAPE '\\')"
        )
        args.extend([like] * 5)
    where = " AND ".join(conds)
    c = connect()
    if q:
        # Exact-identifier fast path (officer pastes a full VPA / account /
        # UTR / holder / IFSC): equality hits idx_pay_* instead of a 5-column
        # %LIKE% full scan over ~105K rows. Falls back to LIKE below when
        # nothing matches exactly (incl. case mismatches — LIKE stays ASCII
        # case-insensitive, so results are unchanged, just slower).
        hit = c.execute(
            "SELECT 1 FROM payment_transactions WHERE direction=? AND "
            "(rail_id=? OR gambler=? OR utr=? OR account_holder=? OR ifsc=?) LIMIT 1",
            (direction, q, q, q, q, q),
        ).fetchone()
        if hit:
            conds.append(
                "(rail_id=? OR gambler=? OR utr=? OR account_holder=? OR ifsc=?)"
            )
            args.extend([q] * 5)
            where = " AND ".join(conds)
    materialized = None
    if not q and not source and not since:
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
        f"SELECT source_key,direction,gambler,amount_inr,utr,rail_type,rail_id,account_holder,ifsc,network,confidence,rail_role,role_confidence,last_modified,evidence_json "
        f"FROM payment_transactions WHERE {where} ORDER BY last_modified DESC,id DESC LIMIT ? OFFSET ?",
        args + [limit, offset],
    ).fetchall()
    total_rows = materialized["rows_total"] if materialized else coverage["rows_total"]
    src_new = c.execute(
        "SELECT MAX(last_modified) m FROM payment_transactions "
        "WHERE direction=? AND source_key NOT LIKE 'reference:%'",
        (direction,),
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
    return {
        "direction": direction,
        "source": source,
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
        "summary_refreshed_utc": materialized["refreshed_utc"]
        if materialized
        else None,
        "top_rails": [dict(r) for r in top],
        "items": items,
    }


def mule_rails(
    rail_type: str = "",
    q: str = "",
    limit: int = 200,
    offset: int = 0,
    source: str = "",
    new_since: str = "",
) -> dict:
    """Aggregate observed gambler↔rail flows for analyst triage.

    The result intentionally calls these candidate rails. Criminal attribution is
    an analyst conclusion requiring corroboration beyond a single identifier.
    """
    conds = ["rail_id!=''", "rail_type!='unknown'"]
    args: list = []
    if source == "object":
        conds.append("source_key NOT LIKE 'reference:%'")
    elif source == "reference":
        conds.append("source_key LIKE 'reference:%'")
    if rail_type:
        conds.append("rail_type=?")
        args.append(rail_type)
    if q:
        like = "%" + q.replace("%", r"\%").replace("_", r"\_") + "%"
        conds.append(
            "(rail_id LIKE ? ESCAPE '\\' OR account_holder LIKE ? ESCAPE '\\' "
            "OR ifsc LIKE ? ESCAPE '\\' OR network LIKE ? ESCAPE '\\' OR gambler LIKE ? ESCAPE '\\')"
        )
        args.extend([like] * 5)
    where = " AND ".join(conds)
    aggregate = f"""
        SELECT rail_type,rail_id,
          GROUP_CONCAT(DISTINCT NULLIF(account_holder,'')) account_holders,
          GROUP_CONCAT(DISTINCT NULLIF(ifsc,'')) ifscs,
          GROUP_CONCAT(DISTINCT NULLIF(network,'')) networks,
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
        FROM payment_transactions WHERE {where}
        GROUP BY rail_type,rail_id
        {("HAVING MIN(last_modified)>=?" if new_since else "")}
    """
    qargs = args + ([new_since] if new_since else [])
    c = connect()
    # One grouped scan supplies rows and totals. The old implementation ran the
    # same aggregate three times, causing avoidable multi-second API latency.
    all_items = [dict(row) for row in c.execute(aggregate, qargs).fetchall()]
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
    }
    return {
        "fact_type": "fact",
        "assessment_fact_type": "analyst_conclusion",
        "scope_note": "Observed links are evidence leads; mule/criminal attribution requires corroboration.",
        "total": len(all_items),
        "summary": summary,
        "items": all_items[offset : offset + limit],
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


def upi_feed(
    limit: int = 300, offset: int = 0, exclude: Collection[str] = ()
) -> list[dict]:
    """Mule-side VPAs only: collection QR images + deposit-slip recipients
    (money paid TO this VPA). Withdrawal-slip VPAs are excluded — those are
    gambler payout accounts, not mules."""
    xclause, xargs = _vpa_exclude_clause(exclude)
    rows = (
        connect()
        .execute(
            "SELECT key,last_modified,seen_utc,extract_json FROM objects"
            " WHERE extract_json IS NOT NULL AND json_extract(extract_json,'$.kind') IN ('qr','deposit')"
            " AND json_extract(extract_json,'$.vpa') IS NOT NULL"
            + _vpa_junk_clause()
            + xclause
            + " ORDER BY last_modified DESC LIMIT ? OFFSET ?",
            (*xargs, limit, offset),
        )
        .fetchall()
    )
    return [
        {
            "key": r["key"],
            "last_modified": r["last_modified"],
            "seen_utc": r["seen_utc"],
            **(json.loads(r["extract_json"]) if r["extract_json"] else {}),
        }
        for r in rows
    ]


def upi_distinct(
    limit: int = 300, offset: int = 0, exclude: Collection[str] = ()
) -> list[dict]:
    """One row per unique VPA (case-insensitive): slip count, summed amount,
    first/last seen, plus the newest slip's key/payee as the sample evidence.
    Mule-side only (collection QRs + deposit-slip recipients; withdrawal-slip
    VPAs are gambler payout accounts, excluded). Origin tells which evidence
    kinds a VPA appears in. Single-pass window query — no per-VPA lookups."""
    xclause, xargs = _vpa_exclude_clause(exclude)
    rows = (
        connect()
        .execute(
            """
        SELECT vpa, slips, total_inr, with_amount, first_seen, last_seen,
               key, payee_name, amount_inr, last_modified, origin
        FROM (
          SELECT LOWER(json_extract(extract_json,'$.vpa')) AS vpa,
                 key, last_modified,
                 json_extract(extract_json,'$.payee_name') AS payee_name,
                 CAST(json_extract(extract_json,'$.amount_inr') AS REAL) AS amount_inr,
                 CASE WHEN MAX(CASE WHEN json_extract(extract_json,'$.kind')='qr' THEN 1 ELSE 0 END) OVER (
                   PARTITION BY LOWER(json_extract(extract_json,'$.vpa'))) = 1
                      AND MAX(CASE WHEN json_extract(extract_json,'$.kind')='deposit' THEN 1 ELSE 0 END) OVER (
                   PARTITION BY LOWER(json_extract(extract_json,'$.vpa'))) = 1
                  THEN 'qr+deposit'
                  WHEN MAX(CASE WHEN json_extract(extract_json,'$.kind')='qr' THEN 1 ELSE 0 END) OVER (
                   PARTITION BY LOWER(json_extract(extract_json,'$.vpa'))) = 1
                  THEN 'qr' ELSE 'deposit' END AS origin,
                 ROW_NUMBER() OVER (
                   PARTITION BY LOWER(json_extract(extract_json,'$.vpa'))
                   ORDER BY last_modified DESC) AS rn,
                 COUNT(*) OVER (
                   PARTITION BY LOWER(json_extract(extract_json,'$.vpa'))) AS slips,
                 SUM(CAST(json_extract(extract_json,'$.amount_inr') AS REAL)) OVER (
                   PARTITION BY LOWER(json_extract(extract_json,'$.vpa'))) AS total_inr,
                 SUM(CASE WHEN json_extract(extract_json,'$.amount_inr') IS NOT NULL
                          THEN 1 ELSE 0 END) OVER (
                   PARTITION BY LOWER(json_extract(extract_json,'$.vpa'))) AS with_amount,
                 MIN(last_modified) OVER (
                   PARTITION BY LOWER(json_extract(extract_json,'$.vpa'))) AS first_seen,
                 MAX(last_modified) OVER (
                   PARTITION BY LOWER(json_extract(extract_json,'$.vpa'))) AS last_seen
            FROM objects
            WHERE extract_json IS NOT NULL
              AND json_extract(extract_json,'$.kind') IN ('qr','deposit')
              AND json_extract(extract_json,'$.vpa') IS NOT NULL
              """
            + _vpa_junk_clause()
            + xclause
            + """
          ) WHERE rn = 1
        ORDER BY last_seen DESC LIMIT ? OFFSET ?
        """,
            (*xargs, limit, offset),
        )
        .fetchall()
    )
    return [dict(r) for r in rows]


def upi_counts(exclude: Collection[str] = (), which: str = "both") -> dict:
    """Exact totals backing UPI-tab pagination (same filters as the queries).

    2026-09-11: the tab shows only newest-500 without this — officers could
    not reach the other ~10k VPAs. feed_total counts slip rows, distinct_total
    counts unique VPAs; both honor the NEW-only exclusion. `which` limits the
    full-table scans (each is seconds on 1.5M rows): "feed", "distinct", "both".
    """
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
    if which in ("both", "feed"):
        feed_total = c.execute("SELECT COUNT(*) " + base, xargs).fetchone()[0]
    if which in ("both", "distinct"):
        distinct_total = c.execute(
            "SELECT COUNT(DISTINCT LOWER(json_extract(extract_json,'$.vpa'))) " + base,
            xargs,
        ).fetchone()[0]
    return {"feed_total": feed_total, "distinct_total": distinct_total}


def wallet_feed(limit: int = 300) -> list[dict]:
    """Every enriched object whose payload contains crypto wallet addresses.

    Uses json_each over the wallets[] array; newest first. Each item carries the
    source object key so the officer can open the exact screenshot/QR.
    """
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
        ORDER BY o.last_modified DESC LIMIT ?
        """,
            (limit,),
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


def enrichment_progress() -> dict:
    r = (
        connect()
        .execute(
            "SELECT COUNT(*) total, SUM(enriched) done, "
            "SUM(CASE WHEN extract_json IS NOT NULL THEN 1 ELSE 0 END) with_payload, "
            "SUM(CASE WHEN enrich_error IS NOT NULL THEN 1 ELSE 0 END) errors, "
            "SUM(CASE WHEN enriched_utc >= strftime('%Y-%m-%dT%H:%M:%SZ','now','-1 hour') THEN 1 ELSE 0 END) processed_last_hour "
            "FROM objects"
        )
        .fetchone()
    )
    pend = pending_enrichment_count()
    eligible = (r["done"] or 0) + pend
    coverage = round(((r["done"] or 0) / eligible) * 100, 2) if eligible else 100.0
    hourly = r["processed_last_hour"] or 0
    eta_hours = round(pend / hourly, 1) if hourly else None
    return {
        "total": r["total"],
        "enriched_done": r["done"] or 0,
        "with_payload": r["with_payload"] or 0,
        "pending_now": pend,
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
