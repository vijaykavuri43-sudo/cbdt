"""Enrichment workers: QR decode (UPI VPA/amount/name) + deposit-slip OCR (tesseract)."""

import json
import os as _os
import re
import subprocess
import tempfile
import threading
from pathlib import Path
from typing import Any, Callable, Optional

# Tesseract spawns one OpenMP thread per core PER PROCESS by default — with N
# decode workers that is N×cores threads thrashing 4 vCPUs. Single-thread each
# tesseract and scale by worker COUNT instead (2026-09-03: ~2-3x throughput).
# Set before any pool/tesseract starts; inherited by spawn children via env.
_os.environ.setdefault("OMP_THREAD_LIMIT", "1")

from . import config, db

VPA_RE = re.compile(r"[A-Za-z0-9.\-_]{2,}\s*@\s*[a-zA-Z]{2,}")
VALID_VPA = re.compile(
    r"^(?=.*[A-Za-z0-9]{4})[A-Za-z0-9][A-Za-z0-9.\-_]{3,}@[a-zA-Z]{2,}$"
)
# Email domains that must never be treated as UPI mule identifiers
_EMAIL_VPA_BLACKLIST = {
    "gmail.com",
    "yahoo.com",
    "yahoo.co.in",
    "hotmail.com",
    "outlook.com",
    "outlook.in",
    "aol.com",
    "protonmail.com",
    "proton.me",
    "icloud.com",
    "rediffmail.com",
    "ymail.com",
}
# Common UPI handle suffixes — anything outside this still accepted but
# flagged as medium confidence unless seen in master list.
_KNOWN_UPI_SUFFIXES = {
    "okaxis",
    "okhdfcbank",
    "okicici",
    "oksbi",
    "okbizaxis",
    "okyesbank",
    "ybl",
    "ibl",
    "axl",
    "pingpay",
    "paytm",
    "upi",
    "apl",
    "airtel",
    "jio",
    "freecharge",
}
IFSC_RE = re.compile(r"\b([A-Z]{4})\s*0\s*([A-Z0-9]{6})\b", re.IGNORECASE)
ACCOUNT_CONTEXT_RE = re.compile(
    r"(?:account\s*(?:number|no\.?|#)?|a/?c\s*(?:number|no\.?)?)\s*[:=\-]?\s*([Xx*0-9][Xx*0-9\s\-]{5,24})",
    re.IGNORECASE,
)
BENEFICIARY_RE = re.compile(
    r"(?:paid\s+to|beneficiary|receiver|recipient)\s*[:\-]?\s*([A-Za-z][A-Za-z .]{2,60}?)(?=\s+(?:[A-Za-z0-9._-]+@[A-Za-z]{2,}|upi|bank|account|a/?c|amount|₹|rs\.?|inr|\d{2}\s+(?:aug|sep|oct|nov|dec|jan|feb|mar|apr|may|jun|jul))|$)",
    re.IGNORECASE,
)
SENDER_RE = re.compile(
    r"(?:paid\s+by|sender|from)\s*[:\-]?\s*([A-Za-z][A-Za-z .]{2,60}?)(?=\s+(?:upi|bank|account|a/?c|amount|₹|rs\.?|inr)|$)",
    re.IGNORECASE,
)

# ---- crypto wallet extraction (CASE-2026-CBTF: gamblers deposit USDT to platform hot wallets) ----
# TRON / TRC-20 USDT: base58, starts with T, 34 chars, excludes ambiguous chars
TRON_RE = re.compile(r"\b(T[1-9A-HJ-NP-Za-km-z]{33})\b")
# Ethereum / EVM (ERC-20 USDT): 0x + 40 hex
ETH_RE = re.compile(r"\b(0x[a-fA-F0-9]{40})\b")
# Bitcoin: legacy 1/3 or bech32 bc1
BTC_RE = re.compile(r"\b((?:[13][a-km-zA-HJ-NP-Z1-9]{25,34}|bc1[a-z0-9]{11,71}))\b")
CRYPTO_HINT_RE = re.compile(
    r"usdt|trc.?20|erc.?20|tron|trust\s*wallet|binance|coinbase|wallet", re.IGNORECASE
)

_BENEFICIARY_ROLE_RE = re.compile(
    r"(?:paid\s+to|payee|beneficiary|recipient|receiver|credited\s+to|sent\s+to|transfer(?:red)?\s+to)\s*[:=\-]?",
    re.IGNORECASE,
)
_SENDER_ROLE_RE = re.compile(
    r"(?:paid\s+by|payer|sender|remitter|debited\s+from|received\s+from|transfer(?:red)?\s+from|from)\s*[:=\-]?",
    re.IGNORECASE,
)


def infer_rail_role(text: str, start: int, end: int) -> tuple[str, str]:
    """Infer whether an identifier belongs to the payer or beneficiary.

    Labels immediately before an identifier are deliberately weighted more than
    generic words elsewhere on the receipt. Unknown is retained as evidence but
    must not be presented as a confirmed mule-side role.
    """
    before = " ".join(text[max(0, start - 120) : start].split())
    after = " ".join(text[end : end + 50].split())
    # Strip an intervening display name (for example "Paid to ACME <vpa>") while
    # keeping the role label close enough to detect.
    before_tail = before[-120:]
    beneficiary = list(_BENEFICIARY_ROLE_RE.finditer(before_tail))
    sender = list(_SENDER_ROLE_RE.finditer(before_tail))
    if beneficiary and (not sender or beneficiary[-1].start() > sender[-1].start()):
        return "beneficiary", "high"
    if sender and (not beneficiary or sender[-1].start() > beneficiary[-1].start()):
        return "sender", "high"
    # Some OCR layouts place the label after the value.
    if re.match(r"\s*(?:payee|beneficiary|recipient|receiver)\b", after, re.I):
        return "beneficiary", "medium"
    if re.match(r"\s*(?:payer|sender|remitter)\b", after, re.I):
        return "sender", "medium"
    return "unknown", "low"


def extract_wallets(text: str) -> list[dict]:
    """Pull crypto wallet addresses out of QR text / OCR text with network labels.

    Dedupes; keeps first-seen order. Confidence: exact charset match on an
    address-shaped token is strong for TRON (T + base58, no false positives seen
    in this bucket's data); ETH/BTC get a context hint boost when nearby words
    like USDT/TRC20/wallet appear.
    """
    found: list[dict] = []
    seen: set[str] = set()
    hint = bool(CRYPTO_HINT_RE.search(text))
    for m in TRON_RE.finditer(text):
        addr = m.group(1)
        if addr in seen:
            continue
        # checksum-ish sanity: reject all-same-char artifacts from OCR smears
        if len(set(addr)) < 8:
            continue
        seen.add(addr)
        role, role_confidence = infer_rail_role(text, m.start(), m.end())
        found.append(
            {
                "address": addr,
                "network": "TRON",
                "standard": "TRC-20",
                "confidence": "high",
                "role": role,
                "role_confidence": role_confidence,
            }
        )
    for m in ETH_RE.finditer(text):
        addr = m.group(1).lower()
        if addr in seen:
            continue
        if len(set(addr)) < 10:
            continue
        seen.add(addr)
        conf = "high" if hint else "medium"
        role, role_confidence = infer_rail_role(text, m.start(), m.end())
        found.append(
            {
                "address": addr,
                "network": "EVM",
                "standard": "ERC-20",
                "confidence": conf,
                "role": role,
                "role_confidence": role_confidence,
            }
        )
    for m in BTC_RE.finditer(text):
        addr = m.group(1)
        if addr in seen:
            continue
        if addr.lower().startswith("bc1") and not (14 <= len(addr) <= 90):
            continue
        if len(set(addr)) < 8:
            continue
        seen.add(addr)
        conf = "high" if hint else "low"
        role, role_confidence = infer_rail_role(text, m.start(), m.end())
        found.append(
            {
                "address": addr,
                "network": "BITCOIN",
                "standard": "BTC",
                "confidence": conf,
                "role": role,
                "role_confidence": role_confidence,
            }
        )
    return found


