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

TRUTH GATE 2026-09-17: base58-invalid TRON strings and non-address
payloads are NEVER census rows. Quarantined (excluded from the census,
preserved with reasons in quarantined-wallets.csv):
  24 platform-hot strings — every one contains TRON-forbidden 0/O/I/l,
    attributed only by uncorroborated cached_extract to the deleted
    deposit/1780826098942.json (content unrecoverable without AWS
    version restore); they cannot exist on-chain.
  C12 TEeqmf variant — digit 0 makes it base58-invalid; chat-OCR variant
    of the confirmed TEeqmfN74hg8vP8umv8ubD3oGAroVwRTFD row (retained).
  2 upi_gateway TQD numeric strings — UPI transaction/reference IDs,
    not wallet addresses; source qr-decode-sample.txt permanently deleted.
Post-gate census: 72 - 26 + 42 - 1 = 87 rows.

2026-09-21 ADDITIONS: the CBTF_Crypto_Wallets_Updated-21-09-26.docx
table 11 adds 5 checksum-valid TRON addresses (A1-A5) that were already
live-decoded in the bucket. They are evidence-only rows, not operator
attributions (A1 is a Binance withdrawal wallet; A2-A5 need transaction
corroboration), so they are appended here but never enter the LE master
`17-CRYPTO-WALLETS.csv`. Census after additions: 87 + 5 = 92 rows.

2026-09-21 FLAG: the same document flags
`TAVawJ7BF7L9XwSf5RJdtrigHSyVTzzCxf` as an OCR near-duplicate of B6
(`...dtr1g...`). It is base58-checksum-invalid and is not a separate
wallet; it is quarantined (not census, not operator master). Quarantine
after this addition: 27 + 1 = 28 rows. The canonical B6
`TAVawJ7BF7L9XwSf5RJdtr1gHSyVTzzCxf` remains in the census but is not in
the 5-row LE operator master.

Output: s3-live-monitor/reference/identified-wallets.csv
         s3-live-monitor/reference/quarantined-wallets.csv
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
# Truth gate: full-string strict match. TRON base58 excludes 0, O, I, l —
# any T-string containing them cannot exist on-chain, however it was labeled.
TRON_STRICT = re.compile(r"^T[1-9A-HJ-NP-Za-km-z]{33}$")
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

# 2026-09-21 docx table 11 A1-A5. These are not in the roll-data extras and
# are not LE operator wallets; they are evidence-backed additions.
ADDITIONAL_2026_09_21 = (
    {
        "address": "TCLgK89AnXbC9rewvhNb9UgXCc2qJJpBXh",
        "network": "TRON",
        "category": "exchange",
        "attribution": "",
        "role": "BINANCE_WITHDRAW_10 (public explorer label; exchange withdrawal wallet, not operator)",
        "confidence": "high",
        "evidence_type": "deposit_evidence + tronscan_label",
        "source_ref": "deposit/1789365938958.jpg",
        "proof_s3_key": "deposit/1789365938958.jpg",
        "proof_url": "https://bx-s3-dev-001.s3.ap-southeast-1.amazonaws.com/deposit/1789365938958.jpg",
        "fact_type": "fact",
    },
    {
        "address": "THM4uT2VxzSsBEp8WRbYMzRkiqvbEuBBLU",
        "network": "TRON",
        "category": "evidence_only",
        "attribution": "",
        "role": "DEPOSIT_EVIDENCE_UNATTRIBUTED",
        "confidence": "medium",
        "evidence_type": "deposit_evidence",
        "source_ref": "deposit/1789387030596.jpg",
        "proof_s3_key": "deposit/1789387030596.jpg",
        "proof_url": "https://bx-s3-dev-001.s3.ap-southeast-1.amazonaws.com/deposit/1789387030596.jpg",
        "fact_type": "fact",
    },
    {
        "address": "TFkz4usfWmLHu78zENZU63Znqnmh267B25",
        "network": "TRON",
        "category": "evidence_only",
        "attribution": "",
        "role": "WITHDRAWAL_EVIDENCE_UNATTRIBUTED",
        "confidence": "medium",
        "evidence_type": "withdrawal_evidence",
        "source_ref": "withdrawal/1789394882913.webp",
        "proof_s3_key": "withdrawal/1789394882913.webp",
        "proof_url": "https://bx-s3-dev-001.s3.ap-southeast-1.amazonaws.com/withdrawal/1789394882913.webp",
        "fact_type": "fact",
    },
    {
        "address": "TFv1isf5EXpXnu2NPpGSwTgipckNvsmEvz",
        "network": "TRON",
        "category": "evidence_only",
        "attribution": "",
        "role": "DEPOSIT_EVIDENCE_UNATTRIBUTED",
        "confidence": "medium",
        "evidence_type": "deposit_evidence",
        "source_ref": "deposit/1789463619457.jpg",
        "proof_s3_key": "deposit/1789463619457.jpg",
        "proof_url": "https://bx-s3-dev-001.s3.ap-southeast-1.amazonaws.com/deposit/1789463619457.jpg",
        "fact_type": "fact",
    },
    {
        "address": "TCgcGDFVmeqgxXvsUSZAUMkdLcfovc2aVm",
        "network": "TRON",
        "category": "evidence_only",
        "attribution": "",
        "role": "WITHDRAWAL_EVIDENCE_UNATTRIBUTED",
        "confidence": "medium",
        "evidence_type": "withdrawal_evidence",
        "source_ref": "withdrawal/1789482635019.webp",
        "proof_s3_key": "withdrawal/1789482635019.webp",
        "proof_url": "https://bx-s3-dev-001.s3.ap-southeast-1.amazonaws.com/withdrawal/1789482635019.webp",
        "fact_type": "fact",
    },
)

