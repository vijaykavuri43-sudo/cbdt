"""Operator ledger ingestion (csv-files/ XLSX + CSV) — CASE-2026-CBTF.

Reads timestamped admin-panel exports already in the bucket manifest and
normalises them into ledger_files / ledger_rows. Never touches
payment_transactions. Cash rule: cash = Amount - Bonus parsed from Remark
(Remark holds e.g. 'Pay by UTR - <12d> + Bonus (40)'). Raw Amount is chips.
"""

import csv
import io
import re
import time

from . import db

BONUS_RE = re.compile(r"Bonus\s*\(?([\d.,]+)\)?")
UTR_RE = re.compile(r"\b\d{12}\b")
MAX_FETCH_BYTES = 15 * 1024 * 1024


def _kind(key: str) -> str:
    base = key.split("/", 1)[1] if "/" in key else key
    m = re.match(r"^([A-Za-z_]+)", base)
    return (m.group(1) if m else "other").rstrip("_")


def _fnum(v) -> float | None:
    try:
        if v is None or v == "":
            return None
        return float(str(v).replace(",", "").strip())
    except (ValueError, TypeError):
        return None


def parse_transaction_xlsx(data: bytes) -> list[dict]:
    """Parse branch ledger: No., Sender, Receiver, Amount, Type, Remark, Message, parent, Date."""
    import openpyxl

    wb = openpyxl.load_workbook(io.BytesIO(data), read_only=True, data_only=True)
    ws = wb.active
    it = ws.iter_rows(values_only=True)
    next(it, None)  # header
    out = []
    for i, row in enumerate(it):
        if not row or len(row) < 9:
            continue
        typ = str(row[4] or "").strip()
        if typ not in ("Deposit", "Withdraw", "Redeem"):
            continue
        remark = str(row[5] or "")
        amt = _fnum(row[3])
        b = 0.0
        m = BONUS_RE.search(remark)
        if m:
            b = _fnum(m.group(1)) or 0.0
        utrs = UTR_RE.findall(remark)
        out.append(
            {
                "row_no": i,
                "type": typ,
                "sender": str(row[1] or "").strip(),
                "gambler": str(row[2] or "").strip(),
                "amount": amt,
                "bonus": b,
                "cash": (amt - b) if amt is not None else None,
                "utr": utrs[0] if utrs else "",
                "remark": remark[:300],
                "ts": str(row[8] or "").strip(),
            }
        )
    return out


def parse_deposit_csv(text: str) -> list[dict]:
    """Parse deposit request queue: Account,...,Reference Number,Origin,Amount,...,Status."""
    if text.startswith("\ufeff"):
        text = text[1:]
    rows = list(csv.DictReader(io.StringIO(text)))
    out = []
    for i, r in enumerate(rows):
        u = (r.get("Reference Number") or "").strip()
        out.append(
            {
                "row_no": i,
                "type": "DepositRequest",
                "sender": (r.get("Trader") or "").strip(),
                "gambler": (r.get("Account") or "").strip(),
                "amount": _fnum(r.get("Amount")),
                "bonus": 0.0,
                "cash": _fnum(r.get("Amount")),
                "utr": u if (len(u) == 12 and u.isdigit()) else "",
                "remark": f"{(r.get('Origin') or '').strip()}|{(r.get('Status') or '').strip()}"[
                    :300
                ],
                "ts": (r.get("Request Time") or "").strip(),
            }
        )
    return out


def _parse(key: str, data: bytes) -> tuple[str, list[dict]]:
    ext = key.rsplit(".", 1)[-1].lower() if "." in key.rsplit("/", 1)[-1] else ""
    base = key.split("/", 1)[1] if "/" in key else key
    if ext == "xlsx" and base.startswith("transaction_"):
        return "transaction", parse_transaction_xlsx(data)
    if ext == "csv" and base.startswith("deposit_"):
        return "deposit_request", parse_deposit_csv(
            data.decode("utf-8", errors="replace")
        )
    return _kind(key), []


