"""Payment-gateway ledger ingestion (paymentgateway/csv-files/) — CASE-2026-CBTF.

The bucket holds timestamped gateway exports the ledger module never touches:
  deposit_<ts>.csv — gateway settlement rows. Two schema variants observed live:
    v1 (2024): Customer Name,Merchant OrderID,Amount,Mode,Website,Client Name,
               ActionBy,Receipt,Type,Approved At,Created At  (OrderID is 32-hex)
    v2 (2025+): Customer Name,UTR,Amount,Mode,Website,Client Name,ActionBy,
               Receipt,Type,Approved At,Created At            (UTR is 12-digit)
  payout_<ts>.csv — Holder Name,Account Number,IFSC Number,Transaction Id,Amount
  transaction_<ts>.csv — internal test-ledger remarks (inventoried only).

Website = white-label brand attribution (bhavaniexch, 4xexch, newlotus,
lotus365s, reddyid247, 1explay.com, dream365.com, ...). Payout rows are direct
mule bank accounts (holder + account + IFSC).

Test-data rule: some exports are synthetic operator tests (e.g. 2026-07-01
deposit file: 1749 identical Rs.1000 rows, Customer=test*, Client=testclient,
ActionBy=testtrader, sequential fake UTRs). Such rows are flagged is_test=1
and EXCLUDED from officer totals — they are facts about testing, not volume.

Never touches payment_transactions (writes gateway_* only).
"""

import csv
import io
import time

from . import db

MAX_FETCH_BYTES = 15 * 1024 * 1024


def _fnum(v) -> float | None:
    try:
        if v is None or v == "":
            return None
        return float(str(v).replace(",", "").strip())
    except (ValueError, TypeError):
        return None


def _is_12d(s: str) -> bool:
    s = (s or "").strip()
    return len(s) == 12 and s.isdigit()


def _deposit_is_test(r: dict) -> bool:
    cust = (r.get("Customer Name") or "").strip().lower()
    client = (r.get("Client Name") or "").strip()
    action = (r.get("ActionBy") or "").strip()
    return cust.startswith("test") or client == "testclient" or action == "testtrader"


def parse_gateway_deposit(text: str) -> list[dict]:
    """Parse gateway deposit CSV (v1 Merchant OrderID or v2 UTR schema)."""
    if text.startswith("\ufeff"):
        text = text[1:]
    rows = list(csv.DictReader(io.StringIO(text)))
    out = []
    for i, r in enumerate(rows):
        u = (r.get("UTR") or "").strip()
        oid = (r.get("Merchant OrderID") or "").strip()
        utr = u if _is_12d(u) else (oid if _is_12d(oid) else "")
        out.append(
            {
                "row_no": i,
                "kind": "gateway_deposit",
                "customer": (r.get("Customer Name") or "").strip(),
                "amount": _fnum(r.get("Amount")),
                "utr": utr,
                "website": (r.get("Website") or "").strip(),
                "client": (r.get("Client Name") or "").strip(),
                "actionby": (r.get("ActionBy") or "").strip(),
                "receipt": (r.get("Receipt") or "").strip(),
                "holder": "",
                "account": "",
                "ifsc": "",
                "txid": "",
                "is_test": 1 if _deposit_is_test(r) else 0,
                "ts": (r.get("Approved At") or r.get("Created At") or "").strip(),
            }
        )
    return out


def _payout_is_test(r: dict) -> bool:
    holder = (r.get("Holder Name") or "").strip()
    acct = (r.get("Account Number") or "").strip()
    return holder == "Bank Holder Name" or acct == "121212121"


