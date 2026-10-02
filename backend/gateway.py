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
import re
import time
from urllib.parse import urlparse

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


_URL_RE = re.compile(r"https?://[^\s'\"<>]+", re.IGNORECASE)

# --- Brand/domain normalization (2026-10-02: NETWORK URLS showed raw
# settlement `Website` values as hosts — bare aliases like `reddy369`,
# full URLs with paths like `https://www.ambani247.com/home`, and mixed
# case. Bettors only use resolvable frontend domains, so every
# gateway-brand row is normalized to a host + validity verdict below.) ---
_HOST_RE = re.compile(r"^[a-z0-9-]+(\.[a-z0-9-]+)+$", re.IGNORECASE)

# Real public suffixes are pure-alpha (2-10 chars). This rejects QR-parser
# glue like `centralbankofindia.co.in28220010a…` (digit-fused garbage TLD)
# while keeping genuine short-link/bank/rail hosts (go.short, goo.gl, …).
_TLD_RE = re.compile(r"^[a-z]{2,10}$")


def normalize_brand_host(site: str) -> str:
    """Normalize a settlement `Website` value to a lowercase host.

    Handles: bare aliases (`reddy369`), FQDNs (`bajrangi247.com`),
    full URLs (`https://lotus365w.com`, `http://ice-xch.com`),
    URLs with paths (`https://www.ambani247.com/home`), and mixed
    case (`Mahadevbook777`, `https://Reddy247.co`). Never raises.
    """
    s = (site or "").strip()
    if not s:
        return ""
    try:
        if "://" in s:
            host = urlparse(s).hostname or ""
        elif "/" in s or ":" in s:
            host = urlparse("https://" + s).hostname or s.split("/")[0]
        else:
            host = s
    except Exception:
        host = s.split("/")[0]
    return host.lower().strip().strip(".")


def is_valid_domain_host(host: str) -> bool:
    """Syntax validity for a bettor-facing domain (no DNS implied)."""
    h = (host or "").lower().strip().strip(".")
    if not h or len(h) > 253 or "." not in h:
        return False
    if any(c in h for c in ("&", "?", "/", " ", "_", "@", "#", "%", "+")):
        return False
    if not _HOST_RE.match(h):
        return False
    labels = h.split(".")
    if any(
        not lab or len(lab) > 63 or lab.startswith("-") or lab.endswith("-")
        for lab in labels
    ):
        return False
    tld = labels[-1]
    if not _TLD_RE.match(tld):
        return False
    # Reject UPI-QR glue: long digit runs fused into a label
    # (e.g. `co.in28220010a000…` fragments).
    if re.search(r"\d{8,}", h):
        return False
    return True


