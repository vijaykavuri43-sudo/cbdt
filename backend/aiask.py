"""AI investigator assistant: Gemini answers over live case data (read-only).

Officers ask in plain language; this module gathers bounded, read-only
context from the SQLite manifest + payment index (+ one GCS/S3 object at
most for "show/describe this image" questions) and has Gemini answer with
cited sources. Safety rails:

- read-only: only SELECT queries + single object fetch, never writes/deletes
- gated: POST /api/ask sits behind the same access-key Depends(auth)
- budgeted: separate small daily cap (S3LM_AIASK_DAILY_CAP, default 500) +
  per-minute rate limit, independent of the OCR Vision budget
- bounded: question <= 2000 chars, context <= ~20KB, answer <= 1024 tokens
- the API key never leaves the server (never echoed in answers/logs)
"""

import re
import threading
import time
from collections import deque
from typing import Any, Optional

from . import config, db

_lock = threading.Lock()
_day = ""
_used_today = 0
_consec_fail = 0
_cooldown_until = 0.0
_stats = {"calls": 0, "ok": 0, "errors": 0, "capped": 0, "limited": 0, "last_error": ""}
_minute_hits: deque = deque()
_overview_cache: Optional[tuple[float, str, list]] = None
_ov_lock = threading.Lock()
_OVERVIEW_TTL = max(
    30.0,
    float(__import__("os").environ.get("S3LM_AIASK_OVERVIEW_TTL", "600") or 600),
)
_GREETING_RE = re.compile(
    r"^(hi|hello|hey|hiya|yo|thanks|thank you|ok|okay|"
    r"good (morning|afternoon|evening))[!.\s]*$",
    re.I,
)

# Officer Q&A deliberately uses gemini-2.5-flash, NOT the enrich pair
# (3.8-flash + lite fallback): free-tier quota is 20 req/day/MODEL, and the
# enrich workers burn the 3.8/lite pools. 2.5-flash text verified 200 OK
# 2026-09-03. S3LM_AI_MODEL overrides.
AI_MODEL = (
    __import__("os").environ.get("S3LM_AI_MODEL", "gemini-2.5-flash").strip()
    or "gemini-2.5-flash"
)
AIASK_DAILY_CAP = max(
    0, int(__import__("os").environ.get("S3LM_AIASK_DAILY_CAP", "500"))
)
AIASK_PER_MIN = max(1, int(__import__("os").environ.get("S3LM_AIASK_PER_MIN", "10")))


def aiask_stats() -> dict:
    with _lock:
        d = dict(_stats)
        d.update(
            {
                "used_today": _used_today,
                "day_utc": _day,
                "cap": AIASK_DAILY_CAP,
                "model": AI_MODEL,
                "groq_model": (
                    f"groq:{config.GROQ_MODEL}" if config.GROQ_ENABLED else ""
                ),
            }
        )
    return d


def _claim() -> Optional[str]:
    """Per-minute rate limit + daily budget + breaker. None = proceed."""
    global _day, _used_today
    now = time.monotonic()
    today = time.strftime("%Y-%m-%d", time.gmtime())
    with _lock:
        if _day != today:
            _day, _used_today = today, 0
        while _minute_hits and now - _minute_hits[0] > 60.0:
            _minute_hits.popleft()
        if len(_minute_hits) >= AIASK_PER_MIN:
            _stats["limited"] += 1
            return "rate-limited: wait a minute and retry"
        if now < _cooldown_until:
            return "breaker-cooldown"
        if _used_today >= AIASK_DAILY_CAP:
            _stats["capped"] += 1
            return "daily-budget-exhausted"
        _minute_hits.append(now)
        _used_today += 1
        _stats["calls"] += 1
    return None


def _note(ok: bool, err: str = ""):
    global _consec_fail, _cooldown_until
    with _lock:
        if ok:
            _consec_fail = 0
            _stats["ok"] += 1
        elif err.startswith("429"):
            # Quota, not breakage: don't burn the breaker, just back off and
            # surface the scope (free-tier 20/day/model, 2026-09-03).
            _consec_fail = 0
            _stats["errors"] += 1
            _stats["last_error"] = err[:300]
            _cooldown_until = max(_cooldown_until, time.monotonic() + 300.0)
        else:
            _consec_fail += 1
            _stats["errors"] += 1
            _stats["last_error"] = err[:200]
            if _consec_fail >= 4:
                _cooldown_until = time.monotonic() + 600.0
                _consec_fail = 0


# ---------- read-only context gatherers (all bounded) ----------


def _ctx_overview() -> tuple[str, list]:
    """Cached whole-bucket overview (heavy: wallets, prefix + money scans).

    The first build costs 20-30s on a busy host, so warm it at startup and
    reuse it for S3LM_AIASK_OVERVIEW_TTL seconds. Greetings never touch it.
    """
    global _overview_cache
    now = time.monotonic()
    with _ov_lock:
        c = _overview_cache
    if c and now - c[0] < _OVERVIEW_TTL:
        return c[1], c[2]
    txt, src = _build_overview()
    with _ov_lock:
        _overview_cache = (now, txt, src)
    return txt, src


def warm() -> None:
    """Build the overview cache off the request path."""
    try:
        _ctx_overview()
    except Exception:
        pass


