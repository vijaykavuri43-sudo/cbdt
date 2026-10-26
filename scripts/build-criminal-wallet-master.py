#!/usr/bin/env python3
"""Build the criminal-side wallet master DOCX/XLSX/CSV.

Inputs are the reconciled 21-09-2026 document audit, the 92-row census,
the 28-row quarantine, and the 2026-10-26 live-only candidate list.  The
output deliberately separates:

  * asserted criminal-side wallets (operator + hawala/collection),
  * criminal-side candidates (QR/platform/support),
  * bettor/user-side wallets (excluded from the criminal master),
  * exchange infrastructure (context only),
  * unresolved evidence-only / ETH wallets,
  * invalid/non-wallet/quarantine strings,
  * valid live-only addresses that are not yet curated.

No address is added to the five-row operator master.
"""

from __future__ import annotations

import argparse
import csv
import json
import re
from collections import Counter, OrderedDict
from pathlib import Path
from typing import Any

from docx import Document
from docx.enum.section import WD_ORIENT
from docx.enum.table import WD_TABLE_ALIGNMENT
from docx.enum.text import WD_ALIGN_PARAGRAPH
from docx.oxml import OxmlElement
from docx.oxml.ns import qn
from docx.shared import Inches, Pt, RGBColor
from openpyxl import Workbook
from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
from openpyxl.utils import get_column_letter


ROOT = Path(__file__).resolve().parents[1]
REF = ROOT / "reference"

DOCX_DEFAULT = Path(
    "/mnt/c/Users/prism/Downloads/CBTF_Crypto_Wallets_Updated-21-09-26.docx"
)
OUT_DOCX = REF / "CBTF_Crypto_Wallets_Criminal_Side_Master_2026-10-26.docx"
OUT_XLSX = REF / "CBTF_Crypto_Wallets_Criminal_Side_Master_2026-10-26.xlsx"
OUT_MASTER_CSV = REF / "WALLET-MASTER-2026-10-26.csv"
OUT_CRIMINAL_CSV = REF / "CRIMINAL-SIDE-WALLETS-2026-10-26.csv"

# ---------------------------------------------------------------------------
# Inputs
# ---------------------------------------------------------------------------


def read_csv(path: Path) -> list[dict[str, str]]:
    with path.open(encoding="utf-8", newline="") as fh:
        return list(csv.DictReader(fh))


def load_docx_tables(path: Path) -> dict[str, list[str]]:
    """Return a mapping like T3R2 -> cells of that table row."""
    if not path.exists():
        return {}
    try:
        doc = Document(path)
    except Exception:
        return {}
    out: dict[str, list[str]] = {}
    for ti, table in enumerate(doc.tables, start=1):
        for ri, row in enumerate(table.rows):
            out[f"T{ti}R{ri}"] = [cell.text.strip().replace("\n", " / ") for cell in row.cells]
    return out


def load_live_kinds() -> dict[str, str]:
    path = Path("/tmp/opencode/wallet_hit_rows_20261026.json")
    if not path.exists():
        return {}
    try:
        rows = json.loads(path.read_text())
    except Exception:
        return {}
    kinds: dict[str, set[str]] = {}
    for row in rows:
        kinds.setdefault(row.get("address", ""), set()).add(row.get("kind", ""))
    return {a: ",".join(sorted(k for k in ks if k)) for a, ks in kinds.items()}


# ---------------------------------------------------------------------------
# Notes / presentation helpers
# ---------------------------------------------------------------------------

DETAIL_NOTES: dict[str, str] = {
    # Operator wallets
    "TVHL9Wfc2rM7MaqDq6TL5E4yQ6biSP6gMU": (
        "Binance withdrawal destination across arunbaba / benylo929 / ramji001 / "
        "ravi001 / nitrogen123 (163.04 USDT); QR-confirmed known operator; 54 live hits."
    ),
    "TQbXYxYw3NtKLaHbKEWtu1xMfPbdD3A5Ef": (
        "CR7 bidirectional operator hot wallet: 10 withdrawal sends 8,817.78 USDT + "
        "2 deposit receives 3,715.69 USDT; Trust Wallet corroboration; 40 live hits; "
        "chat keys deleted, proof preserved."
    ),
    "TUeZqJPJX6tMUwyiaJNWe2TAYmAoH46tW2": (
        "LOCKER operator hot send wallet: 1 send 490.19 USDT; 26 live hits; proof preserved."
    ),
    "TD63G1qvWu7ke2ycW848BjMCj2sHEZZusa": (
        "Binance TRX-network withdraw destination; visual-verified; operator deposit receive."
    ),
    "TJ5yq6udNUQ5iQNaapbN5f5YMUvRJKP17B": (
        "KuCoin TRC20 withdraw destination; visual-verified; operator deposit receive."
    ),
    "TAVawJ7BF7L9XwSf5RJdtr1gHSyVTzzCxf": (
        "Canonical B6 operator deposit receive; to-side of 10 USDT usdtplayer deposit; "
        "Tronscan in shot. B6 is operator-side but is NOT in the 5-row operator master."
    ),
    # Hawala double-role rows
    "TAhW4QKFYFBZX2qQ8yuZCoVNC4qrVUNJpQ": (
        "Secondary collection wallet; also C1: CR7 → TAhW 718 USDT on 2026-06-04, "
        "proof withdrawal/1780531600385.jpg."
    ),
    "TFpP4TqspHYa1Xzd3L1qkmhyfd6gzEsoQr": (
        "Crypto-to-fiat bridge; also C3: CR7 → TFpP 1,061.15 USDT on 2026-06-04, "
        "proof withdrawal/1780533116563.jpg."
    ),
    "TQ9LitUK7iBhayHruHBN7VT9sjtpfarH6v": (
        "Andhra collection; also C11: CR7 → TQ9Lit 798.03 USDT on 2026-06-13, "
        "proof withdrawal/1781365547577.jpg."
    ),
    "TEeqmfN74hg8vP8umv8ubD3oGAroVwRTFD": (
        "Telangana/Andhra collection; canonical target of invalid C12 variant "
        "TEeqmfN74hg8vP8umv8ubD30GAroVWRTFD (quarantined)."
    ),
    # Desk/support
    "TXPHc8arZJwhhas4ubvmegTmdW8KLYSAC9": (
        "Received 530.25 USDT in support chat; no named attribution. Candidate support/"
        "collection wallet, not asserted core."
    ),
    # User withdrawals
    "TYRTwpCZXwRVDopT92ZFrMSXRNEfJMhFb7": "CR7 payout 850 USDT on 2026-06-04; bettor/user withdrawal beneficiary.",
    "TJF8N5Ab8ByHzSmUZ3rjeh7DMyecP3YncR": "LOCKER payout 490.19 USDT on 2026-06-05; bettor/user withdrawal beneficiary.",
    "THNDsUs8qDSj2rPJSuEDCWBaxKZ97ppZvT": "CR7 payout 1,001 USDT on 2026-06-05; bettor/user withdrawal beneficiary.",
    "TL4et7AXPaHtGUxjXYbiDLHQt7kzVhDbEs": "CR7 payout 980.39 USDT on 2026-06-05; bettor/user withdrawal beneficiary.",
    "THKa488cYP3dwiNik59UW8N7nruVFtnhia": "CR7 payout 196 USDT on 2026-06-06; bettor/user withdrawal beneficiary.",
    "TQ1wQtt3qRHjaLTWUcjaZZdygGQv1UpGKH": "CR7 payout 343.13 USDT on 2026-06-07; bettor/user withdrawal beneficiary.",
    "TCmUiVhPfpv4n29sVLU4B74outgR6p3p9e": "CR7 payout 1,960.78 USDT on 2026-06-10; largest single; bettor/user withdrawal beneficiary.",
    "TJNg1dmSZir8aDWM1PRrTtw5PenMoFb9ss": "CR7 payout 909.3 USDT on 2026-06-10; bettor/user withdrawal beneficiary.",
    # User wallets / dust
    "TZ8Ksz21Hk1tQuztCKCUJBRXStCav9uyjM": "User deposit sender; sent 10 USDT into B6; dashboard rail-confirmed; bettor/user wallet.",
    "TVXfReZqGEAj4mjZ3G47VQCtET4T7fQm9L": "User Trust Wallet receive screen; dashboard rail-confirmed (gamblers=1); bettor/user wallet.",
    "TLbhuZ12zFwqKNDErNwwewBRvvUAFk1H4j": "0.000002 TRX dust From; possible address poisoning; NOT a betting rail.",
    "TP5Rj2hz6BQhHTDigsxMZbs1b1ngpEC8xc": "Dust To paired with X1; possible address poisoning; NOT a betting rail.",
    # Exchange
    "TJqwA7SoZnERE4zW5uDEiPkbz4B66h9TFj": "Binance-Hot 11 (Tronscan-labeled); 390 USDT via Binance hot; source deleted, OCR preserved; exchange infrastructure, not operator.",
    "TCLgK89AnXbC9rewvhNb9UgXCc2qJJpBXh": "Binance Withdraw 10 public explorer label; exchange withdrawal wallet; NOT an operator attribution.",
    # Evidence-only
    "THM4uT2VxzSsBEp8WRbYMzRkiqvbEuBBLU": "Valid TRON deposit evidence; attribution and case role need transaction-level corroboration; side unresolved.",
    "TFkz4usfWmLHu78zENZU63Znqnmh267B25": "Valid TRON withdrawal evidence; attribution and case role need transaction-level corroboration; side unresolved.",
    "TFv1isf5EXpXnu2NPpGSwTgipckNvsmEvz": "Valid TRON deposit evidence; 5 live hits, latest 2026-10-24; attribution unresolved.",
    "TCgcGDFVmeqgxXvsUSZAUMkdLcfovc2aVm": "Valid TRON withdrawal evidence; attribution and case role need transaction-level corroboration; side unresolved.",
    # ETH
    "0x1c481952f4510d2dadeef486e9e3ffd7af53b682": "ETH likely operator-or-user receive; rajvir47 Binance chats 2026-01-24; medium; side unresolved.",
    "0x4d5089151dbd317b3b3dcbb5d4872f280017f559": "ETH likely operator-or-user receive; rajvir47 Binance chats 2026-01-24; 1 live hit; medium; side unresolved.",
    # Invalid census
    "TJPVHGb3FUPd3754hg8h9vjHoUgZwZCzBb": "Support-chat TRC20 transfer proof; checksum-invalid; marked fact/high in census but cannot exist on-chain. Correct to OCR lead.",
    "TJnxipzrpf9NKTTkyWo2YXBJ6HF7otHjar": "Support-chat 'received 60 USDT'; checksum-invalid; marked fact/high in census but cannot exist on-chain. Correct to OCR lead.",
    "TCgLo2vsT4BNxXB5ZNKvW9vev9t54Tv4kt": "Dashboard OCR deposit rail; checksum-invalid; 39 live hits; re-OCR required; not a wallet.",
    "TX1HkKxnsHNPZHdhUETZXJp6NBozdVN9kq": "Dashboard OCR deposit rail; checksum-invalid; 15 live hits; valid case-variant TX1HkKxnsHNPzHdhUETZXJp6NBozdVN9kq exists but uses different withdrawal evidence; do not merge without image re-read.",
    "TGhiEsAtLfH2cawDa7Ru7LWXnG7tWjbWys": "Live-dashboard deposit rail; checksum-invalid; 18 live hits; valid case-variant TGhiEsAtLfH2cawDa7Ru7LWXnG7tWjbWyS appears in the same deposit evidence; re-OCR candidate, not a promotion.",
    "TXGFq9BxneuNGUSd4uEgEg6KJ8rJGFaGoD": "Live-dashboard withdrawal beneficiary; checksum-invalid; 1 live hit; re-OCR required; not a wallet.",
    "TFaoUrWJWmfNJVVeDYTFK423i858yQy3mg": "Live-dashboard deposit rail; checksum-invalid; 2 live hits; re-OCR required; not a wallet.",
    "TFZANTUBkFqYuka4d931vpoa7T5gYu4rVF": "Live-dashboard deposit rail; checksum-invalid; 4 live hits; re-OCR required; not a wallet.",
    "TDqSquXBgUCLYVYC4XZerprLK589dkhSCf": "Live-dashboard deposit-record beneficiary; checksum-invalid; 1 live hit; re-OCR required; not a wallet.",
    # Invalid variants
    "TAVawJ7BF7L9XwSf5RJdtrigHSyVTzzCxf": "Base58-checksum-invalid OCR near-duplicate of canonical B6 TAVawJ7BF7L9XwSf5RJdtr1gHSyVTzzCxf (…dtrig… vs …dtr1g…); not a separate wallet.",
    "TEeqmfN74hg8vP8umv8ubD30GAroVWRTFD": "Base58-invalid (digit 0) OCR variant of confirmed TEeqmfN74hg8vP8umv8ubD3oGAroVwRTFD; not a separate wallet.",
    "TTmepj9RqbfuzTRkuLuQ9vAX4..iEKKeXL": "Partial/truncated To address in image; resolve via tx 4e1ca1ebe997f433.. on Tronscan; not a usable wallet.",
    # Live-only re-OCR candidates
    "TGhiEsAtLfH2cawDa7Ru7LWXnG7tWjbWyS": "Valid live-only case-variant of checksum-invalid census TGhiEsAtLfH2cawDa7Ru7LWXnG7tWjbWys; same deposit/1787289222499.png; re-OCR candidate, not curated.",
    "TX1HkKxnsHNPzHdhUETZXJp6NBozdVN9kq": "Valid live-only case-variant of checksum-invalid census TX1HkKxnsHNPZHdhUETZXJp6NBozdVN9kq; different withdrawal evidence; re-OCR candidate, not curated.",
}