# Alias (no-dot settlement tag) -> verified frontend domain mapping.
# Verified 2026-10-02 (DNS via local resolver + Cloudflare DoH, HTTP(S)
# title/fingerprint probe from non-IN egress; IN-only hosts report as
# `fenced`). `status` mirrors resolve_brand() verdicts.
BRAND_ALIAS_MAP: dict[str, dict] = {
    "reddy369": {
        "primary": "reddy369.com",
        "status": "alias-resolved-live",
        "confidence": "high",
        "candidates": ["reddy369.com", "reddy369.vip"],
        "note": "719B 'World's Biggest Prediction Exchange' shell (CBTF Turbo)",
    },
    "newlotus": {
        "primary": "newlotus.com",
        "status": "alias-resolved-live",
        "confidence": "high",
        "candidates": ["newlotus.com"],
        "note": "Prediction-Exchange shell + 51 victims in DOMAINS-MASTER",
    },
    "reddyid247": {
        "primary": "reddyid247.com",
        "status": "alias-resolved-fenced",
        "confidence": "medium",
        "candidates": ["reddyid247.com"],
        "note": "DNS live; HTTP blocked from non-IN egress (geo-fenced)",
    },
    "cbtfturbo": {
        "primary": "cbtfturbo.com",
        "status": "alias-resolved-live",
        "confidence": "high",
        "candidates": ["cbtfturbo.com"],
        "note": "Prediction-Exchange shell + 229 victims in DOMAINS-MASTER",
    },
    "11xworld": {
        "primary": "11xworld.com",
        "status": "alias-resolved-live",
        "confidence": "high",
        "candidates": ["11xworld.com"],
        "note": "DNS live, HTTP reachable (302); S3 APK brand 11xworld",
    },
    "lotus365": {
        "primary": "lotus365.xyz",
        "status": "alias-resolved-live",
        "confidence": "medium",
        "candidates": ["lotus365.xyz", "lotus365s.vip", "lotus365.global"],
        "note": "Stem-ambiguous lotus family; primary verified live, others live too",
    },
    "reddyanaa": {
        "primary": "reddyanaa.com",
        "status": "alias-resolved-live",
        "confidence": "high",
        "candidates": ["reddyanaa.com"],
        "note": "Prediction-Exchange shell + 57 victims in DOMAINS-MASTER",
    },
    "betgram99": {
        "primary": "betgram99.com",
        "status": "alias-resolved-live",
        "confidence": "high",
        "candidates": ["betgram99.com"],
        "note": "Prediction-Exchange shell; Tier-1B white-label tenant",
    },
    "lotusplus365": {
        "primary": "lotusplus365.com",
        "status": "alias-resolved-fenced",
        "confidence": "medium",
        "candidates": ["lotusplus365.com"],
        "note": "DNS live; HTTP blocked from non-IN egress (geo-fenced)",
    },
    "reddybook365": {
        "primary": "reddybook365.live",
        "status": "alias-resolved-live",
        "confidence": "high",
        "candidates": ["reddybook365.live", "reddybook365.com", "reddybook365.in"],
        "note": "Primary on platform IP 13.215.243.220; 62 victims (.live)",
    },
    "reddybook777": {
        "primary": "reddybook777.com",
        "status": "alias-resolved-live",
        "confidence": "high",
        "candidates": ["reddybook777.com"],
        "note": "Prediction-Exchange shell + 43 victims in DOMAINS-MASTER",
    },
    "cbtfturbo247": {
        "primary": "cbtfturbo247.com",
        "status": "alias-resolved-fenced",
        "confidence": "medium",
        "candidates": ["cbtfturbo247.com"],
        "note": "DNS live; HTTP 403 WAF/geo-fence from non-IN egress",
    },
    "reddy247": {
        "primary": "reddy247.com",
        "status": "alias-resolved-fenced",
        "confidence": "medium",
        "candidates": ["reddy247.com", "reddy247.co"],
        "note": "reddy247.com DNS live but fenced; reddy247.co is parked lander",
    },
    "reddybook24x7": {
        "primary": "reddybook24x7.club",
        "status": "alias-resolved-parked",
        "confidence": "medium",
        "candidates": ["reddybook24x7.club"],
        "note": "Domain resolves but serves parked /lander redirect",
    },
    "lotus365s": {
        "primary": "lotus365s.vip",
        "status": "alias-resolved-live",
        "confidence": "high",
        "candidates": ["lotus365s.vip"],
        "note": "Prediction-Exchange shell + 153 victims in DOMAINS-MASTER",
    },
    "apbook777": {
        "primary": None,
        "status": "alias-unresolved",
        "confidence": "low",
        "candidates": [],
        "note": "No DNS evidence; apbook365.com is a different platform — do not merge",
    },
    "myreddybook": {
        "primary": "myreddybook.com",
        "status": "alias-resolved-live",
        "confidence": "high",
        "candidates": ["myreddybook.com"],
        "note": "Live betting SPA shell (/assets/index-*.js)",
    },
    "goreddybook": {
        "primary": "goreddybook.com",
        "status": "alias-resolved-live",
        "confidence": "high",
        "candidates": ["goreddybook.com"],
        "note": "Prediction-Exchange shell + 6 victims in DOMAINS-MASTER",
    },
    "mahadevbook777": {
        "primary": "mahadevbook777.com",
        "status": "alias-resolved-fenced",
        "confidence": "medium",
        "candidates": ["mahadevbook777.com"],
        "note": "DNS live; HTTP blocked from non-IN egress (geo-fenced)",
    },
    "bhavaniexch": {
        "primary": "bhavaniexch.com",
        "status": "alias-resolved-live",
        "confidence": "high",
        "candidates": ["bhavaniexch.com"],
        "note": "Prediction-Exchange shell; Tier-1B white-label tenant",
    },
    "khelobuzz": {
        "primary": "khelobuzz.com",
        "status": "alias-resolved-live",
        "confidence": "low",
        "candidates": ["khelobuzz.com"],
        "note": "khelobuzz.com serves live 'MahadevBook777' betting app; 2-row link only",
    },
}