def _build_overview() -> tuple[str, list]:
    e = db.enrichment_progress()
    try:
        w = db.wallet_stats()
    except Exception:
        w = {}
    try:
        n_pay = db.payment_row_count()
    except Exception:
        n_pay = "?"
    try:
        prefixes = db.prefix_stats()[:16]
        ptxt = "; ".join(
            f"{r.get('p') or '(root)'}={int(r.get('n') or 0):,}" for r in prefixes
        )
    except Exception:
        ptxt = ""
    try:
        dep = db.money_flows(direction="deposit", source="object", limit=1)
        wd = db.money_flows(direction="withdrawal", source="object", limit=1)
        money = (
            f"Deposits (object slips): {dep.get('transactions')} slips · "
            f"₹{dep.get('sum_amount_inr')} · {dep.get('distinct_rails')} rails. "
            f"Withdrawals: {wd.get('transactions')} slips · ₹{wd.get('sum_amount_inr')} · "
            f"{wd.get('distinct_rails')} rails."
        )
    except Exception:
        money = ""
    w_hits = w.get("wallet_hits") if isinstance(w, dict) else None
    w_dist = w.get("distinct_wallets") if isinstance(w, dict) else None
    w_net = ""
    if isinstance(w, dict) and w.get("by_network"):
        w_net = "; ".join(
            f"{n.get('network')}={n.get('count')}" for n in (w.get("by_network") or [])[:8]
        )
    txt = (
        f"CASE-2026-CBTF GCS mirror (whole evidence bucket). Indexed objects: {e.get('total')}; "
        f"enriched: {e.get('enriched_done')} ({e.get('coverage_pct')}%); "
        f"pending OCR: {e.get('pending_now')}; slip backlog: {e.get('slip_backlog')}. "
        f"Payment rows: {n_pay}. Distinct crypto wallets: {w_dist} (hits {w_hits}"
        f"{'; ' + w_net if w_net else ''}). {money} "
        f"Top folders (file counts): {ptxt}."
    )
    return txt, [{"kind": "overview", "ref": "/api/status"}]


def _ctx_money(q: str) -> tuple[str, list]:
    parts, sources = [], []
    for direction in ("deposit", "withdrawal"):
        try:
            f = db.money_flows(direction=direction, source="object", limit=6)
        except Exception:
            continue
        rows = f.get("items") or []
        sample = [
            {
                "rail": r.get("rail_id"),
                "type": r.get("rail_type"),
                "amount": r.get("amount_inr"),
                "key": r.get("source_key"),
            }
            for r in rows[:6]
        ]
        parts.append(
            f"{direction}: slips={f.get('transactions')} amount_inr={f.get('sum_amount_inr')} "
            f"unique_rails={f.get('distinct_rails')} sample={sample}"
        )
        sources.append({"kind": "money", "ref": f"/api/flows?direction={direction}"})
    # NOTE: rail detail moved to _ctx_upi_list (real handles). money_flows'
    # top-rail summary + a second mule_rails aggregate here doubled the
    # heaviest GROUP BY scans on every money question (2026-09-18).
    return "\n".join(parts) or "No money-flow data yet.", sources


def _inr(n) -> str:
    """Indian digit grouping: 12345678 -> 1,23,45,678."""
    try:
        s = str(int(round(float(n))))
    except Exception:
        return str(n)
    neg = s.startswith("-")
    if neg:
        s = s[1:]
    if len(s) <= 3:
        return ("-" if neg else "") + s
    tail, head = s[-3:], s[:-3]
    groups = []
    while len(head) > 2:
        groups.insert(0, head[-2:])
        head = head[:-2]
    if head:
        groups.insert(0, head)
    return ("-" if neg else "") + ",".join(groups) + "," + tail


def _ist_day_bounds_utc(days_back: int = 0, span_days: int = 1) -> tuple[str, str]:
    """IST calendar window -> UTC ISO bounds for last_modified compare.

    days_back=0,span=1 = today IST; days_back=1,span=1 = yesterday IST.
    """
    from datetime import datetime as _dt, timedelta as _td

    ist_now = _dt.utcnow() + _td(hours=5, minutes=30)
    start_ist = _dt(ist_now.year, ist_now.month, ist_now.day) - _td(days=days_back)
    start_utc = start_ist - _td(hours=5, minutes=30)
    end_utc = start_utc + _td(days=span_days)
    f = lambda d: d.strftime("%Y-%m-%dT%H:%M:%S")
    return f(start_utc) + "Z", f(end_utc) + "Z"


_CALC_TRIGGERS = (
    "total",
    "sum",
    "average",
    "avg",
    "top",
    "biggest",
    "largest",
    "how much",
    "count",
    "per day",
    "today",
    "yesterday",
    "week",
    "month",
    "above",
    "over ",
    "more than",
    "calculate",
    "calculation",
    "highest",
)


def _parse_window(ql: str) -> tuple[str, tuple[str, str] | None]:
    """Detect an IST date window in the question. Returns (label, (utc_lo, utc_hi))."""
    import re as _re
    from datetime import datetime as _dt, timedelta as _td

    m = _re.search(r"last (\d{1,3}) days?", ql)
    if m:
        n = max(1, min(365, int(m.group(1))))
        hi = _ist_day_bounds_utc(0, 1)[1]
        lo = _ist_day_bounds_utc(n - 1, 1)[0]
        return f"last {n} days (IST)", (lo, hi)
    if "today" in ql:
        lo, hi = _ist_day_bounds_utc(0, 1)
        return ("today (IST)", (lo, hi))
    if "yesterday" in ql:
        lo, hi = _ist_day_bounds_utc(1, 1)
        return ("yesterday (IST)", (lo, hi))
    if "this month" in ql:
        ist_now = _dt.utcnow() + _td(hours=5, minutes=30)
        lo = _ist_day_bounds_utc(ist_now.day - 1, 1)[0]
        hi = _ist_day_bounds_utc(0, 1)[1]
        return ("this month so far (IST)", (lo, hi))
    m = _re.search(r"(20\d{2}-\d{2}-\d{2})", ql)
    if m:
        try:
            y, mo, d = map(int, m.group(1).split("-"))
            base = _dt(y, mo, d) - _td(hours=5, minutes=30)
            f = lambda x: x.strftime("%Y-%m-%dT%H:%M:%S") + "Z"
            return (m.group(1) + " (IST)", (f(base), f(base + _td(days=1))))
        except Exception:
            pass
    return ("all time", None)