def plausible_vpa(v: str) -> bool:
    """Reject OCR artifacts of masked UPIs like '-1@okbizaxis'."""
    return bool(v) and bool(VALID_VPA.match(v))


AMOUNT_PATTERNS = [
    re.compile(
        r"(?:txn|transaction)\s*amount\s*[:=\-]?\s*(?:₹|rs\.?|inr|[%₹])?\s*([0-9][0-9,]*(?:\.[0-9]{1,2})?)",
        re.IGNORECASE,
    ),
    re.compile(
        r"\bamount\s*[:=\-]?\s*(?:₹|rs\.?|inr|[%₹])?\s*([0-9][0-9,]*(?:\.[0-9]{1,2})?)",
        re.IGNORECASE,
    ),
    re.compile(
        r"(?:[₹%]|rs\.?|inr|=)\s*([0-9][0-9,]*(?:\.[0-9]{1,2})?)", re.IGNORECASE
    ),
    # Deposit slips: principal often shown as "~3,000" or bare "2,000" next to Paid-to name (no ₹ symbol due to OCR/thumbnail).
    re.compile(r"~\s*([0-9][0-9,]*(?:\.[0-9]{1,2})?)"),
    # OCR frequently reads the ₹ glyph as '=' — "=2,500" is a verified PhonePe misread.
    re.compile(r"=\s*([0-9][0-9,]*(?:\.[0-9]{1,2})?)"),
    # Spaced thousands — "1 000" / "2 000" when OCR drops the comma.
    re.compile(r"\b([0-9]{1,3}(?: [0-9]{3})+(?:\.[0-9]{1,2})?)\b"),
    # Bare comma amounts (2,000 / 12,345.00) — scored lower, boosted only near Paid-to / Payment context; UTRs have no commas so safe.
    re.compile(r"\b([0-9]{1,3}(?:,[0-9]{3})+(?:\.[0-9]{1,2})?)\b"),
]
# Context clues that an amount is NOT the principal transaction — balance, promo/ads
_BALANCE_CTX = re.compile(r"balance\s*[:>\-]?\s*[₹%]?\s*[0-9,]*", re.IGNORECASE)
_SPENT_CTX = re.compile(
    r"you\s+have\s+spent|spent\s+.*less\s+than|less\s+than\s+aug|sponsored\s+products|valid\s+for\s+24",
    re.IGNORECASE,
)
_PAYMENT_CTX = re.compile(
    r"payment\s+successful|paid\s+to|paid\s+by|transaction\s+successful|view\s+details",
    re.IGNORECASE,
)
UTR_RE = re.compile(
    r"\b(?:UTR|utr|Ref\.?(?: no)?|reference)\s*[:#\-]?\s*([A-Z0-9]{8,16})\b",
    re.IGNORECASE,
)
# Strict 12-digit UTR fallback — but must not be an account/phone/aadhaar
NUM12_RE = re.compile(r"\b(\d{12})\b")
# OCR confusable normalisation for digit strings (O→0, I/l→1) when flanked by digits
_OCR_DIGIT_FIX = str.maketrans({"O": "0", "o": "0", "I": "1", "l": "1", "|": "1"})
_ACCOUNT_KEYWORDS_RE = re.compile(
    r"(?:a/?c|account|acc\.?\s*no|saving|current)\b", re.IGNORECASE
)


def fetch_bytes(s3: Any, key: str) -> bytes:
    fast = getattr(s3, "get_object_bytes", None)
    if callable(fast):
        return fast(key)
    resp, _ = s3.get_object_stream(key)
    data = resp.read()
    resp.close()
    return data


def extract_vpas(text: str) -> list[str]:
    """Extract UPI VPAs, handling OCR split '@' and filtering email artifacts."""
    # Normalise stray spaces around @ introduced by tesseract line breaks
    # but keep original for other extractors
    norm = re.sub(r"\s*@\s*", "@", text)
    out: list[str] = []
    seen: set[str] = set()
    for m in VPA_RE.finditer(norm):
        raw = re.sub(r"\s+", "", m.group(0)).lower().strip(".,;:)\"' ")
        # Strip trailing punctuation leftover from sentence ends
        raw = raw.strip()
        if not plausible_vpa(raw):
            continue
        handle = raw.split("@", 1)[1] if "@" in raw else ""
        # Reject common email providers — they are not UPI mule rails
        if (
            handle in _EMAIL_VPA_BLACKLIST
            or handle.endswith(".com")
            and handle.split(".")[0] not in _KNOWN_UPI_SUFFIXES
            and handle in {"gmail.com"}
        ):
            # Also reject any gmail/outlook etc with dot - already covered
            continue
        if handle.count(".") >= 1 and handle.split(".")[-1] in {
            "com",
            "net",
            "org",
            "in",
            "co",
        }:
            # gmail.com/yahoo.com style email — skip unless handle is known UPI with dot (rare)
            if handle not in _KNOWN_UPI_SUFFIXES:
                # Be strict for dot domains: only allow if suffix base is known
                base = handle.split(".")[0]
                if base not in _KNOWN_UPI_SUFFIXES:
                    # allow paytm etc without dot, but gmail.com must be dropped
                    if (
                        handle in _EMAIL_VPA_BLACKLIST
                        or handle.endswith("gmail.com")
                        or handle.endswith("yahoo.com")
                    ):
                        continue
                    # For unknown dot-domain, keep but will be medium confidence downstream
                    # Still filter obvious email TLDs when not in known UPI list
                    if handle.endswith(".com") and handle not in _KNOWN_UPI_SUFFIXES:
                        # Check master list later; for now keep but don't drop silently for unknown corp VPAs
                        # Only drop if handle is email blacklist; otherwise keep
                        pass
        if raw in seen:
            continue
        seen.add(raw)
        out.append(raw)
    # Post-filter: drop anything detected as email with dot-TLD that isn't a known UPI
    filtered: list[str] = []
    for v in out:
        h = v.split("@", 1)[1]
        if h in _EMAIL_VPA_BLACKLIST:
            continue
        filtered.append(v)
    return filtered


def extract_vpa_details(text: str) -> list[dict]:
    """Return normalized VPAs with payer/beneficiary role provenance."""
    norm = re.sub(r"\s*@\s*", "@", text)
    allowed = set(extract_vpas(norm))
    details: list[dict] = []
    seen: set[str] = set()
    for match in VPA_RE.finditer(norm):
        vpa = re.sub(r"\s+", "", match.group(0)).lower().strip(".,;:)\"' ")
        if vpa not in allowed or vpa in seen:
            continue
        seen.add(vpa)
        role, role_confidence = infer_rail_role(norm, match.start(), match.end())
        details.append(
            {
                "vpa": vpa,
                "role": role,
                "role_confidence": role_confidence,
                "context": " ".join(
                    norm[max(0, match.start() - 60) : match.end() + 30].split()
                )[:160],
            }
        )
    return details


