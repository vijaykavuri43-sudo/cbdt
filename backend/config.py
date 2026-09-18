"""Config for S3 Live Monitor - CASE-2026-CBTF (STANDING-JUDICIAL-AUTH-ILLEGAL-BETTING).

READ-ONLY monitoring of publicly exposed bucket (VULN-004).
Never issue PUT/POST/DELETE to the bucket.
"""

import os
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
APP_DIR = Path(__file__).resolve().parents[1]

BUCKET_URL = os.environ.get(
    "S3LM_BUCKET_URL", "https://bx-s3-dev-001.s3.ap-southeast-1.amazonaws.com"
)
BUCKET_HOST = BUCKET_URL.split("//", 1)[1].split("/", 1)[0]

# The dashboard reads the preserved copy by default. Set S3LM_OBJECT_SOURCE=s3
# only for source-side diagnostics. GCS keys below s3-mirror/ are exposed to the
# rest of the app using their original S3 key names.
OBJECT_SOURCE = os.environ.get("S3LM_OBJECT_SOURCE", "gcs").strip().lower()
GCS_BUCKET = os.environ.get("S3LM_GCS_BUCKET", "cbtf-case-evidence-518405669914")
GCS_PREFIX = os.environ.get("S3LM_GCS_PREFIX", "s3-mirror/")
SOURCE_ID = f"gs://{GCS_BUCKET}/{GCS_PREFIX}" if OBJECT_SOURCE == "gcs" else BUCKET_URL

DATA_DIR = Path(
    os.environ.get("S3LM_DATA", Path.home() / ".local" / "share" / "s3-live-monitor")
)
DB_PATH = DATA_DIR / "manifest.db"

CASE_ID = os.environ.get("S3LM_CASE", "CASE-2026-CBTF")
AUTH_REF = os.environ.get("S3LM_AUTH", "STANDING-JUDICIAL-AUTH-ILLEGAL-BETTING")

INTELLIGENCE_ROOT = Path(
    os.environ.get("S3LM_INTELLIGENCE_ROOT", REPO_ROOT / "intelligence")
)
DOWNLOADS_ROOT = INTELLIGENCE_ROOT / "analysis" / CASE_ID / "live-monitor"
EVIDENCE_LOG = INTELLIGENCE_ROOT / "audit" / "collection-log.jsonl"

MASTER_UPI_LIST = (
    INTELLIGENCE_ROOT
    / "analysis"
    / CASE_ID
    / "mule-accounts"
    / "extracted"
    / "unique-upi-list-full.txt"
)


# Known operator/hawala wallets (LE submission master, CASE-2026-102).
# Optional: absence is harmless — known_operator badging simply degrades to off.
# Resolution order: explicit S3LM_MASTER_WALLET_CSV env, repo-bundled copy,
# then the production reference dir on the persistent volume (production
# git deploys intentionally ship no reference data — ship.sh/scp places the
# CSV at <data-dir>/reference/ so badging survives redeploys).
def _resolve_master_wallet_csv() -> Path:
    candidates = []
    env = os.environ.get("S3LM_MASTER_WALLET_CSV")
    if env:
        candidates.append(Path(env))
    candidates.append(REPO_ROOT / "LE-SUBMISSION-MASTER" / "17-CRYPTO-WALLETS.csv")
    candidates.append(APP_DIR / "reference" / "17-CRYPTO-WALLETS.csv")
    candidates.append(DATA_DIR / "reference" / "17-CRYPTO-WALLETS.csv")
    for c in candidates:
        try:
            if c.is_file():
                return c
        except OSError:
            continue
    return candidates[0]


MASTER_WALLET_CSV = _resolve_master_wallet_csv()

# chat_file images whose filename hints at crypto/wallet content get OCR'd for
# wallet addresses without dragging the whole ~700K chat backlog in.
CHAT_WALLET_HINT_TOKENS = (
    "trustapp",
    "trust_wallet",
    "usdt",
    "tron",
    "wallet",
    "tether",
    "binance",
    "crypto",
)

HOST = os.environ.get("S3LM_HOST", "127.0.0.1")
PORT = int(os.environ.get("S3LM_PORT", "8787"))
# Public base URL baked into officer CSV exhibits so every row's source_link
# opens the exact evidence object on the live dashboard for manual verification.
PUBLIC_URL = os.environ.get("S3LM_PUBLIC_URL", "http://8.231.108.145").rstrip("/")


def _load_or_create_token() -> str:
    """Gate token: env override > persisted auto-generated key (0600)."""
    env = os.environ.get("S3LM_TOKEN", "").strip()
    if env:
        return env
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    tf = DATA_DIR / "access-token.txt"
    if tf.exists():
        tok = tf.read_text(encoding="utf-8").strip()
        if tok:
            return tok
    import secrets

    tok = secrets.token_hex(8)
    tf.write_text(tok + "\n", encoding="utf-8")
    try:
        os.chmod(tf, 0o600)
    except OSError:
        pass
    return tok


