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
# Status/UI words that OCR glues to a preceding token ("1336@completed",
# "clothe@completed" — verified 2026-09-11 on deposit slips). No UPI handle
# is ever one of these, so any vpa@<word> match is discarded outright.
_VPA_STATUS_WORDS = {
    "completed",
    "complete",
    "success",
    "successful",
    "failed",
    "failure",
    "pending",
    "processing",
    "processed",
    "confirmed",
    "received",
    "sent",
    "payment",
    "payments",
    "transaction",
    "transactions",
    "transferred",
    "debited",
    "credited",
    "receipt",
    "invoice",
    "statement",
    "balance",
    "amount",
    "status",
    "history",
    "details",
    "detail",
    "refund",
    "refunded",
    "cashback",
    "reward",
    "rewards",
    "offer",
    "offers",
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
# Blockchain transaction hash (TRON/EVM txid: 64 hex). Verified 2026-09-04:
# a Binance receipt's wrapped TRON txid "144fdabb…baa6b|c0fde…" had its first
# 28-char line falsely match BTC_RE (starts with '1', all base58 chars) and
# was shown as a BITCOIN wallet with ₹70. Txids are evidence, never wallets.
TXID_RE = re.compile(r"\b([0-9a-fA-F]{64})\b")
_CRYPTO_CCY_RE = re.compile(
    r"\b(usdt|usdc|tusd|dai|btc|xbt|eth|trx|bnb|matic|sol)\b", re.IGNORECASE
)
_FIAT_CCY_RE = re.compile(r"(₹|rs\.?|inr|\$)", re.IGNORECASE)
# Plausibility bounds per currency (crypto bounds are generous — on-chain
# verification, not OCR, is the authority for large crypto moves).
CRYPTO_MAX_AMOUNT = 50_000_000.0
CRYPTO_HINT_RE = re.compile(
    r"usdt|trc.?20|erc.?20|tron|trust\s*wallet|binance|coinbase|wallet", re.IGNORECASE
)

_BENEFICIARY_ROLE_RE = re.compile(
    r"(?:paid\s+to|payee|beneficiary|recipient|receiver|credited\s+to|sent\s+to|transfer(?:red)?\s+to|\bto\b)\s*[:=\-]?",
    re.IGNORECASE,
)
_SENDER_ROLE_RE = re.compile(
    r"(?:paid\s+by|payer|sender|remitter|debited\s+from|received\s+from|transfer(?:red)?\s+from|from)\s*[:=\-]?",
    re.IGNORECASE,
)


# Join whitespace ONLY where a wrapped txid was split mid-run: 20+ hex chars
# on each side. A plain nospace-join glues words to hashes ("Txid 144f…" →
# "…d144f…"), shifting the 64-window by one ("d144…f34a", seen live).
_TXID_WRAP_JOIN_RE = re.compile(r"(?<=[0-9a-fA-F]{20})\s+(?=[0-9a-fA-F]{20})")


def extract_txids(text: str) -> list[str]:
    """Pull 64-hex blockchain transaction hashes (evidence, never wallets).

    Reassembles OCR line-wrapped txids ("…baa6b\\nc0fde…", verified on a
    Binance receipt 2026-09-04) via _TXID_WRAP_JOIN_RE, then matches exact
    64-hex tokens — no offset drift, no word-glued prefixes.
    """
    if not text:
        return []
    joined = _TXID_WRAP_JOIN_RE.sub("", text)
    out: list[str] = []
    seen: set[str] = set()
    for m in TXID_RE.finditer(joined):
        h = m.group(1).lower()
        if h not in seen:
            seen.add(h)
            out.append(h)
    return out


# A wallet-looking token immediately after a Txid label is a transaction
# hash fragment, never a wallet (verified live 2026-09-04: degraded tesseract
# read "Txid 144fdabb…baa6b cOfde…" — OCR errors broke the 64-hex run so the
# long-run guard could not fire, but the label is unambiguous).
_TXID_LABEL_RE = re.compile(
    r"(?:\btx\.?\s*id\b|\btxid\b|transaction\s*(?:hash|id)\b|hash\b)\s*[:#]?\s*$",
    re.IGNORECASE,
)


def _labeled_as_txid(text: str, start: int, window: int = 60) -> bool:
    """True if a Txid/hash label sits immediately before position start."""
    before = text[max(0, start - window) : start]
    return bool(_TXID_LABEL_RE.search(before))


def _in_long_hex_run(text: str, start: int, end: int, min_run: int = 40) -> bool:
    """True if the [start:end) token is a fragment of a longer hex run.

    Whitespace-tolerant: OCR wraps 64-hex txids across lines, so each wrapped
    line looks like a standalone token. Joins a ±40-char window and checks
    whether the candidate sits inside a hex run of >= min_run chars.
    """
    window = text[max(0, start - 40) : end + 40]
    nospace = re.sub(r"\s+", "", window)
    frag = re.sub(r"\s+", "", text[start:end])
    if not frag:
        return False
    idx = nospace.find(frag)
    if idx < 0:
        return False
    run_start, run_end = idx, idx
    while run_start > 0 and nospace[run_start - 1] in "0123456789abcdefABCDEF":
        run_start -= 1
    while run_end < len(nospace) and nospace[run_end] in "0123456789abcdefABCDEF":
        run_end += 1
    return (run_end - run_start) >= min_run


# A txid label followed by a SHORT gap of hash-like text then the candidate
# means the candidate is the tail of one broken hash (verified live
# 2026-09-11: "Transaction ID O NEF54¢3985c…f1c" — the label's own value got
# glued to OCR garbage, so the tight immediate-label guard never fired and a
# 29-char fragment was stored as a BITCOIN wallet).
_TXID_LABEL_ANY_RE = re.compile(
    r"\btx\.?\s*id\b|\btxid\b|transaction\s*(?:hash|id)\b|\bhash\b"
    r"|upi\s*ref(?:erence)?(?:\s*id)?\b|ref(?:erence)?\s*(?:no|id|number)\b|\brrn\b",
    re.IGNORECASE,
)


def _labeled_as_txid_loose(text: str, start: int, end: int, window: int = 120) -> bool:
    """True if a Txid/hash/ref label sits NEAR start with only hash-like
    text between the label and the candidate.

    The gap (label value remainder + OCR junk) plus the candidate must form a
    long (>= 20 chars) mostly-hex (>= 60%) segment. Genuine addresses carry
    enough non-hex base58 letters to stay below the ratio; the gap cap (45)
    keeps distant labels from suppressing real wallets.
    """
    before = text[max(0, start - window) : start]
    label = None
    for mm in _TXID_LABEL_ANY_RE.finditer(before):
        label = mm
    if not label:
        return False
    gap = before[label.end() :]
    if len(gap.strip()) > 45:
        return False
    seg = re.sub(r"[\s.…]+", "", gap + text[start:end])
    if len(seg) < 20:
        return False
    hexcount = sum(ch in "0123456789abcdefABCDEF" for ch in seg)
    return hexcount / len(seg) >= 0.6


def _after_ellipsis(text: str, start: int, end: int, window: int = 15) -> bool:
    """True if the candidate sits right after an ellipsis with a hex head.

    Truncated-display shape ("8ce402586fedf9 ...111bcedb…", verified live
    2026-09-11): the visible tail is a display fragment, never an address.
    """
    before = text[max(0, start - window) : start]
    if not re.search(r"(?:\.\.\.|\u2026)\s*$", before):
        return False
    head = re.match(r"[0-9a-fA-F]+", text[start:end])
    return bool(head) and len(head.group(0)) >= 8


_B58_ALPHABET = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"


def _tron_checksum_ok(addr: str) -> bool:
    """Validate a TRON address via base58check (version 0x41 + double-SHA256).

    On-chain math, not OCR, is the authority for whether a T-address is real.
    """
    try:
        n = 0
        for ch in addr:
            n = n * 58 + _B58_ALPHABET.index(ch)
        raw = n.to_bytes(25, "big")
    except Exception:
        return False
    if len(raw) != 25 or raw[0] != 0x41:
        return False
    import hashlib

    chk = hashlib.sha256(hashlib.sha256(raw[:21]).digest()).digest()[:4]
    return chk == raw[21:]


# Relaxed TRON shape for OCR-degraded reads (lookalike errors 0/O, 5/S…).
# Matches are NEVER kept directly — only checksum-validated variants are.
TRON_RELAXED_RE = re.compile(r"\b(T[1-9A-HJ-NP-Za-km-z0OIil]{33})\b")
_TRON_CONFUSE = {
    "0": "oOQ",
    "O": "o0Q",
    "o": "0O",
    "I": "1l",
    "l": "1I",
    "1": "lI",
    "S": "589",
    "5": "S6",
    "8": "B6",
    "B": "86",
    "6": "b85",
    "b": "6",
}


def _recover_tron_ocr(token: str, combo_cap: int = 1500) -> Optional[str]:
    """Return a checksum-valid variant of an OCR-degraded T-address, else None.

    Tries lookalike substitutions at ambiguous positions (capped combinatorial
    budget). A wrong guess cannot survive: only base58check-valid output wins.
    """
    spots = [
        (i, token[i] + "".join(o for o in _TRON_CONFUSE[token[i]] if o != token[i]))
        for i, ch in enumerate(token)
        if ch in _TRON_CONFUSE
    ]
    if not spots:
        return None
    total = 1
    for _, opts in spots:
        total *= len(opts)
        if total > combo_cap:
            return None
    chars = list(token)

    def _dfs(k: int) -> Optional[str]:
        if k == len(spots):
            cand = "".join(chars)
            return cand if _tron_checksum_ok(cand) else None
        i, opts = spots[k]
        orig = chars[i]
        for o in opts:
            chars[i] = o
            hit = _dfs(k + 1)
            if hit:
                return hit
        chars[i] = orig
        return None

    return _dfs(0)


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
        # Same txid-fragment class as BTC (a glued "Txid"+hash can read as T…).
        if _labeled_as_txid(text, m.start()):
            continue
        if _labeled_as_txid_loose(text, m.start(), m.end()):
            continue
        if _after_ellipsis(text, m.start(), m.end()):
            continue
        # checksum-ish sanity: reject all-same-char artifacts from OCR smears
        if len(set(addr)) < 8:
            continue
        checksum_ok = _tron_checksum_ok(addr)
        seen.add(addr)
        role, role_confidence = infer_rail_role(text, m.start(), m.end())
        found.append(
            {
                "address": addr,
                "network": "TRON",
                "standard": "TRC-20",
                # OCR can flip case/lookalikes yet stay in-charset; the
                # base58check checksum is the authority — invalid checksum
                # downgrades confidence instead of silently asserting.
                "confidence": "high" if checksum_ok else "medium",
                "checksum": "valid" if checksum_ok else "invalid",
                "role": role,
                "role_confidence": role_confidence,
            }
        )
    # OCR-degraded TRON recovery: a T-address with lookalike errors (0-for-o,
    # 5-for-S…) misses TRON_RE. Retried charset-relaxed, but ONLY a variant
    # whose base58check checksum validates is kept — zero false-positive risk.
    for m in TRON_RELAXED_RE.finditer(text):
        token = m.group(1)
        if token in seen:
            continue
        if _labeled_as_txid(text, m.start()):
            continue
        if _labeled_as_txid_loose(text, m.start(), m.end()):
            continue
        if _after_ellipsis(text, m.start(), m.end()):
            continue
        fixed = _recover_tron_ocr(token)
        if not fixed or fixed in seen:
            continue
        seen.add(fixed)
        role, role_confidence = infer_rail_role(text, m.start(), m.end())
        found.append(
            {
                "address": fixed,
                "network": "TRON",
                "standard": "TRC-20",
                "confidence": "medium",
                "checksum": "valid",
                "ocr_recovered": True,
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
        # Txids are not wallets: a wrapped 64-hex TRON txid starting with '1'
        # matches BTC_RE on its first line (verified 2026-09-04). Suppress any
        # candidate that is a fragment of a longer hex run, or that sits right
        # behind a Txid/hash label (degraded OCR breaks the run but not the label).
        if _in_long_hex_run(text, m.start(), m.end()):
            continue
        if _labeled_as_txid(text, m.start()):
            continue
        if _labeled_as_txid_loose(text, m.start(), m.end()):
            continue
        if _after_ellipsis(text, m.start(), m.end()):
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
    # Rs/INR-prefixed without an "amount" label ("Rs.2,000", "INR 2000" on
    # GPay/bank slips) — 2026-09-11: bare misses like these surfaced as empty (—) amounts.
    re.compile(
        r"(?:\brs\.?\b|\binr\b)\s*([0-9][0-9,]*(?:\.[0-9]{1,2})?)", re.IGNORECASE
    ),
    # Paytm success: UPI ID then the hero rupees with the ₹ glyph eaten
    # (officer exhibit deposit/1789719398070.jpg: "okbizaxis a 150" stored
    # as 31,49,658 from the scratch split of Ref. No 314965897745).
    # 3–6 digits so day-of-month "18" is not captured.
    re.compile(
        r"upi\s*id\s*:\s*\S+\s+[^\d]{0,12}(\d{3,6})\b",
        re.IGNORECASE,
    ),
]
# Context clues that an amount is NOT the principal transaction — balance, promo/ads
_BALANCE_CTX = re.compile(r"balance\s*[:>\-]?\s*[₹%]?\s*[0-9,]*", re.IGNORECASE)
_SPENT_CTX = re.compile(
    r"you\s+have\s+spent|spent\s+.*less\s+than|less\s+than\s+aug|sponsored\s+products|valid\s+for\s+24",
    re.IGNORECASE,
)
_PAYMENT_CTX = re.compile(
    r"payment\s+successful|paid\s+to|paid\s+by|transaction\s+successful|"
    r"debited\s+from|view\s+details|\bpaytm\b|upi\s*id",
    re.IGNORECASE,
)
# In-app ad banners beside the receipt ("iPhone 16 Pro ₹50,000 Buy now",
# verified 2026-09-11: beat the true ₹2,000 principal when sitting near the
# header; "Get up to ₹60,000 credit on UPI / Unlock Paytm Postpaid" banner,
# verified 2026-09-11: tilde-pattern beat the true ₹450 principal).
# Narrow ±30-char window; word-boundaries so "premium"/"wholesale"
# never match. Silent drop (no Gemini trigger — ads are certain decoys).
_AD_CTX_RE = re.compile(
    r"buy\s*now|shop\s*now|add\s*to\s*cart|\bemi\b|discount|\bmrp\b|\bsale\b"
    r"|deal\s+of\s+the|coupon|cashback|order\s*now|limited\s*(period|offer)|hurry\b"
    r"|\bup\s*to\b|\bupto\b|getupto|get\s*up\s*to|postpaid|pre[\s-]?approved"
    r"|instant\s+(loan|credit|approval)|credit\s+(limit|line|on\s+upi)",
    re.IGNORECASE,
)
# Generic "up to / upto <amount>" cap decoy — the Navi loan ad
# ("Get upto INR 20,00,000.00 instant", verified 2026-09-11: beat every true
# principal on 12 slips) and Paytm Postpaid banner ("~60,000 credit",
# verified 2026-09-11: beat the true ₹450). Fires on the cap phrase itself,
# no promo-footer words required.
_UPTO_CAP_RE = re.compile(r"up\s*to|upto|getupto", re.IGNORECASE)
# Reference/RRN context — a bare digit run right after ref/RRN wording is a
# transaction/reference id, never the principal ("RRNNumber « = 6252477"
# stored 6.25M over TRUE 1,500; "Ref. No: 613726905" stored 6.1M;
# verified 2026-09-11).
_REF_CTX_RE = re.compile(
    r"(?:\bref\b\.?|\breference\b|\brrn\b|ref\.?\s*(?:no|number|num))\s*"
    r"[a-z]*\s*[«»:=~#\-]?\s*=?\s*$",
    re.IGNORECASE,
)
# Scratch-card promo context — number followed by SCRATCH is the game id
# ("~ 6137269 SCRATCH", verified 2026-09-11).
_SCRATCH_CTX_RE = re.compile(r"^\s*~?\s*scratch", re.IGNORECASE)
# Paytm success screen prints SCRATCHCARD *above* the split ref
# (officer exhibit deposit/1789719398070.jpg: ₹150 stored as 31,49,658).
_SCRATCH_BEFORE_RE = re.compile(r"scratch(?:\s*card|card)?", re.IGNORECASE)
# 10–16 digit UPI/Paytm refs. A 7–9 digit "amount" that is a prefix of one
# is the truncated ref, never the principal (3149658 from 314965897745).
_LONG_ID_RE = re.compile(r"\d{10,16}")
# VPA tail — digits running into @ belong to a VPA local part, never an
# amount ("rs3689580-1@okaxis" stored 3.69M twice, verified 2026-09-11).
_VPA_TAIL_RE = re.compile(r"[\w.\-]*@")
# Indian (7,14,000) vs western (714,000) digit grouping.
_IND_GROUP_RE = re.compile(r"^\d{1,2}(,\d{2})*(,\d{3})$")
_WEST_GROUP_RE = re.compile(r"^\d{1,3}(,\d{3})+$")


def detect_bad_grouping(num: str) -> bool:
    """True when a comma-bearing digit string matches NEITHER Indian NOR
    western grouping — OCR ate/added a digit ('%714,0000' 10x'd a ₹7.14L
    slip to ₹71.4L; '%50080,00' 100x'd ₹50,080 to ₹50.08L; '%36,000,00'
    100x'd ₹36,000; verified 2026-09-11). No-comma strings are unjudgeable
    here (bare true amounts like 710000 exist) → False.
    """
    if not num or "," not in num:
        return False
    head = num.split(".", 1)[0].strip()
    return not (_IND_GROUP_RE.match(head) or _WEST_GROUP_RE.match(head))


UTR_RE = re.compile(
    # 2026-09-16: GPay/PhonePe slips label it "UPI transaction ID" /
    # "Transaction ID" (e.g. "UPI transaction ID 129679804197" on a
    # ₹1,500 Union Bank deposit slip) — the old pattern only knew
    # UTR/Ref/reference, so labeled UTRs fell through to the bare
    # 12-digit fallback where the "bank account" guard killed them.
    r"\b(?:UTR|utr|UPI\s*transaction\s*ID|transaction\s*ID|txn\s*(?:ID|no)|Ref\.?(?: no)?|reference)\s*[:#\-]?\s*([A-Z0-9]{8,16})\b",
    re.IGNORECASE,
)
# Strict 12-digit UTR fallback — but must not be an account/phone/aadhaar
NUM12_RE = re.compile(r"\b(\d{12})\b")
# UTR-label proximity: when a bare 12-digit number sits right after an
# explicit UTR-ish label, it is the UTR even if an account/balance keyword
# appears nearby ("...deposit of money to X's bank account | UPI transaction
# ID 129679804197", verified 2026-09-16). Checked against the last ~30 chars
# before the candidate so the label must be immediately adjacent.
_UTR_LABEL_NEAR_RE = re.compile(
    r"(?:\butr\b|upi\s*transaction\s*id|transaction\s*id|txn\s*(?:id|no)|"
    r"\bref\b\.?|reference|\brrn\b)\s*[:#\-]?\s*$",
    re.IGNORECASE,
)
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
    # Tesseract often reads @ as © on small phone screenshots — recover those
    # (2026-09-11: VPAs missed entirely → empty UPI column on deposit rows).
    norm = (text or "").replace("©", "@")
    # Normalise stray spaces around @ introduced by tesseract line breaks
    # but keep original for other extractors
    norm = re.sub(r"\s*@\s*", "@", norm)
    out: list[str] = []
    seen: set[str] = set()
    for m in VPA_RE.finditer(norm):
        raw = re.sub(r"\s+", "", m.group(0)).lower().strip(".,;:)\"' ")
        # Strip trailing punctuation leftover from sentence ends
        raw = raw.strip()
        if not plausible_vpa(raw):
            continue
        handle = raw.split("@", 1)[1] if "@" in raw else ""
        # Status/UI word glued by OCR ("1336@completed") — never a real handle
        if handle in _VPA_STATUS_WORDS:
            continue
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


def extract_vpa_details(text: str, allowed: Optional[set] = None) -> list[dict]:
    """Return normalized VPAs with payer/beneficiary role provenance.

    Pass the already-computed extract_vpas() set as `allowed` to skip a
    duplicate full-text VPA sweep (2026-09-11: ocr_slip ran the VPA_RE scan
    twice per slip — once here, once in extract_vpas).
    """
    # Same normalization as extract_vpas (©→@ recovery + space collapse) so a
    # caller-passed `allowed` set always matches this scan's tokens.
    norm = re.sub(r"\s*@\s*", "@", (text or "").replace("©", "@"))
    if allowed is None:
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


def detect_amount_currency(text: str, start: int, end: int) -> str:
    """Currency of an amount match from its ±30-char window.

    Crypto tokens win over fiat glyphs ("Amount 69.5 USDT" → USDT, verified
    2026-09-04: was stored as amount_inr and shown as ₹70). Default INR —
    this bucket is overwhelmingly UPI rupee slips.
    """
    window = text[max(0, start - 30) : end + 30]
    mc = _CRYPTO_CCY_RE.search(window)
    if mc:
        tok = mc.group(1).upper()
        return {"XBT": "BTC"}.get(tok, tok)
    mf = _FIAT_CCY_RE.search(window)
    if mf:
        return "USD" if mf.group(1) == "$" else "INR"
    return "INR"


_AMOUNT_PATTERN_WEIGHTS = {
    0: 10,  # txn/transaction amount label
    1: 5,  # amount label
    2: 6,  # ₹/Rs/INR/= prefix
    3: 7,  # tilde ~3,000 — principal on PhonePe/GooglePay slips
    4: 6,  # "=2,500" — tesseract reads ₹ as '=' (verified PhonePe misread)
    5: 4,  # spaced thousands "1 000" / "2 000"
    7: 5,  # Rs./INR-prefixed without label
    8: 12,  # Paytm UPI-ID then hero 3–6 digit amount
    # default 3: bare 2,000 — fallback when ₹ missed by OCR
}


def _scan_amount_matches(flat: str) -> list[dict]:
    """Single full-text amount sweep backing picker AND audit (2026-09-11b:
    ocr_slip ran this 8-pattern sweep TWICE per slip — once for the winner,
    once for the audit trail — roughly doubling post-OCR regex CPU).

    Each dict carries: value, score (picker formula), s, e, match, ccy,
    dropped (picker skips it — same blacklist conditions as before),
    rejected + reason (narrow balance/promo flags — these drive the UI
    'ignored' badge AND Gemini adjudication, unchanged), audit_score (legacy
    audit formula so the UI candidate order is preserved), context.
    Out-of-bound values and far-from-context bare numbers stay silent.
    """
    pay_positions = [m.start() for m in _PAYMENT_CTX.finditer(flat)]
    long_ids = _LONG_ID_RE.findall(flat)
    scanned: list[dict] = []
    for pi, pattern in enumerate(AMOUNT_PATTERNS):
        weight = _AMOUNT_PATTERN_WEIGHTS.get(pi, 3)
        for m in pattern.finditer(flat):
            raw = m.group(1).replace(",", "").replace(" ", "")
            try:
                value = float(raw)
            except ValueError:
                continue
            s, e = m.start(), m.end()
            ccy = detect_amount_currency(flat, s, e)
            bound = (
                CRYPTO_MAX_AMOUNT
                if ccy not in ("INR", "USD")
                else config.MAX_AMOUNT_INR
            )
            if not (0 < value <= bound):
                continue
            bare_digits = raw.split(".", 1)[0]
            if len(bare_digits) > 9:
                continue
            # OCR debris "~ 4" / "= 8" beside Transfer Details (officer
            # exhibit deposit/1789721443450.jpeg: reparse stored ₹4 instead
            # of ₹3,000). PhonePe ~amounts are thousands (~3,000).
            if pi in (3, 4) and len(bare_digits) < 3:
                continue
            # Truncated Paytm/UPI ref used as rupees (₹150 slip stored
            # 31,49,658 because scratch showed 3149658 / 97745).
            if len(bare_digits) >= 7 and any(
                lid != bare_digits and lid.startswith(bare_digits) for lid in long_ids
            ):
                continue
            ctx_before = flat[max(0, s - 60) : s]
            ctx_after = flat[e : e + 60]
            ctx_before_low = ctx_before.lower()
            ctx_after_low = ctx_after.lower()
            # Blacklist ONLY if amount is itself the balance/promo value.
            dropped, reason = False, ""
            if re.search(
                r"balance\s*[:>\-]?\s*[₹%]?\s*$", ctx_before_low.strip()[-28:]
            ):
                dropped, reason = True, "balance"
            elif "you have spent" in ctx_before_low[-45:] or re.search(
                r"spent\s*[%₹]?\s*$", ctx_before_low.strip()[-30:]
            ):
                dropped, reason = True, "spent-promo"
            elif _SPENT_CTX.search(ctx_before_low[-50:] + " " + m.group(0).lower()):
                # ensure spent is immediate predecessor, not 60 chars after;
                # otherwise fall through to the sponsored/up-to checks below
                if "spent" in ctx_before_low[-40:]:
                    dropped, reason = True, "spent-promo"
            if not dropped and (
                "sponsored" in ctx_before_low[-35:] or "sponsored" in ctx_after_low[:35]
            ):
                if "sponsored" in (ctx_before_low[-35:] + ctx_after_low[:35]):
                    if "sponsored" in ctx_before_low or "sponsored" in ctx_after_low:
                        if (
                            "sponsored" in ctx_before_low[-30:]
                            or "sponsored" in ctx_after_low[:30]
                        ):
                            dropped, reason = True, "sponsored"
                        else:
                            dropped, reason = False, ""
                    else:
                        dropped, reason = False, ""
                else:
                    pass
            if not dropped and (
                "up to" in ctx_before_low[-30:]
                and "seamless" in (ctx_before_low + ctx_after_low).lower()[:60]
            ):
                dropped, reason = True, "promo-cap"
            # In-app ad / shopping banner price — never the deposit principal.
            # Narrow exemption: bill-class words GOVERNED by paid/debited
            # ('Paid postpaid bill ₹1,200' is a real transfer). Hard-ad words
            # (buy now/EMI/discount/...) always drop, and slip headers like
            # 'Payment successful' never exempt a neighboring banner
            # (verified 2026-09-11: iPhone ₹50,000 beside header must lose).
            if not dropped and (
                _AD_CTX_RE.search(ctx_before_low[-30:])
                or _AD_CTX_RE.search(ctx_after_low[:30])
            ):
                _bill_gov = re.search(
                    r"\b(?:paid|debited)\b.{0,25}(?:postpaid|\bbill\b|credit\s+card|due\s+tomorrow)",
                    ctx_before_low[-40:],
                )
                _hard_ad = re.search(
                    r"buy\s*now|\bemi\b|discount|cashback|sponsored|up\s*to"
                    r"|upto|getupto|loan|credit\s+(?:limit|line|on\s+upi)"
                    r"|pre[\s-]?approved|instant\s+(?:loan|credit|approval)",
                    ctx_before_low[-30:] + " " + ctx_after_low[:30],
                )
                if not (_bill_gov and not _hard_ad):
                    dropped, reason = True, "ad-promo"
            if not dropped and "up to" in ctx_before_low[-28:] and "%" in m.group(0):
                dropped, reason = True, "promo-cap"
            # Malformed comma grouping — OCR ate/added a digit, value is 10-100x
            # off ('%714,0000' → 71.4L for true 7.14L; verified 2026-09-11).
            # The winner can never be trusted → silent drop (Gemini re-reads
            # the pixels when quota allows).
            if not dropped and detect_bad_grouping(m.group(1)):
                dropped, reason = True, "bad-grouping"
            # VPA local-part digits — number runs into @ ('rs3689580-1@okaxis'
            # stored 3.69M twice; verified 2026-09-11).
            if not dropped and _VPA_TAIL_RE.match(flat[e : e + 12]):
                dropped, reason = True, "vpa-digits"
            # Reference / RRN / ref-no context — bare run is a transaction id
            # ('RRNNumber « = 6252477' over TRUE 1,500; 'Ref. No: 613726905'
            # over TRUE 1,500; verified 2026-09-11).
            if not dropped and _REF_CTX_RE.search(ctx_before_low[-35:]):
                dropped, reason = True, "ref-number"
            # Scratch-card promo id ('~ 6137269 SCRATCH'; verified 2026-09-11).
            if not dropped and _SCRATCH_CTX_RE.match(ctx_after_low[:25]):
                dropped, reason = True, "scratch-promo"
            # Paytm scratch id sits *below* the SCRATCHCARD banner, so the
            # word is before the digits (deposit/1789719398070.jpg).
            if not dropped and _SCRATCH_BEFORE_RE.search(ctx_before_low[-90:]):
                dropped, reason = True, "scratch-promo"
            # Generic up-to cap without promo-footer words (Navi loan ad
            # 'Get upto INR 20,00,000'; verified 2026-09-11).
            if not dropped and _UPTO_CAP_RE.search(ctx_before_low[-40:]):
                dropped, reason = True, "ad-cap"
            # '=' echo of a long bare run — transaction/ref id, not a principal
            # ('= 8460704' stored 8.46M over TRUE 21,000; verified 2026-09-11).
            # Comma-bearing '= 21,00,000' (true 21L IMPS) is exempt: Indians
            # write commas on real principals; 7+ digit comma-less runs don't.
            g0 = m.group(0).strip()
            bare_digits = m.group(1).replace(",", "").replace(" ", "").split(".")[0]
            if (
                not dropped
                and g0.startswith("=")
                and "," not in m.group(1)
                and len(bare_digits) >= 7
            ):
                dropped, reason = True, "ref-echo"
            # Timestamp-fragment concat — spaced run glued to a clock time is a
            # datetime, not a principal ('1:05 1 111 202' stored 1,111,202
            # over TRUE 200; verified 2026-09-11). Prefixed amounts (pi<5)
            # are exempt: 'at 10:30 ₹500' keeps its glyph.
            if not dropped and pi in (5, 6):
                if re.search(r"\d{1,2}:\d{2}(?:\s+\d{1,3})?\s*$", ctx_before[-14:]):
                    dropped, reason = True, "timestamp-frag"
            # Bill-reminder context — amount due on a reminder is not a
            # completed transfer ('due tomorrow', 'Pay Postpaid', masked
            # 'Credit Card - 1668'; 8× ₹13.28L rows; verified 2026-09-11).
            # Genuine bill PAYMENTS say 'Paid', never these cues.
            if not dropped and re.search(
                r"due\s+tomorrow|pay\s+postpaid|credit\s+card\s*-\s*\d",
                ctx_before_low[-40:],
            ):
                dropped, reason = True, "bill-context"
            # Token-glued digits — run attaches to letters that aren't a
            # currency prefix ('towards7105960' over TRUE 1,20,000; verified
            # 2026-09-11). Rs/INR-glued ('Rs20000') stay: legit shorthand.
            if not dropped:
                pre_letters = re.search(r"[A-Za-z]{2,}$", ctx_before[-6:])
                if pre_letters and not re.search(
                    r"(?:rs|inr)\.?$", ctx_before, re.IGNORECASE
                ):
                    dropped, reason = True, "token-digits"
            if not dropped and pi in (5, 6):  # spaced/bare — no currency glyph
                if pay_positions:
                    nearest = min(abs(s - p) for p in pay_positions)
                    if nearest > 250:
                        dropped, reason = True, "far-context"
                elif (
                    "paid to" not in ctx_before_low[-80:].lower()
                    and "paid to" not in ctx_after_low[:80].lower()
                ):
                    if "~" not in flat[max(0, s - 5) : e + 5]:
                        dropped, reason = True, "far-context"
            score = weight
            if pay_positions:
                nearest = min(abs(s - p) for p in pay_positions)
                if nearest < 80:
                    score += 20
                elif nearest < 180:
                    score += 8
            if "view details" in ctx_after.lower()[:40]:
                score += 6
            if m.group(0).strip().startswith("%"):
                score -= 8
            # Narrow audit flags (legacy semantics): only balance / spent /
            # sponsored surface as 'ignored' and trigger Gemini adjudication.
            rejected = reason in ("balance", "spent-promo", "sponsored")
            audit_score = (
                weight
                + (
                    8
                    if pay_positions and min(abs(s - p) for p in pay_positions) < 180
                    else 0
                )
                - (50 if rejected else 0)
            )
            scanned.append(
                {
                    "value": value,
                    "score": score,
                    "s": s,
                    "e": e,
                    "match": m.group(0),
                    "ccy": ccy,
                    "dropped": dropped,
                    "rejected": rejected,
                    "reason": reason,
                    "audit_score": audit_score,
                    "context": (flat[max(0, s - 50) : e + 50]).lower()[:80],
                }
            )
    return scanned


def _project_audit(d: dict) -> dict:
    """Audit-trail view of one scanned match (stable UI shape)."""
    return {
        "raw": d["match"],
        "value": d["value"],
        "score": d["audit_score"],
        "rejected": d["rejected"],
        "context": d["context"],
    }


def _collect_amount_candidates(
    flat: str,
) -> list[tuple[float, int, int, int, str, str]]:
    """Score every amount candidate in flattened text.

    Returns (value, score, start, end, raw, currency) best-first. Currency is
    detected per-match; crypto amounts use CRYPTO_MAX_AMOUNT so a "69.5 USDT"
    principal is never killed by the INR bound. Thin filter over the shared
    _scan_amount_matches sweep (single full-text pass per slip).
    """
    candidates = [
        (d["value"], d["score"], d["s"], d["e"], d["match"], d["ccy"])
        for d in _scan_amount_matches(flat)
        if not d["dropped"]
    ]
    # Best-first: highest score, ties by earliest position then smallest value
    candidates.sort(key=lambda x: (-x[1], x[2], x[0]))
    return candidates


_RUPEE_MATCH_RE = re.compile(r"^(?:[₹%]|rs\.?|inr)", re.IGNORECASE)


def _pick_principal(
    cands: list[tuple[float, int, int, int, str, str]],
) -> Optional[tuple[float, str]]:
    """Best candidate as (value, currency); None when nothing qualifies.

    A 7+ digit winner next to a ₹/Rs-prefixed 2–6 digit amount is the Paytm
    scratch/ref case (officer exhibit ₹150 vs 31,49,658). Prefer the rupee
    glyph — prefix-drop already removed most of these; this is the last guard.
    """
    if not cands:
        return None
    top_val, top_ccy = cands[0][0], cands[0][5]
    try:
        top_digits = str(int(top_val))
    except Exception:
        top_digits = ""
    if len(top_digits) >= 7:
        for value, _score, _s, _e, raw, ccy in cands:
            if value >= 100000:
                continue
            if _RUPEE_MATCH_RE.match((raw or "").strip()):
                return (value, ccy)
    return (top_val, top_ccy)


def extract_amount(text: str) -> Optional[float]:
    """Context-aware amount extraction — principal transaction only.

    Collects every candidate, scores it, and blacklists promo/balance amounts.
    Fixes the ₹200 vs ₹31,785 promo bug (withdrawal/1788262500581.jpeg):
      - "Balance: ₹355" and "You have spent ₹31,785 less than Aug" must NOT win
      - The large centered amount under "Payment successful" wins.
    Ranking: label weight + payment-proximity boost - blacklist penalty.
    Currency-blind legacy entry point (kept for QR/UPI paths and tests);
    prefer extract_amount_with_currency for new code.
    """
    if not text:
        return None
    flat = " ".join(text.split())
    picked = _pick_principal(_collect_amount_candidates(flat))
    amt = picked[0] if picked else extract_amount_in_words(flat)
    if amt is None:
        return None
    fixed = correct_rupee_eaten(flat, amt)
    return fixed if fixed is not None else amt


def extract_amount_with_currency(text: str) -> tuple[Optional[float], str]:
    """Principal amount + currency code (INR default; USDT/BTC/ETH/TRX…)."""
    if not text:
        return (None, "INR")
    flat = " ".join(text.split())
    picked = _pick_principal(_collect_amount_candidates(flat))
    if picked:
        amt, ccy = picked
        fixed = correct_rupee_eaten(flat, amt)
        return (fixed if fixed is not None else amt, ccy)
    words_amt = extract_amount_in_words(flat)
    return (words_amt, "INR") if words_amt else (None, "INR")


_AMOUNT_WORD_ONES = {
    "zero": 0,
    "one": 1,
    "two": 2,
    "three": 3,
    "four": 4,
    "five": 5,
    "six": 6,
    "seven": 7,
    "eight": 8,
    "nine": 9,
    "ten": 10,
    "eleven": 11,
    "twelve": 12,
    "thirteen": 13,
    "fourteen": 14,
    "fifteen": 15,
    "sixteen": 16,
    "seventeen": 17,
    "eighteen": 18,
    "nineteen": 19,
}
_AMOUNT_WORD_TENS = {
    "twenty": 20,
    "thirty": 30,
    "forty": 40,
    "fifty": 50,
    "sixty": 60,
    "seventy": 70,
    "eighty": 80,
    "ninety": 90,
}
_AMOUNT_WORD_SCALES = {
    "hundred": 100,
    "thousand": 1000,
    "lakh": 100000,
    "lac": 100000,
    "lacs": 100000,
    "lakhs": 100000,
    "crore": 10000000,
    "crores": 10000000,
}


def _words_to_number(words: str) -> Optional[int]:
    """Parse English number words (Indian scale) → int; None if unparseable."""
    total = 0
    current = 0
    seen = False
    for tok in re.split(r"[\s\-]+", (words or "").strip().lower()):
        if not tok or tok == "and":
            continue
        if tok in _AMOUNT_WORD_ONES:
            current += _AMOUNT_WORD_ONES[tok]
            seen = True
        elif tok in _AMOUNT_WORD_TENS:
            current += _AMOUNT_WORD_TENS[tok]
            seen = True
        elif tok in _AMOUNT_WORD_SCALES:
            scale = _AMOUNT_WORD_SCALES[tok]
            if scale == 100:
                current = max(current, 1) * scale
            else:
                total += max(current, 1) * scale
                current = 0
            seen = True
        else:
            return None
    return (total + current) if seen else None


def extract_amount_in_words(text: str) -> Optional[float]:
    """Amount from the 'Rupees <words> Only' construction; None if absent."""
    if not text:
        return None
    flat = " ".join(text.split())
    m = re.search(r"rupees\s+([a-zA-Z\s\-]+?)\s+only\b", flat, re.IGNORECASE)
    if not m:
        return None
    val = _words_to_number(m.group(1))
    if val is None or not (0 < val <= 99999999):
        return None
    return float(val)


def detect_glyph_collision(text: str) -> Optional[dict]:
    """Detect ₹-glyph misread collisions (verified 2026-09-03: ₹1,000 read as
    BOTH '71,000' (₹→7) and '21,000' (₹→2) on one PhonePe slip; scorer crowned
    71,000 by earliest-position tiebreak).

    Tesseract eats the ₹ into the leading digit, so the SAME true amount yields
    ≥2 distinct bare numbers sharing their trailing digits. True distinct
    same-suffix amounts on one slip are near-impossible, so a collision means
    the winner is untrustworthy → return detail for Gemini adjudication.
    Only bare numbers (no ₹/=~/Rs prefix) participate: prefixed reads kept
    their glyph and are not suspect.
    """
    if not text:
        return None
    flat = " ".join(text.split())
    bare: list[str] = []
    for pi in (5, 6):  # spaced-thousands / bare-comma patterns
        for m in AMOUNT_PATTERNS[pi].finditer(flat):
            g0 = m.group(0)
            # skip if a currency glyph survived just before the number
            pre = flat[max(0, m.start() - 4) : m.start()]
            if re.search(r"[₹%=~]", pre) or re.search(
                r"(?:rs\.?|inr)$", pre.strip(), re.IGNORECASE
            ):
                continue
            digits = m.group(1).replace(",", "").replace(" ", "").split(".")[0]
            if len(digits) >= 5:  # need a leading digit + 4-digit suffix
                bare.append(digits)
    if len(set(bare)) < 2:
        return None
    by_suffix: dict[str, set[str]] = {}
    for d in set(bare):
        by_suffix.setdefault(d[-4:], set()).add(d)
    for suffix, vals in by_suffix.items():
        if len(vals) >= 2:
            return {"shared_suffix": suffix, "variants": sorted(vals)}
    return None


def detect_twin_artifact(text: str, amount: Optional[float]) -> Optional[dict]:
    """Detect TWIN glyph-eaten misreads (verified 2026-09-03 on a real slip:
    ₹1,000 rendered twice, tesseract read BOTH copies as '71,000' — Paid-to
    row AND the total — while the Debited-from row read the true '1,000').

    Invisible to detect_glyph_collision (needs DISTINCT variants): here the
    winner X is 5-digit, repeats identically (count>=2), and a 4-digit sibling
    Y == X[-4:] exists elsewhere in the text. PhonePe slips print the true
    amount twice (Paid row + Debited row), so a genuine 5-digit amount would
    NOT have a shorter same-suffix sibling — X is a ₹-eaten artifact of Y.
    2026-09-11: extended to 6-digit X ("lakh-eaten": ₹12,000 hero read as
    '212,000', ₹→2 prepended — officer-verified Navi UPI slip showing
    ₹2,12,000). 2026-09-11b: generalized over Y lengths 4–6 (lakh/crore-eaten
    uniformly) instead of hardcoded branches — same guards throughout.
    X matches in ANY grouping ('2,12,000' / '212,000' / '2 12 000'); Y must
    appear delimited ('12,000') so a bare UTR fragment can never qualify, and
    Y hits in a balance context ("Balance: ₹12,000" next to a genuine
    ₹2,12,000) are excluded.
    Returns {'artifact': X, 'true_amount': Y} or None.
    """
    if not text or amount is None:
        return None
    try:
        iv = int(amount)
    except Exception:
        return None
    if float(iv) != float(amount):
        return None
    xs = str(iv)
    if not (5 <= len(xs) <= 8):
        return None
    flat = " ".join(text.split())

    def _digit_run(digits: str) -> str:
        # digits with optional OCR separators in ANY positions
        return r"[, ]?".join(re.escape(d) for d in digits)

    # count exact whole-token occurrences of X (any comma/space grouping)
    xhits = len(re.findall(r"(?<![\d,])" + _digit_run(xs) + r"(?![\d,])", flat))
    # ₹-eaten amounts start with 2 or 7 (₹→2 / ₹→7). A single Paid-to
    # '23,000' plus Debited '~3,000' is enough — officer exhibit
    # deposit/1789721443450.jpeg stored 23,000 for true ₹3,000 because
    # X appeared once. Other twins still need X twice.
    rupee_lead = xs[:1] in "27"
    if xhits < 1:
        return None
    if xhits < 2 and not rupee_lead:
        return None
    # Try shorter same-suffix siblings, longest first (Y = X[-6:] … X[-4:]).
    for ylen in (6, 5, 4):
        ys = xs[-ylen:]
        if len(ys) < 4 or int(ys) < 1000 or ys == xs:
            continue
        ypat = r"(?<![\d,])" + _digit_run(ys) + r"(?![\d,])"
        yhits = 0
        for m in re.finditer(ypat, flat):
            token = m.group(0)
            if "," not in token and " " not in token.strip():
                continue  # bare digit run — could be a UTR fragment
            before = flat[max(0, m.start() - 20) : m.start()].lower()
            after = flat[m.end() : m.end() + 20].lower()
            if re.search(
                r"(?:balance|cashback|reward|offer|up to)\s*[:>\-]?\s*[₹%=~]?\s*$",
                before.strip()[-16:],
            ):
                continue  # Y is a balance/promo readout, not the debited twin
            if re.match(r"\s*(?:cashback|reward|offer|balance)\b", after.strip()[:16]):
                continue  # decoy word follows Y ("₹1,234 cashback")
            yhits += 1
        if yhits:
            return {
                "artifact": xs,
                "true_amount": int(ys),
                "x_hits": xhits,
                "y_hits": yhits,
            }
    return None


def correct_rupee_eaten(text: str, amount: Optional[float]) -> Optional[float]:
    """Return the true principal when `amount` is a ₹-glyph-eaten misread.

    Officer exhibit deposit/1789721443450.jpeg (PhonePe 18 Sep 2026): on-screen
    ₹3,000 stored as 23,000. OCR has '23,000' on Paid-to and '~3,000' on
    Debited-from. Same family as 1,000→21,000 / 3,000→23,000.

    GPay often prints both: 'Amount 23,000' and 'Rupees Three Thousand Only'
    — the words are authoritative when they are the ₹-eaten suffix of the
    digit winner.

    Returns None when no correction applies.
    """
    if not text or amount is None:
        return None
    flat = " ".join(text.split())
    try:
        iv = int(amount)
    except Exception:
        return None
    if float(iv) != float(amount):
        return None
    words = extract_amount_in_words(flat)
    if words is not None:
        wi = int(words)
        sa, sw = str(iv), str(wi)
        if sa != sw and sa[:1] in "27" and sa.endswith(sw) and len(sa) == len(sw) + 1:
            return float(wi)
    twin = detect_twin_artifact(flat, amount)
    if twin:
        return float(twin["true_amount"])
    return None


def amount_is_ref_id(text: str, amount: Optional[float], utr: str = "") -> bool:
    """True when `amount` is a truncated UPI/Paytm reference, not rupees.

    Officer exhibit deposit/1789719398070.jpg: Paytm ₹150 + Ref. No
    314965897745 stored as ₹31,49,658 (scratch split 3149658 / 97745).
    """
    if amount is None:
        return False
    try:
        iv = int(amount)
    except Exception:
        return False
    if float(iv) != float(amount):
        return False
    digits = str(iv)
    if len(digits) < 7:
        return False
    ids = list(_LONG_ID_RE.findall(text or ""))
    u = re.sub(r"\D", "", utr or "")
    if 10 <= len(u) <= 16:
        ids.append(u)
    return any(lid != digits and lid.startswith(digits) for lid in ids)


def proven_amount_correction(
    text: str,
    current: Optional[float],
    utr: str = "",
    raw: Optional[float] = None,
) -> tuple[Optional[float], str]:
    """Return (new_amount, reason) only when the change is proven from text.

    reason=='' means leave `current` unchanged. new_amount may be None when
    the stored figure is a Paytm/UPI ref and the true rupees are not in the
    sample — better empty than ₹31,49,658 for a ₹150 slip.
    """
    amt = extract_amount(text) if text else None
    if current is not None:
        try:
            cur_f = float(current)
        except (TypeError, ValueError):
            cur_f = None
        else:
            eaten = correct_rupee_eaten(text, cur_f)
            if eaten is None and raw is not None:
                try:
                    eaten = correct_rupee_eaten(text, float(raw))
                except (TypeError, ValueError):
                    eaten = None
            if eaten is not None and abs(cur_f - eaten) > 0.001:
                return eaten, "rupee_eaten"
            if amount_is_ref_id(text, cur_f, utr):
                if (
                    amt is not None
                    and not amount_is_ref_id(text, amt, utr)
                    and abs(cur_f - amt) > 0.001
                ):
                    return amt, "ref_as_amount"
                return None, "ref_as_amount_clear"
            if cur_f < 100 and amt is not None and amt >= 100:
                return amt, "debris_restore"
    if current is None and amt is not None and not amount_is_ref_id(text, amt, utr):
        return amt, "fill_null"
    return current, ""


def extract_amount_candidates(text: str) -> list[dict]:
    """Return all scored candidates for audit/UI (ignored amounts).

    Projection over the shared _scan_amount_matches sweep — no second
    full-text pass. UI shape unchanged (raw/value/score/rejected/context).
    """
    if not text:
        return []
    flat = " ".join(text.split())
    out = [
        _project_audit(d)
        for d in _scan_amount_matches(flat)
        if not (d["dropped"] and not d["rejected"])
    ]
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
        if "beneficiary_name" not in out:
            # Third fallback: bank receipts label the payee "Name X"
            # ("Name Pebba Rajesh | Bank Name ..." — officer case 2026-09-11).
            # Skip Bank Name / Customer Name labels via preceding-word guard.
            for m3 in re.finditer(
                r"\bName\s*[:\-]?\s*([A-Z][A-Za-z .]{1,40}?)(?=\s*[|\n]|$)",
                flat,
            ):
                before3 = flat[max(0, m3.start() - 12) : m3.start()].lower()
                if re.search(r"(?:bank|customer|user)\s*$", before3.strip()):
                    continue
                cand3 = " ".join(m3.group(1).split()).strip(" .")
                words3 = cand3.split()
                if 1 <= len(words3) <= 4 and all(
                    re.match(r"^[A-Za-z.]{2,20}$", w) for w in words3
                ):
                    out["beneficiary_name"] = cand3[:120]
                    break
    return out


_SHORT_URL_RES = r"goo\.gl|qrstud\.io|bit\.ly|tinyurl\.com|t\.co|is\.gd|cutt\.ly|rebrand\.ly|shorturl\.at"


def _expand_short_url(text: str) -> Optional[str]:
    """Follow a bare short-URL QR to its landing URL (redirects only, no body
    download). Returns the final URL or None. Pure function, child-safe."""
    import re as _re

    t = (text or "").strip()
    if not _re.match(r"https?://", t, _re.I) or "upi:" in t.lower():
        return None
    if not _re.search(_SHORT_URL_RES, t, _re.I) and "upi" not in t.lower():
        return None
    try:
        import httpx as _hx

        with _hx.Client(timeout=10.0, follow_redirects=True) as c:
            r = c.get(t)
            final = str(r.url)
            return final if final != t else None
    except Exception:
        return None


def _qr_read_variants(img, zxingcpp):
    """Yield candidate decoded texts across PIL-only variants (no cv2 on host
    or VM image): raw, contrast, invert, upscales, rotations. Stops at the
    first text containing a payment marker, else returns the first text found."""
    from PIL import ImageOps as _Ops

    base = img.convert("RGB")
    variants = [("raw", base)]
    try:
        g = base.convert("L")
        variants.append(("gray-contrast", _Ops.autocontrast(g, cutoff=1.0)))
        variants.append(("gray-invert", _Ops.invert(g)))
        w, h = g.size
        if max(w, h) < 1200:
            variants.append(("gray-2x", g.resize((w * 2, h * 2))))
            variants.append(("gray-3x", g.resize((w * 3, h * 3))))
        for ang, name in ((90, "rot90"), (180, "rot180"), (270, "rot270")):
            variants.append((name, g.rotate(ang, expand=True)))
    except Exception:
        pass
    first = None
    for _name, v in variants[:9]:
        try:
            rs = zxingcpp.read_barcodes(v)
        except Exception:
            continue
        if not rs:
            continue
        t = (rs[0].text or "").strip()
        if not t:
            continue
        if first is None:
            first = t
        low = t.lower()
        if "upi:" in low or "@" in t or low.startswith("bitcoin"):
            return t  # payment-bearing payload wins immediately
    return first


def decode_qr_image(data: bytes, allow_gemini: bool = True) -> dict:
    """Decode QR from image bytes; extract UPI VPA / amount / payee name / crypto wallets.

    allow_gemini=False (spawned decode workers): on total decode failure flag
    needs_qr_rescue so the parent Gemini batch can attempt a Vision read.
    """
    out: dict = {
        "kind": "qr",
        "raw_text": None,
        "vpa": None,
        "payee_name": None,
        "amount_inr": None,
        "amount_value": None,
        "amount_currency": "INR",
    }
    try:
        import zxingcpp
        from PIL import Image
        import io

        img = Image.open(io.BytesIO(data))
        text = _qr_read_variants(img, zxingcpp)
        if not text:
            if not allow_gemini:
                out["needs_qr_rescue"] = True
            return out
        out["raw_text"] = text[:500]
        expanded = _expand_short_url(text)
        if expanded:
            out["expanded_url"] = expanded[:500]
            text = expanded
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
                for detail in extract_vpa_details(text, set(vpas))
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
                out["amount_value"] = am_value
                out["amount_currency"] = "INR"
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
    "reserved": 0,
    "quota_skips": 0,
    "last_error": "",
    "quota_scope": "",
    "detected_cap": 0,
    "day_dead": [],
}
_gemini_client = None
# Quota-aware state (2026-09-03: key is free tier, 20 req/day/model —
# quotaId GenerateRequestsPerDayPerProjectPerModel-FreeTier).
_gemini_day_dead: dict[str, str] = {}  # model -> UTC date it died for
_gemini_detected_cap = 0  # learned from quotaValue in 429 bodies (x keys)
_gemini_key_cursor = 0  # round-robin over config.GEMINI_API_KEYS
_gemini_minute_hits: list[float] = []  # monotonic ts of recent claims


def _gemini_parse_429(text: str) -> dict:
    """Extract scope from a 429 body: day vs minute, retry delay, quotaValue."""
    import re as _re

    out = {"scope": "unknown", "retry_s": 0.0, "quota_value": 0}
    if not text:
        return out
    m = _re.search(r'"quotaId"\s*:\s*"([^"]+)"', text)
    qid = m.group(1) if m else ""
    if "PerDay" in qid or "per_day" in qid or "daily" in qid.lower():
        out["scope"] = "day"
    elif "PerMinute" in qid or "per_minute" in qid or "minute" in qid.lower():
        out["scope"] = "minute"
    m = _re.search(r'"quotaValue"\s*:\s*"(\d+)"', text)
    if m:
        out["quota_value"] = int(m.group(1))
    m = _re.search(r'"retryDelay"\s*:\s*"(\d+(?:\.\d+)?)s"', text)
    if m:
        out["retry_s"] = float(m.group(1))
    else:
        m = _re.search(r'"retryInfo"[^}]*?"retryDelay"\s*:\s*"(\d+)s"', text, _re.S)
        if m:
            out["retry_s"] = float(m.group(1))
    return out


def gemini_stats() -> dict:
    with _gemini_lock:
        d = dict(_gemini_stats)
        d["used_today"] = _gemini_used_today
        d["day_utc"] = _gemini_day
        d["cap"] = config.GEMINI_DAILY_CAP
        d["model"] = config.GEMINI_MODEL
        d["enabled"] = config.GEMINI_ENABLED
    return d


# ---- drain timing instrumentation (stall watch 2026-09-03) ----
# In-memory only (a DB write per drain would add writer contention to the
# very thing being measured). One log line per drain + rolling last-20.
_drain_lock2 = _th.Lock()
_drain_stats = {
    "drains": 0,
    "keys": 0,
    "done": 0,
    "errors": 0,
    "lock_skips": 0,
    "claim_skips": 0,
    "last": [],
}


# ---- cross-drain claim registry (duplicate-OCR guard 2026-09-07) ----
# enrich_loop (pending), wallet_loop, slip_loop, the poll_loop inline drain,
# the wallet full-scan worker pool and on-demand POST /api/enrich ALL pick
# from overlapping queues (pending covers bankingqr/qrcode/paymentgateway AND
# payment/, which the wallet/slip queues also select). Without coordination
# the same object was GCS-fetched + OCR'd 2-4x concurrently, and any batch
# that died mid-flight left its keys pending so every loop re-OCR'd them.
# Claims are process-wide: a key is fetched/decoded by at most one drain at
# a time; everyone else skips it (counted as claim_skips, not errors).
_claims_lock = _th.Lock()
_claimed: set[str] = set()


def claim_keys(keys: list) -> list:
    """Return the subset of keys not already in flight; claim them."""
    if not keys:
        return []
    with _claims_lock:
        fresh = [k for k in keys if k not in _claimed]
        _claimed.update(fresh)
        with _drain_lock2:
            _drain_stats["claim_skips"] += len(keys) - len(fresh)
        return fresh


def release_keys(keys: list):
    if not keys:
        return
    with _claims_lock:
        for k in keys:
            _claimed.discard(k)


def _note_lock_skip(source: str):
    with _drain_lock2:
        _drain_stats["lock_skips"] += 1


def _note_drain(
    source: str,
    n_keys: int,
    fetch_ms: int,
    decode_ms: int,
    persist_ms: int,
    done: int,
    errors: int,
):
    total = fetch_ms + decode_ms + persist_ms
    with _drain_lock2:
        _drain_stats["drains"] += 1
        _drain_stats["keys"] += n_keys
        _drain_stats["done"] += done
        _drain_stats["errors"] += errors
        _drain_stats["last"].append(
            {
                "source": source,
                "n": n_keys,
                "fetch_ms": fetch_ms,
                "decode_ms": decode_ms,
                "persist_ms": persist_ms,
                "total_ms": total,
                "done": done,
                "errors": errors,
            }
        )
        del _drain_stats["last"][:-20]
    print(
        f"[drain {source}] n={n_keys} fetch={fetch_ms / 1000:.1f}s "
        f"decode={decode_ms / 1000:.1f}s persist={persist_ms / 1000:.1f}s "
        f"done={done} err={errors} (total {total / 1000:.1f}s)",
        flush=True,
    )


def drain_stats() -> dict:
    with _drain_lock2:
        d = {k: (list(v) if k == "last" else v) for k, v in _drain_stats.items()}
    return d


def _gemini_client_shared():
    global _gemini_client
    if _gemini_client is None:
        import httpx

        _gemini_client = httpx.Client(timeout=25.0)
    return _gemini_client


def _gemini_model_dead(model: str) -> bool:
    """True if this model hit a day-scope 429 today (free-tier 20/day)."""
    import time as _t

    with _gemini_lock:
        return _gemini_day_dead.get(model) == _t.strftime("%Y-%m-%d", _t.gmtime())


def _gemini_budget_claim(
    manual: bool = False,
) -> tuple[Optional[str], str]:
    """Claim one call. Returns (block_reason_or_None, api_key_to_use).

    manual=True (officer Re-run buttons) bypasses pacing + reserve but never
    the day-dead flags. Key rotation spreads the free-tier 20/day/model
    across every configured key.
    """
    import time as _t

    keys = config.GEMINI_API_KEYS
    if not keys:
        return "disabled", ""
    today = _t.strftime("%Y-%m-%d", _t.gmtime())
    with _gemini_lock:
        global _gemini_day, _gemini_used_today, _gemini_key_cursor
        if _gemini_day != today:
            _gemini_day, _gemini_used_today = today, 0
            _gemini_day_dead.clear()
            _gemini_minute_hits.clear()
        now = _t.monotonic()
        if now < _gemini_cooldown_until:
            _gemini_stats["cooled"] += 1
            return "breaker-cooldown", ""
        hits = [h for h in _gemini_minute_hits if h > now - 60]
        if not manual and len(hits) >= config.GEMINI_PER_MIN:
            _gemini_stats["paced"] = _gemini_stats.get("paced", 0) + 1
            return "paced-per-minute", ""
        cap = config.GEMINI_DAILY_CAP
        if _gemini_detected_cap:
            cap = min(cap, _gemini_detected_cap * max(1, len(keys)))
        if not manual and _gemini_used_today >= cap:
            _gemini_stats["capped"] += 1
            return "daily-budget-exhausted", ""
        if not manual and cap - _gemini_used_today <= config.GEMINI_RESERVE:
            _gemini_stats["reserved"] += 1
            return "reserved-for-manual", ""
        _gemini_used_today += 1
        _gemini_stats["calls"] += 1
        _gemini_minute_hits[:] = hits + [now]
        key = keys[_gemini_key_cursor % len(keys)]
        _gemini_key_cursor += 1
    return None, key


def _gemini_note(ok: bool, err: str = "", model: Optional[str] = None):
    """Record a call outcome; LEARN from 429s (the old code threw them away).

    Day-scope quota → model flagged dead until UTC midnight + short global
    cooldown (RetryInfo or 60s) so the chain's fallback model gets its turn.
    Minute-scope → cooldown per RetryInfo (default 120s). 5 straight
    non-quota failures → 600s breaker as before.
    """
    import time as _t

    with _gemini_lock:
        global _gemini_consec_fail, _gemini_cooldown_until, _gemini_detected_cap
        if ok:
            _gemini_consec_fail = 0
            _gemini_stats["ok"] += 1
            return
        _gemini_consec_fail += 1
        _gemini_stats["errors"] += 1
        _gemini_stats["last_error"] = err[:200]
        q = _gemini_parse_429(err)
        if q.get("scope"):
            _gemini_stats["quota_scope"] = q["scope"]
        if q.get("quota_value"):
            try:
                qv = int(q["quota_value"])
                if _gemini_detected_cap is None or qv < _gemini_detected_cap:
                    _gemini_detected_cap = qv
            except Exception:
                pass
        cool = 600.0
        if q.get("scope") == "day":
            if model:
                _gemini_day_dead[model] = _t.strftime("%Y-%m-%d", _t.gmtime())
            try:
                cool = max(60.0, float(q.get("retry_s") or 60))
            except Exception:
                cool = 60.0
            _gemini_cooldown_until = _t.monotonic() + cool
            _gemini_consec_fail = 0
            return
        if q.get("scope") == "minute":
            try:
                cool = max(120.0, float(q.get("retry_s") or 120))
            except Exception:
                cool = 120.0
            _gemini_cooldown_until = _t.monotonic() + cool
            _gemini_consec_fail = 0
            return
        if _gemini_consec_fail >= 5:
            _gemini_cooldown_until = _t.monotonic() + cool
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


def _gemini_extract(
    data: bytes, text_sample: str = "", mode: str = "slip", manual: bool = False
) -> Optional[dict]:
    """Call Gemini Vision if a key is configured; return parsed JSON or None.

    mode='slip' (default): strict prompt — principal transaction amount ONLY,
    ignore Balance / promo. Returns amount_value + amount_currency, utr/txid,
    vpa, wallet_address, names, ifsc, account.
    mode='qr': the QR was unreadable by zxing — transcribe ANY machine-readable
    payment payload visible (QR URI, UPI id, wallet address) VERBATIM into
    qr_text; the caller re-parses it through the normal QR field extractors.
    Budgeted: daily cap + 10-min breaker after 5 consecutive failures, model
    chain with backoff on 429/5xx (400-class is fatal, no retry).
    """
    blocked, _key = _gemini_budget_claim(manual=manual)
    if blocked:
        return {"_gemini_error": blocked}
    try:
        import base64
        import json as _json
        import time as _t

        small, mime = _gemini_shrink(data)
        b64 = base64.b64encode(small).decode("ascii")

        if mode == "qr":
            prompt = (
                "You are a forensic QR transcriber. This image contains a QR code "
                "that automated decoders failed to read (rotated, stylized, low "
                "contrast, or damaged). Transcribe the payment payload encoded in "
                "the QR VERBATIM — typically a upi://pay?... URI, a VPA like "
                "name@okbank, or a crypto wallet address. Copy character-for-character; "
                "never guess missing characters. Return JSON only with keys: "
                "qr_text (the full decoded string, or null if truly unreadable), "
                "vpa (string or null), payee_name (string or null), amount_inr "
                "(number from am= or null). If unsure, use null. Do not invent."
            )
        else:
            prompt = (
                "You are a forensic payment-slip extractor. Two receipt kinds exist: "
                "(A) Indian UPI/bank screenshots (amounts in ₹/Rs/INR). "
                "(B) Crypto exchange/wallet receipts (Binance/Trust Wallet: amounts in USDT/BTC/ETH/TRX, a 64-hex Txid, a wallet Address). "
                "For kind A: extract ONLY the principal transaction amount — the large centered amount under 'Payment successful' / 'Transaction Successful' (e.g. ₹200). "
                "IGNORE: Balance (e.g. Balance: ₹355), promo 'You have spent ₹31,785 less than Aug', sponsored products, fees, or any other secondary amount. "
                "For kind B: extract the crypto Amount with its token (e.g. 69.5 USDT — NOT the network fee), the full 64-hex Txid, and the destination wallet Address. "
                "Return JSON only with keys: amount_value (number or null), amount_currency (string like INR, USDT, BTC, ETH, TRX — default INR for kind A), "
                "utr (12-digit string or null, kind A only), txid (64-hex string or null, kind B only), wallet_address (string or null, kind B only), "
                "vpa (string or null), beneficiary_name (string or null), sender_name (string or null), ifsc (string or null), bank_account (string or null). "
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
        client = _gemini_client_shared()
        j = None
        served_by = config.GEMINI_MODEL
        last_err = "unknown"
        # Model chain: primary (best verified) then fallback (separate quota
        # pool). 429/quota on one model must not kill the whole batch.
        models = [config.GEMINI_MODEL]
        if config.GEMINI_FALLBACK_MODEL not in models:
            models.append(config.GEMINI_FALLBACK_MODEL)
        err_model: Optional[str] = None
        for model in models:
            if _gemini_model_dead(model):
                last_err = f"quota-day-dead@{model}"
                err_model = model
                continue  # free-tier day budget spent — try next model
            url = (
                "https://generativelanguage.googleapis.com/v1beta/models/"
                f"{model}:generateContent?key={_key}"
            )
            for attempt in range(2):
                try:
                    r = client.post(url, json=body)
                    if r.status_code in (400, 404):
                        last_err = f"{r.status_code}@{model}: {r.text[:160]}"
                        err_model = model
                        break  # model-level fatal — try next model
                    if r.status_code == 429 or r.status_code >= 500:
                        # Keep the FULL 429 body (quotaId/quotaValue/retryDelay
                        # live past char 160 — truncating hid the diagnosis).
                        last_err = f"{r.status_code}@{model}: {r.text[:800]}"
                        err_model = model
                        _t.sleep(1.5 * (2**attempt))
                        continue
                    r.raise_for_status()
                    j = r.json()
                    served_by = model
                    break
                except Exception as e:
                    last_err = f"{model}: {str(e)[:160]}"
                    err_model = model
                    _t.sleep(1.5 * (2**attempt))
            if j is not None:
                break
        if j is None:
            _gemini_note(False, last_err, err_model)
            return {"_gemini_error": last_err[:200]}
        # Parse candidates[0].content.parts[0].text
        try:
            txt = j["candidates"][0]["content"]["parts"][0]["text"]
        except Exception as e:
            _gemini_note(False, f"bad-response@{served_by}: {str(e)[:120]}", served_by)
            return {"_gemini_error": "bad-response-shape"}
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
        # Sanitize amount via same bounds as OCR (legacy amount_inr or new
        # amount_value + amount_currency pair).
        amt = parsed.get("amount_value", parsed.get("amount_inr"))
        ccy = str(parsed.get("amount_currency") or "INR").upper()[:8]
        if ccy not in (
            "INR",
            "USD",
            "USDT",
            "USDC",
            "TUSD",
            "DAI",
            "BTC",
            "XBT",
            "ETH",
            "TRX",
            "BNB",
            "MATIC",
            "SOL",
        ):
            ccy = "INR"
        if ccy == "XBT":
            ccy = "BTC"
        try:
            amt_f = float(amt) if amt is not None else None
        except Exception:
            amt_f = None
        bound = (
            CRYPTO_MAX_AMOUNT if ccy not in ("INR", "USD") else config.MAX_AMOUNT_INR
        )
        if amt_f is not None and not (0 < amt_f <= bound):
            amt_f = None
        parsed["amount_value"] = amt_f
        parsed["amount_currency"] = ccy
        parsed["amount_inr"] = amt_f if ccy == "INR" else None
        # Normalise txid: exact 64-hex only (a Txid label with wrapping is
        # joined the same way as the tesseract path).
        txid = parsed.get("txid")
        if txid is not None:
            joined_tx = _TXID_WRAP_JOIN_RE.sub("", str(txid))
            m_tx = re.search(r"\b([0-9a-fA-F]{64})\b", joined_tx)
            parsed["txid"] = m_tx.group(1).lower() if m_tx else None
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
        parsed["_model"] = served_by
        _gemini_note(True)
        return parsed
    except Exception as e:
        # Never fail OCR because Gemini failed — caller logs to error field
        _gemini_note(False, str(e)[:200])
        return {"_gemini_error": str(e)[:300]}


def _merge_gemini_wallet(out: dict, addr: str) -> None:
    """Append a Gemini-spotted wallet address if tesseract missed it.

    Reuses extract_wallets for network classification + txid-fragment guard,
    so a model-echoed Txid can never become a BITCOIN row either.
    """
    try:
        for w in extract_wallets(str(addr)):
            existing = {x.get("address") for x in out.get("wallets", [])}
            if w["address"] not in existing:
                out.setdefault("wallets", []).append(
                    {**w, "role": "beneficiary", "role_confidence": "medium"}
                )
    except Exception:
        pass


def _merge_gemini(out: dict, g: dict) -> None:
    """Merge a successful Gemini extraction into a tesseract payload (in place)."""
    if g.get("amount_value") is not None:
        if out.get("amount_value") != g["amount_value"]:
            out["amount_tesseract"] = out.get("amount_value")
            out["amount_value"] = g["amount_value"]
            out["amount_currency"] = g.get("amount_currency", "INR")
            out["amount_inr"] = (
                g["amount_value"] if out["amount_currency"] == "INR" else None
            )
            out["engine"] = "tesseract+gemini"
        else:
            out["engine"] = "tesseract+gemini"
    elif g.get("amount_inr") is not None and out.get("amount_value") is None:
        # Legacy-shaped model reply (prompt asked for amount_inr).
        out["amount_tesseract"] = None
        out["amount_value"] = g["amount_inr"]
        out["amount_currency"] = "INR"
        out["amount_inr"] = g["amount_inr"]
        out["engine"] = "tesseract+gemini"
    if g.get("txid") and not out.get("txids"):
        out["txids"] = [g["txid"]]
    if g.get("wallet_address"):
        _merge_gemini_wallet(out, g["wallet_address"])
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
    # Quota-skip reasons: the flag must SURVIVE these (old code popped it, so
    # the item never retried when budget returned — the 20/day burn bug).
    _SKIP = (
        "quota-day-dead",
        "breaker-cooldown",
        "daily-budget-exhausted",
        "reserved-for-manual",
        "paced-per-minute",
    )
    done = 0
    for key, payload, data in items[: config.GEMINI_PER_BATCH]:
        try:
            if not isinstance(payload, dict):
                continue
            if payload.get("needs_qr_rescue"):
                # QR unreadable by zxing: Vision transcription attempt, then
                # re-parse through the normal QR field extractors.
                g = _gemini_extract(data, mode="qr")
                if g and not g.get("_gemini_error") and g.get("qr_text"):
                    payload.pop("needs_qr_rescue", None)
                    text = g["qr_text"]
                    payload["raw_text"] = text[:500]
                    payload["qr_rescued_by"] = g.get("_model")
                    vpas = extract_vpas(text)
                    if vpas:
                        payload["vpa"] = vpas[0]
                        payload["vpas"] = vpas
                    wallets = extract_wallets(text)
                    if wallets:
                        payload["wallets"] = wallets
                    payload["engine"] = "qr+gemini"
                    done += 1
                elif g and g.get("_gemini_error"):
                    payload["_gemini_error"] = g["_gemini_error"]
                    if str(g["_gemini_error"]) in _SKIP:
                        break  # quota gone — keep flag, retry next cycle
                    payload.pop("needs_qr_rescue", None)
                continue
            if not payload.get("needs_gemini"):
                continue
            g = _gemini_extract(data, payload.get("text_sample") or "")
            if g and not g.get("_gemini_error"):
                payload.pop("needs_gemini", None)
                _merge_gemini(payload, g)
                done += 1
            elif g and g.get("_gemini_error"):
                payload["_gemini_error"] = g["_gemini_error"]
                if str(g["_gemini_error"]) in _SKIP:
                    break  # quota gone — keep flag, retry next cycle
                payload.pop("needs_gemini", None)
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
        # Slips read fine at native phone resolution (~1080px): cap the long
        # side at 2000px and upscale only tiny thumbnails (<1000px). The old
        # always-2x upscale turned a 1080×2400 slip into 10MP (~2-3x tesseract
        # CPU for no accuracy gain on 12-digit UTRs/VPAs).
        if max(w, h) < 1000:
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
            # Single shared amount sweep straight after pass 1: the winner,
            # the retry decision, and the UI audit trail all derive from it
            # (2026-09-11b: three full-text sweeps per slip → one).
            flat = " ".join(text.split())
            scanned = _scan_amount_matches(flat)
            # Fallback: first pass yielded nothing usable (washed-out green
            # header, faint ₹) and output is short — retry with psm 11
            # (sparse text) once. Skipped when pass 1 already found an
            # amount/UTR/VPA (saves a full tesseract pass on good slips) and
            # when text is long (sparse-text mode won't help a dense slip).
            if len(text.strip()) < 300 and (
                not any(not d["dropped"] for d in scanned)
                and not UTR_RE.search(text)
                and not NUM12_RE.search(text)
                and not extract_vpas(text)
            ):
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
                    flat = " ".join(text.split())
                    scanned = _scan_amount_matches(flat)
        out["text_sample"] = flat[:1200]
        wallets = extract_wallets(text)
        if wallets:
            out["wallets"] = wallets
        accepted = sorted(
            (d for d in scanned if not d["dropped"]),
            key=lambda d: (-d["score"], d["s"], d["value"]),
        )
        picked = _pick_principal(
            [
                (d["value"], d["score"], d["s"], d["e"], d["match"], d["ccy"])
                for d in accepted
            ]
        )
        if picked:
            tesseract_amount, tesseract_ccy = picked
        else:
            # Fallback: amount printed only in words ("Rupees Four Hundred
            # Fifty Only"; verified 2026-09-11: Paytm slip whose only digits
            # were a ~60,000 postpaid ad, now correctly skipped). Same fallback
            # as extract_amount_with_currency — ocr_slip reads the shared
            # sweep directly so it needs it here too.
            tesseract_amount = extract_amount_in_words(flat)
            tesseract_ccy = "INR"
        out["amount_value"] = tesseract_amount
        out["amount_currency"] = tesseract_ccy
        # amount_inr feeds ₹ totals — set ONLY when the principal really is
        # rupees, so a "69.5 USDT" crypto receipt never pollutes INR sums.
        out["amount_inr"] = tesseract_amount if tesseract_ccy == "INR" else None
        txids = extract_txids(text)
        if txids:
            out["txids"] = txids
        # Audit: keep ignored promo/balance candidates for dashboard transparency
        try:
            cands = sorted(
                (
                    _project_audit(d)
                    for d in scanned
                    if not (d["dropped"] and not d["rejected"])
                ),
                key=lambda c: -c["score"],
            )
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
            # Bare 12-digit fallback — skip if clearly an account/balance context.
            # 2026-09-16: a UTR label in the immediate window ("UPI transaction
            # ID 129679804197" sitting right after "...confirmed deposit of
            # money to ...'s bank account") OVERRIDES the account-keyword guard —
            # the specific label wins over the generic context word.
            for mn in NUM12_RE.finditer(text):
                cand_digits = mn.group(1)
                s, e = mn.start(), mn.end()
                before = text[max(0, s - 40) : s].lower()
                after = text[e : e + 20].lower()
                # Skip if preceded by account/aadhaar/phone keywords
                if _ACCOUNT_KEYWORDS_RE.search(
                    before
                ) and not _UTR_LABEL_NEAR_RE.search(before[-30:]):
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
            out["vpa_details"] = extract_vpa_details(text, set(vpas))
        accounts = extract_bank_accounts(text)
        if accounts:
            out["bank_accounts"] = accounts
            if accounts[0].get("ifsc"):
                out["ifsc"] = accounts[0]["ifsc"]
        out.update(extract_participants(" ".join(text.split())))

        # — Gemini verification: cost-controlled. Auto-call when tesseract fails
        # to find a principal amount, when the ₹-glyph collision detector fires
        # (₹1,000 → '71,000' + '21,000' verified 2026-09-03), or when promo
        # candidates were ignored (medium confidence = winner uncorroborated).
        # High-confidence clean tesseract reads stay local (no token burn).
        try:
            collision = detect_glyph_collision(text)
        except Exception:
            collision = None
        if collision:
            # Annotate only. Wiping the principal (old behaviour) left empty
            # amounts whenever Gemini was quota-dead — ₹1,000→71,000/21,000
            # is recovered by correct_rupee_eaten below.
            out["glyph_collision"] = collision
        # Twin / ₹-eaten (verified 2026-09-03 ₹1,000→71,000; officer 2026-09-18
        # PhonePe ₹3,000 stored as 23,000). Applied locally so the fix works
        # even with Gemini quota-dead; Vision re-adjudicates when live.
        try:
            eaten = correct_rupee_eaten(text, out.get("amount_value"))
        except Exception:
            eaten = None
        if (
            eaten is None
            and out.get("amount_value") is not None
            and amount_is_ref_id(text, out.get("amount_value"), out.get("utr") or "")
        ):
            out["ref_as_amount"] = True
            out["amount_tesseract_raw"] = out.get("amount_value")
            words = extract_amount_in_words(flat)
            out["amount_value"] = words
            out["amount_inr"] = words if out.get("amount_currency") == "INR" else None
            tesseract_amount = words
        twin = None
        if eaten is not None:
            out["amount_tesseract_raw"] = out.get("amount_value")
            out["amount_value"] = eaten
            out["amount_inr"] = (
                eaten if out.get("amount_currency") == "INR" else None
            )
            out["rupee_eaten"] = True
            twin = detect_twin_artifact(text, out.get("amount_tesseract_raw"))
            if twin:
                out["twin_artifact"] = twin
        use_gemini = False
        if config.GEMINI_ENABLED:
            has_rejected = bool(out.get("amount_ignored"))
            # Only auto-invoke when tesseract found nothing (e.g. faint ₹ glyph) —
            # the 200 vs 31785 case is now solved by context-aware scoring, no need to burn tokens.
            if tesseract_amount is None:
                # If we have rejected promo but no principal, likely ₹ was missed — Gemini can rescue
                use_gemini = True
            elif collision or twin or has_rejected:
                # Winner uncorroborated (medium confidence): let Vision adjudicate
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
            out.get("amount_value") is None
            and out["utr"] is None
            and out["vpa"] is None
            and not wallets
            and not out.get("txids")
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
    # If still tesseract-only, force a Gemini call regardless of heuristic.
    # Manual trigger: bypasses the auto reserve so the officer Re-run always
    # spends when any quota remains (free-tier 20/day/model, 2026-09-03).
    if config.GEMINI_ENABLED and base.get("engine") == "tesseract":
        g = _gemini_extract(data, base.get("text_sample") or "", manual=True)
        if g and not g.get("_gemini_error") and g.get("amount_value") is not None:
            _merge_gemini(base, g)
            base["engine"] = "gemini"
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
        return decode_qr_image(data, allow_gemini=allow_gemini)
    if kind not in ("deposit", "withdrawal"):
        return None
    payload = ocr_slip(data, allow_gemini=allow_gemini)
    payload["kind"] = kind
    q = decode_qr_image(
        data, allow_gemini=allow_gemini
    )  # slips often embed payment QR too
    if q.get("amount_inr") is not None or q.get("vpa"):
        for k, v in q.items():
            if not v or payload.get(k):
                continue
            if (
                k in ("amount_inr", "amount_value", "amount_currency")
                and payload.get("amount_value") is not None
            ):
                # Never let an embedded-QR rupee amount overwrite an OCR
                # crypto principal (e.g. USDT receipt with a UPI QR sticker).
                continue
            payload[k] = v
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
        # Specific (wallet/slip) drains run on their own lock so a minutes-long
        # full-scan batch never blocks the 2s live pending drain. They share
        # _cpu_pool (thread-safe) with halved workers (see callers).
        self._specific_lock = threading.Lock()
        self._cpu_pool = None

    def _prepare_payload(self, key: str, payload: Optional[dict]):
        """Mule/wallet flag enrichment (pure: mutates payload, no IO)."""
        if payload is None:
            return payload
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
        return payload

    def _persist_payload(self, key: str, payload: Optional[dict]):
        if payload is None:
            return
        self._prepare_payload(key, payload)
        db.mark_enriched(key, payload)
        if self.on_result:
            # Attach live size/modified: sizeless `enriched` events made the
            # Live Feed modal render real images as 0-byte markers (2026-09-11).
            try:
                meta = db.get_object_sizes([key]).get(key, {})
            except Exception:
                meta = {}
            self.on_result(
                {
                    "type": "enriched",
                    "key": key,
                    "size": meta.get("size"),
                    "last_modified": meta.get("last_modified", ""),
                    "payload": payload,
                }
            )

    def process_key(self, key: str, full_scan: bool = False, _claim: bool = True):
        # _claim=False when the caller (drain_pending/drain_specific batch
        # path) already owns the claim for this key. Standalone callers
        # (wallet full-scan pool, POST /api/enrich) claim here so two paths
        # never OCR the same object concurrently.
        if _claim:
            if not claim_keys([key]):
                return
        try:
            self._process_key_inner(key, full_scan=full_scan)
        finally:
            if _claim:
                release_keys([key])

    def _process_key_inner(self, key: str, full_scan: bool = False):
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
            _note_lock_skip("pending")
            return
        try:
            keys = claim_keys(db.pending_enrichment(batch, full_scan=full_scan))
            try:
                self._drain_keys(keys, workers, full_scan, "pending")
            finally:
                release_keys(keys)
        finally:
            self._drain_lock.release()

    def drain_specific(self, keys: list, workers: int = 1, full_scan: bool = False):
        """Wallet/slip-priority drain over caller-picked keys.

        Own lock (never blocks the live pending drain); shares _cpu_pool.
        Callers pass halved workers so concurrent drains fit the core count.
        Keys already in flight in another drain are skipped via the claim
        registry (duplicate-OCR guard) instead of being re-fetched/re-OCR'd.
        full_scan=True lets unhinted chat_file images run wallet OCR (wallet_loop
        only; slip_loop stays False so deposit/withdrawal use the normal path).
        """
        if not keys:
            return
        if not self._specific_lock.acquire(blocking=False):
            _note_lock_skip("specific")
            return
        try:
            fresh = claim_keys(list(keys))
            try:
                self._drain_keys(fresh, workers, full_scan, "specific")
            finally:
                release_keys(fresh)
        finally:
            self._specific_lock.release()

    def _drain_keys(
        self, keys: list, workers: int, full_scan: bool, source: str = "pending"
    ):
        """Drain one claimed batch. Callers own the claims (see claim_keys).

        No-silent-drop rule (hang review 2026-09-07): EVERY key that enters
        this method leaves either persisted (enriched=1) or error-counted via
        mark_enrichment_error (bounded retries, then skipped with the error
        recorded). The old `except: pass` let crashed batches recycle the
        same keys through fetch+OCR on every loop forever.
        """
        import time as _t

        if not keys:
            return
        # Keys fetched/decoded but never settled (pool crash, timeout, bug).
        # The abort handler error-counts exactly these — nothing is silently
        # re-queued, nothing is double-counted.
        unsettled: set = set(keys)
        try:
            if workers <= 1:
                t0 = _t.monotonic()
                done = 0
                for k in keys:
                    if not self.enabled:
                        break
                    try:
                        self.process_key(k, full_scan=full_scan, _claim=False)
                        done += 1
                    except Exception as e:
                        try:
                            db.mark_enrichment_error(k, f"drain: {e}"[:500])
                        except Exception:
                            pass
                    finally:
                        unsettled.discard(k)
                _note_drain(
                    source, len(keys), 0, int((_t.monotonic() - t0) * 1000), 0, done, 0
                )
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
            t_fetch = _t.monotonic()
            try:
                with ThreadPoolExecutor(
                    max_workers=min(16, len(keys) or 1),
                    thread_name_prefix="evidence-fetch",
                ) as fetch_pool:
                    fetched = list(fetch_pool.map(fetch_one, keys))
            except Exception as e:
                for k in keys:
                    try:
                        db.mark_enrichment_error(k, f"fetch-pool: {e}"[:500])
                    except Exception:
                        pass
                    unsettled.discard(k)
                _note_drain(source, len(keys), 0, 0, 0, 0, len(keys))
                return
            fetch_ms = int((_t.monotonic() - t_fetch) * 1000)

            tasks = []
            for key, data, error in fetched:
                if error:
                    try:
                        db.mark_enrichment_error(key, error)
                    except Exception:
                        pass
                    unsettled.discard(key)
                elif data is not None:
                    tasks.append((key, data, full_scan))
                else:
                    # already enriched between selection and fetch — settled.
                    unsettled.discard(key)
            if not tasks:
                _note_drain(
                    source, len(keys), fetch_ms, 0, 0, 0, len(fetched) - len(tasks)
                )
                return
            if self._cpu_pool is None:
                self._cpu_pool = ProcessPoolExecutor(
                    max_workers=workers,
                    mp_context=multiprocessing.get_context("spawn"),
                )
            # Deadlock insurance: in-child tesseract already caps at 60s per
            # image, so a batch exceeding this bound is wedged, not slow.
            # Generous on purpose — aborting only error-counts keys for a
            # later bounded retry, it never drops them silently.
            per_worker = (len(tasks) + max(1, workers) - 1) // max(1, workers)
            decode_timeout = max(3600.0, 150.0 * per_worker)
            t_dec = _t.monotonic()
            try:
                results = list(
                    self._cpu_pool.map(_decode_task, tasks, timeout=decode_timeout)
                )
            except Exception as e:
                # Pool wedged or a child crashed (BrokenProcessPool): the pool
                # object is unusable afterwards — drop it so the next drain
                # rebuilds a fresh one instead of failing the same way.
                try:
                    self._cpu_pool.shutdown(wait=False, cancel_futures=True)
                except Exception:
                    pass
                self._cpu_pool = None
                for key, _d, _fs in tasks:
                    try:
                        db.mark_enrichment_error(key, f"decode-abort: {e}"[:500])
                    except Exception:
                        pass
                    unsettled.discard(key)
                _note_drain(
                    source,
                    len(keys),
                    fetch_ms,
                    int((_t.monotonic() - t_dec) * 1000),
                    0,
                    0,
                    len(tasks),
                )
                return
            decode_ms = int((_t.monotonic() - t_dec) * 1000)
            by_key = {k: d for k, d, _fs in tasks}
            try:
                flagged = [
                    (key, payload, by_key[key])
                    for key, payload, error in results
                    if not error
                    and isinstance(payload, dict)
                    and (payload.get("needs_gemini") or payload.get("needs_qr_rescue"))
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
                            p.pop("needs_qr_rescue", None)
            except Exception:
                pass
            t_per = _t.monotonic()
            done = errs = 0
            ok = []
            for key, payload, error in results:
                if error:
                    try:
                        db.mark_enrichment_error(key, error)
                    except Exception:
                        pass
                    errs += 1
                else:
                    ok.append((key, self._prepare_payload(key, payload)))
                unsettled.discard(key)
            # One commit per drain: per-key commits made persist take
            # 167-280s live (2026-09-05). Falls back to per-key on any error.
            try:
                done = db.persist_enriched_batch(ok)
                if self.on_result:
                    try:
                        sizes = db.get_object_sizes([k for k, _ in ok])
                    except Exception:
                        sizes = {}
                    for key, payload in ok:
                        try:
                            meta = sizes.get(key, {})
                            self.on_result(
                                {
                                    "type": "enriched",
                                    "key": key,
                                    "size": meta.get("size"),
                                    "last_modified": meta.get("last_modified", ""),
                                    "payload": payload,
                                }
                            )
                        except Exception:
                            pass
            except Exception:
                done = 0
                for key, payload in ok:
                    try:
                        self._persist_payload(key, payload)
                        done += 1
                    except Exception as e:
                        # Poison-pill guard (2026-09-07): a payload that can
                        # never persist used to stay enriched=0 forever, so
                        # every loop re-OCR'd it. Error-count it instead —
                        # bounded retries, then skipped with error recorded.
                        try:
                            db.mark_enrichment_error(key, f"persist: {e}"[:500])
                        except Exception:
                            pass
                        errs += 1
            persist_ms = int((_t.monotonic() - t_per) * 1000)
            # Keep the WAL small: a 4.2GB uncheckpointed WAL made this phase
            # take 60-76s live (2026-09-05). PASSIVE never blocks readers.
            try:
                from . import db as _db

                _db.checkpoint()
            except Exception:
                pass
            _note_drain(source, len(keys), fetch_ms, decode_ms, persist_ms, done, errs)
        except Exception as e:
            # Last-resort settlement: anything not persisted or error-counted
            # above gets a bounded retry count, never a silent re-queue.
            for k in list(unsettled):
                try:
                    db.mark_enrichment_error(k, f"drain-abort: {e}"[:500])
                except Exception:
                    pass
                unsettled.discard(k)
            try:
                _note_drain(source, len(keys), 0, 0, 0, 0, len(keys))
            except Exception:
                pass

    def close(self):
        self.enabled = False
        if self._cpu_pool is not None:
            self._cpu_pool.shutdown(wait=False, cancel_futures=True)