def parse_gateway_payout(text: str) -> list[dict]:
    """Parse gateway payout CSV: Holder Name,Account Number,IFSC Number,Transaction Id,Amount."""
    if text.startswith("\ufeff"):
        text = text[1:]
    rows = list(csv.DictReader(io.StringIO(text)))
    out = []
    for i, r in enumerate(rows):
        out.append(
            {
                "row_no": i,
                "kind": "gateway_payout",
                "customer": "",
                "amount": _fnum(r.get("Amount")),
                "utr": "",
                "website": "",
                "client": "",
                "actionby": "",
                "receipt": "",
                "holder": (r.get("Holder Name") or "").strip(),
                "account": (r.get("Account Number") or "").strip(),
                "ifsc": (r.get("IFSC Number") or "").strip(),
                "txid": (r.get("Transaction Id") or "").strip(),
                "is_test": 1 if _payout_is_test(r) else 0,
                "ts": "",
            }
        )
    return out


def _parse(key: str, data: bytes) -> tuple[str, list[dict]]:
    base = key.split("/", 1)[1] if "/" in key else key
    leaf = base.rsplit("/", 1)[-1]
    text = data.decode("utf-8", errors="replace")
    if leaf.startswith("deposit_") and leaf.endswith(".csv"):
        return "gateway_deposit", parse_gateway_deposit(text)
    if leaf.startswith("payout_") and leaf.endswith(".csv"):
        return "gateway_payout", parse_gateway_payout(text)
    if leaf.startswith("transaction_") and leaf.endswith(".csv"):
        return "gateway_internal", []
    return "other", []