API_TOKEN = _load_or_create_token()
TOKEN_FILE = DATA_DIR / "access-token.txt"

FULL_SWEEP_CONCURRENCY = int(os.environ.get("S3LM_SWEEP_WORKERS", "6"))
POLL_INTERVAL_SEC = float(os.environ.get("S3LM_POLL_SEC", "10"))
COLD_RECHECK_SEC = float(os.environ.get("S3LM_COLD_SEC", "1800"))
# Objects re-checked per prefix per cold cycle by the wrapping key-order crawl
# (collector.crawl_once). Full coverage of a 290K-key prefix at 2000/cycle takes
# ~36h; raise S3LM_CRAWL_CAP for faster catch-up at the cost of list requests.
CRAWL_CAP = int(os.environ.get("S3LM_CRAWL_CAP", "2000"))
# Hot prefixes dominate evidence; polling them in parallel cuts tail latency by ~4x
HOT_POLL_CONCURRENCY = int(os.environ.get("S3LM_HOT_POLL_WORKERS", "4"))
MAX_GET_RATE_PER_SEC = 5
# Only auto-OCR images uploaded within this window. Prevents a permanent
# multi-week tesseract backlog (951K+ historical images) from starving the API.
# 0 disables the cutoff (old behaviour — full-bucket wallet hunt uses 0).
ENRICH_MAX_AGE_HOURS = float(os.environ.get("S3LM_ENRICH_MAX_AGE_HOURS", "0"))
_DEFAULT_WORKERS = max(2, min(6, (os.cpu_count() or 4)))
ENRICH_WORKERS = max(
    1, int(os.environ.get("S3LM_ENRICH_WORKERS", str(_DEFAULT_WORKERS)))
)
ENRICH_BATCH = max(ENRICH_WORKERS, int(os.environ.get("S3LM_ENRICH_BATCH", "48")))
# Full-bucket wallet scan: when set, the pending queue also includes every chat_file image
# (not just hint-named). Keep live default False for latency; POST /api/wallets/full-scan enables it.
WALLET_FULL_SCAN = os.environ.get("S3LM_WALLET_FULL_SCAN", "0").strip() == "1"
# Gambler-paid slip OCR (deposit/withdrawal/payment images): dedicated drain
# loop surfacing receiving-rail VPAs/UTRs the QR-tier queue never sees.
# Default OFF — 500k+ slip backlog would compete with live QR decoding;
# enable on the VM once the QR backlog drains (S3LM_SLIP_OCR=1).
SLIP_OCR = os.environ.get("S3LM_SLIP_OCR", "0").strip() == "1"
SLIP_BATCH = max(1, int(os.environ.get("S3LM_SLIP_BATCH", "24")))
SLIP_INTERVAL_SEC = max(5.0, float(os.environ.get("S3LM_SLIP_INTERVAL_SEC", "30")))

# Single-slip / single-QR amount plausibility bound (₹1 crore). UPI/IMPS and even
# large crypto deposits never land remotely near this on one object; OCR/QR
# mis-parses DO (12-digit UTR / reference numbers promoted to "amount"). Every
# money aggregate — Overview stats, Money Flows, materialized totals — applies
# this same bound so the dashboard never sums garbage.
MAX_AMOUNT_INR = float(os.environ.get("S3LM_MAX_AMOUNT_INR", "10000000"))

GEMINI_API_KEY = (
    os.environ.get("GEMINI_API_KEY", "").strip()
    or os.environ.get("GOOGLE_API_KEY", "").strip()
    or os.environ.get("S3LM_GEMINI_API_KEY", "").strip()
    or os.environ.get("S3LM_GOOGLE_API_KEY", "").strip()
)
GEMINI_MODEL = (
    os.environ.get("S3LM_GEMINI_MODEL", "gemini-3.8-flash").strip()
    or "gemini-3.8-flash"
)
# Verified 2026-09-03 with the case key (live generateContent probes):
# 3.8/3.7/3.6-flash all 200 clean-JSON (3.8 newest = primary); 3.5-flash 200
# but prose (rejected); 2.5-flash 429 quota-burned (rejected); 3.5-flash-lite
# timed out (rejected); 2.5-flash-lite 200 exact-JSON (fallback). Chain tries
# ONLY these two — no dead models.
GEMINI_FALLBACK_MODEL = (
    os.environ.get("S3LM_GEMINI_FALLBACK_MODEL", "gemini-2.5-flash-lite").strip()
    or "gemini-2.5-flash-lite"
)
GEMINI_ENABLED = bool(GEMINI_API_KEY)
# Multi-key rotation: comma-separated keys in S3LM_GEMINI_API_KEYS, each with
# its own free-tier daily budget (verified 2026-09-03: 20 req/day/model/key).
GEMINI_API_KEYS = [
    k.strip()
    for k in os.environ.get("S3LM_GEMINI_API_KEYS", "").split(",")
    if k.strip()
] or ([GEMINI_API_KEY] if GEMINI_API_KEY else [])
# Reserve the last N calls of the effective daily budget for manual officer
# triggers (Re-run buttons); the background batch must not burn them.
GEMINI_RESERVE = max(0, int(os.environ.get("S3LM_GEMINI_RESERVE", "5")))
# Per-minute pacing for the background batch (free-tier minute quotas are
# tiny; bursts 429 even when daily budget remains).
GEMINI_PER_MIN = max(1, int(os.environ.get("S3LM_GEMINI_PER_MIN", "8")))
# Max Gemini Vision calls per UTC day across all workers (parent-enforced).
# Only tesseract-miss slips reach Vision; shrunk 768px images run ~700 tokens
# ≈ $0.0002/call on 2.5-flash, so 100K/day caps worst-case spend ≈ $20/day.
GEMINI_DAILY_CAP = max(0, int(os.environ.get("S3LM_GEMINI_DAILY_CAP", "100000")))
# Max Gemini verifications handed to the background thread per drain batch.
GEMINI_PER_BATCH = max(1, int(os.environ.get("S3LM_GEMINI_PER_BATCH", "48")))