# 2026-09-21 docx table 8 FLAG. The document itself says this is an OCR
# near-duplicate of B6, not a separate wallet, pending image re-verification.
# It has the right base58 alphabet and length but fails the TRON checksum.
# Keep it out of the census and preserve it with an explicit reason.
ADDITIONAL_QUARANTINE_2026_09_21 = (
    {
        "address": "TAVawJ7BF7L9XwSf5RJdtrigHSyVTzzCxf",
        "network": "TRON",
        "category": "operator_hot_variant",
        "quarantine_reason": (
            "base58-checksum-invalid OCR near-duplicate of canonical B6 "
            "TAVawJ7BF7L9XwSf5RJdtr1gHSyVTzzCxf (…dtrig… vs …dtr1g…); "
            "the source document flags it as NOT a separate wallet until "
            "the image is re-verified; cannot exist on-chain"
        ),
        "source_ref": "chat_file/gggdeepsify_1769499035441_usdtplayer_deposit.jpg",
        "proof_s3_key": "",
        "fact_type": "ai_lead",
        "quarantined_on": "2026-10-25",
    },
)

# Source-key rule 2026-09-17: the ONLY valid source is the true original
# TRUE object metadata recovered 2026-09-17 from the 2026-07-04 bucket
# inventory (predates all deletions) + 2026-07-05 download log. Corrects the
# "~34 KB" size claim in 19-REPORT (files 1&2 were 38,436 / 41,646 bytes).
TRUE_META_1780826098942 = (
    "true object 38,436 B, mtime 2026-06-07T09:54:59Z, etag 4fcc87b0 "
    "(07-04 inventory); last local copy 2026-07-05 old-machine raw/deposit "
    "(not migrated — check old disk first)"
)
# object key in the GCS bucket (synced from S3). Investigator-created files
# (OCR text, bundle copies, chat notes) are NEVER sources — rows whose
# object is gone keep their true key and are badged deleted, never relinked.
BANKDATA_RE = re.compile(r"bank_data_(\d{10,})")