def sync_ledger(store, limit_files: int = 25) -> dict:
    """Index up to limit_files new/changed csv-files/ objects. Bounded per call."""
    c = db.connect()
    todo = c.execute(
        "SELECT o.key,o.size,o.etag,o.last_modified FROM objects o"
        " LEFT JOIN ledger_files f ON f.key=o.key"
        " WHERE o.key LIKE 'csv-files/%' AND (f.key IS NULL OR f.etag!=o.etag OR f.size!=o.size)"
        " ORDER BY o.last_modified DESC LIMIT ?",
        (limit_files,),
    ).fetchall()
    done, skipped, errors = 0, 0, []
    now = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    with db._writer_lock:
        c = db.connect()
        for r in todo:
            key = r["key"]
            try:
                if (r["size"] or 0) > MAX_FETCH_BYTES:
                    c.execute(
                        "INSERT OR REPLACE INTO ledger_files(key,etag,size,last_modified,kind,rows,status,synced_utc)"
                        " VALUES(?,?,?,?,?,?,?,?)",
                        (
                            key,
                            r["etag"] or "",
                            r["size"] or 0,
                            r["last_modified"] or "",
                            _kind(key),
                            0,
                            "oversized-skipped",
                            now,
                        ),
                    )
                    skipped += 1
                    continue
                resp, _ = store.get_object_stream(key)
                data = resp.read()
                resp.close()
                kind, rows = _parse(key, data)
                c.execute("DELETE FROM ledger_rows WHERE key=?", (key,))
                c.executemany(
                    "INSERT INTO ledger_rows(key,row_no,type,sender,gambler,amount,bonus,cash,utr,remark,ts)"
                    " VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                    [
                        (
                            key,
                            x["row_no"],
                            x["type"],
                            x["sender"],
                            x["gambler"],
                            x["amount"],
                            x["bonus"],
                            x["cash"],
                            x["utr"],
                            x["remark"],
                            x["ts"],
                        )
                        for x in rows
                    ],
                )
                c.execute(
                    "INSERT OR REPLACE INTO ledger_files(key,etag,size,last_modified,kind,rows,status,synced_utc)"
                    " VALUES(?,?,?,?,?,?,?,?)",
                    (
                        key,
                        r["etag"] or "",
                        r["size"] or 0,
                        r["last_modified"] or "",
                        kind,
                        len(rows),
                        "indexed" if rows else "inventoried",
                        now,
                    ),
                )
                done += 1
            except Exception as e:  # noqa: BLE001 — per-file fault isolation
                errors.append(f"{key}: {e}"[:200])
                try:
                    c.execute(
                        "INSERT OR REPLACE INTO ledger_files(key,etag,size,last_modified,kind,rows,status,synced_utc)"
                        " VALUES(?,?,?,?,?,?,?,?)",
                        (
                            key,
                            r["etag"] or "",
                            r["size"] or 0,
                            r["last_modified"] or "",
                            _kind(key),
                            0,
                            "error",
                            now,
                        ),
                    )
                except Exception:
                    pass
        c.commit()
    total = c.execute(
        "SELECT COUNT(*) n FROM objects WHERE key LIKE 'csv-files/%'"
    ).fetchone()["n"]
    indexed = c.execute(
        "SELECT COUNT(*) n FROM ledger_files WHERE status='indexed'"
    ).fetchone()["n"]
    return {
        "attempted": len(todo),
        "indexed": done,
        "skipped": skipped,
        "errors": errors[:10],
        "files_indexed": indexed,
        "files_total": total,
        "synced_utc": now,
    }


def summary() -> dict:
    """Officer totals: corrected cash (ex-bonus), per-day, per-sender, coverage."""
    c = db.connect()
    by_type = [
        dict(r)
        for r in c.execute(
            "SELECT type,COUNT(*) txns,COALESCE(SUM(cash),0) cash,COALESCE(SUM(bonus),0) bonus,"
            " COUNT(DISTINCT NULLIF(gambler,'')) gamblers,COUNT(DISTINCT utr) utrs"
            " FROM ledger_rows GROUP BY type"
        ).fetchall()
    ]
    by_day = [
        dict(r)
        for r in c.execute(
            "SELECT substr(f.last_modified,1,10) day,COUNT(DISTINCT f.key) files,"
            " COALESCE(SUM(CASE WHEN r.type='Deposit' THEN r.cash ELSE 0 END),0) cash_in,"
            " COALESCE(SUM(CASE WHEN r.type IN ('Withdraw','Redeem') THEN r.cash ELSE 0 END),0) cash_out"
            " FROM ledger_files f LEFT JOIN ledger_rows r ON r.key=f.key"
            " WHERE f.last_modified>='' GROUP BY day ORDER BY day DESC LIMIT 30"
        ).fetchall()
    ]
    by_sender = [
        dict(r)
        for r in c.execute(
            "SELECT sender,COUNT(*) txns,COALESCE(SUM(cash),0) cash FROM ledger_rows"
            " WHERE type='Deposit' AND sender!='' GROUP BY sender ORDER BY cash DESC LIMIT 20"
        ).fetchall()
    ]
    inv = [
        dict(r)
        for r in c.execute(
            "SELECT CASE WHEN key LIKE 'csv-files/transaction_%' THEN 'transaction'"
            " WHEN key LIKE 'csv-files/data_%' THEN 'data'"
            " WHEN key LIKE 'csv-files/deposit_%' THEN 'deposit'"
            " WHEN key LIKE 'csv-files/users%' THEN 'users'"
            " WHEN key LIKE 'csv-files/bet_list_%' THEN 'bet_list'"
            " WHEN key LIKE 'csv-files/balance_log_%' THEN 'balance'"
            " ELSE 'other' END t,COUNT(*) files,COALESCE(SUM(size),0) bytes,"
            " MAX(last_modified) newest FROM objects WHERE key LIKE 'csv-files/%' GROUP BY t ORDER BY files DESC"
        ).fetchall()
    ]
    cov = c.execute(
        "SELECT COUNT(DISTINCT l.utr) matched FROM ledger_rows l"
        " WHERE l.utr!='' AND EXISTS (SELECT 1 FROM payment_transactions p WHERE p.utr=l.utr)"
    ).fetchone()
    luts = c.execute(
        "SELECT COUNT(DISTINCT utr) n FROM ledger_rows WHERE utr!=''"
    ).fetchone()
    files = c.execute(
        "SELECT COUNT(*) AS total, SUM(CASE WHEN status='indexed' THEN 1 ELSE 0 END) AS idx FROM ledger_files"
    ).fetchone()
    return {
        "fact_type": "fact",
        "note": "cash = Amount − Bonus from Remark. Raw Amount is chips, never cash.",
        "by_type": by_type,
        "by_day": by_day,
        "by_sender": by_sender,
        "inventory": inv,
        "coverage": {"ledger_utrs": luts["n"], "matched_in_flows": cov["matched"]},
        "files": {"indexed": files["idx"] or 0, "total": files["total"] or 0},
    }