# Groq Cloud: text failover for the AI assistant AND vision OCR for slips.
# Vision (Groq docs 2026-09): qwen/qwen3.6-27b (JSON mode, 5 images) and
# qwen/qwen3.8-27b (already the chat primary; 3 images). Llama 4 Scout retired.
GROQ_API_KEY = (
    os.environ.get("S3LM_GROQ_API_KEY", "").strip()
    or os.environ.get("GROQ_API_KEY", "").strip()
)
GROQ_MODEL = (
    os.environ.get("S3LM_GROQ_MODEL", "qwen/qwen3.8-27b").strip() or "qwen/qwen3.8-27b"
)
GROQ_FALLBACK_MODEL = (
    os.environ.get("S3LM_GROQ_FALLBACK_MODEL", "openai/gpt-oss-120b").strip()
    or "openai/gpt-oss-120b"
)
GROQ_VISION_MODEL = (
    os.environ.get("S3LM_GROQ_VISION_MODEL", "qwen/qwen3.6-27b").strip()
    or "qwen/qwen3.6-27b"
)
GROQ_VISION_FALLBACK = (
    os.environ.get("S3LM_GROQ_VISION_FALLBACK", "qwen/qwen3.8-27b").strip()
    or "qwen/qwen3.8-27b"
)
GROQ_ENABLED = bool(GROQ_API_KEY)

# Optional pre-joined transaction corpus. These paths are searched in order;
# absence is harmless and live OCR/QR extraction continues normally.
REFERENCE_ROOT = Path(os.environ.get("S3LM_REFERENCE_ROOT", INTELLIGENCE_ROOT))
REFERENCE_UTR_GAMBLER = (
    REFERENCE_ROOT
    / "exports"
    / "CASE-2026-CBTF-5DAY-2026-08-14-to-2026-08-19"
    / "data"
    / "analysis.utr-gambler.tsv"
)
REFERENCE_UTR_VPA = (
    REFERENCE_ROOT
    / "exports"
    / "CASE-2026-CBTF-5DAY-2026-08-14-to-2026-08-19"
    / "data"
    / "analysis.utr-vpa-join.tsv"
)

HOT_PREFIXES = [
    "deposit/",
    "withdrawal/",
    "bankingqr/",
    "qrcode/",
    "payment/",
    "chat_file/",
    "csv-files/",
    "uploads/",
]

QR_PREFIXES = ("bankingqr/", "qrcode/", "payment/", "paymentgateway/")
OCR_PREFIXES = ("deposit/", "withdrawal/")
IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".webp", ".gif", ".bmp"}
AUDIO_EXTS = {
    ".wav",
    ".mp3",
    ".ogg",
    ".amr",
    ".m4a",
    ".opus",
    ".webm",
    ".3gp",
    ".flac",
    ".oga",
}
TEXT_EXTS = {".txt", ".json", ".csv", ".xml", ".log", ".md", ".html", ".js", ".css"}

DEFAULT_PREFIXES = [
    "Logo/",
    "all-casino/",
    "apk/",
    "audio/",
    "aura/",
    "ball-video/",
    "ball_by_ball/",
    "bankingqr/",
    "banner/",
    "casino-images/",
    "chat_file/",
    "coming-soon/",
    "csv-files/",
    "currency/",
    "deposit/",
    "icons/",
    "level/",
    "notification/",
    "payment/",
    "paymentgateway/",
    "qrcode/",
    "score-board/",
    "st8-gaming/",
    "theme-logo/",
    "undefined/",
    "uploads/",
    "virtual_games/",
    "withdrawal/",
]

ensure_dirs = [DATA_DIR, DOWNLOADS_ROOT]