def _ctx_calc(q: str) -> tuple[str, list]:
    """Deterministic payment math over payment_transactions (exact SQL, IST windows).

    The model MUST quote COMPUTED numbers verbatim — it never re-derives them.
    """
    import re as _re

    ql = q.lower()
    label, window = _parse_window(ql)
    _MONEY_WORDS = (
        "deposit",
        "withdraw",
        "payment",
        "upi",
        "utr",
        "vpa",
        "mule",
        "rail",
        "transaction",
        "amount",
        "inr",
        "₹",
        "rs ",
    )
    # Fire on explicit calc words, OR on a money question with an IST window
    # ("deposits in the last 7 days?" has no calc verb but needs exact math).
    if not any(w in ql for w in _CALC_TRIGGERS) and not (
        window and any(w in ql for w in _MONEY_WORDS)
    ):
        return "", []
    wclause, args = ("", [])
    if window:
        wclause = "AND last_modified>=? AND last_modified<?"
        args = [window[0], window[1]]
    bound = config.MAX_AMOUNT_INR
    parts = [f"WINDOW: {label}"]
    sources = []
    try:
        con = db.connect()
        for direction in ("deposit", "withdrawal"):
            r = con.execute(
                "SELECT COUNT(*) n, COALESCE(SUM(amount_inr),0) s, "
                "COALESCE(AVG(amount_inr),0) a, COALESCE(MAX(amount_inr),0) m "
                f"FROM payment_transactions WHERE direction=? AND amount_inr>0 "
                f"AND amount_inr<=? {wclause}",
                [direction, bound, *args],
            ).fetchone()
            parts.append(
                f"COMPUTED {direction}s [{label}]: count={r['n']:,} "
                f"total=₹{_inr(r['s'])} avg=₹{_inr(r['a'])} max=₹{_inr(r['m'])}"
            )
            sources.append(
                {"kind": "computed", "ref": f"payment_transactions:{direction}:{label}"}
            )
        # top rails by volume in window
        rows = con.execute(
            "SELECT rail_type, rail_id, COUNT(*) n, SUM(amount_inr) s "
            "FROM payment_transactions WHERE amount_inr>0 AND amount_inr<=? "
            f"{wclause} GROUP BY rail_type, rail_id ORDER BY s DESC LIMIT 10",
            [bound, *args],
        ).fetchall()
        if rows:
            tops = "; ".join(
                f"{r['rail_type']}:{r['rail_id'] or '?'} = ₹{_inr(r['s'])} ({r['n']} txns)"
                for r in rows
            )
            parts.append(f"COMPUTED top rails [{label}]: {tops}")
        # threshold count: "above ₹50000" / "over 10000"
        m = _re.search(r"(?:above|over|more than|>=?)\s*₹?\s*([\d,]+)", ql)
        if m:
            try:
                thr = float(m.group(1).replace(",", ""))
                for direction in ("deposit", "withdrawal"):
                    c = con.execute(
                        "SELECT COUNT(*) n, COALESCE(SUM(amount_inr),0) s "
                        "FROM payment_transactions WHERE direction=? AND amount_inr>=? "
                        f"AND amount_inr<=? {wclause}",
                        [direction, thr, bound, *args],
                    ).fetchone()
                    parts.append(
                        f"COMPUTED {direction}s >= ₹{_inr(thr)} [{label}]: "
                        f"count={c['n']:,} total=₹{_inr(c['s'])}"
                    )
            except Exception:
                pass
    except Exception as e:
        return f"CALC unavailable ({str(e)[:120]} — use Money Flows page).", []
    return "\n".join(parts), sources


_ID_PATTERNS = [
    ("utr", re.compile(r"\b\d{12}\b")),
    ("vpa", re.compile(r"[a-zA-Z0-9._-]{2,}@[a-zA-Z]{2,}")),
    (
        "wallet",
        re.compile(
            r"\b(T[1-9A-HJ-NP-Za-km-z]{25,34}|0x[a-fA-F0-9]{40}|[13][a-km-zA-HJ-NP-Z1-9]{25,34})\b"
        ),
    ),
]


def _ctx_identifier(q: str) -> tuple[str, list]:
    found = []
    for kind, rx in _ID_PATTERNS:
        m = rx.search(q)
        if m:
            found.append((kind, m.group(0)))
    if not found:
        return "", []
    parts, sources = [], []
    for kind, val in found[:3]:
        # manifest keys mentioning it
        try:
            hits = db.search(q=val, limit=10)
        except Exception:
            hits = []
        keys = [h.get("key") for h in (hits or []) if h.get("key")][:10]
        # extracted-payload mentions (read-only LIKE over extract_json)
        pay = []
        try:
            rows = (
                db.connect()
                .execute(
                    "SELECT key, substr(extract_json,1,400) j FROM objects "
                    "WHERE enriched=1 AND extract_json LIKE ? LIMIT 10",
                    (f"%{val}%",),
                )
                .fetchall()
            )
            pay = [{"key": r["key"], "snippet": r["j"]} for r in rows]
        except Exception:
            pass
        parts.append(
            f"{kind} {val}: {len(keys)} matching files, "
            f"{len(pay)} enriched payload mentions. "
            f"files={keys[:5]} payloads={pay[:5]}"
        )
        sources.append({"kind": kind, "ref": val})
    return "\n".join(parts), sources