def main() -> None:
    rows: dict[str, dict] = {}
    quarantined: list[dict] = []

    def quarantine(
        addr,
        net,
        cat,
        reason,
        source_ref,
        proof_key,
        fact_type,
        quarantined_on="2026-09-17",
    ):
        quarantined.append(
            {
                "address": addr,
                "network": net,
                "category": cat,
                "quarantine_reason": reason,
                "source_ref": source_ref,
                "proof_s3_key": proof_key,
                "fact_type": fact_type,
                "quarantined_on": quarantined_on,
            }
        )

    # ---- base: 36-CSV (72) ----
    p36 = REPO / "LE-SUBMISSION-MASTER" / "36-ALL-IDENTIFIED-WALLETS.csv"
    with open(p36, encoding="utf-8", errors="ignore", newline="") as f:
        for r in csv.DictReader(f):
            addr = (r.get("wallet_address") or "").strip()
            if not addr or addr in rows:
                continue
            cat = (r.get("category") or "").strip()
            net = "UPI" if cat == "upi_gateway" else "TRON"
            src = (r.get("source_s3_file") or "").strip()
            if net == "TRON" and not TRON_STRICT.match(addr):
                # 2026-09-17 truth gate: base58-invalid strings are not
                # addresses and never enter the census, whatever the
                # investigator label claims.
                reason = (
                    "base58-invalid TRON string (contains 0/O/I/l); "
                    "cannot exist on-chain"
                )
                if cat == "platform_hot_wallet":
                    reason += (
                        "; attributed only by uncorroborated cached_extract "
                        "to deleted deposit/1780826098942.json "
                        "(content unrecoverable without AWS version restore; "
                        + TRUE_META_1780826098942
                        + ")"
                    )
                quarantine(addr, net, cat, reason, src, "", "ai_lead")
                continue
            if cat == "upi_gateway" and not TRON_STRICT.match(addr):
                # TQD numeric strings: UPI transaction/reference IDs, not
                # wallet addresses; source qr-decode-sample.txt is gone
                # (permanent NoSuchKey, never versioned).
                quarantine(
                    addr,
                    net,
                    cat,
                    "UPI transaction/reference ID, not a wallet address; "
                    "source file permanently deleted (NoSuchKey)",
                    src,
                    "",
                    "ai_lead",
                )
                continue
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

    # ---- key corrections (2026-09-17, all verified LIVE against the bucket):
    # the proof map drops prefixes/extensions on some rows. The true source
    # is always the full original object key.
    #   B1 nitrogen: bare filename -> chat_file/ prefix (per its own S3 URL)
    #   B4 TD63G: map key lacks the real .png extension
    #   B5 TJ5yq: note-only citation -> KuCoin screenshot whose held OCR
    #             reads this address (both 1757641435813 + 1757643087528 do;
    #             primary = 1757641435813, mirror-verified present)
    for addr, row in rows.items():
        pk = (row.get("proof_s3_key") or "").strip()
        url = (row.get("proof_url") or "").strip()
        if pk and "/" not in pk and "/chat_file/" in url:
            row["proof_s3_key"] = "chat_file/" + pk
            pk = row["proof_s3_key"]
    if "TD63G1qvWu7ke2ycW848BjMCj2sHEZZusa" in rows:
        rows["TD63G1qvWu7ke2ycW848BjMCj2sHEZZusa"]["proof_s3_key"] = (
            "chat_file/07081990_1766755679252_Screenshot_20251226-185751.Binance.png"
        )
    if "TJ5yq6udNUQ5iQNaapbN5f5YMUvRJKP17B" in rows:
        rows["TJ5yq6udNUQ5iQNaapbN5f5YMUvRJKP17B"]["proof_s3_key"] = (
            "chat_file/inad_1757641435813_Screenshot_20250912-043626_KuCoin.png"
        )

    # ---- self-key rule (2026-09-17): source_refs that are already a single
    # bucket path (e.g. deposit/1780826098942.json) become the proof key when
    # the proof map has no entry — same rule the extras section uses below.
    for addr, row in rows.items():
        src = (row.get("source_ref") or "").strip()
        if not row.get("proof_s3_key") and "/" in src and " " not in src:
            row["proof_s3_key"] = src

    # ---- bank_data resolution (2026-09-17): hawala source_refs carry bare
    # file IDs (bank_data_1777489496421, sometimes comma-separated). The full
    # keys are csv-files/data_<id>.xlsx per 37-ALL-WALLETS-S3-SOURCE-TRACE —
    # verified LIVE in the GCS mirror — so they become proof keys and the
    # dashboard gets view-proof instead of "case file".
    for addr, row in rows.items():
        if row.get("proof_s3_key"):
            continue
        ids = BANKDATA_RE.findall(row.get("source_ref") or "")
        if ids:
            row["proof_s3_key"] = f"csv-files/data_{ids[0]}.xlsx"

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
    txt = (REPO / "docs" / "reference" / "crypto-wallet-roll-data.txt").read_text(
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
            "chat_file/anshul_1769687598637_Screenshot_20260129_172259_Trust_Wallet.jpg",
            "fact",
        ),
        "X2": (
            "dust_uncertain",
            "TRON",
            "UNCERTAIN dust To — NOT betting rail",
            "medium",
            "chat_evidence",
            "chat_file/anshul_1769687598637_Screenshot_20260129_172259_Trust_Wallet.jpg",
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
        if tag == "C12":
            # 2026-09-17 truth gate: the variant string is base58-invalid
            # (digit 0) so it cannot exist on-chain — quarantine it, keep
            # the confirmed canonical row. Cross-ref preserved here.
            quarantine(
                addr,
                net,
                cat,
                "base58-invalid (digit 0); chat-OCR variant of confirmed "
                "TEeqmfN74hg8vP8umv8ubD3oGAroVwRTFD (retained in census)",
                src,
                src,
                "ai_lead",
            )
            continue
        if net == "TRON" and tag != "P1" and not TRON_STRICT.match(addr):
            # Any other strict-invalid extra: quarantine, never census.
            quarantine(
                addr,
                net,
                cat,
                "base58-invalid TRON string (contains 0/O/I/l); cannot exist on-chain",
                src,
                "",
                "ai_lead",
            )
            continue
        proof_key = rkeys.get(tag, src if "/" in src and " " not in src else "")
        if tag == "C12":
            # pavan chat image key contains spaces (WhatsApp filename) but is
            # a real bucket key — keep verbatim; mirror presence decides proof.
            proof_key = src
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

    # ---- 2026-09-21 docx table 11 A1-A5 ----
    for addition in ADDITIONAL_2026_09_21:
        addr = addition["address"]
        if addr in rows:
            continue
        if not TRON_STRICT.match(addr):
            quarantine(
                addr,
                "TRON",
                addition["category"],
                "base58-invalid TRON string (contains 0/O/I/l); cannot exist on-chain",
                addition["source_ref"],
                addition["proof_s3_key"],
                addition["fact_type"],
            )
            continue
        rows[addr] = dict(addition)

    # ---- 2026-09-21 docx table 8 FLAG (not a wallet) ----
    for addition in ADDITIONAL_QUARANTINE_2026_09_21:
        addr = addition["address"]
        if addr in rows or any(q["address"] == addr for q in quarantined):
            continue
        quarantine(
            addr,
            addition["network"],
            addition["category"],
            addition["quarantine_reason"],
            addition["source_ref"],
            addition["proof_s3_key"],
            addition["fact_type"],
            addition["quarantined_on"],
        )

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
    if len(rows) != 92:
        print(f"WARN: expected 92 rows, got {len(rows)}", file=sys.stderr)
    # ---- quarantine file: excluded false addresses, fully preserved ----
    qout = OUT.parent / "quarantined-wallets.csv"
    qcols = (
        "address",
        "network",
        "category",
        "quarantine_reason",
        "source_ref",
        "proof_s3_key",
        "fact_type",
        "quarantined_on",
    )
    with open(qout, "w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=qcols)
        w.writeheader()
        for q in sorted(quarantined, key=lambda d: d["address"]):
            w.writerow(q)
    qh = hashlib.sha256(qout.read_bytes()).hexdigest()
    with open(str(qout) + ".sha256", "w") as f:
        f.write(f"{qh}  {qout.name}\n")
    print(f"wrote {qout} rows={len(quarantined)} sha256={qh[:16]}")
    if len(quarantined) != 28:
        print(
            f"WARN: expected 28 quarantined rows, got {len(quarantined)}",
            file=sys.stderr,
        )


if __name__ == "__main__":
    main()