ROLE_OVERRIDES: dict[str, str] = {
    "TQbXYxYw3NtKLaHbKEWtu1xMfPbdD3A5Ef": "OPERATOR_HOT_BIDIRECTIONAL (CR7)",
    "TUeZqJPJX6tMUwyiaJNWe2TAYmAoH46tW2": "OPERATOR_HOT_SEND (LOCKER)",
    "TVHL9Wfc2rM7MaqDq6TL5E4yQ6biSP6gMU": "OPERATOR_DEPOSIT_RECEIVE_PRIMARY",
    "TD63G1qvWu7ke2ycW848BjMCj2sHEZZusa": "OPERATOR_DEPOSIT_RECEIVE",
    "TJ5yq6udNUQ5iQNaapbN5f5YMUvRJKP17B": "OPERATOR_DEPOSIT_RECEIVE",
    "TAVawJ7BF7L9XwSf5RJdtr1gHSyVTzzCxf": "OPERATOR_DEPOSIT_RECEIVE (B6, census-only)",
    "TXPHc8arZJwhhas4ubvmegTmdW8KLYSAC9": "DESK_SUPPORT_RECEIVED_530_USDT",
    "TJqwA7SoZnERE4zW5uDEiPkbz4B66h9TFj": "BINANCE_HOT_11 (exchange)",
    "TCLgK89AnXbC9rewvhNb9UgXCc2qJJpBXh": "BINANCE_WITHDRAW_10 (exchange)",
}


def short(value: str, limit: int = 240) -> str:
    value = re.sub(r"\s+", " ", (value or "").strip())
    return value if len(value) <= limit else value[: limit - 1].rstrip() + "…"


def docx_context_for(refs: str, docx_rows: dict[str, list[str]]) -> str:
    if not refs:
        return ""
    first = refs.split(";")[0].strip()
    cells = docx_rows.get(first)
    if not cells:
        return ""
    m = re.match(r"T(\d+)R", first)
    if not m:
        return ""
    ti = int(m.group(1)) - 1
    if ti == 0 and len(cells) >= 4:
        return f"Reported person/entity: {cells[2]}; reported role: {cells[3]}"
    if ti == 1 and len(cells) >= 3:
        return f"Report attribution: {cells[2]}"
    if ti == 2 and len(cells) >= 5:
        return f"{cells[2]}; direction: {cells[3]}; evidence: {cells[4]}"
    if ti == 3 and len(cells) >= 6:
        return f"From: {cells[2]}; amount: {cells[3]} USDT; date: {cells[4]}; proof: {cells[5]}"
    if ti == 4 and len(cells) >= 4:
        return f"Context: {cells[2]}; S3: {cells[3]}"
    if ti == 5 and len(cells) >= 4:
        return f"Type: {cells[2]}; evidence: {cells[3]}"
    if ti == 6 and len(cells) >= 4:
        return f"Party/label: {cells[2]}; evidence: {cells[3]}"
    if ti == 7 and len(cells) >= 5:
        return f"Direction: {cells[2]}; dashboard: {cells[3]}; note: {cells[4]}"
    if ti == 8 and len(cells) >= 6:
        return f"Dashboard hits: {cells[2]}; first: {cells[3]}; last: {cells[4]}; note: {cells[5]}"
    if ti == 9 and len(cells) >= 7:
        return f"Dashboard hits: {cells[2]}; kind: {cells[3]}; first: {cells[4]}; last: {cells[5]}; S3: {cells[6]}"
    if ti == 10 and len(cells) >= 5:
        return f"Context: {cells[2]}; source: {cells[3]}; verification: {cells[4]}"
    return " | ".join(cells[1:])


# ---------------------------------------------------------------------------
# Classification
# ---------------------------------------------------------------------------

SECTION_ORDER = [
    "3.1 Operator wallets",
    "3.2 Hawala / collection",
    "4.1 Banking QR rails",
    "4.2 Dashboard / platform rails",
    "4.3 Desk / support",
    "5.1 Bettor/user withdrawal beneficiaries",
    "5.2 Bettor/user wallets",
    "5.3 Dust / poisoning",
    "6. Exchange / infrastructure",
    "7.1 Evidence-only TRON",
    "7.2 ETH unresolved",
    "8.1 Invalid census OCR leads",
    "8.2 Invalid platform-hot claims",
    "8.3 OCR variants",
    "8.4 Non-crypto UPI identifiers",
    "8.5 Partial / truncated",
    "9. Live-only unverified",
]

SECTION_PREFIX = {
    "3.1 Operator wallets": "CR-OP",
    "3.2 Hawala / collection": "CR-HW",
    "4.1 Banking QR rails": "CC-QR",
    "4.2 Dashboard / platform rails": "CC-PL",
    "4.3 Desk / support": "CC-SP",
    "5.1 Bettor/user withdrawal beneficiaries": "EX-UW",
    "5.2 Bettor/user wallets": "EX-US",
    "5.3 Dust / poisoning": "EX-DU",
    "6. Exchange / infrastructure": "CX",
    "7.1 Evidence-only TRON": "UR-EV",
    "7.2 ETH unresolved": "UR-ETH",
    "8.1 Invalid census OCR leads": "IN-OCR",
    "8.2 Invalid platform-hot claims": "IN-PH",
    "8.3 OCR variants": "IN-VAR",
    "8.4 Non-crypto UPI identifiers": "IN-UPI",
    "8.5 Partial / truncated": "IN-PT",
    "9. Live-only unverified": "LO",
}

SIDE_CLASS = {
    "3.1 Operator wallets": "criminal_operator",
    "3.2 Hawala / collection": "criminal_hawala_collection",
    "4.1 Banking QR rails": "criminal_qr_candidate",
    "4.2 Dashboard / platform rails": "criminal_platform_candidate",
    "4.3 Desk / support": "criminal_support_candidate",
    "5.1 Bettor/user withdrawal beneficiaries": "bettor_user_excluded",
    "5.2 Bettor/user wallets": "bettor_user_excluded",
    "5.3 Dust / poisoning": "bettor_user_excluded",
    "6. Exchange / infrastructure": "exchange_context",
    "7.1 Evidence-only TRON": "unresolved_evidence",
    "7.2 ETH unresolved": "unresolved_eth",
    "8.1 Invalid census OCR leads": "invalid_nonwallet",
    "8.2 Invalid platform-hot claims": "invalid_nonwallet",
    "8.3 OCR variants": "invalid_nonwallet",
    "8.4 Non-crypto UPI identifiers": "noncrypto_payment",
    "8.5 Partial / truncated": "invalid_nonwallet",
    "9. Live-only unverified": "live_only_unverified",
}

CRIMINAL_SIDE_LABEL = {
    "criminal_operator": "Yes — asserted operator",
    "criminal_hawala_collection": "Yes — asserted collection/hawala",
    "criminal_qr_candidate": "Yes — candidate (QR collection)",
    "criminal_platform_candidate": "Yes — candidate (platform rail)",
    "criminal_support_candidate": "Yes — candidate (support/collection)",
    "bettor_user_excluded": "No — bettor/user side",
    "exchange_context": "No — exchange infrastructure",
    "unresolved_evidence": "Uncertain — evidence only",
    "unresolved_eth": "Uncertain — ETH, side unresolved",
    "invalid_nonwallet": "No — invalid/non-wallet",
    "noncrypto_payment": "Criminal-side identifier — not a crypto wallet",
    "live_only_unverified": "Unverified — live only",
}

STATUS_LABEL = {
    "3.1 Operator wallets": "CORE — operator",
    "3.2 Hawala / collection": "CORE — hawala/collection",
    "4.1 Banking QR rails": "CANDIDATE — QR",
    "4.2 Dashboard / platform rails": "CANDIDATE — platform",
    "4.3 Desk / support": "CANDIDATE — support",
    "5.1 Bettor/user withdrawal beneficiaries": "EXCLUDED — bettor/user",
    "5.2 Bettor/user wallets": "EXCLUDED — bettor/user",
    "5.3 Dust / poisoning": "EXCLUDED — dust",
    "6. Exchange / infrastructure": "CONTEXT — exchange",
    "7.1 Evidence-only TRON": "UNRESOLVED — evidence",
    "7.2 ETH unresolved": "UNRESOLVED — ETH",
    "8.1 Invalid census OCR leads": "INVALID — OCR lead",
    "8.2 Invalid platform-hot claims": "INVALID — platform claim",
    "8.3 OCR variants": "INVALID — OCR variant",
    "8.4 Non-crypto UPI identifiers": "NON-CRYPTO — UPI",
    "8.5 Partial / truncated": "INVALID — partial",
    "9. Live-only unverified": "UNVERIFIED — live only",
}

