"""Tronscan chain verification for crypto slips (CASE-2026-CBTF).

The OCR pipeline already extracts wallet addresses and (sometimes) transaction
hashes. This module is the missing authority layer:

  1. canonicalise OCR-degraded TRON addresses (base58check + the existing
     lookalike repair in enrich.recover_tron_ocr);
  2. extract full *and partial* tx hashes (Tronscan shows ``Hash: abc…def``);
  3. cross-check against the public Tronscan API using several independent
     factors — wallet address, direction, amount, time and the hash fragment;
  4. store the canonical address + full verified transaction details so the
     Officer Query table and CSV never show a partial/invalid value as fact.

Design rules:
  * Tronscan is never called from a request thread; the background worker and
    the explicit backfill endpoint use a global rate limiter + cache.
  * A hash is only attached when it is unique and consistent. Ambiguous rows
    keep every candidate and are labelled ``ambiguous`` — never guessed.
  * The raw OCR value is preserved in ``rail_id_raw`` / ``wallet_resolutions``.

Public API (used by app.py and db.py):
  canonicalize_address_light(raw, network) -> dict
  extract_txid_candidates(text) -> list[dict]
  verify_source_key(key) -> dict
  backfill(since, until, limit, dry_run) -> dict
  start_backfill(...) -> dict
  status() -> dict
  start_worker() / stop_worker()
"""

from __future__ import annotations

import hashlib
import itertools
import json
import os
import re
import threading
import time
from datetime import datetime, timedelta, timezone
from typing import Any, Iterable, Optional

import httpx

from . import config, db

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

TRONSCAN_BASE = os.environ.get(
    "S3LM_TRONSCAN_API", "https://apilist.tronscanapi.com/api"
).rstrip("/")
TRONSCAN_API_KEY = (
    os.environ.get("S3LM_TRONSCAN_API_KEY", "").strip()
    or os.environ.get("TRONSCAN_API_KEY", "").strip()
)
ENABLED = os.environ.get("S3LM_CHAIN_VERIFY", "1").strip() != "0"
WORKER_ENABLED = os.environ.get("S3LM_CHAIN_WORKER", "1").strip() != "0"
RATE_PER_SEC = float(os.environ.get("S3LM_CHAIN_RATE", "3") or 3)
WORKER_BATCH = int(os.environ.get("S3LM_CHAIN_BATCH", "8") or 8)
WORKER_SLEEP = float(os.environ.get("S3LM_CHAIN_SLEEP", "30") or 30)
WINDOW_BEFORE_H = float(os.environ.get("S3LM_CHAIN_WINDOW_BEFORE_H", "24") or 24)
WINDOW_AFTER_H = float(os.environ.get("S3LM_CHAIN_WINDOW_AFTER_H", "2") or 2)
WIDEN_BEFORE_H = float(os.environ.get("S3LM_CHAIN_WIDEN_BEFORE_H", "72") or 72)
MAX_PAGES = int(os.environ.get("S3LM_CHAIN_MAX_PAGES", "24") or 24)
PAGE_SIZE = 50
HTTP_TIMEOUT = float(os.environ.get("S3LM_CHAIN_TIMEOUT", "20") or 20)
USDT_CONTRACT = "TR7NHqjeKQxGTCi8q8ZY4pL8otSzgjLj6t"
USDT_SYMBOLS = {"USDT", "USDC", "TUSD", "DAI"}
TRON_NETWORKS = {"TRON", "TRC-20", "TRC20", "TRX", ""}

# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------

_UTC = timezone.utc