_IMG_EXT = (".jpg", ".jpeg", ".png", ".webp", ".gif", ".bmp", ".jfif", ".avif")


# ---------- actual-data gatherers: real addresses/handles/rows, not counts ---
# (2026-09-18: "list all the wallets" only quoted wallet_stats aggregates —
# officers need the verbatim identifiers. These return exact DB output.)


_WALLET_WORDS = (
    "wallet",
    "crypto",
    "usdt",
    "tron",
    "btc",
    "bitcoin",
    "eth",
    "address",
    "addresses",
    "coin",
    "token",
    "blockchain",
    "binance",
    "tether",
    "exchange",
    "deposit address",
    "trust",
)


def _ctx_wallet_list(ql: str) -> tuple[str, list]:
    """Every distinct wallet address (up to 200, by hits) for list questions."""
    if not any(w in ql for w in _WALLET_WORDS):
        return "", []
    try:
        rows = db.wallet_distinct(limit=200, sort="hits")
    except Exception as e:
        return (
            f"Wallet list unavailable ({str(e)[:120]} — use Crypto Wallets page).",
            [],
        )
    if not rows:
        return "No wallets extracted yet.", [
            {"kind": "wallets", "ref": "/api/wallets/distinct"}
        ]
    lines = [
        f"{r['address']} | {r.get('network') or '?'} | hits={r.get('hits')} "
        f"| last_seen={r.get('last_seen') or '?'} | sample={r.get('key') or '?'}"
        for r in rows
    ]
    txt = (
        f"WALLET ADDRESSES (all {len(rows)} distinct, most-hit first — "
        "quote these verbatim, never invent):\n" + "\n".join(lines)
    )
    return txt, [{"kind": "wallets", "ref": "/api/wallets/distinct"}]


_UPI_WORDS = (
    "upi",
    "vpa",
    "mule",
    "rail",
    "handle",
    "account",
    "ifsc",
    "payee",
    "bank",
    "transfer",
    "imps",
    "neft",
    "rtgs",
    "qr",
    "collect",
    "payout",
    "settle",
    "payment",
    "gateway",
)


_PEOPLE_WORDS = (
    "agent",
    "operator",
    "gambler",
    "player",
    "customer",
    "holder",
    "sender",
    "beneficiary",
    "beneficiar",
    "receiver",
    "who",
    "person",
    "people",
    "users",
)


def _ctx_people(ql: str) -> tuple[str, list]:
    """Top gamblers + account holders by observed volume (agents/operators
    have no dedicated table — these are the closest person-level leads)."""
    if not any(w in ql for w in _PEOPLE_WORDS):
        return "", []
    parts = []
    try:
        con = db.connect()
        rows = con.execute(
            "SELECT gambler, COUNT(*) n, "
            "COALESCE(SUM(CASE WHEN direction='deposit' THEN amount_inr ELSE 0 END),0) dep, "
            "COALESCE(SUM(CASE WHEN direction='withdrawal' THEN amount_inr ELSE 0 END),0) wd "
            "FROM payment_transactions WHERE gambler!='' "
            "GROUP BY gambler ORDER BY dep + wd DESC LIMIT 20"
        ).fetchall()
        if rows:
            parts.append(
                "TOP GAMBLERS (by volume): "
                + "; ".join(
                    f"{r['gambler']} = dep₹{_inr(r['dep'])} + wd₹{_inr(r['wd'])} ({r['n']} txns)"
                    for r in rows
                )
            )
        rows = con.execute(
            "SELECT account_holder, rail_type, COUNT(DISTINCT rail_id) rails, "
            "COUNT(*) n FROM payment_transactions WHERE account_holder!='' "
            "GROUP BY account_holder, rail_type ORDER BY n DESC LIMIT 20"
        ).fetchall()
        if rows:
            parts.append(
                "TOP ACCOUNT HOLDERS: "
                + "; ".join(
                    f"{r['account_holder']} [{r['rail_type']}] "
                    f"({r['rails']} rails, {r['n']} rows)"
                    for r in rows
                )
            )
    except Exception as e:
        return (
            f"People lists unavailable ({str(e)[:120]} — use UPI / Mules page).",
            [],
        )
    if not parts:
        return "No gambler/holder data yet.", [
            {"kind": "people", "ref": "/api/mule-rails"}
        ]
    return "\n".join(parts), [{"kind": "people", "ref": "/api/mule-rails"}]


def _ctx_upi_list(ql: str) -> tuple[str, list]:
    """Top candidate rails with real handles/IDs (UPI/VPA/bank/crypto)."""
    if not any(w in ql for w in _UPI_WORDS):
        return "", []
    try:
        res = db.mule_rails(limit=60)
    except Exception as e:
        return (
            f"Rail list unavailable ({str(e)[:120]} — use UPI / Mules page).",
            [],
        )
    items = res.get("items") or []
    if not items:
        return "No rails observed yet.", [{"kind": "rails", "ref": "/api/mule-rails"}]
    lines = [
        f"{r.get('rail_type')}:{r.get('rail_id')} | txns={r.get('transactions')} "
        f"| dep=₹{_inr(r.get('deposit_amount_inr') or 0)} "
        f"| wd=₹{_inr(r.get('withdrawal_amount_inr') or 0)} "
        f"| gamblers={r.get('gamblers')} "
        f"| holders={r.get('account_holders') or '?'}"
        for r in items
    ]
    txt = (
        f"CANDIDATE RAILS (top {len(lines)} of {res.get('total', '?')} — "
        "quote handles verbatim; these are leads, not proof):\n" + "\n".join(lines)
    )
    return txt, [{"kind": "rails", "ref": "/api/mule-rails"}]


