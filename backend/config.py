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
GCS_BUCKET = os.environ.get("S3LM_GCS_BUCKET", "cbtf-case-evidence-991096649643")
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
# Known operator/hawala wallets (LE submission master, CASE-2026-102)
MASTER_WALLET_CSV = REPO_ROOT / "LE-SUBMISSION-MASTER" / "17-CRYPTO-WALLETS.csv"

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
COLD_RECHECK_SEC = float(os.environ.get("S3LM_COLD_SEC", "900"))
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
ENRICH_WORKERS = max(1, int(os.environ.get("S3LM_ENRICH_WORKERS", "6")))
ENRICH_BATCH = max(ENRICH_WORKERS, int(os.environ.get("S3LM_ENRICH_BATCH", "24")))
# Full-bucket wallet scan: when set, the pending queue also includes every chat_file image
# (not just hint-named). Keep live default False for latency; POST /api/wallets/full-scan enables it.
WALLET_FULL_SCAN = os.environ.get("S3LM_WALLET_FULL_SCAN", "0").strip() == "1"

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
    os.environ.get("S3LM_GEMINI_MODEL", "gemini-1.5-flash").strip()
    or "gemini-1.5-flash"
)
GEMINI_ENABLED = bool(GEMINI_API_KEY)

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
AUDIO_EXTS = {".wav", ".mp3", ".ogg", ".amr"}
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