PRIORITY = {
    "3.1 Operator wallets": "P1 — core",
    "3.2 Hawala / collection": "P1 — core",
    "4.1 Banking QR rails": "P2 — candidate",
    "4.2 Dashboard / platform rails": "P2 — candidate",
    "4.3 Desk / support": "P2 — candidate",
    "5.1 Bettor/user withdrawal beneficiaries": "P4 — excluded",
    "5.2 Bettor/user wallets": "P4 — excluded",
    "5.3 Dust / poisoning": "P4 — excluded",
    "6. Exchange / infrastructure": "P3 — context",
    "7.1 Evidence-only TRON": "P3 — unresolved",
    "7.2 ETH unresolved": "P3 — unresolved",
    "8.1 Invalid census OCR leads": "P1 — re-OCR",
    "8.2 Invalid platform-hot claims": "P2 — re-OCR",
    "8.3 OCR variants": "P1 — re-OCR",
    "8.4 Non-crypto UPI identifiers": "P3 — non-crypto",
    "8.5 Partial / truncated": "P3 — resolve tx",
    "9. Live-only unverified": "P3 — unverified",
}


def classify_docx_row(row: dict[str, str], audit: dict[str, str], master: set[str]) -> tuple[str, str, str]:
    """Return (section, criminal_side, status_note)."""
    cat = row["category"]
    address = row["address"]
    tron = audit.get("tron_check", "")
    invalid = tron in {"checksum-invalid"} or tron.startswith("base58-invalid") or tron.startswith("bad-length")
    if cat == "operator_hot":
        return "3.1 Operator wallets", CRIMINAL_SIDE_LABEL["criminal_operator"], (
            "In 5-row operator master" if address in master else "Census-only; NOT in 5-row operator master"
        )
    if cat == "hawala_collection":
        return "3.2 Hawala / collection", CRIMINAL_SIDE_LABEL["criminal_hawala_collection"], "Valid TRON; source-report fact"
    if cat == "bankingqr_qr":
        return "4.1 Banking QR rails", CRIMINAL_SIDE_LABEL["criminal_qr_candidate"], "Valid TRON; QR-derived candidate"
    if cat == "dashboard_ocr":
        if invalid:
            return "8.1 Invalid census OCR leads", CRIMINAL_SIDE_LABEL["invalid_nonwallet"], "Checksum-invalid OCR lead; cannot exist on-chain"
        return "4.2 Dashboard / platform rails", CRIMINAL_SIDE_LABEL["criminal_platform_candidate"], "Valid TRON; dashboard OCR candidate"
    if cat == "live_dashboard":
        if invalid:
            return "8.1 Invalid census OCR leads", CRIMINAL_SIDE_LABEL["invalid_nonwallet"], "Checksum-invalid OCR lead; cannot exist on-chain"
        return "4.2 Dashboard / platform rails", CRIMINAL_SIDE_LABEL["criminal_platform_candidate"], "Valid TRON; live-dashboard candidate"
    if cat == "desk_support":
        if invalid:
            return "8.1 Invalid census OCR leads", CRIMINAL_SIDE_LABEL["invalid_nonwallet"], "Checksum-invalid support lead; fact label is wrong"
        return "4.3 Desk / support", CRIMINAL_SIDE_LABEL["criminal_support_candidate"], "Valid TRON; support/collection candidate"
    if cat == "user_withdrawal":
        return "5.1 Bettor/user withdrawal beneficiaries", CRIMINAL_SIDE_LABEL["bettor_user_excluded"], "Valid TRON; payout beneficiary — excluded"
    if cat == "user_wallet":
        return "5.2 Bettor/user wallets", CRIMINAL_SIDE_LABEL["bettor_user_excluded"], "Valid TRON; user wallet — excluded"
    if cat == "dust_uncertain":
        return "5.3 Dust / poisoning", CRIMINAL_SIDE_LABEL["bettor_user_excluded"], "Valid TRON; dust/poisoning — excluded"
    if cat == "exchange":
        return "6. Exchange / infrastructure", CRIMINAL_SIDE_LABEL["exchange_context"], "Exchange infrastructure — not an operator attribution"
    if cat == "evidence_only":
        return "7.1 Evidence-only TRON", CRIMINAL_SIDE_LABEL["unresolved_evidence"], "Valid TRON; attribution unresolved"
    if cat == "eth_uncertain":
        return "7.2 ETH unresolved", CRIMINAL_SIDE_LABEL["unresolved_eth"], "ETH; operator-or-user unresolved"
    if cat == "partial":
        return "8.5 Partial / truncated", CRIMINAL_SIDE_LABEL["invalid_nonwallet"], "Partial/truncated; not a usable wallet"
    raise ValueError(f"Unclassified census category: {cat} ({address})")


def classify_quarantine_row(row: dict[str, str]) -> tuple[str, str, str]:
    cat = row["category"]
    if cat == "platform_hot_wallet":
        return "8.2 Invalid platform-hot claims", CRIMINAL_SIDE_LABEL["invalid_nonwallet"], "Base58-invalid; source deleted; not a wallet"
    if cat == "operator_hot_variant":
        return "8.3 OCR variants", CRIMINAL_SIDE_LABEL["invalid_nonwallet"], "OCR variant of B6; not a separate wallet"
    if cat == "user_withdrawal":
        return "8.3 OCR variants", CRIMINAL_SIDE_LABEL["invalid_nonwallet"], "OCR variant of canonical hawala TEeq…D3o; not a wallet"
    if cat == "upi_gateway":
        return "8.4 Non-crypto UPI identifiers", CRIMINAL_SIDE_LABEL["noncrypto_payment"], "UPI transaction/reference ID; not a crypto wallet"
    raise ValueError(f"Unclassified quarantine category: {cat}")


def build_records(
    census: list[dict[str, str]],
    quarantine: list[dict[str, str]],
    audit_rows: list[dict[str, str]],
    new_rows: list[dict[str, str]],
    docx_rows: dict[str, list[str]],
    live_kinds: dict[str, str],
) -> list[dict[str, Any]]:
    census_by_addr = {r["address"]: r for r in census}
    quar_by_addr = {r["address"]: r for r in quarantine}
    master = {r["address"] for r in read_csv(REF / "17-CRYPTO-WALLETS.csv")}
    audit_by_token = {r["token"]: r for r in audit_rows}

    records: list[dict[str, Any]] = []
    seen_docx: set[str] = set()

    # 1) All 92 census rows.  The audit has a truncated partial token; map it.
    for row in census:
        addr = row["address"]
        audit = audit_by_token.get(addr)
        if audit is None and row["category"] == "partial":
            audit = audit_by_token.get("TTmepj9RqbfuzTRkuLuQ9vAX4")
        if audit is None:
            raise KeyError(f"No audit row for census address {addr}")
        section, side, status_note = classify_docx_row(row, audit, master)
        refs = audit.get("docx_refs", "")
        records.append(
            {
                "source_kind": "docx_census",
                "section": section,
                "address": addr,
                "network": row["network"],
                "asset_type": "TRON" if row["network"] == "TRON" else row["network"],
                "checksum": audit.get("tron_check", ""),
                "side_class": SIDE_CLASS[section],
                "criminal_side": side,
                "role": ROLE_OVERRIDES.get(addr, row.get("role", "")),
                "attribution": row.get("attribution", ""),
                "confidence": row.get("confidence", ""),
                "fact_type": row.get("fact_type", ""),
                "docx_ref": refs,
                "docx_context": docx_context_for(refs, docx_rows),
                "source_evidence": (
                    row.get("source_ref", "")
                    if row.get("category") == "hawala_collection"
                    else (row.get("proof_s3_key") or row.get("source_ref", ""))
                ),
                "live_hits": int(audit.get("live_hits") or 0),
                "live_first": audit.get("live_first", ""),
                "live_last": audit.get("live_last", ""),
                "live_kinds": live_kinds.get(addr, ""),
                "in_operator_master": "yes" if addr in master else "no",
                "in_census": "yes",
                "status": STATUS_LABEL[section],
                "priority": PRIORITY[section],
                "status_note": status_note,
                "notes": "",
            }
        )
        seen_docx.add(addr)

    # 2) 28 quarantine rows (all are in the 120-token document audit).
    for row in quarantine:
        addr = row["address"]
        audit = audit_by_token.get(addr)
        if audit is None:
            raise KeyError(f"No audit row for quarantine address {addr}")
        section, side, status_note = classify_quarantine_row(row)
        refs = audit.get("docx_refs", "")
        records.append(
            {
                "source_kind": "docx_quarantine",
                "section": section,
                "address": addr,
                "network": row["network"],
                "asset_type": "UPI" if row["network"] == "UPI" else "TRON (invalid)",
                "checksum": audit.get("tron_check", ""),
                "side_class": SIDE_CLASS[section],
                "criminal_side": side,
                "role": row.get("category", ""),
                "attribution": "",
                "confidence": "",
                "fact_type": row.get("fact_type", ""),
                "docx_ref": refs,
                "docx_context": docx_context_for(refs, docx_rows),
                "source_evidence": row.get("proof_s3_key") or row.get("source_ref", ""),
                "live_hits": int(audit.get("live_hits") or 0),
                "live_first": audit.get("live_first", ""),
                "live_last": audit.get("live_last", ""),
                "live_kinds": live_kinds.get(addr, ""),
                "in_operator_master": "no",
                "in_census": "no",
                "status": STATUS_LABEL[section],
                "priority": PRIORITY[section],
                "status_note": status_note,
                "notes": row.get("quarantine_reason", ""),
            }
        )
        seen_docx.add(addr)

    # 3) 109 live-only valid candidates (exclude the 5 docx additions).
    for row in new_rows:
        if row["category"] == "docx_2026_09_21_new":
            continue
        addr = row["address"]
        if addr in seen_docx or addr in census_by_addr or addr in quar_by_addr:
            continue
        section = "9. Live-only unverified"
        records.append(
            {
                "source_kind": "live_only",
                "section": section,
                "address": addr,
                "network": row["network"],
                "asset_type": "TRON",
                "checksum": row["checksum"],
                "side_class": SIDE_CLASS[section],
                "criminal_side": CRIMINAL_SIDE_LABEL["live_only_unverified"],
                "role": f"live {row['kinds']} rail",
                "attribution": "Unattributed — live feed only",
                "confidence": row.get("confidence", ""),
                "fact_type": "live_lead",
                "docx_ref": "",
                "docx_context": "",
                "source_evidence": row.get("evidence_samples", ""),
                "live_hits": int(row.get("live_hits") or 0),
                "live_first": row.get("first_seen", ""),
                "live_last": row.get("last_seen", ""),
                "live_kinds": row.get("kinds", ""),
                "in_operator_master": "no",
                "in_census": "no",
                "status": STATUS_LABEL[section],
                "priority": row.get("priority", "low"),
                "status_note": "Valid TRON; not in 21-09 document/census/quarantine",
                "notes": row.get("notes", ""),
            }
        )

    # Sort by section order, then docx ref / hits.
    def sort_key(r: dict[str, Any]) -> tuple[int, str, int, str]:
        try:
            si = SECTION_ORDER.index(r["section"])
        except ValueError:
            si = 99
        ref = r.get("docx_ref", "")
        m = re.match(r"T(\d+)R(\d+)", ref)
        ref_key = f"{int(m.group(1)):02d}.{int(m.group(2)):02d}" if m else "99.99"
        return (si, ref_key, -int(r.get("live_hits") or 0), r["address"])

    records.sort(key=sort_key)

    # Add notes and IDs.
    counters: Counter[str] = Counter()
    for rec in records:
        prefix = SECTION_PREFIX[rec["section"]]
        counters[prefix] += 1
        rec["record_id"] = f"{prefix}-{counters[prefix]:03d}"
        detail = DETAIL_NOTES.get(rec["address"], "")
        parts = [p for p in [detail, rec.get("status_note", ""), rec.get("docx_context", "")] if p]
        # de-duplicate repeated phrases
        dedup: list[str] = []
        for p in parts:
            if p not in dedup:
                dedup.append(p)
        rec["notes"] = short(" | ".join(dedup), 420)
    return records


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------