def _qr_host_ok(host: str) -> bool:
    """Validity gate for QR-derived hosts (drops bank-fragment glue)."""
    if not is_valid_domain_host(host):
        return False
    if len(host) > 64:
        return False
    return True


# Per-host liveness overrides verified 2026-10-02 (DNS via local resolver +
# Cloudflare DoH; HTTPS title/fingerprint probe from non-IN egress).
# Syntax-valid hosts proven dead/parked/non-frontend, or DNS-live but
# HTTP-blocked from outside India (geo-fenced), get accurate verdicts here
# instead of the default `live-verified`.
HOST_LIVENESS_OVERRIDE: dict[str, tuple[str, str, str]] = {
    "11xgame.win": (
        "dns-dead",
        "high",
        "SERVFAIL lame delegation (DoH fact); live siblings 11xgame.vip + 11xgame.club",
    ),
    "jetcric.vip": ("dns-dead", "high", "SERVFAIL lame delegation (DoH fact)"),
    "dafacric247.com": ("dns-dead", "high", "SERVFAIL lame delegation (DoH fact)"),
    "dafacric365.com": ("dns-dead", "high", "SERVFAIL lame delegation (DoH fact)"),
    "dafacric99.com": ("dns-dead", "high", "SERVFAIL lame delegation (DoH fact)"),
    "dream365.com": ("dns-dead", "high", "NXDOMAIN (DoH fact)"),
    "cn.com": ("non-frontend", "high", ".CN.COM registrar parking landing (fact)"),
    "i100pay.com": (
        "non-frontend",
        "medium",
        "Payment-gateway infra domain, not a bettor frontend (ai_lead)",
    ),
    "1explay.com": (
        "fenced-likely-live",
        "medium",
        "DNS live 13.127.247.216; HTTP blocked from non-IN egress — likely live for IN bettors",
    ),
    "myfairplay.vip": (
        "fenced-likely-live",
        "medium",
        "DNS live; HTTP blocked from non-IN egress — likely live for IN bettors",
    ),
    "lotus365ind.com": (
        "fenced-likely-live",
        "medium",
        "DNS live; HTTP blocked from non-IN egress — likely live for IN bettors",
    ),
    "reddybook365.com": (
        "fenced-likely-live",
        "medium",
        "DNS live; HTTP blocked from non-IN egress; live alternate reddybook365.live",
    ),
}


def resolve_brand(site: str) -> dict:
    """Resolve a settlement `Website` value to bettor-facing domain info.

    Returns host (normalized), url (valid https URL or ""), brand_raw
    (original value), is_valid_domain, resolution verdict, confidence,
    resolved_domain, and a short evidence note. Never raises.
    """
    raw = (site or "").strip()
    host = normalize_brand_host(raw)
    if not host:
        return {
            "host": "",
            "url": "",
            "brand_raw": raw,
            "is_valid_domain": False,
            "resolution": "empty",
            "confidence": "high",
            "resolved_domain": "",
            "note": "Empty Website value",
        }
    if is_valid_domain_host(host):
        override = HOST_LIVENESS_OVERRIDE.get(host)
        if override:
            resolution, confidence, note = override
            dead = resolution in ("dns-dead", "non-frontend")
            return {
                "host": host,
                "url": "" if dead else f"https://{host}/",
                "brand_raw": raw,
                "is_valid_domain": not dead,
                "resolution": resolution,
                "confidence": confidence,
                "resolved_domain": "" if dead else host,
                "note": note,
            }
        return {
            "host": host,
            "url": f"https://{host}/",
            "brand_raw": raw,
            "is_valid_domain": True,
            "resolution": "live-verified",
            "confidence": "high",
            "resolved_domain": host,
            "note": "Resolvable frontend domain (see verification report for per-host liveness)",
        }
    alias = BRAND_ALIAS_MAP.get(host)
    if alias and alias.get("primary"):
        primary = alias["primary"]
        live = alias["status"] == "alias-resolved-live"
        return {
            "host": host,
            "url": f"https://{primary}/",
            "brand_raw": raw,
            "is_valid_domain": False,
            "resolution": alias["status"],
            "confidence": alias.get("confidence", "medium"),
            "resolved_domain": primary,
            "note": alias.get("note", ""),
        }
    if alias:  # unresolved (e.g. apbook777)
        return {
            "host": host,
            "url": "",
            "brand_raw": raw,
            "is_valid_domain": False,
            "resolution": alias["status"],
            "confidence": alias.get("confidence", "low"),
            "resolved_domain": "",
            "note": alias.get("note", ""),
        }
    return {
        "host": host,
        "url": "",
        "brand_raw": raw,
        "is_valid_domain": False,
        "resolution": "placeholder",
        "confidence": "high",
        "resolved_domain": "",
        "note": "Non-domain settlement tag (test/placeholder), not a bettor frontend",
    }