def extract_amount(text: str) -> Optional[float]:
    """Context-aware amount extraction — principal transaction only.

    Collects every candidate, scores it, and blacklists promo/balance amounts.
    Fixes the ₹200 vs ₹31,785 promo bug (withdrawal/1788262500581.jpeg):
      - "Balance: ₹355" and "You have spent ₹31,785 less than Aug" must NOT win
      - The large centered amount under "Payment successful" wins.
    Ranking: label weight + payment-proximity boost - blacklist penalty.
    """
    if not text:
        return None
    # Preserve original for context windows; also normalize whitespace
    flat = " ".join(text.split())
    lower = flat.lower()
    candidates: list[
        tuple[float, int, int, str]
    ] = []  # (value, score, start, raw_with_prefix)
    # Pre-compute payment keyword positions for proximity scoring
    pay_positions = [m.start() for m in _PAYMENT_CTX.finditer(flat)]
    for pi, pattern in enumerate(AMOUNT_PATTERNS):
        if pi == 0:
            weight = 10
        elif pi == 1:
            weight = 5
        elif pi == 2:
            weight = 6
        elif pi == 3:
            weight = 7  # tilde ~3,000 — principal on PhonePe/GooglePay slips
        elif pi == 4:
            weight = 6  # "=2,500" — tesseract reads ₹ as '=' (verified PhonePe misread)
        elif pi == 5:
            weight = 4  # spaced thousands "1 000" / "2 000"
        else:
            weight = 3  # bare 2,000 — fallback when ₹ missed by OCR

        for m in pattern.finditer(flat):
            raw = m.group(1).replace(",", "").replace(" ", "")
            try:
                value = float(raw)
            except ValueError:
                continue
            if not (0 < value <= config.MAX_AMOUNT_INR):
                continue
            if len(raw.split(".", 1)[0]) > 9:
                continue
            s, e = m.start(), m.end()
            ctx_before = flat[max(0, s - 60) : s]
            ctx_after = flat[e : e + 60]
            ctx_before_low = ctx_before.lower()
            ctx_after_low = ctx_after.lower()
            # Blacklist ONLY if amount is itself the balance/promo value, not merely nearby.
            # For "Balance: ₹355" the 355's ctx_before ends with "balance:" / "balance: ₹"
            if re.search(
                r"balance\s*[:>\-]?\s*[₹%]?\s*$", ctx_before_low.strip()[-28:]
            ):
                continue
            # Promo "You have spent X less" — the X is directly after "spent"
            if "you have spent" in ctx_before_low[-45:] or re.search(
                r"spent\s*[%₹]?\s*$", ctx_before_low.strip()[-30:]
            ):
                continue
            # Also catch "spent ... less" split across amount (rare) via combined check but only when spent is just before
            if _SPENT_CTX.search(ctx_before_low[-50:] + " " + m.group(0).lower()):
                # ensure spent is immediate predecessor, not 60 chars after
                if "spent" in ctx_before_low[-40:]:
                    continue
            # Fallback narrow: if amount sits inside sponsored promo footer
            if "sponsored" in ctx_before_low[-35:] or "sponsored" in ctx_after_low[:35]:
                # sponsored amounts are never transaction principal
                if "sponsored" in (ctx_before_low[-35:] + ctx_after_low[:35]):
                    # only reject if nearby, not distant ₹200 case — check distance already narrow
                    if "sponsored" in ctx_before_low or "sponsored" in ctx_after_low:
                        # be conservative: only if amount is within 30 chars of sponsored
                        if (
                            "sponsored" in ctx_before_low[-30:]
                            or "sponsored" in ctx_after_low[:30]
                        ):
                            continue
            # Promo footer "up to ₹5,000" / "Enjoy seamless ..." — never principal
            if (
                "up to" in ctx_before_low[-30:]
                and "seamless" in (ctx_before_low + ctx_after_low).lower()[:60]
            ):
                continue
            if "up to" in ctx_before_low[-28:] and "%" in m.group(0):
                continue
            # Bare comma / spaced amounts outside payment context are likely table/noise — penalize far distance
            if pi in (5, 6):  # spaced thousands / bare comma — no rupee glyph
                if pay_positions:
                    nearest = min(abs(s - p) for p in pay_positions)
                    if nearest > 250:
                        # too far from any payment cue — likely footer list or debited duplicate
                        continue
                else:
                    # no payment cue at all — only keep tilde/bare if Paid-to context nearby
                    if (
                        "paid to" not in ctx_before_low[-80:].lower()
                        and "paid to" not in ctx_after_low[:80].lower()
                    ):
                        if "~" not in flat[max(0, s - 5) : e + 5]:
                            continue
            score = weight
            # Proximity boost: near "Payment successful" / "View details"
            if pay_positions:
                nearest = min(abs(s - p) for p in pay_positions)
                if nearest < 80:
                    score += 20
                elif nearest < 180:
                    score += 8
            # Standalone large-font amount tends to have whitespace / newline around ₹200
            # In flat text, it appears as "Payment successful 1 Sep ... ₹200 View details"
            # Give extra if ctx contains view details shortly after
            if "view details" in ctx_after.lower()[:40]:
                score += 6
            # Penalise amounts that look like % spill from OCR (mis-read ₹ → %)
            if m.group(0).strip().startswith("%"):
                score -= 8
            # Prefer amounts with ₹/Rs prefix over bare numbers, but pattern already encodes that
            candidates.append((value, score, s, m.group(0)))
    if not candidates:
        return None
    # Pick highest score, break ties by earliest position (closest to payment header) then smallest
    candidates.sort(key=lambda x: (-x[1], x[2], x[0]))
    return candidates[0][0]


def extract_amount_candidates(text: str) -> list[dict]:
    """Return all scored candidates for audit/UI (ignored amounts)."""
    if not text:
        return []
    flat = " ".join(text.split())
    out: list[dict] = []
    pay_positions = [m.start() for m in _PAYMENT_CTX.finditer(flat)]
    for pi, pattern in enumerate(AMOUNT_PATTERNS):
        if pi == 0:
            weight = 10
        elif pi == 1:
            weight = 5
        elif pi == 2:
            weight = 6
        elif pi == 3:
            weight = 7
        else:
            weight = 3
        for m in pattern.finditer(flat):
            raw = m.group(1).replace(",", "").replace(" ", "")
            try:
                value = float(raw)
            except ValueError:
                continue
            if (
                not (0 < value <= config.MAX_AMOUNT_INR)
                or len(raw.split(".", 1)[0]) > 9
            ):
                continue
            s, e = m.start(), m.end()
            ctx_before_low = flat[max(0, s - 40) : s].lower()
            ctx_after_low = flat[e : e + 40].lower()
            # Narrow audit rejection: only mark as rejected if immediately in balance/promo phrase
            is_bal = bool(
                re.search(
                    r"balance\s*[:>\-]?\s*[₹%]?\s*$", ctx_before_low.strip()[-28:]
                )
            )
            is_spent = bool(
                "you have spent" in ctx_before_low[-45:]
                or re.search(r"spent\s*[%₹]?\s*$", ctx_before_low.strip()[-30:])
            )
            is_sponsored = bool(
                "sponsored" in ctx_before_low[-30:] or "sponsored" in ctx_after_low[:30]
            )
            rejected = bool(is_bal or is_spent or is_sponsored)
            score = weight + (
                8
                if pay_positions and min(abs(s - p) for p in pay_positions) < 180
                else 0
            )
            if rejected:
                score -= 50
            ctx_full = (flat[max(0, s - 50) : e + 50]).lower()
            out.append(
                {
                    "raw": m.group(0),
                    "value": value,
                    "score": score,
                    "rejected": rejected,
                    "context": ctx_full[:80],
                }
            )
    out.sort(key=lambda x: -x["score"])
    return out


