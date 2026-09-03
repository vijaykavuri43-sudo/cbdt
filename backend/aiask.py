"""AI investigator assistant: Gemini answers over live case data (read-only).

Officers ask in plain language; this module gathers bounded, read-only
context from the SQLite manifest + payment index (+ one GCS/S3 object at
most for "show/describe this image" questions) and has Gemini answer with
cited sources. Safety rails:

- read-only: only SELECT queries + single object fetch, never writes/deletes
- gated: POST /api/ask sits behind the same access-key Depends(auth)
- budgeted: separate small daily cap (S3LM_AIASK_DAILY_CAP, default 500) +
  per-minute rate limit, independent of the OCR Vision budget
- bounded: question <= 2000 chars, context <= ~12KB, answer <= 1024 tokens
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

AI_MODEL = (
    __import__("os").environ.get("S3LM_AI_MODEL", "gemini-3.8-flash").strip()
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
        else:
            _consec_fail += 1
            _stats["errors"] += 1
            _stats["last_error"] = err[:200]
            if _consec_fail >= 4:
                _cooldown_until = time.monotonic() + 600.0
                _consec_fail = 0


# ---------- read-only context gatherers (all bounded) ----------


def _ctx_overview() -> tuple[str, list]:
    e = db.enrichment_progress()
    try:
        w = db.wallet_stats()
    except Exception:
        w = {}
    try:
        n_pay = db.payment_row_count()
    except Exception:
        n_pay = "?"
    txt = (
        f"Indexed objects: {e.get('total')}; enriched: {e.get('enriched_done')} "
        f"({e.get('coverage_pct')}%); pending OCR: {e.get('pending_now')}; "
        f"processed last hour: {e.get('processed_last_hour')} (ETA {e.get('eta_hours')}h). "
        f"Payment rows: {n_pay}. Wallet stats: {w}."
    )
    return txt, [{"kind": "overview", "ref": "/api/status"}]


def _ctx_money(q: str) -> tuple[str, list]:
    parts, sources = [], []
    for direction in ("deposit", "withdrawal"):
        try:
            f = db.money_flows(direction=direction, limit=8)
        except Exception:
            continue
        rows = f.get("flows") or f.get("rows") or []
        total = f.get("total_inr") or f.get("total") or "?"
        parts.append(f"{direction}s: total≈₹{total}; top rails: {rows[:8]}")
        sources.append({"kind": "money", "ref": f"/api/flows?direction={direction}"})
    try:
        mr = db.mule_rails(limit=10)
        rows = mr.get("rails") or mr.get("rows") or []
        parts.append(f"Top candidate mule rails: {rows[:10]}")
        sources.append({"kind": "mules", "ref": "/api/mule-rails"})
    except Exception:
        pass
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


def _generate(prompt_parts: list[dict], system: str) -> Optional[str]:
    import time as _t

    try:
        from .enrich import _gemini_client_shared
    except Exception:
        return None
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
            if r.status_code == 429 or r.status_code >= 500:
                last_err = f"{r.status_code}"
                _t.sleep(2.0 * (2**attempt))
                continue
            r.raise_for_status()
            return r.json()["candidates"][0]["content"]["parts"][0]["text"]
        except Exception as e:
            last_err = str(e)[:200]
            _t.sleep(2.0 * (2**attempt))
    _note(False, last_err)
    return None


_SYSTEM = (
    "You are a forensic analyst assistant for an authorized illegal-betting "
    "investigation (case CASE-2026-CBTF). Answer ONLY from the provided case-data "
    "context. Amounts are in INR. Candidate mule rails / wallet hits are leads, "
    "not proof — say so when relevant. If the context lacks the answer, say what "
    "is missing and suggest the exact dashboard page or query to check. "
    "Be concise: short summary first, then 3-8 bullets. Never invent identifiers. "
    "Numbers under EXACT MATH are exact database results — quote them verbatim, "
    "never recompute or round them. A COMPUTED count=0 means ZERO transactions "
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
    if not config.GEMINI_ENABLED:
        return {
            "answer": "AI assistant is offline — set the Gemini key on the VM first.",
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

    ql = q.lower()
    ctx_parts, sources = [], []

    # image question: fetch first, vision-describe with data context
    img_txt, img_src, img_blob = _ctx_image(q, store)
    if img_txt:
        ctx_parts.append(img_txt)
        sources += img_src

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
    if (
        any(
            w in ql
            for w in (
                "status",
                "progress",
                "pending",
                "coverage",
                "eta",
                "how many",
                "overview",
                "health",
            )
        )
        or not ctx_parts
    ):
        t, s = _ctx_overview()
        ctx_parts.append("CASE OVERVIEW:\n" + t)
        sources += s
    if (
        any(w in ql for w in ("wallet", "crypto", "usdt", "tron", "btc"))
        and "wallet" not in " ".join(ctx_parts).lower()[:50]
    ):
        try:
            w = db.wallet_stats()
            ctx_parts.append(f"WALLETS:\n{w}")
            sources.append({"kind": "wallets", "ref": "/api/wallets/stats"})
        except Exception:
            pass

    context = "\n\n".join(ctx_parts)[:12000]
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
    ans = _generate(parts, _SYSTEM)
    if ans is None:
        return {
            "answer": "AI backend unreachable right now (quota/network). The dashboard pages still work — try Search / Money Flows directly.",
            "sources": sources,
            "model": AI_MODEL,
        }
    _note(True)
    if img_key:
        sources = [{"kind": "object", "ref": img_key}] + sources
    # de-dupe sources, cap 12
    seen, uniq = set(), []
    for s in sources:
        k = (s.get("kind"), s.get("ref"))
        if k not in seen:
            seen.add(k)
            uniq.append(s)
    return {"answer": ans, "sources": uniq[:12], "model": AI_MODEL}