def validate(records: list[dict[str, Any]], census: list[dict[str, str]], quarantine: list[dict[str, str]]) -> dict[str, Any]:
    docx_records = [r for r in records if r["source_kind"] in {"docx_census", "docx_quarantine"}]
    live_only = [r for r in records if r["source_kind"] == "live_only"]
    all_addrs = [r["address"] for r in records]
    docx_addrs = [r["address"] for r in docx_records]
    live_addrs = [r["address"] for r in live_only]
    census_addrs = {r["address"] for r in census}
    quar_addrs = {r["address"] for r in quarantine}
    audit_addrs = {r["token"] for r in read_csv(REF / "WALLET-AUDIT-2026-09-21.csv")}
    # The audit uses a truncated P1 token; allow the census partial display string.
    missing_audit = {
        a for a in census_addrs | quar_addrs
        if a not in audit_addrs and not a.startswith("TTmepj9RqbfuzTRkuLuQ9vAX4..")
    }
    counts = Counter(r["section"] for r in records)
    side_counts = Counter(r["side_class"] for r in records)
    valid = [
        r for r in records
        if (r["asset_type"] == "TRON" and r["checksum"] == "valid")
        or (r["asset_type"] == "ETH" and r["checksum"] == "not-tron")
    ]
    return {
        "docx_records": len(docx_records),
        "live_only": len(live_only),
        "total": len(records),
        "docx_unique": len(set(docx_addrs)),
        "live_unique": len(set(live_addrs)),
        "intersection": len(set(docx_addrs) & set(live_addrs)),
        "missing_audit": sorted(missing_audit),
        "duplicates": [a for a, n in Counter(all_addrs).items() if n > 1],
        "counts": dict(counts),
        "side_counts": dict(side_counts),
        "valid_wallets": len(valid),
        "valid_tron": sum(1 for r in valid if r["asset_type"] == "TRON"),
        "valid_eth": sum(1 for r in valid if r["asset_type"] == "ETH"),
        "criminal_core": sum(1 for r in records if r["section"] in {"3.1 Operator wallets", "3.2 Hawala / collection"}),
        "criminal_candidates": sum(1 for r in records if r["section"] in {"4.1 Banking QR rails", "4.2 Dashboard / platform rails", "4.3 Desk / support"}),
        "bettor_excluded": sum(1 for r in records if r["section"] in {"5.1 Bettor/user withdrawal beneficiaries", "5.2 Bettor/user wallets", "5.3 Dust / poisoning"}),
        "exchange_context": sum(1 for r in records if r["section"] == "6. Exchange / infrastructure"),
        "unresolved": sum(1 for r in records if r["section"] in {"7.1 Evidence-only TRON", "7.2 ETH unresolved"}),
        "invalid": sum(1 for r in records if r["section"] in {"8.1 Invalid census OCR leads", "8.2 Invalid platform-hot claims", "8.3 OCR variants", "8.4 Non-crypto UPI identifiers", "8.5 Partial / truncated"}),
    }


# ---------------------------------------------------------------------------
# CSV / XLSX
# ---------------------------------------------------------------------------

CSV_COLUMNS = [
    "record_id", "section", "address", "network", "asset_type", "checksum",
    "side_class", "criminal_side", "role", "attribution", "confidence",
    "fact_type", "docx_ref", "docx_context", "source_evidence", "live_hits",
    "live_first", "live_last", "live_kinds", "in_operator_master", "in_census",
    "status", "priority", "notes",
]


def write_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    with path.open("w", encoding="utf-8", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=CSV_COLUMNS, extrasaction="ignore")
        writer.writeheader()
        for row in rows:
            writer.writerow({k: row.get(k, "") for k in CSV_COLUMNS})


def write_xlsx(path: Path, records: list[dict[str, Any]], stats: dict[str, Any]) -> None:
    wb = Workbook()
    wb.remove(wb.active)

    title_fill = PatternFill("solid", fgColor="1F3864")
    core_fill = PatternFill("solid", fgColor="7B1E1E")
    cand_fill = PatternFill("solid", fgColor="9C5700")
    excl_fill = PatternFill("solid", fgColor="595959")
    ctx_fill = PatternFill("solid", fgColor="2F5597")
    unres_fill = PatternFill("solid", fgColor="7030A0")
    invalid_fill = PatternFill("solid", fgColor="A61C00")
    live_fill = PatternFill("solid", fgColor="1F4E79")
    white_bold = Font(color="FFFFFF", bold=True, size=10)
    thin = Side(style="thin", color="BFBFBF")
    border = Border(left=thin, right=thin, top=thin, bottom=thin)
    wrap = Alignment(wrap_text=True, vertical="top")

    def add_sheet(name: str, rows: list[dict[str, Any]], fill: PatternFill, title: str) -> None:
        ws = wb.create_sheet(name)
        ws.sheet_view.showGridLines = False
        ws.merge_cells("A1:X1")
        ws["A1"] = title
        ws["A1"].fill = title_fill
        ws["A1"].font = Font(color="FFFFFF", bold=True, size=14)
        ws["A1"].alignment = Alignment(vertical="center")
        ws.row_dimensions[1].height = 24
        headers = CSV_COLUMNS
        for ci, h in enumerate(headers, start=1):
            c = ws.cell(row=3, column=ci, value=h.replace("_", " ").title())
            c.fill = fill
            c.font = white_bold
            c.alignment = Alignment(wrap_text=True, vertical="center")
            c.border = border
        ws.row_dimensions[3].height = 28
        for ri, row in enumerate(rows, start=4):
            for ci, key in enumerate(headers, start=1):
                c = ws.cell(row=ri, column=ci, value=row.get(key, ""))
                c.alignment = wrap
                c.border = border
                c.font = Font(size=8)
            ws.row_dimensions[ri].height = 28
        widths = {
            "record_id": 11, "section": 24, "address": 38, "network": 7,
            "asset_type": 12, "checksum": 14, "side_class": 22,
            "criminal_side": 28, "role": 26, "attribution": 28,
            "confidence": 10, "fact_type": 10, "docx_ref": 10,
            "docx_context": 45, "source_evidence": 40, "live_hits": 8,
            "live_first": 19, "live_last": 19, "live_kinds": 14,
            "in_operator_master": 10, "in_census": 9, "status": 24,
            "priority": 16, "notes": 70,
        }
        for ci, h in enumerate(headers, start=1):
            ws.column_dimensions[get_column_letter(ci)].width = widths.get(h, 18)
        ws.freeze_panes = "D4"
        ws.auto_filter.ref = f"A3:{get_column_letter(len(headers))}{max(3, len(rows) + 3)}"

    # Summary
    ws = wb.create_sheet("Summary")
    ws.sheet_view.showGridLines = False
    ws.merge_cells("A1:F1")
    ws["A1"] = "CBTF Crypto Wallet Master — Criminal-Side Focus (26 Oct 2026)"
    ws["A1"].fill = title_fill
    ws["A1"].font = Font(color="FFFFFF", bold=True, size=16)
    ws["A1"].alignment = Alignment(vertical="center")
    ws.row_dimensions[1].height = 30
    summary = [
        ("Metric", "Count", "Definition"),
        ("Valid wallet addresses", stats["valid_wallets"], "189 TRON + 2 ETH; includes 109 live-only unverified"),
        ("Criminal-side total", stats["criminal_core"] + stats["criminal_candidates"], "34 core + 28 candidates; all TRON"),
        ("  Criminal-side core", stats["criminal_core"], "28 hawala/collection + 6 operator-side"),
        ("  Criminal-side candidates", stats["criminal_candidates"], "20 QR + 7 platform + 1 support"),
        ("Bettor/user excluded", stats["bettor_excluded"], "8 withdrawal beneficiaries + 2 user wallets + 2 dust"),
        ("Exchange context", stats["exchange_context"], "E1 + A1; not criminal attribution"),
        ("Unresolved", stats["unresolved"], "4 evidence-only TRON + 2 ETH"),
        ("Invalid / non-wallet", stats["invalid"], "9 invalid OCR + 26 invalid claims + 2 UPI + 1 partial"),
        ("Live-only unverified", stats["live_only"], "Valid TRON; side unknown; not curated"),
        ("Document tokens (120)", stats["docx_records"], "92 census + 28 quarantine"),
        ("Total records", stats["total"], "120 document + 109 live-only"),
    ]
    for ri, row in enumerate(summary, start=3):
        for ci, val in enumerate(row, start=1):
            c = ws.cell(row=ri, column=ci, value=val)
            c.border = border
            c.alignment = wrap
            if ri == 3:
                c.fill = title_fill
                c.font = white_bold
            else:
                c.font = Font(size=10)
    ws.column_dimensions["A"].width = 30
    ws.column_dimensions["B"].width = 12
    ws.column_dimensions["C"].width = 95

    # Read me
    ws = wb.create_sheet("Read Me")
    ws.sheet_view.showGridLines = False
    ws.merge_cells("A1:B1")
    ws["A1"] = "How to use this workbook"
    ws["A1"].fill = title_fill
    ws["A1"].font = Font(color="FFFFFF", bold=True, size=14)
    notes = [
        ("Scope", "Criminal-side wallet master built from the 21-09-2026 CBTF wallet document, the 92-row census, the 28-row quarantine, and the 2026-10-26 live wallet_hits feed."),
        ("Criminal-side core", "Asserted operator and hawala/collection wallets. These are the strongest criminal-side records."),
        ("Criminal-side candidates", "QR, dashboard/platform, and support/collection wallets. Likely criminal infrastructure but low/medium confidence or OCR-derived; corroborate before case use."),
        ("Bettor/user excluded", "Valid wallets that are payout beneficiaries, user wallets, or dust/poisoning. Listed so no wallet is omitted, but NOT in the criminal master."),
        ("Exchange context", "Exchange hot/withdraw wallets. Not operator attributions."),
        ("Unresolved", "Evidence-only TRON and ETH wallets whose side/attribution is unknown. Do not assume criminal."),
        ("Invalid / non-wallet", "Checksum-invalid strings, OCR variants, UPI identifiers, and a partial/truncated string. Not usable as wallets."),
        ("Live-only unverified", "109 valid TRON addresses observed only in the live feed. Side/attribution unknown; not curated and not in the criminal master."),
        ("Operator master", "The five-row operator master is unchanged: B1-B5. B6 is operator-side but census-only. No live-only address was promoted."),
        ("Evidence", "Source evidence keys are preserved. Deleted S3 keys are marked in notes; re-OCR is required where checksum is invalid."),
        ("Caution", "This is an intelligence classification, not a legal finding. Confidence labels reflect source evidence; low/medium candidates require independent corroboration."),
    ]
    for ri, (k, v) in enumerate(notes, start=3):
        ws.cell(row=ri, column=1, value=k).font = Font(bold=True, size=10)
        ws.cell(row=ri, column=1).alignment = wrap
        ws.cell(row=ri, column=1).border = border
        ws.cell(row=ri, column=2, value=v).alignment = wrap
        ws.cell(row=ri, column=2).border = border
        ws.cell(row=ri, column=2).font = Font(size=10)
    ws.column_dimensions["A"].width = 28
    ws.column_dimensions["B"].width = 130

    # Section sheets
    add_sheet("Criminal Core", [r for r in records if r["section"] in {"3.1 Operator wallets", "3.2 Hawala / collection"}], core_fill, "Criminal-Side Core — 34 records (operator + hawala/collection)")
    add_sheet("Criminal Candidates", [r for r in records if r["section"] in {"4.1 Banking QR rails", "4.2 Dashboard / platform rails", "4.3 Desk / support"}], cand_fill, "Criminal-Side Candidates — 28 records (QR + platform + support)")
    add_sheet("Excluded Bettor", [r for r in records if r["section"] in {"5.1 Bettor/user withdrawal beneficiaries", "5.2 Bettor/user wallets", "5.3 Dust / poisoning"}], excl_fill, "Excluded Bettor/User-Side Wallets — 12 records")
    add_sheet("Exchange Context", [r for r in records if r["section"] == "6. Exchange / infrastructure"], ctx_fill, "Exchange / Infrastructure Context — 2 records")
    add_sheet("Unresolved", [r for r in records if r["section"] in {"7.1 Evidence-only TRON", "7.2 ETH unresolved"}], unres_fill, "Unresolved Evidence / ETH — 6 records")
    add_sheet("Invalid Quarantine", [r for r in records if r["section"] in {"8.1 Invalid census OCR leads", "8.2 Invalid platform-hot claims", "8.3 OCR variants", "8.4 Non-crypto UPI identifiers", "8.5 Partial / truncated"}], invalid_fill, "Invalid / Non-Wallet / Quarantine — 38 records")
    add_sheet("Live Only", [r for r in records if r["section"] == "9. Live-only unverified"], live_fill, "Live-Only Unverified — 109 valid TRON addresses (not curated)")
    add_sheet("All Records", records, title_fill, "All 229 Records — complete reconciliation")

    # Reconciliation
    ws = wb.create_sheet("Reconciliation")
    ws.sheet_view.showGridLines = False
    ws.merge_cells("A1:D1")
    ws["A1"] = "Completeness and reconciliation"
    ws["A1"].fill = title_fill
    ws["A1"].font = Font(color="FFFFFF", bold=True, size=14)
    rows = [
        ("Check", "Result", "Expected", "Status"),
        ("Document tokens", stats["docx_records"], 120, "PASS" if stats["docx_records"] == 120 else "FAIL"),
        ("Document unique addresses", stats["docx_unique"], 120, "PASS" if stats["docx_unique"] == 120 else "FAIL"),
        ("Live-only records", stats["live_only"], 109, "PASS" if stats["live_only"] == 109 else "FAIL"),
        ("Live-only unique addresses", stats["live_unique"], 109, "PASS" if stats["live_unique"] == 109 else "FAIL"),
        ("Docx/live-only intersection", stats["intersection"], 0, "PASS" if stats["intersection"] == 0 else "FAIL"),
        ("Duplicate addresses", len(stats["duplicates"]), 0, "PASS" if not stats["duplicates"] else "FAIL"),
        ("Missing audit tokens", len(stats["missing_audit"]), 0, "PASS" if not stats["missing_audit"] else "FAIL"),
        ("Total records", stats["total"], 229, "PASS" if stats["total"] == 229 else "FAIL"),
        ("Valid wallets", stats["valid_wallets"], 191, "PASS" if stats["valid_wallets"] == 191 else "FAIL"),
    ]
    for ri, row in enumerate(rows, start=3):
        for ci, val in enumerate(row, start=1):
            c = ws.cell(row=ri, column=ci, value=val)
            c.border = border
            c.alignment = wrap
            if ri == 3:
                c.fill = title_fill
                c.font = white_bold
            else:
                c.font = Font(size=10)
    ws.column_dimensions["A"].width = 30
    ws.column_dimensions["B"].width = 12
    ws.column_dimensions["C"].width = 12
    ws.column_dimensions["D"].width = 12

    wb.save(path)