def extract_bank_accounts(text: str) -> list[dict]:
    # Normalise IFSC with/without space around 0/O (OCR 0↔O confusion)
    # e.g. "SBIN 0 001234" or "SBIN0001234" or "SBIN O001234"
    ifsc_pat = re.compile(r"\b([A-Z]{4})\s*[0O]\s*([A-Z0-9]{6})\b", re.IGNORECASE)
    ifscs = []
    for m in ifsc_pat.finditer(text):
        code = (m.group(1) + "0" + m.group(2)).upper()
        # Fix OCR O→0 in the 5th char is already 0; also fix 0↔O in tail
        code = code[:4] + "0" + code[5:].replace("O", "0").replace("o", "0")
        if code not in ifscs:
            ifscs.append(code)
    # also try original single-group pattern as fallback
    if not ifscs:
        for m in IFSC_RE.finditer(text):
            try:
                c = (m.group(1) + "0" + m.group(2)).upper()
            except IndexError:
                c = m.group(1).upper()
            if c not in ifscs:
                ifscs.append(c)
    accounts: list[dict] = []
    seen: set[str] = set()
    for match in ACCOUNT_CONTEXT_RE.finditer(text):
        raw = match.group(1)
        # OCR fix: O→0 for digit contexts, keep X/* masks
        # Don't translate isolated 'O' that is masking, but digit-surrounded O →0
        token = re.sub(r"[\s\-]", "", raw).upper().rstrip("X")
        # Fix common OCR confusables only inside digit runs
        token = re.sub(r"(?<=\d)O(?=\d)", "0", token)
        token = re.sub(r"[^0-9X*]", "", token)
        digits = re.sub(r"\D", "", token)
        if not (4 <= len(digits) <= 18 and 6 <= len(token) <= 20) or token in seen:
            continue
        seen.add(token)
        role, role_confidence = infer_rail_role(text, match.start(), match.end())
        accounts.append(
            {
                "account_number": token,
                "masked": "X" in token or "*" in token,
                "ifsc": ifscs[0] if len(ifscs) == 1 else "",
                "role": role,
                "role_confidence": role_confidence,
            }
        )
    # Fallback: standalone 9-18 digit number near account keywords without explicit label capture
    if not accounts:
        # Look for 9-18 digit standalone numbers that are likely accounts (not UTR=12, not phone=10 with 6-9 prefix)
        for m in re.finditer(r"\b([0-9OIl| ]{9,22})\b", text):
            cand = re.sub(
                r"[\sOIl|]",
                lambda x: (
                    "0" if x.group(0) in "O" else "1" if x.group(0) in "Il|" else ""
                ),
                m.group(1),
            )
            cand = re.sub(r"\D", "", cand)
            if not (9 <= len(cand) <= 18):
                continue
            if len(cand) == 12 and NUM12_RE.search(cand):
                # Don't steal UTR — skip if 12-digit and UTR label nearby
                window = text[max(0, m.start() - 30) : m.end() + 30].lower()
                if "utr" in window or "reference" in window or "ref" in window:
                    continue
                # skip Aadhaar-like 12
                if cand.startswith("2") or cand.startswith("3") or cand.startswith("4"):
                    # Aadhaar starts 2-9, but UTR also random — keep ambiguous out
                    pass
            if cand in seen:
                continue
            window = text[max(0, m.start() - 40) : m.start()].lower()
            if _ACCOUNT_KEYWORDS_RE.search(window):
                role, role_confidence = infer_rail_role(text, m.start(), m.end())
                accounts.append(
                    {
                        "account_number": cand,
                        "masked": False,
                        "ifsc": ifscs[0] if ifscs else "",
                        "role": role,
                        "role_confidence": role_confidence,
                    }
                )
                seen.add(cand)
                break
    return accounts


def extract_participants(text: str) -> dict:
    out: dict = {}
    beneficiary = BENEFICIARY_RE.search(text)
    sender = SENDER_RE.search(text)
    if beneficiary:
        out["beneficiary_name"] = " ".join(beneficiary.group(1).split())[:120]
    if sender:
        out["sender_name"] = " ".join(sender.group(1).split())[:120]
    # Fallback for UPI slips where beneficiary is centered below amount without label:
    # e.g. "Payment successful 1 Sep 2026, 5:04 PM View details ₹200 MAHESHWAR REDDY Balance: ₹355"
    if "beneficiary_name" not in out:
        flat = " ".join(text.split())
        # Segment between amount / View details and Balance/UPI Lite
        m = re.search(
            r"View\s+details\s+(.+?)\s+(?:Balance\s*:|UPI\s*Lite|What\s+was\s+this|Self\s+transfer)",
            flat,
            re.IGNORECASE,
        )
        if m:
            cand = " ".join(m.group(1).split())
            # Strip icons/artifacts like bank glyph remnants and trim to name-like tokens
            cand = re.sub(r"[^A-Za-z .]", " ", cand).strip()
            cand = re.sub(r"\s{2,}", " ", cand)
            # Heuristic: 2-4 words, each 2-20 letters, not containing payment keywords
            words = [w for w in cand.split() if len(w) >= 2]
            if 2 <= len(words) <= 4 and all(
                re.match(r"^[A-Za-z.]{2,20}$", w) for w in words
            ):
                # Reject if looks like promo text
                if not re.search(
                    r"(?:less\s+than|sponsored|valid\s+for)", cand, re.IGNORECASE
                ):
                    out["beneficiary_name"] = cand[:120]
        if "beneficiary_name" not in out:
            # Second fallback: line after ₹amount that is all caps name (common in Google Pay/PhonePe)
            m2 = re.search(
                r"[₹%]\s*[0-9,]+\s*(?:✓|✔|View\s+details)?\s*([A-Z][A-Z ]{3,40}?)\s*(?:Balance|UPI|What)",
                flat,
            )
            if m2:
                cand = " ".join(m2.group(1).split())
                if 2 <= len(cand.split()) <= 4 and cand.isupper():
                    out["beneficiary_name"] = cand.title()[:120]
    return out