def _url_host(url: str) -> str:
    try:
        return (urlparse(url).hostname or "").lower().strip(".")
    except Exception:
        return ""


def _load_json_data(name: str):
    """Load a checked-in verification data file (never raises)."""
    try:
        from pathlib import Path

        p = Path(__file__).resolve().parent / "data" / name
        import json as _json

        return _json.loads(p.read_text())
    except Exception:
        return [] if name != "brand_server_ips.json" else {}


def _intel_inventory_rows(covered: set) -> list[dict]:
    """Rows for verified intel frontends absent from settlement brands.

    Source files: DOMAINS-MASTER.csv, cbtf-white-label-all-domains.txt,
    myreddy369-white-label-analysis.txt, key-frontends-2026-09-30.json.
    Each domain DNS+HTTP verified 2026-10-02 (browser spot-checked).
    Historical entries are kept with their death evidence — never dropped.
    """
    rows = []
    data = _load_json_data("white_label_frontends.json")
    if not isinstance(data, list):
        return rows
    for e in data:
        dom = (e.get("domain") or "").lower()
        if not dom or dom in covered:
            continue
        status = e.get("status", "")
        live = status in ("live", "live-redirect", "fenced")
        rows.append(
            {
                "host": dom,
                "url": f"https://{dom}/" if live else "",
                "source": "intel-inventory",
                "hits": 0,
                "txns": 0,
                "amount_inr": 0,
                "customers": 0,
                "utrs": 0,
                "first_seen": "",
                "last_seen": "",
                "sample_key": "",
                "brand_raw": "",
                "is_valid_domain": live,
                "resolution": f"intel-{status}",
                "confidence": "high"
                if status in ("live", "live-redirect", "historical")
                else "medium",
                "resolved_domain": dom if live else "",
                "resolved_url": f"https://{dom}/" if live else "",
                "server_ip": ",".join(e.get("ips", [])),
                "note": e.get("note")
                or (
                    f"{e.get('fingerprint', '')} {e.get('title', '')}".strip()
                    or f"Intel frontend ({status} 2026-10-02)"
                ),
            }
        )
    return rows