# ---------------------------------------------------------------------------
# DOCX
# ---------------------------------------------------------------------------


def set_cell_shading(cell, fill: str) -> None:
    tc_pr = cell._tc.get_or_add_tcPr()
    shd = OxmlElement("w:shd")
    shd.set(qn("w:fill"), fill)
    tc_pr.append(shd)


def set_cell_margins(cell, top=50, start=70, bottom=50, end=70) -> None:
    tc = cell._tc
    tc_pr = tc.get_or_add_tcPr()
    tc_mar = tc_pr.first_child_found_in("w:tcMar")
    if tc_mar is None:
        tc_mar = OxmlElement("w:tcMar")
        tc_pr.append(tc_mar)
    for m, v in (("top", top), ("start", start), ("bottom", bottom), ("end", end)):
        node = tc_mar.find(qn(f"w:{m}"))
        if node is None:
            node = OxmlElement(f"w:{m}")
            tc_mar.append(node)
        node.set(qn("w:w"), str(v))
        node.set(qn("w:type"), "dxa")


def set_repeat_table_header(row) -> None:
    tr = row._tr
    tr_pr = tr.get_or_add_trPr()
    tbl_header = OxmlElement("w:tblHeader")
    tbl_header.set(qn("w:val"), "true")
    tr_pr.append(tbl_header)


def prevent_row_split(row) -> None:
    tr = row._tr
    tr_pr = tr.get_or_add_trPr()
    cant_split = OxmlElement("w:cantSplit")
    tr_pr.append(cant_split)


def add_page_number(paragraph) -> None:
    run = paragraph.add_run()
    fld_char1 = OxmlElement("w:fldChar")
    fld_char1.set(qn("w:fldCharType"), "begin")
    instr_text = OxmlElement("w:instrText")
    instr_text.set(qn("xml:space"), "preserve")
    instr_text.text = "PAGE"
    fld_char2 = OxmlElement("w:fldChar")
    fld_char2.set(qn("w:fldCharType"), "end")
    run._r.append(fld_char1)
    run._r.append(instr_text)
    run._r.append(fld_char2)


def add_table(
    doc: Document,
    headers: list[str],
    rows: list[list[str]],
    widths: list[float],
    header_fill: str,
    font_size: float = 7.5,
    row_height: float = 0.28,
):
    table = doc.add_table(rows=1, cols=len(headers))
    table.style = "Table Grid"
    table.alignment = WD_TABLE_ALIGNMENT.CENTER
    table.autofit = False
    hdr = table.rows[0]
    set_repeat_table_header(hdr)
    for i, h in enumerate(headers):
        cell = hdr.cells[i]
        cell.width = Inches(widths[i])
        cell.text = ""
        p = cell.paragraphs[0]
        run = p.add_run(h)
        run.bold = True
        run.font.size = Pt(font_size)
        run.font.color.rgb = RGBColor(255, 255, 255)
        set_cell_shading(cell, header_fill)
        set_cell_margins(cell)
    for row_data in rows:
        row = table.add_row()
        prevent_row_split(row)
        for i, val in enumerate(row_data):
            cell = row.cells[i]
            cell.width = Inches(widths[i])
            cell.text = ""
            p = cell.paragraphs[0]
            p.paragraph_format.space_after = Pt(0)
            p.paragraph_format.space_before = Pt(0)
            run = p.add_run(str(val))
            run.font.size = Pt(font_size)
            if i == 1 and len(str(val)) > 20:
                run.font.name = "Consolas"
            set_cell_margins(cell)
        row.height = Inches(row_height)
    return table


def add_heading(doc: Document, text: str, level: int = 1):
    p = doc.add_heading(text, level=level)
    p.paragraph_format.keep_with_next = True
    return p


def add_bullets(doc: Document, items: list[str]) -> None:
    for item in items:
        p = doc.add_paragraph(item, style="List Bullet")
        p.paragraph_format.space_after = Pt(2)


def add_callout(doc: Document, text: str, fill: str = "E7E6E6") -> None:
    table = doc.add_table(rows=1, cols=1)
    table.style = "Table Grid"
    cell = table.cell(0, 0)
    cell.text = ""
    p = cell.paragraphs[0]
    run = p.add_run(text)
    run.bold = True
    run.font.size = Pt(9)
    set_cell_shading(cell, fill)
    set_cell_margins(cell, top=80, bottom=80, start=100, end=100)