_LEDGER_WORDS = (
    "ledger",
    "xls",
    "xlsx",
    "sheet",
    "sender",
    "gambler",
    "excel",
    "report",
    "statement",
    "transaction_",
    "data_",
    "bet_list",
    "balance",
)


# Every bucket prefix is itself a trigger ("apk files?", "chat images?"),
# with and without the slash, space- or dash-separated.
# Spaced aliases for compound stems ("payment gateway" has no _/- to split).
_PREFIX_ALIASES = {
    "payment gateway": "paymentgateway/",
    "qr code": "qrcode/",
    "banking qr": "bankingqr/",
    "chat files": "chat_file/",
    "csv files": "csv-files/",
}


def _prefix_triggers() -> tuple[str, ...]:
    out = []
    for p in config.DEFAULT_PREFIXES:
        stem = p.rstrip("/").lower()
        out.append(stem)
        out.append(stem + "/")
        spaced = stem.replace("_", " ").replace("-", " ")
        if spaced != stem:
            out.append(spaced)
    out.extend(_PREFIX_ALIASES)
    return tuple(out)


_PREFIX_TRIGGERS = _prefix_triggers()


# Tiny TTL caches: ledger.summary aggregates + live-grep scans are the two
# slowest gatherers; repeated officer questions reuse them for 5 minutes.
_cache: dict = {}
_CACHE_TTL = 300.0


def _cache_get(key: str):
    import time as _t

    hit = _cache.get(key)
    if hit and _t.monotonic() - hit[0] < _CACHE_TTL:
        return hit[1]
    return None


def _cache_put(key: str, value):
    import time as _t

    if len(_cache) > 64:
        _cache.clear()
    _cache[key] = (_t.monotonic(), value)


def _ctx_ledger(ql: str) -> tuple[str, list]:
    """Ledger XLS/CSV export totals: per-type cash, top senders, coverage."""
    if not any(w in ql for w in _LEDGER_WORDS):
        return "", []
    hit = _cache_get("ledger:summary")
    if hit is not None:
        txt, sources = hit
        return txt + " (cached ≤5 min)", sources
    try:
        from . import ledger

        s = ledger.summary()
    except Exception as e:
        return (
            f"Ledger summary unavailable ({str(e)[:120]} — use Ledger page).",
            [],
        )
    parts = []
    for t in s.get("by_type") or []:
        parts.append(
            f"{t.get('type')}: txns={t.get('txns'):,} cash=₹{_inr(t.get('cash') or 0)} "
            f"bonus=₹{_inr(t.get('bonus') or 0)} gamblers={t.get('gamblers')} utrs={t.get('utrs')}"
        )
    top = (s.get("by_sender") or [])[:10]
    if top:
        parts.append(
            "TOP DEPOSIT SENDERS: "
            + "; ".join(
                f"{r.get('sender')} = ₹{_inr(r.get('cash') or 0)} ({r.get('txns')} txns)"
                for r in top
            )
        )
    cov, files = s.get("coverage") or {}, s.get("files") or {}
    parts.append(
        f"LEDGER FILES: {files.get('indexed')}/{files.get('total')} indexed; "
        f"UTRs {cov.get('ledger_utrs')} ({cov.get('matched_in_flows')} matched in flows). "
        "cash = Amount − Bonus from Remark; raw Amount is chips, never cash."
    )
    out = (
        "LEDGER (XLS/CSV exports):\n" + "\n".join(parts),
        [{"kind": "ledger", "ref": "/api/ledger/summary"}],
    )
    _cache_put("ledger:summary", out)
    return out


_OBJECT_WORDS = (
    "bucket",
    "gcs",
    "object",
    "files in",
    "list files",
    "recent uploads",
    "recently uploaded",
    "latest",
    "newest",
    "uploaded",
    "prefix",
    "folder",
    "directory",
    "apk",
    "file type",
    "filetype",
    "extension",
    "images",
    "videos",
    "audio",
) + _PREFIX_TRIGGERS


# ---------- file-content search: "any file containing X / an IP address" ----
# (2026-09-18: officers need per-file reads, not just aggregates. Raw OCR
# text is NOT stored — only structured extracts — so this greps live object
# bytes, bounded: newest text files only, 40 files / 8MB / 10 hits max.)


_CONTENT_WORDS = (
    "ip address",
    "ipaddress",
    "ip ",
    "ips",
    "contain",
    "inside",
    "mention",
    "keyword",
    "domain",
    "phone",
    "email",
    "api key",
    "password",
    "secret",
    "file with",
    "any file",
    "which file",
    "server",
    "host",
    "callback",
    "webhook",
    "panel",
    "admin",
    "login",
    "username",
    "url",
    "link",
)

_GREP_EXTS = (".csv", ".txt", ".json", ".log", ".md", ".html", ".js")
_GREP_FILES_MAX = 40
_GREP_BYTES_MAX = 8 * 1024 * 1024
_GREP_HITS_MAX = 10

_IPV4_RE = re.compile(r"\b(?:\d{1,3}\.){3}\d{1,3}\b")


def _find_ipv4(text: str) -> list[str]:
    """Valid dotted quads in text (octets 0-255), first-seen order, deduped."""
    out = []
    for m in _IPV4_RE.finditer(text):
        try:
            if all(0 <= int(o) <= 255 for o in m.group(0).split(".")):
                if m.group(0) not in out:
                    out.append(m.group(0))
        except ValueError:
            continue
    return out


def _content_term(q: str) -> str:
    """A literal quoted-or-bare search term from the question, if any."""
    m = re.search(r"""['"]([^'"]{3,80})['"]""", q)
    if m:
        return m.group(1).strip()
    m = re.search(r"\b(?:\d{1,3}\.){3}\d{1,3}\b", q)
    if m:
        return m.group(0)
    m = re.search(
        r"\b((?:[a-z0-9-]+\.)+(?:com|net|org|io|in|xyz|top|site|online|bet|games?))\b",
        q.lower(),
    )
    if m:
        return m.group(1)
    return ""