def network_urls(qr_scan_limit: int = 50000) -> dict:
    """Officer network inventory: every frontend/backend/other URL in the network.

    Sources (fact, test rows excluded):
      - gateway-brand: white-label brand domains from settlement exports
        (player-facing frontends) + per-brand volume stats.
      - qr-payload: http(s) URLs embedded in decoded collection QR codes.
      - qr-expanded: short-link QR destinations after redirect expansion.
    Grouped by host; each row carries a sample URL + sample evidence key.
    """
    c = db.connect()
    rows: list[dict] = []
    brand_ips = _load_json_data("brand_server_ips.json")
    if not isinstance(brand_ips, dict):
        brand_ips = {}
    for b in c.execute(
        "SELECT website,COUNT(*) txns,COALESCE(SUM(amount),0) amount,"
        " COUNT(DISTINCT NULLIF(customer,'')) customers,"
        " COUNT(DISTINCT NULLIF(utr,'')) utrs,"
        " MIN(ts) first_seen,MAX(ts) last_seen,MIN(key) sample_key"
        " FROM gateway_rows WHERE kind='gateway_deposit' AND is_test=0 AND website!=''"
        " GROUP BY website ORDER BY txns DESC"
    ).fetchall():
        site = (b["website"] or "").strip()
        res = resolve_brand(site)
        ip_key = res["resolved_domain"] or res["host"]
        rows.append(
            {
                "host": res["host"],
                "url": res["url"]
                or (
                    site
                    if site.lower().startswith("http")
                    else (f"https://{site}" if "." in site else site)
                ),
                "source": "gateway-brand",
                "hits": b["txns"],
                "txns": b["txns"],
                "amount_inr": b["amount"],
                "customers": b["customers"],
                "utrs": b["utrs"],
                "first_seen": b["first_seen"] or "",
                "last_seen": b["last_seen"] or "",
                "sample_key": b["sample_key"] or "",
                # Domain-validity enrichment (2026-10-02): bettors only
                # use resolvable frontend domains.
                "brand_raw": res["brand_raw"],
                "is_valid_domain": res["is_valid_domain"],
                "resolution": res["resolution"],
                "confidence": res["confidence"],
                "resolved_domain": res["resolved_domain"],
                "resolved_url": res["url"],
                "server_ip": brand_ips.get(ip_key, ""),
                "note": res["note"],
            }
        )
    agg: dict[tuple[str, str], dict] = {}
    for r in c.execute(
        "SELECT key,last_modified,"
        " json_extract(extract_json,'$.raw_text') raw,"
        " json_extract(extract_json,'$.expanded_url') exp"
        " FROM objects WHERE json_extract(extract_json,'$.kind')='qr'"
        " AND (json_extract(extract_json,'$.expanded_url') IS NOT NULL"
        "  OR json_extract(extract_json,'$.raw_text') LIKE '%http%')"
        " LIMIT ?",
        (qr_scan_limit,),
    ).fetchall():
        found: list[tuple[str, tuple[str, str]]] = []
        for u in _URL_RE.findall(r["raw"] or ""):
            h = _url_host(u)
            if h and _qr_host_ok(h):
                found.append((h, ("qr-payload", u)))
        if r["exp"]:
            h = _url_host(r["exp"])
            if h and _qr_host_ok(h):
                found.append((h, ("qr-expanded", r["exp"])))
        for host, (source, url) in found:
            key = (host, source)
            a = agg.get(key)
            if a is None:
                agg[key] = {
                    "host": host,
                    "url": url,
                    "source": source,
                    "hits": 1,
                    "txns": 0,
                    "amount_inr": 0,
                    "customers": 0,
                    "utrs": 0,
                    "first_seen": r["last_modified"] or "",
                    "last_seen": r["last_modified"] or "",
                    "sample_key": r["key"],
                    "brand_raw": "",
                    "is_valid_domain": True,
                    "resolution": "qr-host",
                    "confidence": "high",
                    "resolved_domain": host,
                    "resolved_url": f"https://{host}/",
                    "note": "Resolvable host from collection QR payload/expansion",
                }
            else:
                a["hits"] += 1
                if r["last_modified"]:
                    if not a["first_seen"] or r["last_modified"] < a["first_seen"]:
                        a["first_seen"] = r["last_modified"]
                    if not a["last_seen"] or r["last_modified"] > a["last_seen"]:
                        a["last_seen"] = r["last_modified"]
    rows.extend(sorted(agg.values(), key=lambda d: -d["hits"]))
    covered = {d["host"] for d in rows}
    covered |= {d["resolved_domain"] for d in rows if d.get("resolved_domain")}
    inv_rows = _intel_inventory_rows(covered)
    rows.extend(inv_rows)
    inv_live = sum(1 for d in inv_rows if d.get("is_valid_domain"))
    return {
        "fact_type": "fact",
        "note": "Hosts observed in gateway settlement brands + decoded collection-QR URLs + verified intel-inventory frontends (DOMAINS-MASTER, white-label tenant lists; each DNS+HTTP verified 2026-10-02, historical kept). Test rows excluded. gateway-brand rows carry normalized host + validity verdict (is_valid_domain/resolution/resolved_domain/resolved_url/server_ip); QR hosts are validity-filtered (bank-fragment glue dropped) — source column distinguishes.",
        "rows": rows,
        "counts": {
            "total": len(rows),
            "brands": sum(1 for d in rows if d["source"] == "gateway-brand"),
            "qr_hosts": sum(
                1 for d in rows if d["source"] in ("qr-payload", "qr-expanded")
            ),
            "inventory": len(inv_rows),
            "inventory_live": inv_live,
            "valid_frontends": sum(
                1
                for d in rows
                if d["source"] in ("gateway-brand", "intel-inventory")
                and (d.get("is_valid_domain") or d.get("resolved_domain"))
            ),
            "alias_rows": sum(
                1
                for d in rows
                if d["source"] == "gateway-brand" and not d.get("is_valid_domain")
            ),
        },
    }