def write_docx(path: Path, records: list[dict[str, Any]], stats: dict[str, Any]) -> None:
    doc = Document()
    doc.core_properties.title = "CBTF Crypto Wallet Master — Criminal-Side Focus"
    doc.core_properties.subject = "Criminal-side wallet classification from the 21-09-2026 CBTF document and 2026-10-26 live feed"
    doc.core_properties.author = "CBTF wallet audit"
    doc.core_properties.comments = "Intelligence classification; not a legal finding. Five-row operator master unchanged."

    # Landscape A4.
    section = doc.sections[0]
    section.orientation = WD_ORIENT.LANDSCAPE
    section.page_width, section.page_height = section.page_height, section.page_width
    section.top_margin = Inches(0.45)
    section.bottom_margin = Inches(0.45)
    section.left_margin = Inches(0.5)
    section.right_margin = Inches(0.5)

    # Base style.
    style = doc.styles["Normal"]
    style.font.name = "Calibri"
    style.font.size = Pt(9)
    style.paragraph_format.space_after = Pt(4)

    # Header/footer.
    header = section.header.paragraphs[0]
    header.text = "CBTF — Criminal-Side Crypto Wallet Master — 26 Oct 2026"
    header.alignment = WD_ALIGN_PARAGRAPH.RIGHT
    header.runs[0].font.size = Pt(8)
    header.runs[0].font.color.rgb = RGBColor(100, 100, 100)
    footer = section.footer.paragraphs[0]
    footer.alignment = WD_ALIGN_PARAGRAPH.CENTER
    run = footer.add_run("Page ")
    run.font.size = Pt(8)
    add_page_number(footer)
    footer.add_run("  |  Intelligence classification — not a legal finding").font.size = Pt(8)

    # Title page.
    p = doc.add_paragraph()
    p.alignment = WD_ALIGN_PARAGRAPH.CENTER
    p.paragraph_format.space_before = Pt(80)
    run = p.add_run("CBTF CRYPTO WALLET MASTER")
    run.bold = True
    run.font.size = Pt(30)
    run.font.color.rgb = RGBColor(31, 56, 100)
    p = doc.add_paragraph()
    p.alignment = WD_ALIGN_PARAGRAPH.CENTER
    run = p.add_run("Criminal-Side Focus — Updated 26 October 2026")
    run.bold = True
    run.font.size = Pt(16)
    run.font.color.rgb = RGBColor(123, 30, 30)
    p = doc.add_paragraph()
    p.alignment = WD_ALIGN_PARAGRAPH.CENTER
    run = p.add_run("Supersedes the 21-09-2026 wallet document for classification")
    run.italic = True
    run.font.size = Pt(11)

    doc.add_paragraph()
    summary_rows = [
        ["Valid wallet addresses", str(stats["valid_wallets"]), "189 TRON + 2 ETH; includes 109 live-only unverified"],
        ["Criminal-side total", str(stats["criminal_core"] + stats["criminal_candidates"]), "34 core + 28 candidates; all TRON"],
        ["Criminal-side core", str(stats["criminal_core"]), "28 hawala/collection + 6 operator-side"],
        ["Criminal-side candidates", str(stats["criminal_candidates"]), "20 QR + 7 platform + 1 support"],
        ["Bettor/user excluded", str(stats["bettor_excluded"]), "8 withdrawal + 2 user wallets + 2 dust"],
        ["Exchange context", str(stats["exchange_context"]), "E1 + A1; not criminal attribution"],
        ["Unresolved", str(stats["unresolved"]), "4 evidence-only TRON + 2 ETH"],
        ["Invalid / non-wallet", str(stats["invalid"]), "9 invalid OCR + 26 invalid claims + 2 UPI + 1 partial"],
        ["Live-only unverified", str(stats["live_only"]), "Valid TRON; side unknown; not curated"],
        ["Total records", str(stats["total"]), "120 document tokens + 109 live-only"],
    ]
    add_table(
        doc,
        ["Metric", "Count", "Definition"],
        summary_rows,
        [2.6, 0.8, 6.7],
        "1F3864",
        font_size=9,
        row_height=0.3,
    )
    doc.add_paragraph()
    add_callout(
        doc,
        "IMPORTANT: This document does not change the five-row operator master. "
        "B1-B5 remain the operator master. B6 is operator-side but census-only. "
        "No live-only address was promoted. Criminal-side candidates are explicitly "
        "separated from asserted core wallets and from bettor/user-side wallets.",
        "FFF2CC",
    )
    doc.add_paragraph()
    p = doc.add_paragraph()
    run = p.add_run(
        "Source: CBTF_Crypto_Wallets_Updated-21-09-26.docx; 92-row census; 28-row quarantine; "
        "live wallet_hits feed rechecked 2026-10-26. Evidence keys are preserved; deleted S3 "
        "objects are flagged in the notes."
    )
    run.font.size = Pt(8)
    run.italic = True

    doc.add_page_break()

    # Contents / read this first.
    add_heading(doc, "Contents", 1)
    contents = [
        "1. Read this first — classification rules",
        "2. Executive summary and reconciliation",
        "3. Criminal-side core — 34 wallets (operator + hawala/collection)",
        "4. Criminal-side candidates — 28 wallets (QR + platform + support)",
        "5. Bettor/user-side excluded — 12 wallets",
        "6. Exchange / infrastructure context — 2 wallets",
        "7. Unresolved evidence — 6 wallets (4 TRON + 2 ETH)",
        "8. Invalid / non-wallet / quarantine — 38 records",
        "9. Live-only unverified — 109 valid TRON addresses",
        "10. Completeness, changes, and limitations",
        "Appendix A. Workbook and CSV reference",
        "Appendix B. Confidence and status definitions",
    ]
    for item in contents:
        p = doc.add_paragraph(item, style="List Number")
        p.paragraph_format.space_after = Pt(1)

    add_heading(doc, "1. Read this first — classification rules", 1)
    add_bullets(
        doc,
        [
            "Criminal-side core = asserted operator or hawala/collection wallet with specific source evidence. These are the strongest criminal-side records.",
            "Criminal-side candidate = QR, dashboard/platform, or support/collection rail that is likely criminal infrastructure but is low/medium confidence, OCR-derived, or not independently attributed. Corroborate before case use.",
            "Bettor/user-side excluded = valid payout beneficiaries, user wallets, and dust/poisoning. Listed for completeness but NOT in the criminal master.",
            "Exchange context = exchange hot/withdraw wallets. Not operator attributions.",
            "Unresolved = evidence-only TRON or ETH wallets whose side/attribution is unknown. Do not assume criminal.",
            "Invalid/non-wallet = checksum-invalid strings, OCR variants, UPI identifiers, and one partial/truncated string. These cannot be used as wallets.",
            "Live-only unverified = valid TRON addresses seen only in the live feed. Side/attribution is unknown; they are not curated and not in the criminal master.",
            "No address was added to the five-row operator master. B1-B5 are unchanged.",
        ],
    )
    add_callout(
        doc,
        "If a record is uncertain, it is placed in candidates, unresolved, or live-only — never silently "
        "promoted into the criminal core. The counts in this document are designed to add up: 120/120 "
        "document tokens are accounted for, and 109/109 live-only valid addresses are listed.",
        "DEEAF6",
    )

    # Executive summary.
    add_heading(doc, "2. Executive summary and reconciliation", 1)
    p = doc.add_paragraph()
    run = p.add_run(
        "The 21-09-2026 document contained 120 unique tokens: 92 census wallets and 28 quarantine "
        "strings. This update classifies all 120 and adds the 109 checksum-valid addresses that are "
        "visible only in the live feed. The result is 229 records: 191 valid wallet addresses "
        "(189 TRON + 2 ETH) and 38 invalid/non-wallet records."
    )
    run.font.size = Pt(9)
    recon_rows = [
        ["3. Criminal-side core", str(stats["criminal_core"]), str(stats["criminal_core"]), "28 hawala/collection + 6 operator-side"],
        ["4. Criminal-side candidates", str(stats["criminal_candidates"]), str(stats["criminal_candidates"]), "20 QR + 7 platform + 1 support"],
        ["5. Bettor/user excluded", str(stats["bettor_excluded"]), str(stats["bettor_excluded"]), "8 withdrawal + 2 user + 2 dust"],
        ["6. Exchange context", str(stats["exchange_context"]), str(stats["exchange_context"]), "E1 + A1"],
        ["7. Unresolved", str(stats["unresolved"]), str(stats["unresolved"]), "4 evidence-only + 2 ETH"],
        ["8. Invalid/non-wallet", str(stats["invalid"]), "0", "9 OCR + 26 invalid + 2 UPI + 1 partial"],
        ["9. Live-only unverified", str(stats["live_only"]), str(stats["live_only"]), "Valid TRON; side unknown"],
        ["TOTAL", str(stats["total"]), str(stats["valid_wallets"]), "120 document + 109 live-only"],
    ]
    add_table(
        doc,
        ["Section", "Records", "Valid wallets", "Composition"],
        recon_rows,
        [2.8, 1.0, 1.1, 5.2],
        "1F3864",
        font_size=8.5,
        row_height=0.28,
    )
    p = doc.add_paragraph()
    run = p.add_run(
        "Criminal-side total: 62 wallets (34 core + 28 candidates), all TRON. "
        "The 12 bettor/user wallets, 2 exchange wallets, 4 evidence-only wallets, and 2 ETH "
        "wallets are deliberately excluded from the criminal master. The 109 live-only valid "
        "addresses are unverified and listed separately."
    )
    run.bold = True
    run.font.size = Pt(9)

    # Section 3.
    doc.add_page_break()
    add_heading(doc, "3. Criminal-side core — 34 wallets", 1)
    p = doc.add_paragraph()
    run = p.add_run(
        "This is the strongest criminal-side set. It contains the 28 hawala/collection wallets "
        "from the source report and the six operator-side wallets. B1-B5 are the five-row operator "
        "master; B6 is operator-side but remains census-only."
    )
    run.font.size = Pt(9)

    add_heading(doc, "3.1 Operator wallets (6)", 2)
    operator = [r for r in records if r["section"] == "3.1 Operator wallets"]
    op_rows = [
        [
            r["record_id"], r["address"], r["role"], r["confidence"], r["fact_type"],
            short(r["source_evidence"], 75), str(r["live_hits"]),
            "MASTER" if r["in_operator_master"] == "yes" else "census-only",
        ]
        for r in operator
    ]
    add_table(
        doc,
        ["ID", "Address", "Role", "Conf.", "Fact", "Evidence (short)", "Hits", "Master"],
        op_rows,
        [0.65, 2.25, 1.65, 0.55, 0.55, 2.5, 0.45, 0.8],
        "7B1E1E",
        font_size=7.2,
        row_height=0.34,
    )
    p = doc.add_paragraph()
    run = p.add_run(
        "B1-B5 are the five-row operator master. B6 (TAVawJ7BF7L9XwSf5RJdtr1gHSyVTzzCxf) is a "
        "valid operator deposit-receive wallet but is not in that five-row master; it is included "
        "here as operator-side census-only. The checksum-invalid TAVaw…dtrig… variant is NOT B6 and "
        "is quarantined in Section 8."
    )
    run.font.size = Pt(8)

    add_heading(doc, "3.2 Hawala / collection (28)", 2)
    hawala = [r for r in records if r["section"] == "3.2 Hawala / collection"]
    hw_rows = [
        [
            r["record_id"], r["address"], r["role"], r["attribution"],
            r["confidence"], short(r["source_evidence"], 55), str(r["live_hits"]),
        ]
        for r in hawala
    ]
    add_table(
        doc,
        ["ID", "Address", "Role", "Person / entity", "Conf.", "Source evidence", "Hits"],
        hw_rows,
        [0.65, 2.2, 1.55, 2.55, 0.55, 2.0, 0.45],
        "7B1E1E",
        font_size=7.0,
        row_height=0.32,
    )
    p = doc.add_paragraph()
    run = p.add_run(
        "All 28 are valid TRON addresses with source-report attribution (fact, very_high/high). "
        "Three are double-role entries: C1 TAhW4QKFYFBZX2qQ8yuZCoVNC4qrVUNJpQ (CR7 → 718 USDT, "
        "2026-06-04), C3 TFpP4TqspHYa1Xzd3L1qkmhyfd6gzEsoQr (CR7 → 1,061.15 USDT, 2026-06-04), "
        "and C11 TQ9LitUK7iBhayHruHBN7VT9sjtpfarH6v (CR7 → 798.03 USDT, 2026-06-13). They are "
        "counted once here as hawala/collection wallets. The invalid C12 variant "
        "TEeqmfN74hg8vP8umv8ubD30GAroVWRTFD is in Section 8.3, not here."
    )
    run.font.size = Pt(8)

    # Section 4.
    doc.add_page_break()
    add_heading(doc, "4. Criminal-side candidates — 28 wallets", 1)
    p = doc.add_paragraph()
    run = p.add_run(
        "These wallets are likely criminal infrastructure, but they are not in the same evidentiary "
        "class as the core. They are QR-derived, dashboard/OCR-derived, or a support-chat wallet. "
        "Treat them as leads until the QR/beneficiary/transaction evidence is independently corroborated."
    )
    run.font.size = Pt(9)

    add_heading(doc, "4.1 Banking QR rails (20)", 2)
    qr = [r for r in records if r["section"] == "4.1 Banking QR rails"]
    qr_rows = [
        [
            r["record_id"], r["address"], str(r["live_hits"]),
            short(r["live_first"], 10), short(r["live_last"], 10),
            short(r["docx_context"], 80), short(r["source_evidence"], 35),
        ]
        for r in qr
    ]
    add_table(
        doc,
        ["ID", "Address", "Hits", "First", "Last", "Original QR note / priority", "Evidence"],
        qr_rows,
        [0.6, 2.15, 0.4, 0.7, 0.7, 4.1, 1.5],
        "9C5700",
        font_size=7.0,
        row_height=0.32,
    )
    p = doc.add_paragraph()
    run = p.add_run(
        "The QR rails are low-medium AI leads. High re-upload counts (Q1/Q2) make them operationally "
        "interesting, but a QR beneficiary is not by itself proof of criminal ownership. Re-OCR and "
        "beneficiary corroboration are required before promoting any of these to core."
    )
    run.font.size = Pt(8)

    add_heading(doc, "4.2 Dashboard / platform rails (7)", 2)
    platform = [r for r in records if r["section"] == "4.2 Dashboard / platform rails"]
    pl_rows = [
        [
            r["record_id"], r["address"], r["role"], str(r["live_hits"]),
            short(r["docx_context"], 105), r["confidence"], short(r["source_evidence"], 45),
        ]
        for r in platform
    ]
    add_table(
        doc,
        ["ID", "Address", "Role", "Hits", "Dashboard note / original context", "Conf.", "Evidence"],
        pl_rows,
        [0.6, 2.15, 1.55, 0.4, 3.75, 0.5, 1.5],
        "9C5700",
        font_size=7.0,
        row_height=0.34,
    )
    p = doc.add_paragraph()
    run = p.add_run(
        "These are valid TRON dashboard rails with high live activity. They are platform-side "
        "candidates, not named operator wallets. N1/N3/N4/N5/N6 come from the 21-09 dashboard OCR; "
        "R2/R3 come from the live dashboard. All require re-OCR/transaction corroboration."
    )
    run.font.size = Pt(8)

    add_heading(doc, "4.3 Desk / support (1)", 2)
    support = [r for r in records if r["section"] == "4.3 Desk / support"]
    sp_rows = [
        [
            r["record_id"], r["address"], r["role"], r["confidence"], r["fact_type"],
            short(r["docx_context"], 110), str(r["live_hits"]),
        ]
        for r in support
    ]
    add_table(
        doc,
        ["ID", "Address", "Role", "Conf.", "Fact", "Support-chat context", "Hits"],
        sp_rows,
        [0.65, 2.25, 1.75, 0.55, 0.55, 4.1, 0.45],
        "9C5700",
        font_size=7.2,
        row_height=0.34,
    )
    p = doc.add_paragraph()
    run = p.add_run(
        "D1 received 530.25 USDT in a support chat. There is no named attribution, so it is a "
        "candidate support/collection wallet, not core. D2 and D3 are checksum-invalid and are "
        "listed in Section 8.1, not here."
    )
    run.font.size = Pt(8)

    # Section 5.
    doc.add_page_break()
    add_heading(doc, "5. Bettor/user-side excluded — 12 wallets", 1)
    p = doc.add_paragraph()
    run = p.add_run(
        "These are valid wallets, but they are not criminal-side. They are listed so no wallet is "
        "omitted. C1, C3, and C11 also appear as withdrawal beneficiaries but are hawala/collection "
        "wallets; they are counted in Section 3.2, not here."
    )
    run.font.size = Pt(9)

    add_heading(doc, "5.1 Withdrawal beneficiaries (8)", 2)
    userw = [r for r in records if r["section"] == "5.1 Bettor/user withdrawal beneficiaries"]
    uw_rows = [
        [
            r["record_id"], r["address"], r["role"], short(r["docx_context"], 105),
            short(r["source_evidence"], 35), "Excluded — bettor/user payout",
        ]
        for r in userw
    ]
    add_table(
        doc,
        ["ID", "Address", "Role", "Amount / context", "Evidence", "Why excluded"],
        uw_rows,
        [0.6, 2.1, 1.4, 3.55, 1.45, 1.4],
        "595959",
        font_size=7.0,
        row_height=0.34,
    )

    add_heading(doc, "5.2 Bettor/user wallets (2)", 2)
    user = [r for r in records if r["section"] == "5.2 Bettor/user wallets"]
    us_rows = [
        [r["record_id"], r["address"], r["role"], short(r["docx_context"], 110), short(r["source_evidence"], 40)]
        for r in user
    ]
    add_table(
        doc,
        ["ID", "Address", "Role", "Context", "Evidence"],
        us_rows,
        [0.6, 2.1, 1.7, 4.35, 1.7],
        "595959",
        font_size=7.2,
        row_height=0.34,
    )

    add_heading(doc, "5.3 Dust / poisoning (2)", 2)
    dust = [r for r in records if r["section"] == "5.3 Dust / poisoning"]
    du_rows = [
        [r["record_id"], r["address"], r["role"], short(r["docx_context"], 110), short(r["source_evidence"], 40)]
        for r in dust
    ]
    add_table(
        doc,
        ["ID", "Address", "Role", "Context", "Evidence"],
        du_rows,
        [0.6, 2.1, 1.7, 4.35, 1.7],
        "595959",
        font_size=7.2,
        row_height=0.34,
    )

    # Section 6.
    add_heading(doc, "6. Exchange / infrastructure context — 2 wallets", 1)
    p = doc.add_paragraph()
    run = p.add_run(
        "Exchange wallets are not criminal-side attributions. They are retained as context because "
        "they appear in the source evidence and may help trace deposits/withdrawals."
    )
    run.font.size = Pt(9)
    exch = [r for r in records if r["section"] == "6. Exchange / infrastructure"]
    ex_rows = [
        [r["record_id"], r["address"], r["role"], short(r["docx_context"], 105), short(r["source_evidence"], 45), str(r["live_hits"])]
        for r in exch
    ]
    add_table(
        doc,
        ["ID", "Address", "Role", "Context", "Evidence", "Hits"],
        ex_rows,
        [0.6, 2.1, 1.8, 3.85, 1.7, 0.4],
        "2F5597",
        font_size=7.2,
        row_height=0.36,
    )

    # Section 7.
    doc.add_page_break()
    add_heading(doc, "7. Unresolved evidence — 6 wallets", 1)
    p = doc.add_paragraph()
    run = p.add_run(
        "These wallets are valid, but the evidence does not establish which side they belong to. "
        "They are deliberately not in the criminal master. Resolve via transaction-level attribution."
    )
    run.font.size = Pt(9)

    add_heading(doc, "7.1 Evidence-only TRON (4)", 2)
    ev = [r for r in records if r["section"] == "7.1 Evidence-only TRON"]
    ev_rows = [
        [r["record_id"], r["address"], r["role"], short(r["docx_context"], 105), short(r["source_evidence"], 40), str(r["live_hits"])]
        for r in ev
    ]
    add_table(
        doc,
        ["ID", "Address", "Role", "Original verification note", "Evidence", "Hits"],
        ev_rows,
        [0.6, 2.1, 1.8, 3.9, 1.6, 0.4],
        "7030A0",
        font_size=7.0,
        row_height=0.36,
    )

    add_heading(doc, "7.2 ETH unresolved (2)", 2)
    eth = [r for r in records if r["section"] == "7.2 ETH unresolved"]
    eth_rows = [
        [r["record_id"], r["address"], r["role"], short(r["docx_context"], 105), short(r["source_evidence"], 40), str(r["live_hits"])]
        for r in eth
    ]
    add_table(
        doc,
        ["ID", "Address", "Role", "Context", "Evidence", "Hits"],
        eth_rows,
        [0.6, 2.9, 1.8, 3.2, 1.6, 0.4],
        "7030A0",
        font_size=7.2,
        row_height=0.36,
    )

    # Section 8.
    doc.add_page_break()
    add_heading(doc, "8. Invalid / non-wallet / quarantine — 38 records", 1)
    p = doc.add_paragraph()
    run = p.add_run(
        "These records are retained so the source data is not lost, but they cannot be used as "
        "wallets. The 24 platform-hot strings are base58-invalid and the source JSON was deleted; "
        "the nine census OCR leads are checksum-invalid; TAVaw and C12 are OCR variants; the two "
        "UPI rows are payment identifiers; P1 is a partial/truncated string."
    )
    run.font.size = Pt(9)
    add_callout(
        doc,
        "Do not count any record in this section as a criminal wallet. D2 and D3 are especially "
        "important: they are marked fact/high in the census but fail the Base58 checksum. They must "
        "be corrected to OCR leads before case use.",
        "FCE4D6",
    )

    add_heading(doc, "8.1 Invalid census OCR leads (9)", 2)
    inv = [r for r in records if r["section"] == "8.1 Invalid census OCR leads"]
    inv_rows = [
        [r["record_id"], r["address"], r["role"], str(r["live_hits"]), short(r["docx_context"], 105), short(r["source_evidence"], 40)]
        for r in inv
    ]
    add_table(
        doc,
        ["ID", "Address", "Role", "Hits", "Original context", "Evidence"],
        inv_rows,
        [0.6, 2.1, 1.65, 0.4, 3.85, 1.8],
        "A61C00",
        font_size=7.0,
        row_height=0.36,
    )

    add_heading(doc, "8.2 Invalid platform-hot claims (24)", 2)
    ph = [r for r in records if r["section"] == "8.2 Invalid platform-hot claims"]
    ph_rows = [
        [r["record_id"], r["address"], "Platform hot wallet — named owner not identified", "base58-invalid", short(r["source_evidence"], 45)]
        for r in ph
    ]
    add_table(
        doc,
        ["ID", "Address", "Reported attribution", "Checksum", "Source"],
        ph_rows,
        [0.65, 2.55, 3.4, 1.0, 2.8],
        "A61C00",
        font_size=7.2,
        row_height=0.3,
    )
    p = doc.add_paragraph()
    run = p.add_run(
        "All 24 strings contain characters that cannot occur in a TRON Base58 address. The source "
        "deposit/1780826098942.json is deleted; the true object cannot be recovered without an AWS "
        "version restore. These are re-OCR leads only."
    )
    run.font.size = Pt(8)

    add_heading(doc, "8.3 OCR variants (2)", 2)
    var = [r for r in records if r["section"] == "8.3 OCR variants"]
    var_rows = [
        [r["record_id"], r["address"], r["role"], short(r["docx_context"], 110), short(r["source_evidence"], 45)]
        for r in var
    ]
    add_table(
        doc,
        ["ID", "Address", "Variant type", "Why it is not a wallet", "Source"],
        var_rows,
        [0.6, 2.4, 1.4, 4.0, 1.9],
        "A61C00",
        font_size=7.2,
        row_height=0.4,
    )

    add_heading(doc, "8.4 Non-crypto UPI identifiers (2)", 2)
    upi = [r for r in records if r["section"] == "8.4 Non-crypto UPI identifiers"]
    upi_rows = [
        [r["record_id"], r["address"], "UPI Payment Gateway — VIVEK KUMAR (HDFC Bank)", short(r["source_evidence"], 45), "Not a crypto wallet"]
        for r in upi
    ]
    add_table(
        doc,
        ["ID", "UPI ID", "Reported role", "Source", "Status"],
        upi_rows,
        [0.6, 3.2, 3.0, 2.2, 1.5],
        "A61C00",
        font_size=7.2,
        row_height=0.4,
    )

    add_heading(doc, "8.5 Partial / truncated (1)", 2)
    pt = [r for r in records if r["section"] == "8.5 Partial / truncated"]
    pt_rows = [
        [r["record_id"], r["address"], short(r["docx_context"], 120), short(r["source_evidence"], 55), "Resolve via tx 4e1ca1ebe997f433.. on Tronscan"]
        for r in pt
    ]
    add_table(
        doc,
        ["ID", "Partial string", "Context", "Evidence", "Action"],
        pt_rows,
        [0.6, 2.4, 3.4, 2.2, 1.8],
        "A61C00",
        font_size=7.2,
        row_height=0.45,
    )

    # Section 9 live-only.
    doc.add_page_break()
    add_heading(doc, "9. Live-only unverified — 109 valid TRON addresses", 1)
    p = doc.add_paragraph()
    run = p.add_run(
        "These 109 checksum-valid addresses are in the live wallet_hits feed but not in the "
        "21-09 document, the 92-row census, or quarantine. They are not curated and not in the "
        "criminal master. Some may be bettor/user wallets; do not assume criminal ownership. "
        "Four are high priority (10+ hits), 25 medium (3-9), and 80 low (1-2)."
    )
    run.font.size = Pt(9)
    live = [r for r in records if r["section"] == "9. Live-only unverified"]
    live_rows = []
    for i, r in enumerate(live, start=1):
        note = r["priority"].title()
        if "re-OCR" in r["notes"].lower() or "re-ocr" in r["notes"].lower():
            note += " — re-OCR candidate"
        live_rows.append(
            [
                str(i), r["address"], str(r["live_hits"]), r["live_kinds"],
                short(r["live_first"], 10), short(r["live_last"], 10), note,
                short(r["source_evidence"], 42),
            ]
        )
    add_table(
        doc,
        ["#", "Address", "Hits", "Kind", "First", "Last", "Priority / note", "Evidence sample"],
        live_rows,
        [0.35, 2.2, 0.4, 0.8, 0.7, 0.7, 2.0, 3.0],
        "1F4E79",
        font_size=6.6,
        row_height=0.26,
    )
    p = doc.add_paragraph()
    run = p.add_run(
        "Two live-only addresses are valid case-variants of checksum-invalid census OCR rows: "
        "TGhiEsAtLfH2cawDa7Ru7LWXnG7tWjbWyS (same deposit evidence) and "
        "TX1HkKxnsHNPzHdhUETZXJp6NBozdVN9kq (different withdrawal evidence). They are marked "
        "as re-OCR candidates, not promotions."
    )
    run.font.size = Pt(8)

    # Section 10.
    doc.add_page_break()
    add_heading(doc, "10. Completeness, changes, and limitations", 1)
    add_heading(doc, "10.1 Completeness checks", 2)
    checks = [
        ["Document tokens", str(stats["docx_records"]), "120", "PASS" if stats["docx_records"] == 120 else "FAIL"],
        ["Document unique addresses", str(stats["docx_unique"]), "120", "PASS" if stats["docx_unique"] == 120 else "FAIL"],
        ["Live-only records", str(stats["live_only"]), "109", "PASS" if stats["live_only"] == 109 else "FAIL"],
        ["Live-only unique addresses", str(stats["live_unique"]), "109", "PASS" if stats["live_unique"] == 109 else "FAIL"],
        ["Docx/live-only intersection", str(stats["intersection"]), "0", "PASS" if stats["intersection"] == 0 else "FAIL"],
        ["Duplicate addresses", str(len(stats["duplicates"])), "0", "PASS" if not stats["duplicates"] else "FAIL"],
        ["Missing audit tokens", str(len(stats["missing_audit"])), "0", "PASS" if not stats["missing_audit"] else "FAIL"],
        ["Total records", str(stats["total"]), "229", "PASS" if stats["total"] == 229 else "FAIL"],
        ["Valid wallets", str(stats["valid_wallets"]), "191", "PASS" if stats["valid_wallets"] == 191 else "FAIL"],
    ]
    add_table(
        doc,
        ["Check", "Result", "Expected", "Status"],
        checks,
        [3.4, 1.2, 1.2, 1.2],
        "1F3864",
        font_size=8.5,
        row_height=0.28,
    )
    add_heading(doc, "10.2 New document additions A1-A5", 2)
    add_rows = [
        ["A1", "TCLgK89AnXbC9rewvhNb9UgXCc2qJJpBXh", "deposit/1789365938958.jpg", "Exchange context — Binance Withdraw 10; not operator"],
        ["A2", "THM4uT2VxzSsBEp8WRbYMzRkiqvbEuBBLU", "deposit/1789387030596.jpg", "Unresolved evidence — valid TRON, attribution unknown"],
        ["A3", "TFkz4usfWmLHu78zENZU63Znqnmh267B25", "withdrawal/1789394882913.webp", "Unresolved evidence — valid TRON, attribution unknown"],
        ["A4", "TFv1isf5EXpXnu2NPpGSwTgipckNvsmEvz", "deposit/1789463619457.jpg", "Unresolved evidence — valid TRON; 5 hits, latest 2026-10-24"],
        ["A5", "TCgcGDFVmeqgxXvsUSZAUMkdLcfovc2aVm", "withdrawal/1789482635019.webp", "Unresolved evidence — valid TRON, attribution unknown"],
    ]
    add_table(
        doc,
        ["ID", "Address", "Evidence", "Disposition"],
        add_rows,
        [0.5, 2.6, 3.1, 4.2],
        "1F3864",
        font_size=7.4,
        row_height=0.32,
    )
    p = doc.add_paragraph()
    run = p.add_run(
        "A1 is exchange infrastructure. A2-A5 are evidence-only and are not asserted criminal. "
        "None of the five is in the five-row operator master."
    )
    run.font.size = Pt(8)

    add_heading(doc, "10.3 What changed vs 21-09-2026", 2)
    add_bullets(
        doc,
        [
            "All 120 tokens from the 21-09 document are still present. No old wallet was removed.",
            "The five new document additions A1-A5 are included: A1 is exchange infrastructure; A2-A5 are evidence-only and unresolved. None is asserted criminal.",
            "The 109 checksum-valid live-only addresses are added as a separate unverified appendix. None is in the criminal master.",
            "TAVaw…dtrig… is quarantined as an OCR variant of canonical B6 TAVaw…dtr1g…; B6 is included as operator-side census-only.",
            "The nine checksum-invalid census rows are flagged as OCR leads, not wallets. D2/D3 are marked fact/high but cannot exist on-chain; correct to OCR leads.",
            "The five-row operator master is unchanged: B1-B5. B6 is not added to it.",
            "The 24 platform-hot strings, C12, and the two UPI IDs remain non-wallets; they are retained for re-OCR/data preservation.",
        ],
    )
    add_heading(doc, "10.4 Limitations", 2)
    add_bullets(
        doc,
        [
            "Criminal-side attribution is an intelligence assessment, not a legal finding. The source report's person/entity attributions are preserved but not independently re-verified in this document.",
            "QR and dashboard rails are OCR-derived and low/medium confidence. Re-OCR and transaction tracing are required before promotion.",
            "Some S3 evidence objects are deleted; the original keys and the deletion status are preserved. Re-OCR must use surviving local copies or version restore where available.",
            "The 109 live-only addresses are valid on-chain but have no curated side/attribution. They may include bettor/user wallets; they are not criminal-side merely because they appear in the feed.",
            "The 24 platform-hot strings cannot exist on-chain. They are not wallets and must not be used in tracing.",
        ],
    )

    # Appendix A.
    doc.add_page_break()
    add_heading(doc, "Appendix A. Workbook and CSV reference", 1)
    add_bullets(
        doc,
        [
            "DOCX: reference/CBTF_Crypto_Wallets_Criminal_Side_Master_2026-10-26.docx",
            "XLSX: reference/CBTF_Crypto_Wallets_Criminal_Side_Master_2026-10-26.xlsx",
            "Full 229-row CSV: reference/WALLET-MASTER-2026-10-26.csv",
            "Criminal-side 62-row CSV: reference/CRIMINAL-SIDE-WALLETS-2026-10-26.csv",
            "Original audit: reference/WALLET-AUDIT-2026-09-21.csv",
            "Newly-found list: reference/WALLET-NEWLY-FOUND-2026-10-26.csv",
        ],
    )
    p = doc.add_paragraph()
    run = p.add_run(
        "The XLSX has separate sheets for Criminal Core, Criminal Candidates, Excluded Bettor, "
        "Exchange Context, Unresolved, Invalid Quarantine, Live Only, All Records, and Reconciliation. "
        "Use the All Records sheet for a single-table lookup of all 229 records."
    )
    run.font.size = Pt(9)

    # Appendix B.
    add_heading(doc, "Appendix B. Confidence and status definitions", 1)
    defs = [
        ["very_high / high", "Source-report fact with specific attribution; still not a legal finding."],
        ["medium", "Valid evidence but attribution or side is not fully established."],
        ["low-medium", "QR/dashboard/OCR lead; needs corroboration."],
        ["low", "Live-dashboard/OCR lead; weakest evidentiary class."],
        ["fact", "Source-report assertion. It does not mean checksum-valid or court-verified."],
        ["ai_lead", "AI/OCR-derived lead; re-OCR required."],
        ["CORE", "Asserted criminal-side operator or hawala/collection."],
        ["CANDIDATE", "Likely criminal-side infrastructure; not core."],
        ["EXCLUDED", "Bettor/user-side or dust; not criminal master."],
        ["CONTEXT", "Exchange infrastructure; not criminal attribution."],
        ["UNRESOLVED", "Valid wallet, side/attribution unknown."],
        ["INVALID", "Checksum-invalid, OCR variant, partial, or non-wallet."],
        ["UNVERIFIED", "Live-only valid address; not curated."],
    ]
    add_table(
        doc,
        ["Label", "Meaning"],
        defs,
        [2.2, 8.0],
        "1F3864",
        font_size=8.2,
        row_height=0.3,
    )

    doc.save(path)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--docx", type=Path, default=DOCX_DEFAULT)
    parser.add_argument("--skip-docx", action="store_true")
    args = parser.parse_args()

    census = read_csv(REF / "identified-wallets.csv")
    quarantine = read_csv(REF / "quarantined-wallets.csv")
    audit_rows = read_csv(REF / "WALLET-AUDIT-2026-09-21.csv")
    new_rows = read_csv(REF / "WALLET-NEWLY-FOUND-2026-10-26.csv")
    docx_rows = {} if args.skip_docx else load_docx_tables(args.docx)
    live_kinds = load_live_kinds()

    records = build_records(census, quarantine, audit_rows, new_rows, docx_rows, live_kinds)
    stats = validate(records, census, quarantine)

    print("Validation:")
    for key in [
        "docx_records", "live_only", "total", "docx_unique", "live_unique",
        "intersection", "valid_wallets", "valid_tron", "valid_eth",
        "criminal_core", "criminal_candidates", "bettor_excluded",
        "exchange_context", "unresolved", "invalid",
    ]:
        print(f"  {key}: {stats[key]}")
    print("  duplicates:", stats["duplicates"])
    print("  missing_audit:", stats["missing_audit"])
    print("  counts:", stats["counts"])

    if stats["docx_records"] != 120 or stats["live_only"] != 109 or stats["total"] != 229:
        raise SystemExit("Count validation failed; refusing to write outputs.")
    if stats["duplicates"] or stats["missing_audit"] or stats["intersection"]:
        raise SystemExit("Duplicate/missing/intersection validation failed; refusing to write outputs.")

    write_csv(OUT_MASTER_CSV, records)
    criminal = [
        r for r in records
        if r["section"] in {
            "3.1 Operator wallets", "3.2 Hawala / collection",
            "4.1 Banking QR rails", "4.2 Dashboard / platform rails", "4.3 Desk / support",
        }
    ]
    write_csv(OUT_CRIMINAL_CSV, criminal)
    write_xlsx(OUT_XLSX, records, stats)
    write_docx(OUT_DOCX, records, stats)
    print(f"\nWrote:\n  {OUT_DOCX}\n  {OUT_XLSX}\n  {OUT_MASTER_CSV}\n  {OUT_CRIMINAL_CSV}")
    print(f"  criminal-side rows: {len(criminal)}")


if __name__ == "__main__":
    main()