def _ctx_file_content(ql: str, q: str, store: Any) -> tuple[str, list]:
    """Structured IP/term hits + bounded live grep of newest text files."""
    if not any(w in ql for w in _CONTENT_WORDS):
        return "", []
    parts, sources = [], []
    want_ip = "ip" in ql
    term = _content_term(q)

    # 1) structured: gateway brand websites holding an IPv4 address (indexed)
    if want_ip:
        try:
            rows = (
                db.connect()
                .execute(
                    "SELECT DISTINCT website FROM gateway_rows "
                    "WHERE website!='' AND website GLOB '*[0-9]*.[0-9]*' LIMIT 200"
                )
                .fetchall()
            )
            ip_brands = sorted(
                {w for r in rows for w in _find_ipv4(r["website"] or "")}
            )
            if ip_brands:
                parts.append(
                    "GATEWAY BRAND IPs (settlement exports — quote verbatim): "
                    + ", ".join(ip_brands[:30])
                )
                sources.append({"kind": "gateway", "ref": "/api/gateway/summary"})
        except Exception:
            pass
    # 2) key-name matches for a literal term
    if term:
        try:
            name_hits = db.search(q=term, limit=15)
            keys = [h.get("key") or "" for h in (name_hits or [])]
            keys = [k for k in keys if k]
            if keys:
                parts.append(
                    f"FILES NAMED LIKE '{term}' ({len(keys)} shown): " + "; ".join(keys)
                )
                sources += [{"kind": "file-hit", "ref": k} for k in keys[:12]]
        except Exception:
            pass
    # 3) bounded live grep over newest text files (cached 5 min per term)
    gkey = f"grep:{int(want_ip)}:{term}"
    cached = _cache_get(gkey)
    hits: list[tuple[str, list[str]]] = []
    scanned = 0
    from_cache = False
    if cached is not None:
        hits, scanned = cached
        from_cache = True
        sources += [{"kind": "file-hit", "ref": k} for k, _ in hits]
    else:
        try:
            res = db.list_objects(
                "", limit=200, order="-modified", flat=True, exts=list(_GREP_EXTS)
            )
        except Exception:
            res = {}
        cands = [
            o
            for o in (res.get("items") or [])
            if (o.get("size") or 0) <= 1024 * 1024 and o.get("key")
        ][:_GREP_FILES_MAX]
        read_bytes = 0
        if cands and store is not None:
            import concurrent.futures as _cf

            from .enrich import fetch_bytes

            def _read(key: str) -> tuple[str, str]:
                try:
                    data = fetch_bytes(store, key)
                except Exception:
                    return key, ""
                return key, data[: 1024 * 1024].decode("utf-8", "replace")

            with _cf.ThreadPoolExecutor(max_workers=8) as pool:
                futs = {pool.submit(_read, o["key"]): o["key"] for o in cands}
                for fut in _cf.as_completed(futs, timeout=60):
                    key = futs[fut]
                    scanned += 1
                    try:
                        _, text = fut.result(timeout=10)
                    except Exception:
                        continue
                    read_bytes += min(len(text), 1024 * 1024)
                    found = _find_ipv4(text) if want_ip else []
                    if term and term.lower() in text.lower():
                        found = (found + [f"literal '{term}'"])[:5]
                    if found:
                        hits.append((key, found[:5]))
                        sources.append({"kind": "file-hit", "ref": key})
                    if len(hits) >= _GREP_HITS_MAX or read_bytes >= _GREP_BYTES_MAX:
                        break
        _cache_put(gkey, (hits, scanned))
    if hits:
        lines = [f"{k} -> {', '.join(v)}" for k, v in hits]
        parts.append(
            f"CONTENT HITS (live grep, {scanned} newest text files scanned"
            f"{' — cached ≤5 min' if from_cache else ''} — "
            "open keys in Explorer/Search to verify):\n" + "\n".join(lines)
        )
    else:
        parts.append(
            f"No matches in the bounded scan ({scanned} newest text files, "
            "csv/txt/json/log/md/html/js ≤1MB each). NOT a full-bucket sweep: "
            "1.6M+ objects (images need OCR, sheets need parsing) — narrow by "
            "prefix, filename, or a literal IP/domain to dig deeper."
        )
    return "FILE CONTENT SEARCH:\n" + "\n".join(parts), sources[:12]


def _ctx_objects(ql: str, q: str) -> tuple[str, list]:
    """Newest object keys overall or inside a named prefix (bucket content)."""
    if not any(w in ql for w in _OBJECT_WORDS):
        return "", []
    prefix = ""
    m = re.search(r"([\w][\w-]*)/", q)
    if m:
        cand = m.group(1).lower() + "/"
        if cand in [p.lower() for p in config.DEFAULT_PREFIXES]:
            prefix = next(p for p in config.DEFAULT_PREFIXES if p.lower() == cand)
    if not prefix:
        # bare prefix name without slash ("apk files?", "chat images?")
        for alias, target in _PREFIX_ALIASES.items():
            if re.search(rf"(?<![\w]){re.escape(alias)}s?(?![\w/])", ql):
                prefix = target
                break
    if not prefix:
        for p in config.DEFAULT_PREFIXES:
            stem = p.rstrip("/").lower()
            if re.search(rf"(?<![\w]){re.escape(stem)}s?(?![\w/])", ql):
                prefix = p
                break
    try:
        res = db.list_objects(prefix, limit=15, order="-modified", flat=True)
    except Exception as e:
        return (
            f"Object listing unavailable ({str(e)[:120]} — use Explorer page).",
            [],
        )
    items = res.get("items") or []
    if not items:
        return "No objects found.", [{"kind": "objects", "ref": "/api/live"}]
    lines = [
        f"{o.get('key')} | {o.get('size', '?')} bytes | {o.get('last_modified') or '?'}"
        for o in items
    ]
    scope = f"in {prefix}" if prefix else "bucket-wide"
    txt = (
        f"NEWEST OBJECTS ({scope}, {res.get('total', '?')} total — "
        "newest 15 shown):\n" + "\n".join(lines)
    )
    return txt, [{"kind": "objects", "ref": "/api/live"}]


