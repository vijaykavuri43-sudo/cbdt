#!/usr/bin/env python3
"""Build the canonical identified-wallet reference list for the Crypto tab.

Union of every previously-identified wallet (no bucket re-decode needed):
  base   LE-SUBMISSION-MASTER/36-ALL-IDENTIFIED-WALLETS.csv (72 unique:
         28 hawala + 24 platform-hot + 8 withdrawal-beneficiary + 6 operator
         + 3 desk + 2 UPI-gateway + 1 exchange) with rich metadata
  proof  intelligence/exports/wallets-2026-09-04/WALLET-PROOF-MAP.csv
         (39 rows: per-wallet S3 key + S3 URL + tx detail) left-joined
  label  intelligence/exports/wallets-2026-09-04/ALL-WALLETS-MASTER.csv
         (party/label/direction/evidence_note) left-joined
  extra  crypto-wallet-roll-data.txt sections 7/10/11/12/13 (42 strings:
         C12 TEeqmf variant + U1/U2/X1/X2/V1/V2/P1 + N1-7 + Q1-20 + R1-7)
         72 + 42 = 114 total (matches roll-data section 14)

Output: s3-live-monitor/reference/identified-wallets.csv
Rerun after any census update, then redeploy (docker rebuilds frontend;
backend reads the CSV at request time).

CASE-2026-102 + CASE-2026-CBTF | STANDING-JUDICIAL-AUTH-ILLEGAL-BETTING
fact vs ai_lead per row: file/screenshot-backed = fact; OCR-only rails
(N/Q/R), ETH-uncertain, partial = ai_lead. Attribution labels are report
attributions, NOT proof of ownership/control (see roll-data section 4).
"""

import csv
import hashlib
import re
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
OUT = Path(__file__).resolve().parents[1] / "reference" / "identified-wallets.csv"

TRON = re.compile(r"\bT[1-9A-HJ-NP-Za-km-z]{33}\b")
ETH = re.compile(r"\b0x[0-9a-fA-F]{40}\b")
PARTIAL = "TTmepj9RqbfuzTRkuLuQ9vAX4..iEKKeXL"

COLUMNS = (
    "address",
    "network",
    "category",
    "attribution",
    "role",
    "confidence",
    "evidence_type",
    "source_ref",
    "proof_s3_key",
    "proof_url",
    "fact_type",
)