def sync_gateway(store, limit_files: int = 25) -> dict:
    """Index up to limit_files new/changed paymentgateway/csv-files/*.csv. Bounded per call."""
    c = db.connect()
    todo = c.execute(
        "SELECT o.key,o.size,o.etag,o.last_modified FROM objects o"
        " LEFT JOIN gateway_files f ON f.key=o.key"
        " WHERE o.key LIKE 'paymentgateway/csv-files/%' AND o.key LIKE '%.csv'"
        " AND (f.key IS NULL OR f.etag!=o.etag OR f.size!=o.size)"
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
                        "INSERT OR REPLACE INTO gateway_files(key,etag,size,last_modified,kind,rows,test_rows,status,synced_utc)"
                        " VALUES(?,?,?,?,?,?,?,?,?)",
                        (
                            key,
                            r["etag"] or "",
                            r["size"] or 0,
                            r["last_modified"] or "",
                            "oversized",
                            0,
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
                c.execute("DELETE FROM gateway_rows WHERE key=?", (key,))
                c.executemany(
                    "INSERT INTO gateway_rows(key,row_no,kind,customer,amount,utr,website,client,actionby,receipt,"
                    "holder,account,ifsc,txid,is_test,ts) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    [
                        (
                            key,
                            x["row_no"],
                            x["kind"],
                            x["customer"],
                            x["amount"],
                            x["utr"],
                            x["website"],
                            x["client"],
                            x["actionby"],
                            x["receipt"],
                            x["holder"],
                            x["account"],
                            x["ifsc"],
                            x["txid"],
                            x["is_test"],
                            x["ts"],
                        )
                        for x in rows
                    ],
                )
                c.execute(
                    "INSERT OR REPLACE INTO gateway_files(key,etag,size,last_modified,kind,rows,test_rows,status,synced_utc)"
                    " VALUES(?,?,?,?,?,?,?,?,?)",
                    (
                        key,
                        r["etag"] or "",
                        r["size"] or 0,
                        r["last_modified"] or "",
                        kind,
                        len(rows),
                        sum(1 for x in rows if x["is_test"]),
                        "indexed" if rows else "inventoried",
                        now,
                    ),
                )
                done += 1
            except Exception as e:  # noqa: BLE001 — per-file fault isolation
                errors.append(f"{key}: {e}"[:200])
                try:
                    c.execute(
                        "INSERT OR REPLACE INTO gateway_files(key,etag,size,last_modified,kind,rows,test_rows,status,synced_utc)"
                        " VALUES(?,?,?,?,?,?,?,?,?)",
                        (
                            key,
                            r["etag"] or "",
                            r["size"] or 0,
                            r["last_modified"] or "",
                            "error",
                            0,
                            0,
                            "error",
                            now,
                        ),
                    )
                except Exception:
                    pass
        c.commit()
    total = c.execute(
        "SELECT COUNT(*) n FROM objects WHERE key LIKE 'paymentgateway/csv-files/%.csv'"
    ).fetchone()["n"]
    indexed = c.execute(
        "SELECT COUNT(*) n FROM gateway_files WHERE status='indexed'"
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
    """Officer totals: real (non-test) gateway deposits by brand + payout mule accounts."""
    c = db.connect()
    totals = c.execute(
        "SELECT COUNT(*) rows,COALESCE(SUM(amount),0) amount,"
        " COUNT(DISTINCT NULLIF(customer,'')) customers,"
        " COUNT(DISTINCT NULLIF(utr,'')) utrs,"
        " COUNT(DISTINCT NULLIF(website,'')) brands"
        " FROM gateway_rows WHERE kind='gateway_deposit' AND is_test=0"
    ).fetchone()
    test = c.execute(
        "SELECT COUNT(*) rows FROM gateway_rows WHERE is_test=1"
    ).fetchone()
    brands = [
        dict(r)
        for r in c.execute(
            "SELECT website,COUNT(*) txns,COALESCE(SUM(amount),0) amount,"
            " COUNT(DISTINCT NULLIF(customer,'')) customers,"
            " COUNT(DISTINCT NULLIF(utr,'')) utrs"
            " FROM gateway_rows WHERE kind='gateway_deposit' AND is_test=0 AND website!=''"
            " GROUP BY website ORDER BY txns DESC LIMIT 30"
        ).fetchall()
    ]
    payouts = [
        dict(r)
        for r in c.execute(
            "SELECT holder,account,ifsc,COUNT(*) txns,COALESCE(SUM(amount),0) amount,"
            " MAX(txid) sample_txid FROM gateway_rows"
            " WHERE kind='gateway_payout' AND is_test=0 AND account!=''"
            " GROUP BY holder,account,ifsc ORDER BY txns DESC LIMIT 50"
        ).fetchall()
    ]
    payout_tot = c.execute(
        "SELECT COUNT(*) rows,COALESCE(SUM(amount),0) amount,"
        " COUNT(DISTINCT account) accounts FROM gateway_rows"
        " WHERE kind='gateway_payout' AND is_test=0"
    ).fetchone()
    cov = c.execute(
        "SELECT COUNT(DISTINCT g.utr) matched FROM gateway_rows g"
        " WHERE g.utr!='' AND g.is_test=0"
        " AND EXISTS (SELECT 1 FROM payment_transactions p WHERE p.utr=g.utr AND p.utr!='')"
    ).fetchone()
    guts = c.execute(
        "SELECT COUNT(DISTINCT utr) n FROM gateway_rows WHERE utr!='' AND is_test=0"
    ).fetchone()
    files = c.execute(
        "SELECT COUNT(*) AS total, SUM(CASE WHEN status='indexed' THEN 1 ELSE 0 END) AS idx,"
        " MAX(last_modified) AS newest FROM gateway_files"
    ).fetchone()
    bucket_total = c.execute(
        "SELECT COUNT(*) n FROM objects WHERE key LIKE 'paymentgateway/csv-files/%.csv'"
    ).fetchone()
    return {
        "fact_type": "fact",
        "note": "Gateway settlement exports. Test-pattern rows excluded from totals. Payout rows are observed bank accounts, not attributions.",
        "deposits": {
            "rows": totals["rows"],
            "amount": totals["amount"],
            "customers": totals["customers"],
            "utrs": totals["utrs"],
            "brands": totals["brands"],
        },
        "test_rows_excluded": test["rows"],
        "brands": brands,
        "payouts": {
            "rows": payout_tot["rows"],
            "amount": payout_tot["amount"],
            "accounts": payout_tot["accounts"],
        },
        "payout_accounts": payouts,
        "coverage": {"gateway_utrs": guts["n"], "matched_in_flows": cov["matched"]},
        "files": {
            "indexed": files["idx"] or 0,
            "total": files["total"] or 0,
            "bucket_total": bucket_total["n"],
            "newest": files["newest"] or "",
        },
    }