def _ctx_image(q: str, store: Any) -> tuple[str, list, Optional[tuple[bytes, str]]]:
    """If the question names an object key, fetch it (bounded 8MB) for vision."""
    cands = re.findall(r"[\w\-./]{4,200}\.\w{2,5}", q)
    for cand in cands:
        key = cand.strip().strip("'\"")
        if not key.lower().endswith(_IMG_EXT):
            continue
        try:
            from .enrich import fetch_bytes

            data = fetch_bytes(store, key)
        except Exception as e:
            return (
                f"Could not fetch {key}: {str(e)[:150]}",
                [{"kind": "object", "ref": key}],
                None,
            )
        if len(data) > 8 * 1024 * 1024:
            return (
                f"{key} is {len(data)} bytes (over 8MB vision cap).",
                [{"kind": "object", "ref": key}],
                None,
            )
        try:
            row = db.get_object_row(key) or {}
            meta = f"size={row.get('size')} modified={row.get('last_modified')} enriched={bool(row.get('enriched'))}"
        except Exception:
            meta = ""
        return (
            f"Image {key} ({meta}) attached for visual inspection.",
            [{"kind": "object", "ref": key}],
            (data, key),
        )
    return "", [], None


def _generate_groq(prompt_text: str, system: str) -> tuple[Optional[str], str]:
    """Groq Cloud failover (OpenAI-compat chat completions). Text-only.

    Tries GROQ_MODEL then GROQ_FALLBACK_MODEL. Returns (answer, model_used).
    """
    import time as _t

    try:
        from .enrich import _gemini_client_shared
    except Exception:
        return None, ""
    client = _gemini_client_shared()
    base = {
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": prompt_text},
        ],
        "temperature": 0.2,
        "max_tokens": 1024,
    }
    last_err = "unknown"
    for model in (config.GROQ_MODEL, config.GROQ_FALLBACK_MODEL):
        if not model:
            continue
        try:
            r = client.post(
                "https://api.groq.com/openai/v1/chat/completions",
                headers={"Authorization": f"Bearer {config.GROQ_API_KEY}"},
                json={**base, "model": model},
            )
            if r.status_code == 429:
                last_err = f"429@groq:{model}"
                _t.sleep(5.0)
                continue
            if r.status_code >= 500:
                last_err = f"{r.status_code}@groq:{model}"
                _t.sleep(2.0)
                continue
            r.raise_for_status()
            return r.json()["choices"][0]["message"]["content"], f"groq:{model}"
        except Exception as e:
            last_err = f"groq:{model}:{str(e)[:150]}"
            _t.sleep(2.0)
    _note(False, last_err)
    return None, ""


def _generate(prompt_parts: list[dict], system: str) -> tuple[Optional[str], str]:
    """Gemini primary + Groq text failover. Returns (answer, model_used)."""
    import time as _t

    last_err = "ai-offline"
    if config.GEMINI_ENABLED:
        try:
            from .enrich import _gemini_client_shared
        except Exception:
            return None, ""
        body = {
            "contents": [{"parts": [{"text": system}, *prompt_parts]}],
            "generationConfig": {"temperature": 0.2, "maxOutputTokens": 1024},
        }
        url = (
            f"https://generativelanguage.googleapis.com/v1beta/models/"
            f"{AI_MODEL}:generateContent?key={config.GEMINI_API_KEY}"
        )
        client = _gemini_client_shared()
        last_err = "unknown"
        for attempt in range(3):
            try:
                r = client.post(url, json=body)
                if r.status_code == 404:  # model retired -> lite fallback once
                    fb = url.replace(AI_MODEL, "gemini-2.5-flash-lite")
                    r = client.post(fb, json=body)
                if r.status_code == 429:
                    # Retrying a dead quota inside one request only burns latency;
                    # keep the FULL body (scope/quotaId) for stats, fail fast.
                    last_err = f"429@{AI_MODEL}: {r.text[:500]}"
                    break
                if r.status_code >= 500:
                    last_err = f"{r.status_code}"
                    _t.sleep(2.0 * (2**attempt))
                    continue
                r.raise_for_status()
                return (
                    r.json()["candidates"][0]["content"]["parts"][0]["text"],
                    AI_MODEL,
                )
            except Exception as e:
                last_err = str(e)[:200]
                _t.sleep(2.0 * (2**attempt))
    has_image = any("inline_data" in p for p in prompt_parts)
    if config.GROQ_ENABLED and not has_image:
        # Groq failover is text-only (no vision on available models).
        prompt_text = "".join(p.get("text", "") for p in prompt_parts)
        return _generate_groq(prompt_text, system)
    _note(False, last_err)
    return None, ""