def main() -> None:
    rows: dict[str, dict] = {}

    # ---- base: 36-CSV (72) ----
    p36 = REPO / "LE-SUBMISSION-MASTER" / "36-ALL-IDENTIFIED-WALLETS.csv"
    with open(p36, encoding="utf-8", errors="ignore", newline="") as f:
        for r in csv.DictReader(f):
            addr = (r.get("wallet_address") or "").strip()
            if not addr or addr in rows:
                continue
            cat = (r.get("category") or "").strip()
            net = "UPI" if cat == "upi_gateway" else "TRON"
            rows[addr] = {
                "address": addr,
                "network": net,
                "category": cat,
                "attribution": (r.get("operator_name") or "").strip(),
                "role": (r.get("role") or "").strip(),
                "confidence": (r.get("confidence") or "").strip(),
                "evidence_type": (r.get("evidence_type") or "").strip(),
                "source_ref": (r.get("source_s3_file") or "").strip(),
                "proof_s3_key": "",
                "proof_url": "",
                "fact_type": "fact",
            }

    # ---- proof-map join (S3 key + URL per wallet) ----
    pmap = (
        REPO
        / "intelligence"
        / "exports"
        / "wallets-2026-09-04"
        / "WALLET-PROOF-MAP.csv"
    )
    if pmap.is_file():
        with open(pmap, encoding="utf-8", errors="ignore", newline="") as f:
            for r in csv.DictReader(f):
                addr = (r.get("wallet_address") or "").strip()
                if addr in rows and not rows[addr]["proof_s3_key"]:
                    rows[addr]["proof_s3_key"] = (r.get("s3_key") or "").strip()
                    rows[addr]["proof_url"] = (r.get("s3_url") or "").strip()

    # ---- master-label join (role label top-up only, never overwrite) ----
    mlab = (
        REPO
        / "intelligence"
        / "exports"
        / "wallets-2026-09-04"
        / "ALL-WALLETS-MASTER.csv"
    )
    if mlab.is_file():
        with open(mlab, encoding="utf-8", errors="ignore", newline="") as f:
            for r in csv.DictReader(f):
                addr = (r.get("address") or "").strip()
                if addr in rows:
                    if not rows[addr]["role"]:
                        rows[addr]["role"] = (r.get("label") or "").strip()

    # ---- extras: roll-data sections 7/10/11/12/13 ----
    txt = (REPO / "crypto-wallet-roll-data.txt").read_text(
        encoding="utf-8", errors="ignore"
    )
    meta = {
        # tag: (category, network, role, confidence, evidence, source, fact)
        "C12": (
            "user_withdrawal",
            "TRON",
            "CR7 362 USDT beneficiary 2026-05-17 (TEeqmf variant)",
            "medium",
            "chat_evidence",
            "chat_file/pavan_1779241954065_WhatsApp_Image 2026-05-20 at 00.30.59.jpeg",
            "fact",
        ),
        "U1": (
            "user_wallet",
            "TRON",
            "USER deposit sender (10 USDT into B6)",
            "high",
            "chat_evidence",
            "chat_file/gggdeepsify_1769499035441_usdtplayer_deposit.jpg",
            "fact",
        ),
        "U2": (
            "user_wallet",
            "TRON",
            "USER receive-screen (Trust Wallet USDT TRC20)",
            "high",
            "chat_evidence",
            "chat_file/khiladisher_1779884651213_Trust_Wallet.jpg",
            "fact",
        ),
        "X1": (
            "dust_uncertain",
            "TRON",
            "UNCERTAIN dust From — NOT betting rail",
            "medium",
            "chat_evidence",
            "chat_file/anshul_1769687598637_Trust_Wallet.jpg",
            "fact",
        ),
        "X2": (
            "dust_uncertain",
            "TRON",
            "UNCERTAIN dust To — NOT betting rail",
            "medium",
            "chat_evidence",
            "chat_file/anshul_1769687598637_Trust_Wallet.jpg",
            "fact",
        ),
        "V1": (
            "eth_uncertain",
            "ETH",
            "ETH likely operator-or-user receive",
            "medium",
            "chat_evidence",
            "rajvir47 Binance chats 2026-01-24",
            "ai_lead",
        ),
        "V2": (
            "eth_uncertain",
            "ETH",
            "ETH likely operator-or-user receive",
            "medium",
            "chat_evidence",
            "rajvir47 Binance chats 2026-01-24",
            "ai_lead",
        ),
        "P1": (
            "partial",
            "TRON",
            "PARTIAL To (truncated) — resolve via tx 4e1ca1ebe997f433",
            "medium",
            "deposit_slip",
            "deposit/1780661593981.jpg",
            "ai_lead",
        ),
    }
    for n in range(1, 8):
        meta[f"N{n}"] = (
            "dashboard_ocr",
            "TRON",
            "dashboard OCR rail (deposit/withdrawal/chat-image OCR)",
            "low",
            "dashboard_ocr",
            "dashboard /api/wallets 2026-09-14",
            "ai_lead",
        )
    for n in range(1, 21):
        meta[f"Q{n}"] = (
            "bankingqr_qr",
            "TRON",
            "bankingqr pure-crypto QR rail (beneficiary)",
            "low-medium",
            "qr_decode",
            "bankingqr/ QR raw_text",
            "ai_lead",
        )
    rkeys = {
        "R1": "deposit/1789205071860.png",
        "R2": "deposit/1789203153005.jpg",
        "R3": "deposit/1789290437058.jpg",
        "R4": "withdrawal/1789293782437.webp",
        "R5": "deposit/1789341005405.png",
        "R6": "deposit/1789298809477.png",
        "R7": "deposit/1789278566779.jpg",
    }
    for n in range(1, 8):
        meta[f"R{n}"] = (
            "live_dashboard",
            "TRON",
            "live-dashboard rail 2026-09-12/14",
            "low",
            "dashboard_ocr",
            f"dashboard /api/wallets + {rkeys[f'R{n}']}",
            "ai_lead",
        )
    for line in txt.splitlines():
        m = re.match(r"^(C12|U\d|X\d|V\d|P1|N\d+|Q\d+|R\d+)\s+(\S+)", line.strip())
        if not m:
            continue
        tag, raw = m.group(1), m.group(2)
        if tag not in meta or tag.startswith("FLAG"):
            continue
        addr = (
            raw
            # P1 is truncated with ".."; C12 variant contains digit 0
            # (OCR-variant string, not strict base58) — keep verbatim.
            if tag in ("P1", "C12")
            else (TRON.findall(line) + ETH.findall(line) or [None])[0]
        )
        if not addr or addr in rows:
            continue
        cat, net, role, conf, ev, src, ft = meta[tag]
        proof_key = rkeys.get(tag, src if "/" in src and " " not in src else "")
        rows[addr] = {
            "address": addr,
            "network": net,
            "category": cat,
            "attribution": "",
            "role": role,
            "confidence": conf,
            "evidence_type": ev,
            "source_ref": src,
            "proof_s3_key": proof_key,
            "proof_url": "",
            "fact_type": ft,
        }

    OUT.parent.mkdir(parents=True, exist_ok=True)
    with open(OUT, "w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=COLUMNS)
        w.writeheader()
        for addr in sorted(rows):
            w.writerow(rows[addr])
    h = hashlib.sha256(OUT.read_bytes()).hexdigest()
    with open(str(OUT) + ".sha256", "w") as f:
        f.write(f"{h}  {OUT.name}\n")
    print(f"wrote {OUT} rows={len(rows)} sha256={h[:16]}")
    if len(rows) != 114:
        print(f"WARN: expected 114 rows, got {len(rows)}", file=sys.stderr)


if __name__ == "__main__":
    main()