def _now() -> str:
    return datetime.now(_UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _parse_ts(value: Any) -> Optional[datetime]:
    if not value:
        return None
    s = str(value).strip()
    if not s:
        return None
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00")).astimezone(_UTC)
    except ValueError:
        return None


def _iso(dt: Optional[datetime]) -> str:
    return dt.astimezone(_UTC).strftime("%Y-%m-%dT%H:%M:%SZ") if dt else ""


def _ms_iso(ms: Any) -> str:
    try:
        return datetime.fromtimestamp(float(ms) / 1000.0, _UTC).strftime(
            "%Y-%m-%dT%H:%M:%SZ"
        )
    except (TypeError, ValueError, OSError):
        return ""


def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8", "replace")).hexdigest()


def _json_list(raw: Any) -> list[str]:
    """Parse a JSON array / comma list / single txid into a clean list."""
    if not raw:
        return []
    if isinstance(raw, list):
        items = raw
    else:
        s = str(raw).strip()
        if not s:
            return []
        try:
            parsed = json.loads(s)
            items = parsed if isinstance(parsed, list) else [parsed]
        except Exception:
            items = re.split(r"[,|\s]+", s)
    out: list[str] = []
    for item in items:
        h = str(item or "").strip().lower()
        if re.fullmatch(r"[0-9a-f]{64}", h) and h not in out:
            out.append(h)
    return out


def _is_full_hex(value: Any) -> bool:
    return bool(re.fullmatch(r"[0-9a-fA-F]{64}", str(value or "").strip()))


def _clean_address(raw: Any) -> str:
    s = str(raw or "").strip()
    s = s.replace("\u2026", "...").replace("\u00a0", " ")
    # OCR often glues labels/punctuation to the address.
    s = re.sub(r"^[^T0-9A-Za-z]+", "", s)
    s = re.sub(r"[^1-9A-HJ-NP-Za-km-z]+$", "", s)
    return s.strip()


# ---------------------------------------------------------------------------
# Address canonicalisation (no network)
# ---------------------------------------------------------------------------

_B58 = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"


def _tron_checksum_ok(addr: str) -> bool:
    """Local base58check check; mirrors enrich._tron_checksum_ok."""
    try:
        n = 0
        for ch in addr:
            n = n * 58 + _B58.index(ch)
        raw = n.to_bytes(25, "big")
    except Exception:
        return False
    if len(raw) != 25 or raw[0] != 0x41:
        return False
    chk = hashlib.sha256(hashlib.sha256(raw[:21]).digest()).digest()[:4]
    return chk == raw[21:]


def _recover_tron(raw: str) -> Optional[str]:
    try:
        from .enrich import recover_tron_ocr

        return recover_tron_ocr(raw)
    except Exception:
        return None


def canonicalize_address_light(raw: Any, network: str = "TRON") -> dict:
    """Checksum / OCR repair without any network call.

    Returns {raw, address, status, method, confidence}.
    status: valid | repaired | unresolved | unsupported | empty
    """
    raw_s = _clean_address(raw)
    net = (network or "TRON").strip().upper()
    if not raw_s:
        return {
            "raw": "",
            "address": "",
            "status": "empty",
            "method": "",
            "confidence": "",
        }
    if net not in TRON_NETWORKS and "TRON" not in net:
        return {
            "raw": raw_s,
            "address": raw_s,
            "status": "unsupported",
            "method": "",
            "confidence": "",
        }
    if len(raw_s) == 34 and raw_s.startswith("T") and _tron_checksum_ok(raw_s):
        return {
            "raw": raw_s,
            "address": raw_s,
            "status": "valid",
            "method": "checksum",
            "confidence": "high",
        }
    if len(raw_s) == 34 and raw_s.startswith("T"):
        fixed = _recover_tron(raw_s)
        if fixed and _tron_checksum_ok(fixed):
            return {
                "raw": raw_s,
                "address": fixed,
                "status": "repaired",
                "method": "ocr_repair",
                "confidence": "high",
            }
    return {
        "raw": raw_s,
        "address": raw_s,
        "status": "unresolved",
        "method": "",
        "confidence": "low",
    }


def match_known_fragment(raw: Any, known: Iterable[str], network: str = "TRON") -> dict:
    """Resolve a truncated/ellipsis address against known case addresses.

    Tronscan's public API has no prefix search, so this is the deterministic
    fallback for ``TKrpTXoGqwWWzr…BEKzbG`` style reads. Only a unique match is
    accepted.
    """
    raw_s = _clean_address(raw)
    net = (network or "TRON").strip().upper()
    if net not in TRON_NETWORKS and "TRON" not in net:
        return canonicalize_address_light(raw_s, network)
    if len(raw_s) == 34 and raw_s.startswith("T") and _tron_checksum_ok(raw_s):
        return canonicalize_address_light(raw_s, network)
    body = raw_s.replace("...", "…")
    head, tail = body, ""
    if "…" in body:
        head, tail = body.split("…", 1)
    head = re.sub(r"[^1-9A-HJ-NP-Za-km-z]", "", head)
    tail = re.sub(r"[^1-9A-HJ-NP-Za-km-z]", "", tail)
    if len(head) < 6:
        return canonicalize_address_light(raw_s, network)
    hits = []
    for cand in known:
        c = _clean_address(cand)
        if len(c) != 34 or not c.startswith("T") or not _tron_checksum_ok(c):
            continue
        if c.startswith(head) and (not tail or c.endswith(tail)):
            if c not in hits:
                hits.append(c)
    if len(hits) == 1:
        return {
            "raw": raw_s,
            "address": hits[0],
            "status": "repaired",
            "method": "prefix_match",
            "confidence": "high",
        }
    return {
        "raw": raw_s,
        "address": raw_s,
        "status": "unresolved",
        "method": "",
        "confidence": "low",
        "candidates": hits[:10],
    }


# ---------------------------------------------------------------------------
# Txid extraction (full + partial)
# ---------------------------------------------------------------------------

_HEXISH = "0-9a-fA-FlIiOo£€$§"
_FRAG_RE = re.compile(
    r"(?:hash|tx\.?\s*id|txid|transaction\s*(?:hash|id))\s*[:#]?\s*"
    rf"([{_HEXISH}]{{6,}})\s*[:;.,·•]?\s*(?:\.{{2,}}|…)\s*([{_HEXISH}]{{4,}})",
    re.IGNORECASE,
)
_LABELED_FULL_RE = re.compile(
    rf"(?:hash|tx\.?\s*id|txid|transaction\s*(?:hash|id))\s*[:#]?\s*([{_HEXISH}]{{64}})",
    re.IGNORECASE,
)
# Prefix-only labeled fragment ("Hash: 082babb2699d3c" cut off by OCR).
_LABELED_PREFIX_RE = re.compile(
    rf"(?:hash|tx\.?\s*id|txid|transaction\s*(?:hash|id))\s*[:#]?\s*([{_HEXISH}]{{10,}})",
    re.IGNORECASE,
)
# A long hex run split by whitespace/ellipsis without a label (OCR wrap).
_SPLIT_RUN_RE = re.compile(
    rf"([0-9a-fA-F]{{20,}})\s*(?:\.{{2,}}|…|\s+)\s*([0-9a-fA-F]{{8,}})"
)
# The whole hash line after a label, including OCR whitespace inside the hex
# run ("833f...419 1f20...e4", "957449 1d3c...f50d8"). Scanned separately so
# a wrapped hash is joined instead of being lost.
_LABELED_WINDOW_RE = re.compile(
    rf"(?:hash|tx\.?\s*id|txid|transaction\s*(?:hash|id))\s*[:#]?\s*"
    rf"([{_HEXISH}\s.…]{{10,160}})",
    re.IGNORECASE,
)
_HEX_FIX = str.maketrans(
    {
        "l": "1",
        "i": "1",
        "o": "0",
        "s": "5",
        "g": "6",
        "z": "2",
        "£": "c",
        "€": "e",
        "$": "5",
        "§": "5",
    }
)


def _norm_hex(value: str) -> str:
    s = str(value or "").lower().translate(_HEX_FIX)
    return re.sub(r"[^0-9a-f]", "", s)


def _norm_hex_variants(value: str, max_variants: int = 4) -> list[str]:
    """Normalise OCR hex, keeping both readings of ``€``/``£``.

    Tronscan hashes use lowercase hex, and the OCR confuses ``e``/``c`` with
    the currency symbols (live: ``€5144066…`` was really ``e5144066…``). A
    hash is never guessed from this: every variant is checked against
    Tronscan and only a hit is used.
    """
    raw = str(value or "")
    positions = [i for i, ch in enumerate(raw) if ch in "€£"]
    if not positions:
        return [_norm_hex(raw)]
    out: list[str] = []
    for reps in itertools.product("ce", repeat=min(len(positions), 3)):
        chars = list(raw)
        for i, rep in zip(positions, reps):
            chars[i] = rep
        h = _norm_hex("".join(chars))
        if h and h not in out:
            out.append(h)
        if len(out) >= max_variants:
            break
    return out or [_norm_hex(raw)]


def extract_txid_candidates(text: str) -> list[dict]:
    """Return full and partial txid candidates from OCR text.

    Each candidate: {full, prefix, suffix, raw, kind}
    ``full`` is a 64-hex string; ``prefix``/``suffix`` are normalised fragments.
    Duplicate fragments are collapsed.
    """
    if not text:
        return []
    out: list[dict] = []
    seen: set[tuple] = set()

    def add(full: str = "", prefix: str = "", suffix: str = "", raw: str = "", kind: str = ""):
        full = (full or "").lower()
        prefix = prefix or ""
        suffix = suffix or ""
        key = (full, prefix, suffix)
        if key in seen:
            return
        if not full and not prefix:
            return
        seen.add(key)
        out.append(
            {
                "full": full,
                "prefix": prefix,
                "suffix": suffix,
                "raw": raw,
                "kind": kind,
            }
        )

    # 1) Exact full hashes (also handles wrapped lines).
    try:
        from .enrich import extract_txids

        for h in extract_txids(text):
            add(full=h, raw=h, kind="full")
    except Exception:
        for m in re.finditer(r"\b([0-9a-fA-F]{64})\b", text):
            add(full=m.group(1), raw=m.group(1), kind="full")

    # 2) Labeled full hashes with OCR noise (£, l, O).
    for m in _LABELED_FULL_RE.finditer(text):
        for h in _norm_hex_variants(m.group(1)):
            if len(h) == 64:
                add(full=h, raw=m.group(0), kind="full_ocr")

    # 2b) Wrapped labeled hash lines: OCR splits the 64-hex string with
    # spaces ("833f...419 1f20...e4", "957449 1d3c...f50d8"). Join the run
    # before fragment parsing so the hash is not lost.
    for m in _LABELED_WINDOW_RE.finditer(text):
        body = m.group(1)
        if "..." in body or "…" in body:
            parts = re.split(r"(?:\.{2,}|…)", body, maxsplit=1)
            tail = parts[1].strip() if len(parts) > 1 else ""
            # The hash tail is the first token after the ellipsis; later
            # hexish-looking words ("D", "Block") are page text, not hash.
            tails = _norm_hex_variants(tail.split()[0]) if tail else [""]
            for prefix in _norm_hex_variants(parts[0]):
                for suffix in tails:
                    if len(prefix) + len(suffix) == 64:
                        add(full=prefix + suffix, raw=m.group(0), kind="full_wrapped")
                        # Keep the visible prefix as a fallback: if the joined
                        # full is an OCR artifact (truncated hash + page digits),
                        # the block / fragment path can still resolve it.
                        if len(prefix) >= 10:
                            add(
                                prefix=prefix,
                                suffix=suffix,
                                raw=m.group(0),
                                kind="fragment_wrapped",
                            )
                    elif len(prefix) >= 10:
                        add(
                            prefix=prefix,
                            suffix=suffix,
                            raw=m.group(0),
                            kind="fragment_wrapped",
                        )
        else:
            variants = _norm_hex_variants(body)
            first_tok = body.strip().split()[0] if body.strip() else ""
            firsts = _norm_hex_variants(first_tok)
            for h in variants:
                if len(h) >= 64:
                    add(full=h[:64], raw=m.group(0), kind="full_wrapped")
            if any(len(h) >= 64 for h in variants):
                # OCR can join a truncated 40-char hash with the digits of
                # "Block & Time" / timestamps to reach 64. Preserve the
                # contiguous visible prefix so a failed full lookup can fall
                # back to the deterministic block+fragment match.
                for first in firsts:
                    if 10 <= len(first) < 64:
                        add(prefix=first, raw=first_tok, kind="prefix_wrapped")
            else:
                for h in variants:
                    if len(h) >= 10:
                        add(prefix=h, raw=m.group(0), kind="prefix_wrapped")

    # 3) Labeled fragments: "Hash: 00d1f557c42111f9...34b31".
    for m in _FRAG_RE.finditer(text):
        suffixes = _norm_hex_variants(m.group(2))
        for prefix in _norm_hex_variants(m.group(1)):
            for suffix in suffixes:
                if len(prefix) >= 6:
                    add(prefix=prefix, suffix=suffix, raw=m.group(0), kind="fragment")

    # 3b) Prefix-only fragment (OCR cut the rest of the line).
    for m in _LABELED_PREFIX_RE.finditer(text):
        for prefix in _norm_hex_variants(m.group(1)):
            if len(prefix) < 10:
                continue
            if any(
                c.get("prefix") == prefix
                or (c.get("full") or "").startswith(prefix)
                for c in out
            ):
                continue
            add(prefix=prefix, raw=m.group(0), kind="prefix")

    # 4) Unlabeled long split runs (OCR wrapped a 64-hex hash).
    for m in _SPLIT_RUN_RE.finditer(text):
        suffixes = _norm_hex_variants(m.group(2))
        for prefix in _norm_hex_variants(m.group(1)):
            for suffix in suffixes:
                if len(prefix) + len(suffix) == 64:
                    add(full=prefix + suffix, raw=m.group(0), kind="wrapped_full")
                    if len(prefix) >= 20:
                        add(prefix=prefix, suffix=suffix, raw=m.group(0), kind="wrapped")
                elif len(prefix) >= 20:
                    add(prefix=prefix, suffix=suffix, raw=m.group(0), kind="wrapped")

    return out


_BLOCK_RE = re.compile(
    r"block\s*(?:&|and|/)?\s*(?:time|number|height|no\.?)?\s*[:#]?\s*(\d{6,12})",
    re.IGNORECASE,
)


def extract_block_number(text: str) -> Optional[int]:
    """Block height shown on a Tronscan screenshot ("Block & Time: 86231233")."""
    if not text:
        return None
    m = _BLOCK_RE.search(text)
    if not m:
        return None
    try:
        n = int(m.group(1))
    except ValueError:
        return None
    return n if 1_000_000 <= n <= 999_999_999 else None


_block_cache: dict[tuple[int, str], tuple[float, list[str]]] = {}
_block_cache_lock = threading.Lock()


def block_txids(block: int, max_pages: int = 12, stop_prefix: str = "") -> list[str]:
    """Every transaction hash in one block (deterministic fragment anchor).

    A block holds a few hundred transactions, so this is far cheaper and more
    precise than paging a high-volume hot wallet's transfer history. When a
    long fragment prefix is supplied, stop as soon as it matches: a 10+ hex
    prefix is unique inside a block, so the remaining pages add nothing.
    """
    stop_prefix = (stop_prefix or "").lower()
    cache_key = (int(block), stop_prefix[:14])
    with _block_cache_lock:
        hit = _block_cache.get(cache_key)
        if hit and time.time() - hit[0] < 21600:
            return hit[1]
    out: list[str] = []
    for page in range(max(1, max_pages)):
        data = _api_get(
            "/transaction",
            {
                "block": int(block),
                "limit": PAGE_SIZE,
                "start": page * PAGE_SIZE,
                "count": "true",
            },
        )
        items = data.get("data") or data.get("transactions") or []
        if not items:
            break
        for t in items:
            h = str(t.get("hash") or "").strip().lower()
            if _is_full_hex(h) and h not in out:
                out.append(h)
                if len(stop_prefix) >= 10 and h.startswith(stop_prefix):
                    with _block_cache_lock:
                        _block_cache[cache_key] = (time.time(), [h])
                    return [h]
        if len(items) < PAGE_SIZE:
            break
        try:
            total = int(data.get("total") or 0)
        except (TypeError, ValueError):
            total = 0
        if total and len(out) >= total:
            break
    with _block_cache_lock:
        _block_cache[cache_key] = (time.time(), out)
    return out


def _fragment_hit(txid: str, fragments: list[dict]) -> bool:
    for frag in fragments:
        okp, oks = _fragment_match(
            txid, frag.get("prefix", ""), frag.get("suffix", "")
        )
        if okp or oks:
            return True
    return False


# ---------------------------------------------------------------------------
# Tronscan HTTP client
# ---------------------------------------------------------------------------


class _RateLimiter:
    def __init__(self, rate: float):
        self.rate = max(0.2, rate)
        self.min_interval = 1.0 / self.rate
        self._lock = threading.Lock()
        self._next = 0.0

    def acquire(self):
        with self._lock:
            now = time.monotonic()
            wait = self._next - now
            if wait > 0:
                time.sleep(wait)
                now = time.monotonic()
            self._next = now + self.min_interval


_limiter = _RateLimiter(RATE_PER_SEC)
_client_lock = threading.Lock()
_client: Optional[httpx.Client] = None
_fail_count = 0
_pause_until = 0.0
_last_ok_utc = ""
_last_error = ""
_error_seq = 0
_stats_lock = threading.Lock()


def _get_client() -> httpx.Client:
    global _client
    with _client_lock:
        if _client is None:
            _client = httpx.Client(
                timeout=httpx.Timeout(HTTP_TIMEOUT, connect=10.0),
                follow_redirects=True,
                trust_env=False,
                headers={
                    "User-Agent": "s3-live-monitor-chain/1.0",
                    "Accept": "application/json",
                },
            )
        return _client


def _note_ok():
    global _fail_count, _last_ok_utc, _last_error
    with _stats_lock:
        _fail_count = 0
        _last_ok_utc = _now()
        _last_error = ""


def _note_fail(msg: str):
    global _fail_count, _pause_until, _last_error, _error_seq
    with _stats_lock:
        _fail_count += 1
        _error_seq += 1
        _last_error = str(msg)[:300]
        if _fail_count >= 8:
            _pause_until = time.time() + 60


def api_ready() -> bool:
    """False while Tronscan is failing/paused.

    Callers must not persist a negative ("unresolved") result while the API is
    unavailable: that would permanently mislabel a slip after a transient 429
    or network outage.
    """
    with _stats_lock:
        return (
            ENABLED
            and time.time() >= _pause_until
            and _fail_count < 8
        )


def _api_get(path: str, params: dict) -> dict:
    """GET a Tronscan endpoint with retries, rate limit and circuit breaker."""
    if not ENABLED:
        return {}
    if time.time() < _pause_until:
        return {}
    url = f"{TRONSCAN_BASE}{path}"
    headers = {}
    if TRONSCAN_API_KEY:
        headers["TRON-PRO-API-KEY"] = TRONSCAN_API_KEY
    last_exc: Exception | None = None
    for attempt in range(4):
        _limiter.acquire()
        try:
            r = _get_client().get(url, params=params, headers=headers)
            if r.status_code == 429:
                # Four dashboards share one egress IP; back off well past
                # Tronscan's window instead of hammering it into a pause.
                retry_after = r.headers.get("Retry-After") or ""
                try:
                    wait = min(30.0, max(2.0, float(retry_after)))
                except (TypeError, ValueError):
                    wait = 2.0 * (attempt + 1) + 1.0
                last_exc = httpx.HTTPStatusError(
                    "HTTP 429", request=r.request, response=r
                )
                if attempt < 3:
                    time.sleep(wait)
                continue
            if r.status_code in (500, 502, 503, 504):
                raise httpx.HTTPStatusError(
                    f"HTTP {r.status_code}", request=r.request, response=r
                )
            if r.status_code in (400, 404):
                # Invalid address / unknown hash / block: a negative answer,
                # not an API outage. Counting it as a failure tripped the
                # circuit breaker after three bad OCR hashes and silently
                # skipped the rest of a recheck (live bug, 2026-11-20).
                _note_ok()
                return {}
            r.raise_for_status()
            data = r.json()
            if isinstance(data, dict):
                _note_ok()
                return data
            _note_ok()
            return {}
        except Exception as e:  # noqa: BLE001 - network errors are expected
            last_exc = e
            if attempt < 3:
                time.sleep(1.5 * (2**attempt))
    _note_fail(str(last_exc or "unknown"))
    return {}


# ---------------------------------------------------------------------------
# Tronscan endpoints
# ---------------------------------------------------------------------------


def account_exists(address: str) -> bool:
    if not address or not _tron_checksum_ok(address):
        return False
    data = _api_get("/account", {"address": address})
    return bool(data.get("address"))


# GasFree "Permit Transfer" receipts list the fee transfer first in
# trc20TransferInfo: [0] is the 1–1.5 USDT network fee paid to the relayer,
# [1] is the real transfer. Picking [0] made a 500 USDT withdrawal look like
# a 1.5 USDT fee (live bug, 2026-11-22). Decode the contract call and select
# the transfer it actually moved.
PERMIT_TRANSFER_METHOD = "6f21b898"
_FEE_COLLECTORS = {"TLntW9Z59LYY5KEi9cmwk3PKjQga828ird"}


def _tron_payload_hex(addr: str) -> str:
    """20-byte account payload as hex (no 0x41 prefix), or ""."""
    try:
        n = 0
        for ch in str(addr or ""):
            n = n * 58 + _B58.index(ch)
        raw = n.to_bytes(25, "big")
    except Exception:
        return ""
    if len(raw) != 25 or raw[0] != 0x41:
        return ""
    return raw[1:21].hex()


def _permit_target(data: dict) -> tuple[str, int]:
    """(recipient payload hex, value) for a GasFree permit transfer."""
    cd = data.get("contractData") or {}
    hx = str(cd.get("data") or "")
    if not hx.startswith(PERMIT_TRANSFER_METHOD) or len(hx) < 8 + 4 * 64:
        return "", 0
    try:
        to_hex = hx[8 + 2 * 64 : 8 + 3 * 64][-40:].lower()
        value = int(hx[8 + 3 * 64 : 8 + 4 * 64], 16)
    except (TypeError, ValueError):
        return "", 0
    return to_hex, value


def _pick_trc20_transfer(data: dict) -> dict:
    """The transfer the receipt is about, not the GasFree fee transfer."""
    trc20 = [t for t in (data.get("trc20TransferInfo") or []) if isinstance(t, dict)]
    if not trc20:
        return {}
    if len(trc20) == 1:
        return trc20[0]
    to_hex, value = _permit_target(data)
    if to_hex:
        by_addr = [
            t
            for t in trc20
            if _tron_payload_hex(str(t.get("to_address") or "")) == to_hex
        ]
        for t in by_addr:
            try:
                if int(t.get("amount_str") or 0) == value:
                    return t
            except (TypeError, ValueError):
                pass
        if by_addr:
            return by_addr[0]
    normal = [
        t
        for t in trc20
        if str(t.get("to_address") or "") not in _FEE_COLLECTORS
    ]
    pool = normal or trc20

    def _amt(t: dict) -> int:
        try:
            return int(t.get("amount_str") or 0)
        except (TypeError, ValueError):
            return 0

    return max(pool, key=_amt)


def tx_info(txid: str) -> Optional[dict]:
    """Normalised transaction details, or None if Tronscan does not know it."""
    h = str(txid or "").strip().lower()
    if not _is_full_hex(h):
        return None
    cached = db.chain_tx_cache_get(h)
    if cached:
        return cached
    data = _api_get("/transaction-info", {"hash": h})
    if not data:
        return None
    if not data.get("contractRet") and not data.get("hash"):
        return None
    trc20 = data.get("trc20TransferInfo") or []
    first = _pick_trc20_transfer(data)
    decimals = int(first.get("decimals") or 6)
    amount = None
    token = ""
    from_addr = ""
    to_addr = ""
    if first:
        try:
            amount = int(first.get("amount_str") or 0) / (10**decimals)
        except (TypeError, ValueError):
            amount = None
        token = str(first.get("symbol") or first.get("tokenAbbr") or "USDT")
        from_addr = str(first.get("from_address") or "")
        to_addr = str(first.get("to_address") or "")
    if not first:
        cd = data.get("contractData") or {}
        from_addr = str(data.get("ownerAddress") or cd.get("owner_address") or "")
        to_addr = str(data.get("toAddress") or cd.get("to_address") or "")
        token = "TRX"
        try:
            amount = int(cd.get("amount") or 0) / 1_000_000
        except (TypeError, ValueError):
            amount = None
    info = {
        "txid": h,
        "network": "TRON",
        "from": from_addr,
        "to": to_addr,
        "amount": amount,
        "token": token,
        "token_contract": str(first.get("contract_address") or ""),
        "block_ts": _ms_iso(data.get("timestamp")),
        "contract_ret": str(data.get("contractRet") or ""),
        "confirmed": bool(data.get("confirmed")),
        "raw": data,
    }
    db.chain_tx_cache_put(info)
    return info


def _normalise_transfer(t: dict) -> Optional[dict]:
    if not isinstance(t, dict):
        return None
    txid = str(t.get("transaction_id") or "").strip().lower()
    if not _is_full_hex(txid):
        return None
    ti = t.get("tokenInfo") or {}
    decimals = int(ti.get("tokenDecimal") or ti.get("decimals") or 6)
    try:
        amount = int(t.get("quant") or t.get("value") or 0) / (10**decimals)
    except (TypeError, ValueError):
        amount = None
    return {
        "txid": txid,
        "network": "TRON",
        "from": str(t.get("from_address") or ""),
        "to": str(t.get("to_address") or ""),
        "amount": amount,
        "token": str(ti.get("tokenAbbr") or ti.get("symbol") or "USDT"),
        "token_contract": str(t.get("contract_address") or ""),
        "block_ts": _ms_iso(t.get("block_ts")),
        "contract_ret": str(t.get("contractRet") or ""),
        "confirmed": bool(t.get("confirmed")),
        "raw": t,
    }


_transfer_cache: dict[tuple[str, int, int, int], tuple[float, list[dict]]] = {}
_transfer_lock = threading.Lock()


def _transfer_page(
    address: str, start: int, lo_ms: int = 0, hi_ms: int = 0
) -> list[dict]:
    """One Tronscan transfer page, optionally bounded server-side by time.

    The timestamp bounds matter: a case slip can be months old, and Tronscan
    otherwise returns the newest 50 transfers first. Without the bounds a
    24-page scan never reaches the slip's window (live bug, 2026-11-20).
    """
    key = (address, start, lo_ms, hi_ms)
    with _transfer_lock:
        hit = _transfer_cache.get(key)
        if hit and time.time() - hit[0] < 3600:
            return hit[1]
    params: dict[str, Any] = {
        "limit": PAGE_SIZE,
        "start": start,
        "sort": "-timestamp",
        "count": "true",
        "relatedAddress": address,
    }
    if lo_ms:
        params["start_timestamp"] = lo_ms
    if hi_ms:
        params["end_timestamp"] = hi_ms
    data = _api_get("/token_trc20/transfers", params)
    items = data.get("token_transfers") or data.get("data") or []
    out = [x for x in (_normalise_transfer(t) for t in items) if x]
    with _transfer_lock:
        _transfer_cache[key] = (time.time(), out)
    return out


def transfers_for(
    address: str,
    slip_ts: Optional[datetime],
    before_h: float = WINDOW_BEFORE_H,
    after_h: float = WINDOW_AFTER_H,
    max_pages: int = MAX_PAGES,
) -> list[dict]:
    """TRC20 transfers for an address inside the slip time window.

    Tronscan is asked for the window directly (start_timestamp/end_timestamp),
    then we page within it. The page cache keeps repeat slip/worker runs cheap.
    """
    if not address or not _tron_checksum_ok(address):
        return []
    base = slip_ts or datetime.now(_UTC)
    lo = base - timedelta(hours=before_h)
    hi = base + timedelta(hours=after_h)
    lo_ms = int(lo.timestamp() * 1000)
    hi_ms = int(hi.timestamp() * 1000)
    out: list[dict] = []
    start = 0
    for _ in range(max(1, max_pages)):
        page = _transfer_page(address, start, lo_ms, hi_ms)
        if not page:
            break
        for t in page:
            ts = _parse_ts(t.get("block_ts"))
            if ts and lo <= ts <= hi:
                out.append(t)
        if len(page) < PAGE_SIZE:
            break
        start += PAGE_SIZE
    return out


# ---------------------------------------------------------------------------
# Matching
# ---------------------------------------------------------------------------


def _ocr_usdt_amount(text: str) -> Optional[float]:
    if not text:
        return None
    m = re.search(
        r"transferred\s+([\d,]+(?:\.\d+)?)\s*[^\w\s]{0,2}\s*(?:USDT|USDC|TUSD|DAI)\b",
        text,
        re.IGNORECASE,
    )
    if not m:
        return None
    try:
        return float(m.group(1).replace(",", ""))
    except ValueError:
        return None


def _fragment_match(txid: str, prefix: str, suffix: str) -> tuple[bool, bool]:
    tid = txid.lower()
    p = (prefix or "").lower()
    s = (suffix or "").lower()
    # 14 hex chars is already a 56-bit anchor; going to 16 breaks on the last
    # OCR-degraded character (`…facib9t` → `…fac1b9`).
    okp = bool(p) and len(p) >= 6 and tid.startswith(p[: min(len(p), 14)])
    oks = bool(s) and len(s) >= 4 and tid.endswith(s[-6:] if len(s) >= 6 else s)
    return okp, oks


def _score_transfer(
    t: dict,
    fragments: list[dict],
    subject_addrs: set[str],
    counterparty_addrs: set[str],
    direction: str,
    amount: Optional[float],
    slip_ts: Optional[datetime],
) -> tuple[float, dict]:
    score = 0.0
    why: dict[str, Any] = {}
    tid = t["txid"]
    for frag in fragments:
        okp, oks = _fragment_match(tid, frag.get("prefix", ""), frag.get("suffix", ""))
        if okp:
            score += 2.0
            why["prefix"] = frag.get("prefix")
        if oks:
            score += 2.0
            why["suffix"] = frag.get("suffix")
    token = str(t.get("token") or "").upper()
    if token in USDT_SYMBOLS:
        score += 1.0
        why["token"] = token
    if amount is not None and t.get("amount") is not None:
        try:
            if abs(float(t["amount"]) - float(amount)) <= max(0.01, abs(amount) * 0.005):
                score += 2.0
                why["amount"] = float(t["amount"])
        except (TypeError, ValueError):
            pass
    frm, to = t.get("from") or "", t.get("to") or ""
    d = (direction or "").lower()
    if d == "deposit":
        if to in subject_addrs:
            score += 2.0
            why["direction"] = "to"
        elif frm in subject_addrs:
            score += 1.0
            why["direction"] = "from_weak"
    elif d == "withdrawal":
        if frm in subject_addrs:
            score += 2.0
            why["direction"] = "from"
        elif to in subject_addrs:
            score += 1.0
            why["direction"] = "to_weak"
    else:
        if frm in subject_addrs or to in subject_addrs:
            score += 1.0
    if counterparty_addrs and (frm in counterparty_addrs or to in counterparty_addrs):
        score += 3.0
        why["counterparty"] = frm if frm in counterparty_addrs else to
    ts = _parse_ts(t.get("block_ts"))
    if ts and slip_ts:
        delta_h = abs((slip_ts - ts).total_seconds()) / 3600.0
        if delta_h <= 1:
            score += 2.0
            why["time_h"] = round(delta_h, 2)
        elif delta_h <= 6:
            score += 1.0
            why["time_h"] = round(delta_h, 2)
    return score, why


def _candidate_payload(t: dict, score: float, why: dict) -> dict:
    return {
        "txid": t["txid"],
        "from": t.get("from") or "",
        "to": t.get("to") or "",
        "amount": t.get("amount"),
        "token": t.get("token") or "",
        "block_ts": t.get("block_ts") or "",
        "contract_ret": t.get("contract_ret") or "",
        "score": round(score, 2),
        "why": why,
    }


def _tx_involves(info: dict, addrs: set[str]) -> bool:
    if not info:
        return False
    if info.get("from") in addrs or info.get("to") in addrs:
        return True
    for t in (info.get("raw") or {}).get("trc20TransferInfo") or []:
        if not isinstance(t, dict):
            continue
        if str(t.get("from_address") or "") in addrs or str(t.get("to_address") or "") in addrs:
            return True
    return False


def _visible_address_runs(text: str) -> list[str]:
    """Address-shaped runs on a Tronscan screenshot, including wrapped OCR."""
    if not text:
        return []
    out: list[str] = []
    seen: set[str] = set()
    run_re = re.compile(
        r"T[1-9A-HJ-NP-Za-km-zOoIl0]{6,40}"
        r"(?:\s+[1-9A-HJ-NP-Za-km-zOoIl0]{2,40})?"
    )
    for m in run_re.finditer(text):
        cand = re.sub(r"\s+", "", m.group(0))
        if len(cand) >= 10 and cand not in seen:
            seen.add(cand)
            out.append(cand)
    return out


def match_tx_addresses_in_text(info: Optional[dict], text: str) -> list[dict]:
    """Recover the full from/to addresses from truncated screenshot text.

    Tronscan renders ``TIXHSVWjd2Uo09uutabhSdZztq9cGEM...``; the transaction
    itself gives the exact 34-char address. This closes the last gap where a
    slip's own wallet was too degraded to appear verbatim on-chain.
    """
    if not info or not text:
        return []
    runs = _visible_address_runs(text)
    if not runs:
        return []
    out: list[dict] = []
    used: set[str] = set()
    for role in ("from", "to"):
        addr = str(info.get(role) or "")
        if not (len(addr) == 34 and addr.startswith("T") and _tron_checksum_ok(addr)):
            continue
        best: Optional[tuple[int, str]] = None
        for run in runs:
            if run in used or run == addr:
                continue
            n = min(len(run), 34)
            if n < 10:
                continue
            dist = _levenshtein(run[:n].lower(), addr[:n].lower())
            # Tronscan truncates with "..." after ~20 chars; OCR adds one or
            # two lookalike substitutions at most.
            if dist <= (1 if n <= 16 else 2):
                if best is None or dist < best[0]:
                    best = (dist, run)
        if best is None:
            continue
        dist, run = best
        used.add(run)
        out.append(
            {
                "raw": run,
                "address": addr,
                "network": "TRON",
                "status": "tronscan_confirmed",
                "method": f"tx_{role}",
                "confidence": "high" if dist == 0 else "medium",
                "tx_role": role,
            }
        )
    return out


def _levenshtein(a: str, b: str) -> int:
    """Small edit distance for OCR-degraded address fragments."""
    if a == b:
        return 0
    if not a:
        return len(b)
    if not b:
        return len(a)
    prev = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        cur = [i] + [0] * len(b)
        for j, cb in enumerate(b, 1):
            cur[j] = min(
                prev[j] + 1,
                cur[j - 1] + 1,
                prev[j - 1] + (0 if ca == cb else 1),
            )
        prev = cur
    return prev[-1]


def match_known_in_text(
    text: str, known: Iterable[str], exclude: Iterable[str] = ()
) -> list[dict]:
    """Recover truncated/OCR-degraded wallet addresses shown on a screenshot.

    Tronscan displays ``TIXHSVWjd2Uo09uu LabhSdZztq9cGEMQPqx`` for
    ``TJXhSVWjd2Uo9uu1abhSdZztq9cGEMQPqx``. The public API has no prefix
    search, so match the visible run(s) against known case addresses with a
    tight edit distance. Only a unique match is accepted.
    """
    if not text:
        return []
    valid = [
        str(k).strip()
        for k in known
        if len(str(k).strip()) == 34
        and str(k).strip().startswith("T")
        and _tron_checksum_ok(str(k).strip())
    ]
    if not valid:
        return []
    skip = {str(x).strip() for x in exclude}
    out: list[dict] = []
    seen: set[str] = set()
    run_re = re.compile(
        r"T[1-9A-HJ-NP-Za-km-zOoIl0]{6,40}"
        r"(?:\s+[1-9A-HJ-NP-Za-km-zOoIl0]{2,40})?"
    )
    for m in run_re.finditer(text):
        cand = re.sub(r"\s+", "", m.group(0))
        if len(cand) < 12 or cand in seen or cand in skip:
            continue
        if not any(ch.isdigit() for ch in cand):
            continue
        seen.add(cand)
        scored: list[tuple[int, str]] = []
        for k in valid:
            if len(cand) <= 22:
                if abs(len(cand) - len(k)) > 3:
                    continue
                d = _levenshtein(cand.lower(), k[: len(cand)].lower())
                if d <= 2:
                    scored.append((d, k))
            else:
                d = _levenshtein(cand.lower(), k[: len(cand)].lower())
                if d <= 3:
                    scored.append((d, k))
                if len(cand) >= 30:
                    d2 = _levenshtein(cand.lower(), k.lower())
                    if d2 <= 3:
                        scored.append((d2, k))
        if not scored:
            continue
        scored.sort()
        if (
            len(scored) > 1
            and scored[0][0] == scored[1][0]
            and scored[0][1] != scored[1][1]
        ):
            continue
        dist, addr = scored[0]
        out.append(
            {
                "raw": cand,
                "address": addr,
                "status": "repaired",
                "method": "text_fragment",
                "confidence": "high" if dist <= 1 else "medium",
            }
        )
    return out


# ---------------------------------------------------------------------------
# Source resolution
# ---------------------------------------------------------------------------


def _source_wallets(payload: dict, rows: list[dict], known: list[str]) -> tuple[list[dict], dict[str, dict]]:
    wallets: list[dict] = []
    by_raw: dict[str, dict] = {}
    candidates: list[tuple[str, str]] = []
    for w in payload.get("wallets") or []:
        if isinstance(w, dict) and w.get("address"):
            candidates.append((str(w["address"]), str(w.get("network") or "TRON")))
    for r in rows:
        if str(r.get("rail_type") or "crypto_wallet") != "crypto_wallet":
            continue
        raw = r.get("rail_id_raw") or r.get("rail_id") or ""
        if raw:
            candidates.append((str(raw), str(r.get("network") or "TRON")))
    for raw, net in candidates:
        if raw in by_raw:
            continue
        c = canonicalize_address_light(raw, net)
        if c["status"] == "unresolved":
            c = match_known_fragment(raw, known, net)
        by_raw[raw] = c
        wallets.append(c)
    return wallets, by_raw


def _source_text(payload: dict, rows: list[dict]) -> str:
    text = str(payload.get("text_sample") or "")
    if not text:
        for r in rows:
            try:
                ev = json.loads(r.get("evidence_json") or "{}")
            except Exception:
                ev = {}
            text = str(ev.get("text_sample") or "")
            if text:
                break
    return text


def _source_amount(payload: dict, rows: list[dict], text: str) -> Optional[float]:
    cur = str(payload.get("amount_currency") or "").upper()
    val = payload.get("amount_value")
    if val is not None and (cur in USDT_SYMBOLS or not cur):
        try:
            return float(val)
        except (TypeError, ValueError):
            pass
    for r in rows:
        try:
            ev = json.loads(r.get("evidence_json") or "{}")
        except Exception:
            ev = {}
        cur = str(ev.get("amount_currency") or "").upper()
        val = ev.get("amount_value")
        if val is not None and (cur in USDT_SYMBOLS or not cur):
            try:
                return float(val)
            except (TypeError, ValueError):
                pass
    return _ocr_usdt_amount(text)


def _source_time(payload: dict, rows: list[dict]) -> Optional[datetime]:
    return _parse_ts(
        payload.get("_last_modified")
        or (rows[0].get("last_modified") if rows else "")
        or payload.get("seen_utc")
    )


def resolve_source(
    source_key: str,
    payload: dict,
    rows: list[dict],
    known: Optional[list[str]] = None,
) -> dict:
    """Resolve canonical wallets + transaction hash(es) for one source object."""
    checked = _now()
    text = _source_text(payload, rows)
    known = known or []
    wallets, by_raw = _source_wallets(payload, rows, known)
    slip_ts = _source_time(payload, rows)
    direction = str((rows[0] if rows else {}).get("direction") or "")
    if not direction:
        # Folder is the direction of the gambler's money movement.
        if source_key.startswith(("withdrawal/", "payment-proof/", "payment-proofs/")):
            direction = "withdrawal"
        elif source_key.startswith(("deposit/", "bankingqr/", "qrcode/")):
            direction = "deposit"
    amount = _source_amount(payload, rows, text)
    fragments = extract_txid_candidates(text)
    # Payload-level txids are authoritative even when text_sample is truncated.
    for h in _json_list(payload.get("txids")):
        if not any(c.get("full") == h for c in fragments):
            fragments.append({"full": h, "prefix": "", "suffix": "", "raw": h, "kind": "payload"})
    for w in payload.get("wallets") or []:
        if isinstance(w, dict):
            for h in _json_list(w.get("txids")):
                if not any(c.get("full") == h for c in fragments):
                    fragments.append({"full": h, "prefix": "", "suffix": "", "raw": h, "kind": "payload"})

    subject_addrs = {
        c["address"]
        for c in wallets
        if c.get("status") in ("valid", "repaired", "tronscan_confirmed")
    }
    # Rows may carry a canonical rail even if the payload wallet list is empty.
    for r in rows:
        if str(r.get("rail_type") or "crypto_wallet") != "crypto_wallet":
            continue
        a = str(r.get("rail_id") or "")
        if a and _tron_checksum_ok(a):
            subject_addrs.add(a)
    # Recover truncated recipient addresses visible in the slip text (Tronscan
    # shows "TIX… Labh…" with OCR case/zero noise). They are used to verify
    # involvement and direction; the row's own rail is left untouched.
    if known:
        for w in match_known_in_text(text, known, exclude=subject_addrs):
            addr = str(w.get("address") or "")
            if addr and addr not in subject_addrs:
                wallets.append(w)
                subject_addrs.add(addr)
    # Counterparties are other full addresses visible in the slip text; never
    # the subject itself (that double-counted every subject transfer before).
    counterparties: set[str] = set()
    for m in re.finditer(r"\bT[1-9A-HJ-NP-Za-km-z]{33}\b", text):
        cand = canonicalize_address_light(m.group(0), "TRON")
        a = str(cand.get("address") or "")
        if a and a not in subject_addrs and _tron_checksum_ok(a):
            counterparties.add(a)

    best_info: Optional[dict] = None
    best_status = "unresolved"
    best_method = ""
    best_conf = "low"
    best_candidates: list[dict] = []
    frags = [f for f in fragments if f.get("prefix")]

    # 1) Full hashes first — the strongest signal. A full 64-hex hash read
    # from a Tronscan screenshot is authoritative even when the OCR wallet is
    # missing/degraded; the tx itself supplies the real from/to addresses.
    for frag in fragments:
        full = frag.get("full") or ""
        if not full:
            continue
        info = tx_info(full)
        if info:
            best_info = info
            best_status = "verified"
            best_method = "txid_full"
            best_conf = "high"
            break

    # 1b) Block-height anchor: a Tronscan screenshot shows "Block & Time".
    # One block holds a few hundred tx, so this beats paging a hot wallet.
    # A unique 14-hex prefix + block is deterministic, even with no wallet.
    if best_info is None and fragments:
        block = extract_block_number(text)
        if block:
            # A full hash that Tronscan did not recognise may still be a
            # truncated OCR join; its first 14 chars are a valid block anchor.
            block_frags = list(frags) + [
                {"prefix": f["full"][:14], "suffix": f["full"][-6:]}
                for f in fragments
                if len(f.get("full") or "") == 64
            ]
            best_prefix = max(
                (str(f.get("prefix") or "") for f in block_frags), key=len, default=""
            )
            matches = [
                h
                for h in block_txids(block, stop_prefix=best_prefix)
                if _fragment_hit(h, block_frags)
            ]
            if len(matches) == 1:
                info = tx_info(matches[0])
                if info:
                    best_info = info
                    best_status = "derived"
                    best_method = "block_fragment"
                    best_conf = "high"
                    best_candidates = [
                        _candidate_payload(
                            info, 12.0, {"block": block, "fragment": True}
                        )
                    ]

    def _scan(before_h: float, after_h: float) -> list[dict]:
        scored: dict[str, dict] = {}
        for addr in subject_addrs:
            for t in transfers_for(addr, slip_ts, before_h=before_h, after_h=after_h):
                score, why = _score_transfer(
                    t,
                    frags,
                    subject_addrs,
                    counterparties,
                    direction,
                    amount,
                    slip_ts,
                )
                if score < 2:
                    continue
                prev = scored.get(t["txid"])
                if prev is None or score > prev["score"]:
                    scored[t["txid"]] = _candidate_payload(t, score, why)
        return sorted(
            scored.values(), key=lambda c: (-c["score"], c.get("block_ts") or "")
        )

    # 2) Transfers: tight window first (screenshots are taken right after the
    # transfer), then widen. A high-volume exchange wallet can have >10k
    # transfers in an hour, so a tight window is what makes pagination reach
    # the slip's transaction at all.
    #
    # Without a hash fragment AND without a USDT amount, an incoming transfer
    # in a time window is not evidence of this slip's transaction (a static
    # deposit QR is not a receipt). Stay unresolved rather than attach a
    # random transfer.
    if best_info is None and subject_addrs and (frags or amount is not None):
        if frags:
            stages = [(0.5, 0.05), (6.0, 0.25), (WIDEN_BEFORE_H, 2.0)]
        else:
            stages = [
                (WINDOW_BEFORE_H, WINDOW_AFTER_H),
                (WIDEN_BEFORE_H, 2.0),
            ]
        for before_h, after_h in stages:
            ranked = _scan(before_h, after_h)
            if not ranked:
                continue
            best_candidates = ranked[:10]
            top = ranked[0]
            second = ranked[1] if len(ranked) > 1 else None
            unique = second is None or top["score"] > second["score"] + 0.01
            frag_hit = bool(
                top.get("why", {}).get("prefix")
                or top.get("why", {}).get("suffix")
            )
            if frags:
                # With a hash fragment in the OCR, an amount/time-only match is
                # not evidence — it would attach the wrong tx (live bug).
                if frag_hit and unique and top["score"] >= 4:
                    best_info = tx_info(top["txid"])
                    best_status = "derived"
                    best_method = "txid_fragment"
                    best_conf = "high"
                    break
            elif unique and top["score"] >= 4 and top.get("why", {}).get("amount") is not None:
                best_info = tx_info(top["txid"])
                best_status = "derived"
                best_method = "amount_time"
                best_conf = "medium"
                break
            elif (
                not unique
                and top["score"] >= 4
                and top.get("why", {}).get("amount") is not None
            ):
                best_status = "ambiguous"
                best_method = "multiple_candidates"
                best_conf = "low"
                break
        if best_info is None and frags:
            # Keep only fragment-bearing candidates for audit; an amount-only
            # guess is not a transaction lead.
            frag_cands = [
                c
                for c in best_candidates
                if c.get("why", {}).get("prefix") or c.get("why", {}).get("suffix")
            ]
            if len(frag_cands) > 1:
                best_status = "ambiguous"
                best_method = "multiple_candidates"
                best_conf = "low"
                best_candidates = frag_cands
            else:
                best_status = "unresolved"
                best_method = ""
                best_conf = "low"
                best_candidates = []

    # 3) Reconcile the subject wallet with the on-chain transaction. Screenshots
    # truncate addresses ("TIX…Labh…"); the tx supplies the full 34 chars.
    tx_wallets: list[dict] = []
    subject_address = ""
    subject_raw = ""
    subject_status = ""
    if best_info:
        tx_wallets = match_tx_addresses_in_text(best_info, text)
        for w in tx_wallets:
            addr = str(w.get("address") or "")
            if addr and addr not in subject_addrs:
                wallets.append(w)
                subject_addrs.add(addr)
        if not direction:
            roles = {str(w.get("tx_role") or "") for w in tx_wallets}
            if "to" in roles and "from" not in roles:
                direction = "deposit"
            elif "from" in roles and "to" not in roles:
                direction = "withdrawal"
            else:
                # No visible roles: the side that is a known case address is
                # the subject. Otherwise fall back to the receiving side.
                known_set = {str(k) for k in known}
                tx_to = str(best_info.get("to") or "")
                tx_from = str(best_info.get("from") or "")
                to_known = tx_to in subject_addrs or tx_to in known_set
                from_known = tx_from in subject_addrs or tx_from in known_set
                if to_known and not from_known:
                    direction = "deposit"
                elif from_known and not to_known:
                    direction = "withdrawal"
                elif tx_to:
                    direction = "deposit"
                elif tx_from:
                    direction = "withdrawal"
        role = (
            "to"
            if direction == "deposit"
            else "from"
            if direction == "withdrawal"
            else ""
        )
        if not role:
            role = "to" if best_info.get("to") else "from"
        cand = str(best_info.get(role) or "")
        if len(cand) == 34 and _tron_checksum_ok(cand):
            subject_address = cand
            wmatch = next(
                (w for w in wallets if str(w.get("address") or "") == cand), None
            )
            subject_raw = str((wmatch or {}).get("raw") or cand)
            subject_status = "tronscan_confirmed" if wmatch else "valid"

    # 4) Build per-row results.
    chosen_txid = best_info["txid"] if best_info else ""
    txids = [chosen_txid] if chosen_txid else []
    if best_status == "ambiguous":
        txids = [c["txid"] for c in best_candidates]
    row_results: list[dict] = []
    crypto_rows = [
        r
        for r in rows
        if str(r.get("rail_type") or "crypto_wallet") == "crypto_wallet"
    ]
    single_crypto_row = len(crypto_rows) == 1
    for r in crypto_rows:
        rail = str(r.get("rail_id") or "")
        raw = str(r.get("rail_id_raw") or rail)
        c = by_raw.get(raw) or by_raw.get(rail) or canonicalize_address_light(rail, r.get("network") or "TRON")
        canonical = c.get("address") or rail
        involved = bool(best_info and _tx_involves(best_info, {canonical}))
        # Single-wallet slips whose OCR address is too degraded to appear on
        # the tx: adopt the full on-chain subject address (the recipient for a
        # deposit, the sender for a withdrawal). The raw OCR string is kept.
        if (
            best_info
            and subject_address
            and single_crypto_row
            and not involved
            and c.get("status") not in ("valid", "repaired", "tronscan_confirmed")
        ):
            canonical = subject_address
            c = {
                "address": subject_address,
                "status": "tronscan_confirmed",
                "raw": raw or subject_raw,
                "method": "tx_subject",
                "confidence": "high",
            }
            involved = True
            if not raw:
                raw = subject_raw
        # A source-level verified/derived tx is attached to every row whose
        # wallet is on the tx. On a single-wallet slip the tx is the slip's
        # own transaction even if its OCR address is too degraded to appear
        # verbatim on-chain (the recipient was proven from the slip text).
        if best_info and (involved or single_crypto_row):
            status = best_status
            txid = chosen_txid
        elif best_status == "ambiguous":
            status = "ambiguous"
            txid = ""
        elif best_info:
            status = "unresolved"
            txid = ""
        else:
            status = best_status
            txid = ""
        row_results.append(
            {
                "id": r.get("id"),
                "rail_type": "crypto_wallet",
                "rail_id": canonical,
                "rail_id_raw": raw,
                "wallet_status": c.get("status") or "",
                "txid": txid,
                "txids": json.dumps(txids if status in ("verified", "derived") else txids),
                "tx_status": status,
                "tx_method": best_method,
                "tx_confidence": best_conf,
                "tx_time": (best_info or {}).get("block_ts") or "",
                "tx_from": (best_info or {}).get("from") or "",
                "tx_to": (best_info or {}).get("to") or "",
                "tx_amount": (best_info or {}).get("amount"),
                "tx_token": (best_info or {}).get("token") or "",
                "tx_contract_ret": (best_info or {}).get("contract_ret") or "",
                "tx_candidates": json.dumps(best_candidates if status == "ambiguous" else []),
                "chain_checked_utc": checked,
            }
        )

    links = []
    if chosen_txid or best_candidates:
        for c in best_candidates or (
            [
                {
                    "txid": chosen_txid,
                    "from": (best_info or {}).get("from") or "",
                    "to": (best_info or {}).get("to") or "",
                    "amount": (best_info or {}).get("amount"),
                    "token": (best_info or {}).get("token") or "",
                    "block_ts": (best_info or {}).get("block_ts") or "",
                    "contract_ret": (best_info or {}).get("contract_ret") or "",
                    "score": 0,
                    "why": {},
                }
            ]
        ):
            links.append(
                {
                    "txid": c["txid"],
                    "status": best_status,
                    "method": best_method,
                    "confidence": best_conf,
                    "score": c.get("score") or 0,
                    "from": c.get("from") or "",
                    "to": c.get("to") or "",
                    "amount": c.get("amount"),
                    "token": c.get("token") or "",
                    "block_ts": c.get("block_ts") or "",
                    "contract_ret": c.get("contract_ret") or "",
                    "candidates_json": json.dumps(best_candidates),
                }
            )

    return {
        "source_key": source_key,
        "checked_utc": checked,
        "wallets": wallets,
        "rows": row_results,
        "links": links,
        "summary": {
            "tx_status": best_status,
            "txid": chosen_txid,
            "tx_method": best_method,
            "tx_confidence": best_conf,
            "tx_time": (best_info or {}).get("block_ts") or "",
            "tx_from": (best_info or {}).get("from") or "",
            "tx_to": (best_info or {}).get("to") or "",
            "tx_amount": (best_info or {}).get("amount"),
            "tx_token": (best_info or {}).get("token") or "",
            "tx_contract_ret": (best_info or {}).get("contract_ret") or "",
            "subject_address": subject_address,
            "subject_raw": subject_raw,
            "subject_status": subject_status,
            "candidates": len(best_candidates),
            "amount": amount,
            "direction": direction,
            "source": {
                "last_modified": payload.get("_last_modified")
                or (rows[0].get("last_modified") if rows else "")
                or "",
                "seen_utc": payload.get("seen_utc") or "",
                "text_sample": text[:4000],
                "amount_value": payload.get("amount_value"),
                "amount_currency": payload.get("amount_currency") or "",
                "kind": payload.get("kind") or "",
            },
        },
    }


# ---------------------------------------------------------------------------
# Apply to DB
# ---------------------------------------------------------------------------


def verify_source_key(source_key: str, known: Optional[list[str]] = None) -> dict:
    """Resolve one source object and persist the result. Safe to call repeatedly."""
    bundle = db.chain_source_bundle(source_key)
    if not bundle:
        return {"source_key": source_key, "skipped": "missing"}
    if not api_ready():
        # Never write "unresolved" while Tronscan is failing; the next run
        # (or the worker) retries instead of permanently mislabelling a slip.
        return {"source_key": source_key, "skipped": "api_unavailable"}
    known = known if known is not None else db.chain_known_addresses()
    with _stats_lock:
        err_before = _error_seq
    result = resolve_source(
        source_key,
        bundle.get("payload") or {},
        bundle.get("rows") or [],
        known=known,
    )
    with _stats_lock:
        failed = _error_seq > err_before
    if failed:
        return {"source_key": source_key, "skipped": "api_error", "result": result}
    db.chain_apply_source_result(source_key, result)
    try:
        db.chain_queue_mark_checked(source_key)
    except Exception:
        pass
    return result


def _pending_wallet_keys(limit: int) -> list[str]:
    return db.chain_pending_wallet_keys(limit=limit)


# ---------------------------------------------------------------------------
# Worker
# ---------------------------------------------------------------------------

_stop = threading.Event()
_thread: Optional[threading.Thread] = None
_lock_fd: Optional[int] = None
_job_lock = threading.Lock()
_job: dict = {}
_last_object_scan = 0.0


def _acquire_leader_lock() -> bool:
    """One chain worker per deployment (2 uvicorn workers share SQLite)."""
    global _lock_fd
    if _lock_fd is not None:
        return True
    try:
        import fcntl

        path = config.DATA_DIR / "chain-worker.lock"
        fd = os.open(str(path), os.O_CREAT | os.O_RDWR, 0o644)
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        _lock_fd = fd
        return True
    except Exception:
        return False


def start_worker():
    global _thread
    if not ENABLED or not WORKER_ENABLED or _thread is not None:
        return
    if not _acquire_leader_lock():
        return
    _stop.clear()
    _thread = threading.Thread(target=_worker_loop, daemon=True, name="chain-verify")
    _thread.start()


def stop_worker():
    _stop.set()


def _worker_loop():
    global _last_object_scan
    # Let the live enrichment drain settle first; chain checks are low priority.
    if _stop.wait(15):
        return
    while not _stop.is_set():
        try:
            if not api_ready():
                _stop.wait(WORKER_SLEEP)
                continue
            # A manual/API backfill already owns the Tronscan budget; don't
            # duplicate its work from the background worker.
            with _job_lock:
                backfill_running = bool(_job.get("running"))
            if backfill_running:
                _stop.wait(WORKER_SLEEP)
                continue
            # Discover transaction screenshots that never became payment rows
            # or wallet hits. The rowid cursor makes this incremental.
            if time.time() - _last_object_scan > 21600:
                try:
                    db.chain_queue_crypto_objects(batch=5000, max_batches=10)
                except Exception as e:  # noqa: BLE001
                    print(f"[chain] object scan failed: {e}", flush=True)
                _last_object_scan = time.time()
            keys = db.chain_pending_sources(limit=WORKER_BATCH)
            for key in keys:
                if _stop.is_set():
                    break
                try:
                    verify_source_key(key)
                except Exception as e:  # noqa: BLE001 - never kill the worker
                    print(f"[chain] verify {key} failed: {e}", flush=True)
            # Object-level queue (no payment row / no wallet hit).
            for key in db.chain_pending_queue_keys(limit=WORKER_BATCH):
                if _stop.is_set():
                    break
                try:
                    verify_source_key(key)
                except Exception as e:  # noqa: BLE001
                    print(f"[chain] queue verify {key} failed: {e}", flush=True)
            # QR-only wallet hits (no payment row) still need canonical addresses.
            for key in db.chain_pending_wallet_keys(limit=WORKER_BATCH):
                if _stop.is_set():
                    break
                try:
                    verify_source_key(key)
                except Exception as e:  # noqa: BLE001
                    print(f"[chain] wallet verify {key} failed: {e}", flush=True)
        except Exception as e:  # noqa: BLE001
            print(f"[chain] worker error: {e}", flush=True)
        _stop.wait(WORKER_SLEEP)


# ---------------------------------------------------------------------------
# Backfill
# ---------------------------------------------------------------------------


def _run_backfill(
    since: str,
    until: str,
    limit: int,
    dry_run: bool,
    pending_only: bool = False,
    recheck: bool = False,
) -> dict:
    known = db.chain_known_addresses()
    queue_scan = {"queued": 0, "scanned": 0, "done": False}
    if not dry_run:
        # Safety net: discover Tronscan/hash objects that never became a
        # payment row or wallet hit (truncated wallet OCR, chat screenshots).
        queue_scan = db.chain_queue_crypto_objects(
            batch=5000,
            max_batches=int(os.environ.get("S3LM_CHAIN_OBJECT_BATCHES", "10") or 10),
        )
    if recheck:
        # Focused pass after a resolver/OCR fix or a transient Tronscan error:
        # retry only sources that never produced a transaction.
        keys = db.chain_recheck_sources(limit=limit)
    else:
        keys = db.chain_backfill_sources(
            since=since, until=until, limit=limit, pending_only=pending_only
        )
    queue_keys = db.chain_pending_queue_keys(limit=limit)
    if recheck:
        queue_keys = db.chain_recheck_queue_keys(limit=limit) + queue_keys
    wallet_keys = db.chain_pending_wallet_keys(limit=limit)
    seen: set[str] = set()
    results = []
    counts: dict[str, int] = {}
    todo = keys + queue_keys + wallet_keys
    api_streak = 0
    for idx, key in enumerate(todo, 1):
        if key in seen:
            continue
        seen.add(key)
        try:
            if dry_run:
                bundle = db.chain_source_bundle(key)
                if not bundle:
                    continue
                res = resolve_source(
                    key,
                    bundle.get("payload") or {},
                    bundle.get("rows") or [],
                    known=known,
                )
            else:
                res = {}
                for attempt in range(3):
                    if not api_ready():
                        # Wait out a 429 pause instead of skipping the
                        # backlog (live: 224/228 sources skipped in one pass).
                        deadline = time.time() + 90
                        while not api_ready() and time.time() < deadline:
                            time.sleep(2)
                    res = verify_source_key(key, known=known)
                    if str(res.get("skipped") or "") not in (
                        "api_unavailable",
                        "api_error",
                    ):
                        break
                    time.sleep(2 + attempt * 3)
            skip = str(res.get("skipped") or "")
            if skip:
                counts[skip] = counts.get(skip, 0) + 1
                results.append({"source_key": key, "skipped": skip})
                print(f"[chain] {idx}/{len(todo)} skipped:{skip} {key}", flush=True)
                # Only an API outage stops the pass; a queue entry whose
                # object disappeared is simply done.
                if skip in ("api_unavailable", "api_error"):
                    api_streak += 1
                else:
                    api_streak = 0
            else:
                api_streak = 0
                st = str((res.get("summary") or {}).get("tx_status") or "unknown")
                counts[st] = counts.get(st, 0) + 1
                results.append(
                    {
                        "source_key": key,
                        "tx_status": st,
                        "txid": (res.get("summary") or {}).get("txid") or "",
                    }
                )
                print(
                    f"[chain] {idx}/{len(todo)} {st} {key}",
                    flush=True,
                )
            if api_streak >= 5:
                counts["api_stopped"] = counts.get("api_stopped", 0) + 1
                print(
                    "[chain] Tronscan still unavailable after retries; "
                    "stopping this pass (rerun later)",
                    flush=True,
                )
                break
        except Exception as e:  # noqa: BLE001
            counts["error"] = counts.get("error", 0) + 1
            results.append({"source_key": key, "error": str(e)[:200]})
            print(f"[chain] {idx}/{len(todo)} error {key}: {e}", flush=True)
        with _job_lock:
            if _job.get("running"):
                _job["checked"] = len(seen)
                _job["counts"] = dict(counts)
                _job["current"] = key
    return {
        "ok": True,
        "dry_run": bool(dry_run),
        "pending_only": bool(pending_only),
        "recheck": bool(recheck),
        "since": since,
        "until": until,
        "limit": limit,
        "checked": len(seen),
        "counts": counts,
        "queue_scan": queue_scan,
        "results": results[:200],
        "checked_utc": _now(),
    }


def backfill(
    since: str = "",
    until: str = "",
    limit: int = 500,
    dry_run: bool = True,
    pending_only: bool = False,
    recheck: bool = False,
) -> dict:
    return _run_backfill(since, until, limit, dry_run, pending_only, recheck)


def start_backfill(
    since: str = "",
    until: str = "",
    limit: int = 500,
    dry_run: bool = True,
    pending_only: bool = False,
    recheck: bool = False,
) -> dict:
    global _job
    with _job_lock:
        if _job.get("running"):
            return {"started": False, "job": dict(_job)}
        _job = {
            "running": True,
            "started_utc": _now(),
            "since": since,
            "until": until,
            "limit": limit,
            "dry_run": bool(dry_run),
            "pending_only": bool(pending_only),
            "recheck": bool(recheck),
            "checked": 0,
            "counts": {},
        }

    def _run():
        global _job
        try:
            res = _run_backfill(since, until, limit, dry_run, pending_only, recheck)
            with _job_lock:
                _job.update(
                    {
                        "running": False,
                        "finished_utc": _now(),
                        "checked": res.get("checked", 0),
                        "counts": res.get("counts", {}),
                        "queue_scan": res.get("queue_scan") or {},
                        "error": "",
                    }
                )
        except Exception as e:  # noqa: BLE001
            with _job_lock:
                _job.update({"running": False, "finished_utc": _now(), "error": str(e)[:300]})

    threading.Thread(target=_run, daemon=True, name="chain-backfill").start()
    return {"started": True, "job": dict(_job)}


def refresh_multi_transfer_cache() -> dict:
    """Re-fetch transactions that carried a fee transfer plus the real move.

    GasFree Permit Transfer receipts put the fee first in
    ``trc20TransferInfo``; cache rows written before 2026-11-22 normalised
    that fee as the transaction (500 USDT showed as 1.5 USDT). Drop those
    entries and re-verify every source attached to them.
    """
    txids = db.chain_tx_cache_multi_transfer_txids()
    sources = db.chain_sources_for_txids(txids)
    cleared = db.chain_tx_cache_delete(txids)
    known = db.chain_known_addresses()
    counts: dict[str, int] = {}
    for key in sources:
        res = verify_source_key(key, known=known)
        st = str(
            (res.get("summary") or {}).get("tx_status")
            or res.get("skipped")
            or "unknown"
        )
        counts[st] = counts.get(st, 0) + 1
        print(f"[chain] refresh {key} -> {st}", flush=True)
    return {"cache_cleared": cleared, "sources": len(sources), "counts": counts}


def status() -> dict:
    counts = db.chain_status_counts()
    with _job_lock:
        job = dict(_job)
    with _stats_lock:
        fails = _fail_count
        last_error = _last_error
        last_ok = _last_ok_utc
        paused = time.time() < _pause_until
    return {
        "enabled": ENABLED,
        "api_base": TRONSCAN_BASE,
        "api_key": bool(TRONSCAN_API_KEY),
        "api_ok": fails < 10 and not paused,
        "api_ready": api_ready(),
        "api_last_ok_utc": last_ok,
        "api_last_error": last_error,
        "rate_per_sec": RATE_PER_SEC,
        "counts": counts,
        "job": job,
        "checked_utc": _now(),
    }