def decode_qr_image(data: bytes) -> dict:
    """Decode QR from image bytes; extract UPI VPA / amount / payee name / crypto wallets."""
    out: dict = {
        "kind": "qr",
        "raw_text": None,
        "vpa": None,
        "payee_name": None,
        "amount_inr": None,
    }
    try:
        import zxingcpp
        from PIL import Image
        import io

        img = Image.open(io.BytesIO(data))
        results = zxingcpp.read_barcodes(img)
        if not results:
            img = img.convert("L").resize((img.width * 2, img.height * 2))
            results = zxingcpp.read_barcodes(img)
        if not results:
            return out
        text = results[0].text
        out["raw_text"] = text[:500]
        vpas = extract_vpas(text)
        if vpas:
            out["vpa"] = vpas[0]
            out["vpas"] = vpas
            out["vpa_details"] = [
                {
                    **detail,
                    # A payment URI/QR names its destination rail by protocol.
                    "role": "beneficiary",
                    "role_confidence": "high",
                }
                for detail in extract_vpa_details(text)
            ]
        pn = re.search(r"[?&]pn=([^&]+)", text)
        if pn:
            from urllib.parse import unquote

            out["payee_name"] = unquote(pn.group(1))[:120]
        am = re.search(r"[?&]am=([\d.]+)", text)
        if am:
            try:
                am_value = float(am.group(1))
            except ValueError:
                am_value = None
            # The am= field carries the money amount, but some collect-request /
            # reference QR codes embed a UTR-like value here — bound it by the
            # same plausibility rule as OCR so one bad QR can't skew totals.
            if am_value is not None and 0 < am_value <= config.MAX_AMOUNT_INR:
                out["amount_inr"] = am_value
        wallets = extract_wallets(text)
        if wallets:
            out["wallets"] = [
                {**wallet, "role": "beneficiary", "role_confidence": "high"}
                for wallet in wallets
            ]
    except Exception as e:
        out["error"] = str(e)[:200]
    return out


# ---- Gemini Vision: budgeted, parent-enforced (2026-09-03 robustness) ----
# decode workers run in spawned ProcessPool children, so quota state CANNOT
# live here per-process (each child would get its own budget). Children run
# tesseract-only and flag needs_gemini; the parent batch-verify path below
# (gemini_verify_payloads) owns budget/breaker/stats. The single-slip paths
# (process_key workers=1, /api/enrich) run in-process and may call
# _gemini_extract directly — it enforces the same budget via this gate.
import threading as _th

_gemini_lock = _th.Lock()
_gemini_day = ""
_gemini_used_today = 0
_gemini_consec_fail = 0
_gemini_cooldown_until = 0.0
_gemini_stats = {
    "calls": 0,
    "ok": 0,
    "errors": 0,
    "capped": 0,
    "cooled": 0,
    "last_error": "",
}
_gemini_client = None


def gemini_stats() -> dict:
    with _gemini_lock:
        d = dict(_gemini_stats)
        d["used_today"] = _gemini_used_today
        d["day_utc"] = _gemini_day
        d["cap"] = config.GEMINI_DAILY_CAP
        d["model"] = config.GEMINI_MODEL
        d["enabled"] = config.GEMINI_ENABLED
    return d


def _gemini_client_shared():
    global _gemini_client
    if _gemini_client is None:
        import httpx

        _gemini_client = httpx.Client(timeout=25.0)
    return _gemini_client


def _gemini_budget_claim() -> Optional[str]:
    """Claim one call from the daily budget + breaker. None = proceed."""
    import time as _t

    if not config.GEMINI_ENABLED:
        return "disabled"
    today = _t.strftime("%Y-%m-%d", _t.gmtime())
    with _gemini_lock:
        global _gemini_day, _gemini_used_today
        if _gemini_day != today:
            _gemini_day, _gemini_used_today = today, 0
        if _t.monotonic() < _gemini_cooldown_until:
            _gemini_stats["cooled"] += 1
            return "breaker-cooldown"
        if _gemini_used_today >= config.GEMINI_DAILY_CAP:
            _gemini_stats["capped"] += 1
            return "daily-budget-exhausted"
        _gemini_used_today += 1
        _gemini_stats["calls"] += 1
    return None


def _gemini_note(ok: bool, err: str = ""):
    import time as _t

    with _gemini_lock:
        global _gemini_consec_fail, _gemini_cooldown_until
        if ok:
            _gemini_consec_fail = 0
            _gemini_stats["ok"] += 1
        else:
            _gemini_consec_fail += 1
            _gemini_stats["errors"] += 1
            _gemini_stats["last_error"] = err[:200]
            if _gemini_consec_fail >= 5:
                _gemini_cooldown_until = _t.monotonic() + 600.0
                _gemini_consec_fail = 0


def _gemini_shrink(data: bytes) -> tuple[bytes, str]:
    """Downscale to max-768px JPEG for Vision: full phone screenshots waste
    ~10x tokens with zero extraction gain. Returns (bytes, mime)."""
    try:
        from PIL import Image as _Image
        import io as _io

        im = _Image.open(_io.BytesIO(data))
        try:
            from PIL import ImageOps as _Ops

            im = _Ops.exif_transpose(im)
        except Exception:
            pass
        if im.mode in ("RGBA", "LA", "P"):
            im = im.convert("RGB")
        w, h = im.size
        if max(w, h) > 768:
            im.thumbnail((768, 768))
        buf = _io.BytesIO()
        im.save(buf, "JPEG", quality=60, optimize=True)
        return buf.getvalue(), "image/jpeg"
    except Exception:
        if data[:8].startswith(b"\x89PNG"):
            return data, "image/png"
        if data[:4] == b"RIFF":
            return data, "image/webp"
        return data, "image/jpeg"