_SYSTEM = (
    "You are a forensic analyst assistant for an authorized illegal-betting "
    "investigation (case CASE-2026-CBTF). Answer ONLY from the provided case-data "
    "context. Amounts are in INR. Candidate mule rails / wallet hits are leads, "
    "not proof — say so when relevant. If the context lacks the answer, say what "
    "is missing and suggest the exact dashboard page or query to check. "
    "Be concise: short summary first, then 3-8 bullets. Never invent identifiers. "
    "You have a CASE OVERVIEW of the whole GCS evidence bucket (folder counts, "
    "OCR coverage, deposit/withdrawal slip totals, unique rails, wallets). Use it. "
    "If the officer asks what is in the bucket, list the top folders from overview. "
    "Numbers under EXACT MATH are exact database results — quote them verbatim, "
    "never recompute or round them. Wallet addresses, rail handles, ledger rows "
    "and object keys under WALLETS / RAILS / LEDGER / NEWEST OBJECTS are exact "
    "database output too — when the officer asks to list them, print them "
    "(up to ~100), never refuse for lack of data and never invent any. "
    "FILE CONTENT SEARCH blocks report a bounded live grep — state the scan "
    "coverage (files scanned) and give the hit keys as exhibits; never claim "
    "a full-bucket sweep. "
    "A COMPUTED count=0 means ZERO transactions "
    "in that window: report 'no transactions recorded in <window>', NEVER "
    "'data unavailable' or 'lacks information'. Only say data is missing when "
    "no EXACT MATH block was provided at all. All dates/times are IST "
    "(UTC+5:30); timestamps ending in Z are UTC, add 5:30 for IST. State the "
    "window (today/yesterday/all time) with every figure."
)


def ask(question: str, store: Any) -> dict:
    """Answer an officer question over live case data. Never raises."""
    q = (question or "").strip()[:2000]
    if not q:
        return {
            "answer": "Ask about totals, a UTR/VPA/wallet, an image, or case status.",
            "sources": [],
            "model": AI_MODEL,
        }
    ql = q.lower()
    if _GREETING_RE.match(q.strip()):
        return {
            "answer": (
                "Hello Officer. I'm online and reading the live case index. "
                "Ask about bucket totals, deposits/withdrawals, UPI rails, "
                "crypto wallets, or paste a UTR, VPA, wallet, or object key."
            ),
            "sources": [],
            "model": "local",
        }
    if not (config.GEMINI_ENABLED or config.GROQ_ENABLED):
        return {
            "answer": "AI assistant is offline — set the Gemini or Groq key on the VM first.",
            "sources": [],
            "model": AI_MODEL,
        }
    blocked = _claim()
    if blocked:
        return {
            "answer": f"AI assistant busy: {blocked}.",
            "sources": [],
            "model": AI_MODEL,
        }

    ctx_parts, sources = [], []

    # image question: fetch first, vision-describe with data context
    img_txt, img_src, img_blob = _ctx_image(q, store)
    if img_txt:
        ctx_parts.append(img_txt)
        sources += img_src

    t, s = _ctx_overview()
    ctx_parts.insert(0, "CASE OVERVIEW (whole bucket):\n" + t)
    sources += s
    if any(
        w in ql
        for w in (
            "total",
            "deposit",
            "withdraw",
            "₹",
            "inr",
            "amount",
            "money",
            "mule",
            "rail",
            "flow",
            "bank",
            "payout",
            "transfer",
            "upi",
            "vpa",
            "payment",
        )
    ):
        t, s = _ctx_money(q)
        ctx_parts.append("MONEY:\n" + t)
        sources += s
    id_txt, id_src = _ctx_identifier(q)
    if id_txt:
        ctx_parts.append("IDENTIFIER LOOKUPS:\n" + id_txt)
        sources += id_src
    calc_txt, calc_src = _ctx_calc(q)
    if calc_txt:
        ctx_parts.append(
            "EXACT MATH (SQL over payment_transactions, IST window):\n" + calc_txt
        )
        sources += calc_src
    t, s = _ctx_wallet_list(ql)
    if t:
        ctx_parts.append("WALLETS:\n" + t)
        sources += s
    t, s = _ctx_upi_list(ql)
    if t:
        ctx_parts.append("RAILS:\n" + t)
        sources += s
    t, s = _ctx_people(ql)
    if t:
        ctx_parts.append("PEOPLE:\n" + t)
        sources += s
    t, s = _ctx_ledger(ql)
    if t:
        ctx_parts.append(t)
        sources += s
    t, s = _ctx_objects(ql, q)
    if t:
        ctx_parts.append(t)
        sources += s
    t, s = _ctx_file_content(ql, q, store)
    if t:
        ctx_parts.append(t)
        sources += s

    context = "\n\n".join(ctx_parts)[:20000]
    parts: list[dict] = [{"text": f"CASE DATA:\n{context}\n\nOFFICER QUESTION: {q}"}]
    img_key = None
    if img_blob:
        try:
            from .enrich import _gemini_shrink
            import base64

            small, mime = _gemini_shrink(img_blob[0])
            img_key = img_blob[1]
            parts.append(
                {
                    "inline_data": {
                        "mime_type": mime,
                        "data": base64.b64encode(small).decode("ascii"),
                    }
                }
            )
        except Exception:
            pass
    ans, used_model = _generate(parts, _SYSTEM)
    if ans is None:
        return {
            "answer": "AI backend unreachable right now (quota/network). The dashboard pages still work — try Search / Money Flows directly.",
            "sources": sources,
            "model": used_model or AI_MODEL,
        }
    _note(True)
    used_model = used_model or AI_MODEL
    if img_key:
        sources = [{"kind": "object", "ref": img_key}] + sources
    # de-dupe sources, cap 12
    seen, uniq = set(), []
    for s in sources:
        k = (s.get("kind"), s.get("ref"))
        if k not in seen:
            seen.add(k)
            uniq.append(s)
    return {"answer": ans, "sources": uniq[:12], "model": used_model}