def _gemini_extract(data: bytes, text_sample: str = "") -> Optional[dict]:
    """Call Gemini Vision if a key is configured; return parsed JSON or None.

    Strict prompt: principal transaction amount ONLY, ignore Balance / promo.
    Returns dict with keys amount_inr, utr, vpa, beneficiary_name, etc. or None on failure.
    Budgeted: daily cap + 10-min breaker after 5 consecutive failures, 3 tries
    with backoff on 429/5xx (400-class is fatal, no retry).
    """
    blocked = _gemini_budget_claim()
    if blocked:
        return {"_gemini_error": blocked}
    try:
        import base64
        import json as _json
        import time as _t

        small, mime = _gemini_shrink(data)
        b64 = base64.b64encode(small).decode("ascii")

        prompt = (
            "You are a forensic payment-slip extractor for Indian UPI/bank screenshots. "
            "Extract ONLY the principal transaction amount — the large centered amount under 'Payment successful' / 'Transaction Successful' (e.g. ₹200). "
            "IGNORE: Balance (e.g. Balance: ₹355), promo 'You have spent ₹31,785 less than Aug', sponsored products, fees, or any other secondary amount. "
            "Return JSON only with keys: amount_inr (number or null), utr (12-digit string or null), vpa (string or null), "
            "beneficiary_name (string or null), sender_name (string or null), ifsc (string or null), bank_account (string or null). "
            "If unsure, use null. Do not invent. Text sample for reference: "
            + (text_sample[:300] if text_sample else "")
        )
        body = {
            "contents": [
                {
                    "parts": [
                        {"text": prompt},
                        {"inline_data": {"mime_type": mime, "data": b64}},
                    ]
                }
            ],
            "generationConfig": {
                "temperature": 0.0,
                "maxOutputTokens": 1024,
                "responseMimeType": "application/json",
            },
        }
        url = f"https://generativelanguage.googleapis.com/v1beta/models/{config.GEMINI_MODEL}:generateContent?key={config.GEMINI_API_KEY}"
        client = _gemini_client_shared()
        j = None
        last_err = "unknown"
        for attempt in range(3):
            try:
                r = client.post(url, json=body)
                if (
                    r.status_code == 404
                    and config.GEMINI_MODEL != "gemini-2.5-flash-lite"
                ):
                    # Model retired mid-deploy? One fallback try on flash-lite.
                    try:
                        r = client.post(
                            url.replace(config.GEMINI_MODEL, "gemini-2.5-flash-lite"),
                            json=body,
                        )
                    except Exception:
                        pass
                if r.status_code == 400:
                    last_err = f"400: {r.text[:200]}"
                    break  # fatal (bad model/prompt) — retrying won't help
                if r.status_code == 429 or r.status_code >= 500:
                    last_err = f"{r.status_code}: {r.text[:200]}"
                    _t.sleep(2.0 * (2**attempt))
                    continue
                r.raise_for_status()
                j = r.json()
                break
            except Exception as e:
                last_err = str(e)[:200]
                _t.sleep(2.0 * (2**attempt))
        if j is None:
            _gemini_note(False, last_err)
            return {"_gemini_error": last_err}
        # Parse candidates[0].content.parts[0].text
        try:
            txt = j["candidates"][0]["content"]["parts"][0]["text"]
        except Exception:
            return None
        # Model is asked for JSON; be robust to markdown fences AND prose
        # wrapping (observed live 2026-09-03: truncated/fenced replies raised
        # "Unterminated string", burning budget on 5 straight failures).
        txt = txt.strip()
        if txt.startswith("```"):
            txt = txt.strip("`")
            # remove leading json marker
            if txt.lower().startswith("json"):
                txt = txt[4:].strip()
        try:
            parsed = _json.loads(txt)
        except Exception:
            import re as _re

            m = _re.search(r"\{.*\}", txt, _re.S)
            if not m:
                raise
            parsed = _json.loads(m.group(0))
        # Sanitize amount via same bound as OCR
        amt = parsed.get("amount_inr")
        try:
            amt_f = float(amt) if amt is not None else None
        except Exception:
            amt_f = None
        if amt_f is not None and not (0 < amt_f <= config.MAX_AMOUNT_INR):
            amt_f = None
        parsed["amount_inr"] = amt_f
        # Normalise strings
        for k in (
            "utr",
            "vpa",
            "beneficiary_name",
            "sender_name",
            "ifsc",
            "bank_account",
        ):
            if parsed.get(k) is not None:
                parsed[k] = str(parsed[k]).strip() or None
        parsed["_gemini_raw"] = txt[:600]
        _gemini_note(True)
        return parsed
    except Exception as e:
        # Never fail OCR because Gemini failed — caller logs to error field
        _gemini_note(False, str(e)[:200])
        return {"_gemini_error": str(e)[:300]}


def _merge_gemini(out: dict, g: dict) -> None:
    """Merge a successful Gemini extraction into a tesseract payload (in place)."""
    if g.get("amount_inr") is not None:
        if out.get("amount_inr") != g["amount_inr"]:
            out["amount_tesseract"] = out.get("amount_inr")
            out["amount_inr"] = g["amount_inr"]
            out["engine"] = "tesseract+gemini"
        else:
            out["engine"] = "tesseract+gemini"
    for k in ("utr", "vpa", "beneficiary_name", "sender_name", "ifsc"):
        if not out.get(k) and g.get(k):
            out[k] = g[k]
    if g.get("bank_account") and not out.get("bank_accounts"):
        out["bank_accounts"] = [
            {
                "account_number": g["bank_account"],
                "masked": False,
                "ifsc": g.get("ifsc") or "",
            }
        ]
    out["_gemini"] = {k: g[k] for k in g if not k.startswith("_")}
    out["_gemini_raw"] = g.get("_gemini_raw")


def gemini_verify_payloads(items: list[tuple[str, dict, bytes]]) -> int:
    """Parent-side bounded Gemini pass over tesseract-ambiguous payloads.

    items: (key, payload, raw_image_bytes). Mutates payloads in place, returns
    number of successful verifications. Never raises.
    """
    if not config.GEMINI_ENABLED or not items:
        return 0
    done = 0
    for key, payload, data in items[: config.GEMINI_PER_BATCH]:
        try:
            if not isinstance(payload, dict) or not payload.pop("needs_gemini", False):
                continue
            g = _gemini_extract(data, payload.get("text_sample") or "")
            if g and not g.get("_gemini_error"):
                _merge_gemini(payload, g)
                done += 1
            elif g and g.get("_gemini_error"):
                payload["_gemini_error"] = g["_gemini_error"]
        except Exception:
            continue
    return done


def ocr_slip(data: bytes, allow_gemini: bool = True) -> dict:
    """OCR a deposit/withdrawal slip screenshot; extract amount/UTR/VPA.

    Improved: context-aware amount (principal only), audit of ignored candidates,
    optional Gemini Vision verification when tesseract is ambiguous or key present.
    allow_gemini=False (spawned decode workers): skip the Vision call and flag
    needs_gemini so the parent batch-verify path can spend budget centrally.
    """
    out: dict = {
        "kind": "ocr",
        "text_sample": None,
        "amount_inr": None,
        "utr": None,
        "vpa": None,
        "engine": "tesseract",
    }
    try:
        from PIL import Image, ImageOps, ImageEnhance
        import io

        img = Image.open(io.BytesIO(data))
        # Normalise mode and fix EXIF orientation before greyscale
        try:
            img = ImageOps.exif_transpose(img)
        except Exception:
            pass
        img = img.convert("L")
        # Improve contrast for green UPI headers and small grey balance text
        try:
            img = ImageOps.autocontrast(img, cutoff=1.0)
            # Slight sharpen helps tesseract on low-res phone screenshots
            from PIL.ImageFilter import SHARPEN

            img = img.filter(SHARPEN)
        except Exception:
            pass
        w, h = img.size
        # Cap giant inputs (camera originals, stitched chats): tesseract gains
        # nothing past ~2000px but costs 2-4x time. Downscale first, then the
        # small-image upscale below still applies to thumbnailed variants.
        if max(w, h) > 2000:
            try:
                img.thumbnail((2000, 2000))
                w, h = img.size
            except Exception:
                pass
        # Always upscale small phone screenshots; phone slips are ~1080×2400 but
        # thumbnailed GCS variants can be <800px. 2× LANCZOS preserves ₹ glyph.
        if max(w, h) < 1600:
            try:
                resample = getattr(Image, "LANCZOS", None) or getattr(
                    Image.Resampling, "LANCZOS", 1
                )
                img = img.resize((w * 2, h * 2), resample)
            except Exception:
                img = img.resize((w * 2, h * 2))
        # Optional mild contrast boost for faint promo grey text
        try:
            img = ImageEnhance.Contrast(img).enhance(1.2)
        except Exception:
            pass
        with tempfile.NamedTemporaryFile(suffix=".png", delete=True) as f:
            img.save(f.name, dpi=(300, 300))
            r = subprocess.run(
                [
                    "tesseract",
                    f.name,
                    "stdout",
                    "--oem",
                    "1",
                    "--psm",
                    "6",
                    "-l",
                    "eng",
                ],
                capture_output=True,
                text=True,
                timeout=60,
            )
            text = r.stdout or ""
            # Fallback: if tesseract returns near-empty (e.g. green header
            # washed out), retry with psm 11 (sparse text) once
            if len(text.strip()) < 12:
                r2 = subprocess.run(
                    [
                        "tesseract",
                        f.name,
                        "stdout",
                        "--oem",
                        "1",
                        "--psm",
                        "11",
                        "-l",
                        "eng",
                    ],
                    capture_output=True,
                    text=True,
                    timeout=60,
                )
                if len((r2.stdout or "").strip()) > len(text.strip()):
                    text = r2.stdout or text
        out["text_sample"] = " ".join(text.split())[:400]
        wallets = extract_wallets(text)
        if wallets:
            out["wallets"] = wallets
        tesseract_amount = extract_amount(text)
        out["amount_inr"] = tesseract_amount
        # Audit: keep ignored promo/balance candidates for dashboard transparency
        try:
            cands = extract_amount_candidates(text)
            # Keep at most 3 rejected for UI
            rejected = [c for c in cands if c["rejected"]][:3]
            if rejected:
                out["amount_candidates"] = cands[:4]
                out["amount_ignored"] = rejected
        except Exception:
            pass

        # --- UTR: labeled first, bare 12-digit fallback with account-context guard ---
        def _norm_utr(s: str) -> str:
            # Fix common OCR confusables only when surrounded by digits
            # UTR is strictly digits, so map O→0, I/l/|→1 before validation
            return s.translate(_OCR_DIGIT_FIX) if any(c in s for c in "OoIl|") else s

        utr_candidate: Optional[str] = None
        mu = UTR_RE.search(text)
        if mu:
            cand = _norm_utr(mu.group(1).strip().upper())
            # Strip non-digit suffix if alphanumeric reference (keep only digit runs)
            digits = re.sub(r"\D", "", cand)
            if len(digits) == 12:
                utr_candidate = digits
            elif 8 <= len(cand) <= 16 and re.match(r"^[A-Z0-9]{8,16}$", cand):
                # Some banks emit alphanumeric UTR-like refs — keep as-is if labeled
                utr_candidate = cand
        if not utr_candidate:
            # Bare 12-digit fallback — skip if clearly an account/balance context
            for mn in NUM12_RE.finditer(text):
                cand_digits = mn.group(1)
                s, e = mn.start(), mn.end()
                before = text[max(0, s - 40) : s].lower()
                after = text[e : e + 20].lower()
                # Skip if preceded by account/aadhaar/phone keywords
                if _ACCOUNT_KEYWORDS_RE.search(before):
                    continue
                if "aadhaar" in before or "aadhar" in before:
                    continue
                if re.search(r"\b(?:phone|mobile|91[ -]?)\s*$", before):
                    continue
                # Skip if this 12-digit is actually an amount candidate already (e.g. 31,785 with commas removed)
                # Amounts are already filtered by MAX_AMOUNT and would be 5-7 digits, not 12, so safe
                utr_candidate = cand_digits
                break
            # Also try OCR-fixed variant: e.g. "12O456789012" read as 12O...
            if not utr_candidate:
                # Search for 12-char alnum with OCR confusables that normalises to 12 digits
                for m in re.finditer(r"\b[0-9OIl|]{12}\b", text):
                    cand = _norm_utr(m.group(0))
                    if re.match(r"^\d{12}$", cand):
                        before = text[max(0, m.start() - 40) : m.start()].lower()
                        if _ACCOUNT_KEYWORDS_RE.search(before):
                            continue
                        utr_candidate = cand
                        break
        if utr_candidate:
            out["utr"] = utr_candidate
        vpas = extract_vpas(text)
        if vpas:
            out["vpa"] = vpas[0]
            out["vpas"] = vpas
            out["vpa_details"] = extract_vpa_details(text)
        accounts = extract_bank_accounts(text)
        if accounts:
            out["bank_accounts"] = accounts
            if accounts[0].get("ifsc"):
                out["ifsc"] = accounts[0]["ifsc"]
        out.update(extract_participants(" ".join(text.split())))

        # — Gemini verification: cost-controlled. Auto-call only when tesseract fails
        # to find a principal amount; otherwise rely on healed tesseract heuristics
        # (Balance/promo blacklists) and let officer force Gemini via Re-run button.
        use_gemini = False
        if config.GEMINI_ENABLED:
            has_rejected = bool(out.get("amount_ignored"))
            # Only auto-invoke when tesseract found nothing (e.g. faint ₹ glyph) —
            # the 200 vs 31785 case is now solved by context-aware scoring, no need to burn tokens.
            if tesseract_amount is None:
                # If we have rejected promo but no principal, likely ₹ was missed — Gemini can rescue
                use_gemini = True
            # Optional: also invoke if tesseract amount is plausible but confidence would be low
            # and the slip contains a high-value promo that dwarfed the true amount (defensive)
            # Disabled by default for cost; uncomment to enable:
            # elif has_rejected and out.get("confidence") == "low" and tesseract_amount is not None:
            #     use_gemini = True
            pass
        # Also allow forced Gemini via env flag for officers who want max accuracy
        if config.GEMINI_ENABLED and use_gemini:
            if not allow_gemini:
                # Spawned worker: defer to the parent batch-verify (budget/breaker).
                out["needs_gemini"] = True
            else:
                g = _gemini_extract(data, out.get("text_sample") or "")
                if g and not g.get("_gemini_error"):
                    _merge_gemini(out, g)
                elif g and g.get("_gemini_error"):
                    out["_gemini_error"] = g["_gemini_error"]

        if (
            out["amount_inr"] is None
            and out["utr"] is None
            and out["vpa"] is None
            and not wallets
        ):
            out["confidence"] = "low"
        else:
            # Downgrade confidence if we kept an ignored promo candidate — UI shows badge
            out["confidence"] = "medium" if not out.get("amount_ignored") else "low"
            if out.get("engine") == "tesseract+gemini":
                out["confidence"] = "high"
    except Exception as e:
        out["error"] = str(e)[:200]
    return out


def gemini_ocr_slip(data: bytes) -> dict:
    """Force Gemini path (used by Re-run OCR/QR when key present)."""
    base = ocr_slip(data)
    # If still tesseract-only, force a Gemini call regardless of heuristic
    if config.GEMINI_ENABLED and base.get("engine") == "tesseract":
        g = _gemini_extract(data, base.get("text_sample") or "")
        if g and not g.get("_gemini_error") and g.get("amount_inr") is not None:
            base["amount_tesseract"] = base.get("amount_inr")
            base["amount_inr"] = g["amount_inr"]
            base["engine"] = "gemini"
            base["_gemini"] = g
            base["confidence"] = "high"
        elif g and g.get("_gemini_error"):
            base["_gemini_error"] = g["_gemini_error"]
    return base


def crypto_filename_hint(key: str) -> bool:
    """chat_file images worth OCR-scanning for wallet addresses (cheap key filter).

    Trust Wallet package name, usdt/tron/wallet tokens, or deposit keywords —
    avoids pulling the entire ~700K chat image backlog into enrichment.
    """
    base = key.rsplit("/", 1)[-1].lower()
    return bool(
        re.search(r"trustapp|trust_wallet|usdt|tron|wallet|tether|binance|crypto", base)
    )


def classify(key: str, full_chat_scan: bool = False) -> str:
    top = key.split("/", 1)[0] + "/"
    if top in config.QR_PREFIXES:
        return "qr"
    if top in config.OCR_PREFIXES:
        return "deposit" if "deposit" in top else "withdrawal"
    if top == "chat_file/":
        if crypto_filename_hint(key):
            return "deposit"  # gambler-submitted wallet/USDT proof screenshots
        if full_chat_scan or config.WALLET_FULL_SCAN:
            return "deposit"  # full-bucket wallet hunt — scan every chat image for TRON/ERC20
    return "none"


def _extract_payload_from_bytes(
    key: str, data: bytes, full_scan: bool = False, allow_gemini: bool = True
) -> Optional[dict]:
    """CPU-heavy, process-safe extraction with no DB/client dependencies."""
    kind = classify(key, full_chat_scan=full_scan)
    if kind == "qr":
        return decode_qr_image(data)
    if kind not in ("deposit", "withdrawal"):
        return None
    payload = ocr_slip(data, allow_gemini=allow_gemini)
    payload["kind"] = kind
    q = decode_qr_image(data)  # slips often embed payment QR too
    if q.get("amount_inr") is not None or q.get("vpa"):
        payload.update({k: v for k, v in q.items() if v and not payload.get(k)})
    if q.get("vpas"):
        payload["vpas"] = list(dict.fromkeys((payload.get("vpas") or []) + q["vpas"]))
        payload["vpa"] = payload.get("vpa") or payload["vpas"][0]
        merged_details = {
            d.get("vpa"): d for d in (payload.get("vpa_details") or []) if d.get("vpa")
        }
        for detail in q.get("vpa_details") or []:
            # QR URI semantics are stronger than OCR proximity labels.
            merged_details[detail.get("vpa")] = detail
        payload["vpa_details"] = list(merged_details.values())
    if q.get("wallets"):
        merged = {w["address"]: w for w in (payload.get("wallets") or [])}
        for w in q["wallets"]:
            merged.setdefault(w["address"], w)
        payload["wallets"] = list(merged.values())
    return payload


def _decode_task(task: tuple[str, bytes, bool]):
    """Pickle-friendly ProcessPool entrypoint; errors return to the parent."""
    key, data, full_scan = task
    try:
        # Spawned child: tesseract-only; needs_gemini flags go to the parent
        # batch-verify so the daily budget + breaker are enforced centrally.
        return (
            key,
            _extract_payload_from_bytes(key, data, full_scan, allow_gemini=False),
            None,
        )
    except Exception as exc:
        return key, None, f"decode: {exc}"


class EnrichmentWorker:
    def __init__(
        self,
        s3: Any,
        master_vpas: set[str],
        on_result: Optional[Callable] = None,
        master_wallets: Optional[set[str]] = None,
    ):
        self.s3 = s3
        self.master_vpas = master_vpas
        self.master_wallets = master_wallets or set()
        self.on_result = on_result
        self.enabled = True
        self._drain_lock = threading.Lock()
        self._cpu_pool = None

    def _persist_payload(self, key: str, payload: Optional[dict]):
        if payload is None:
            return
        if payload.get("vpa"):
            v = payload["vpa"]
            payload["known_mule"] = v.lower() in self.master_vpas
            payload["new_mule"] = v.lower() not in self.master_vpas
        if payload.get("wallets"):
            for w in payload["wallets"]:
                addr = (w.get("address") or "").lower()
                w["known_operator"] = addr in self.master_wallets
                w["new_operator"] = bool(addr) and addr not in self.master_wallets
            payload["has_new_wallet"] = any(
                w.get("new_operator") for w in payload["wallets"]
            )
        db.mark_enriched(key, payload)
        if self.on_result:
            self.on_result({"type": "enriched", "key": key, "payload": payload})

    def process_key(self, key: str, full_scan: bool = False):
        row = db.get_object_row(key)
        if not row or row.get("enriched"):
            return
        try:
            data = fetch_bytes(self.s3, key)
        except Exception as e:
            db.mark_enrichment_error(key, f"fetch: {e}")
            return
        try:
            payload = _extract_payload_from_bytes(key, data, full_scan)
        except Exception as e:
            db.mark_enrichment_error(key, f"decode: {e}")
            return
        if payload is None:
            # Object is not an enrichable kind (e.g. chat image without wallet hint
            # outside full-scan). Mark finished so it never re-enters the queue —
            # this was the source of the never-draining 1M backlog re-fetching
            # the same keys every 2s cycle.
            db.mark_enriched(key, {"kind": "none", "skipped": True})
            return
        self._persist_payload(key, payload)

    def drain_pending(self, batch: int = 25, workers: int = 1, full_scan: bool = False):
        # poll_loop and the dedicated enrich_loop can request a drain at the
        # same time. One guarded batch prevents duplicate fetch/decode work.
        if not self._drain_lock.acquire(blocking=False):
            return
        try:
            keys = db.pending_enrichment(batch, full_scan=full_scan)
            if workers <= 1:
                for k in keys:
                    if not self.enabled:
                        break
                    self.process_key(k, full_scan=full_scan)
                return

            from concurrent.futures import ProcessPoolExecutor, ThreadPoolExecutor
            import multiprocessing

            def fetch_one(k: str):
                row = db.get_object_row(k)
                if not row or row.get("enriched"):
                    return k, None, None
                try:
                    return k, fetch_bytes(self.s3, k), None
                except Exception as exc:
                    return k, None, f"fetch: {exc}"

            # Fetch is GCS-latency-bound, not CPU-bound: run it wider than the
            # decode worker count so tesseract procs never starve for bytes.
            with ThreadPoolExecutor(
                max_workers=min(16, len(keys) or 1),
                thread_name_prefix="evidence-fetch",
            ) as fetch_pool:
                fetched = list(fetch_pool.map(fetch_one, keys))

            tasks = []
            for key, data, error in fetched:
                if error:
                    db.mark_enrichment_error(key, error)
                elif data is not None:
                    tasks.append((key, data, full_scan))
            if not tasks:
                return
            if self._cpu_pool is None:
                self._cpu_pool = ProcessPoolExecutor(
                    max_workers=workers,
                    mp_context=multiprocessing.get_context("spawn"),
                )
            results = list(self._cpu_pool.map(_decode_task, tasks))
            by_key = {k: d for k, d, _fs in tasks}
            try:
                flagged = [
                    (key, payload, by_key[key])
                    for key, payload, error in results
                    if not error
                    and isinstance(payload, dict)
                    and payload.get("needs_gemini")
                    and key in by_key
                ]
                if flagged:
                    from . import config as _cfg

                    if _cfg.GEMINI_ENABLED:
                        # Vision calls take ~2s each — NEVER hold the 2s
                        # drain_lock for them. Persist tesseract payloads now
                        # (saved even if Gemini lags), verify in background
                        # and re-persist merged results as they land.
                        def _bg(items=flagged, worker=self):
                            try:
                                gemini_verify_payloads(items)
                                for key, payload, _d in items:
                                    if payload.get("needs_gemini"):
                                        continue  # over per-batch cap; keep tesseract row
                                    try:
                                        worker._persist_payload(key, payload)
                                    except Exception:
                                        pass
                            except Exception:
                                pass

                        threading.Thread(
                            target=_bg, daemon=True, name="gemini-verify"
                        ).start()
                    else:
                        for _, p, _d in flagged:
                            p.pop("needs_gemini", None)
            except Exception:
                pass
            for key, payload, error in results:
                if error:
                    db.mark_enrichment_error(key, error)
                else:
                    self._persist_payload(key, payload)
        finally:
            self._drain_lock.release()

    def close(self):
        self.enabled = False
        if self._cpu_pool is not None:
            self._cpu_pool.shutdown(wait=False, cancel_futures=True)
