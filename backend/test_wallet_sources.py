"""Reference-source coverage: strict true-source rule for the Crypto tab.

2026-09-17: the ONLY valid source is the true original object key in the GCS
bucket (synced from S3: deposit / bankingqr / chat_file / voice-note audio /
csv-files / qrcode / payment). Investigator-created files (OCR text, notes,
bundles) are NEVER sources. Rows whose object was deleted at the source keep
their true key and are badged deleted — never relinked.

These tests pin: (1) the self-key rule, (2) bank_data ID resolution to full
csv-files/ keys, (3) the verified key corrections (anshul full key, nitrogen
prefix, TD63G .png, TJ5yq inad screenshot), (4) no investigator files as
sources, (5) no fallback columns, (6) the 2026-09-17 truth gate: no
base58-invalid TRON strings and no non-address payloads in the census
(28 quarantined with reasons, 92-row census: 87 original + 5 evidence-only
additions from the 2026-09-21 CBTF wallet document; the document's flagged
TAVaw OCR variant is quarantined, not counted as a wallet).
"""

import csv
import re
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
REF_CSV = REPO / "s3-live-monitor" / "reference" / "identified-wallets.csv"
QUAR_CSV = REPO / "s3-live-monitor" / "reference" / "quarantined-wallets.csv"

TRON_STRICT = re.compile(r"^T[1-9A-HJ-NP-Za-km-z]{33}$")
# P1 is an openly-truncated partial string (ai_lead/partial), not an address.
PARTIAL = "TTmepj9RqbfuzTRkuLuQ9vAX4..iEKKeXL"

# Original bucket prefixes synced from S3 — the only valid source locations.
ORIGINAL_PREFIXES = (
    "deposit/",
    "withdrawal/",
    "bankingqr/",
    "chat_file/",
    "csv-files/",
    "qrcode/",
    "payment/",
    "paymentgateway/",
    "audio/",
    "uploads/",
)

# Investigator-created artifacts — must NEVER appear as a source key.
NON_SOURCE_MARKERS = (
    "psm",
    "wd-ocr",
    "dep-ocr",
    "chat-proof",
    "case-preserved",
    "LE-SUBMISSION",
    "intelligence/",
    "live proof",
)


def _rows():
    rows = list(csv.DictReader(open(REF_CSV, encoding="utf-8")))
    assert len(rows) == 92, f"census must be 92, got {len(rows)}"
    return rows


def _quarantined():
    rows = list(csv.DictReader(open(QUAR_CSV, encoding="utf-8")))
    assert len(rows) == 28, f"quarantine must be 28, got {len(rows)}"
    return rows


NEW_2026_09_21 = {
    "TCLgK89AnXbC9rewvhNb9UgXCc2qJJpBXh",
    "THM4uT2VxzSsBEp8WRbYMzRkiqvbEuBBLU",
    "TFkz4usfWmLHu78zENZU63Znqnmh267B25",
    "TFv1isf5EXpXnu2NPpGSwTgipckNvsmEvz",
    "TCgcGDFVmeqgxXvsUSZAUMkdLcfovc2aVm",
}


def test_2026_09_21_evidence_wallets_are_in_census_not_operator_master():
    """The 2026-09-21 docx added 5 valid, live-decoded evidence wallets.

    They are evidence-only rows: the document explicitly says A1 is an
    exchange withdrawal wallet (not operator attribution) and A2-A5 need
    transaction-level corroboration. They must be in the census but must
    never acquire the known_operator badge by entering the LE master.
    """
    rows = {r["address"]: r for r in _rows()}
    for addr in NEW_2026_09_21:
        assert addr in rows, f"missing 2026-09-21 wallet {addr}"
        row = rows[addr]
        assert row["network"] == "TRON"
        assert row["proof_s3_key"] == row["source_ref"]
        assert row["fact_type"] == "fact"
    master = {
        r["address"]
        for r in csv.DictReader(
            open(
                REPO / "s3-live-monitor" / "reference" / "17-CRYPTO-WALLETS.csv",
                encoding="utf-8",
            )
        )
    }
    assert NEW_2026_09_21.isdisjoint(master), (
        "evidence-only 2026-09-21 wallets must not be in the operator master"
    )


def test_no_fallback_columns():
    with open(REF_CSV, encoding="utf-8") as f:
        header = f.readline()
    for col in ("preserved_gcs_key", "live_keys", "preserved_original"):
        assert col not in header, f"fallback column {col} must not exist"


def test_self_key_rule_single_path_source_refs_have_proof():
    bad = [
        r["address"]
        for r in _rows()
        if "/" in (r["source_ref"] or "")
        and " " not in (r["source_ref"] or "")
        and not (r["proof_s3_key"] or "").strip()
    ]
    assert bad == [], f"{len(bad)} single-path source_refs still keyless"


def test_deposit_json_strings_quarantined_not_census():
    """The 24 base58-invalid platform-hot strings are NOT wallet rows.

    They live only in quarantined-wallets.csv with deleted-source reasons;
    no census row may cite the deleted deposit JSON as its source.
    """
    rows = _rows()
    dep = [r for r in rows if r["source_ref"] == "deposit/1780826098942.json"]
    assert dep == [], f"{len(dep)} census rows still cite the deleted JSON"
    assert all(r["category"] != "platform_hot_wallet" for r in rows), (
        "platform_hot_wallet category must not appear in the census"
    )
    quar = _quarantined()
    qdep = [
        q
        for q in quar
        if q["source_ref"] == "deposit/1780826098942.json"
        or q["proof_s3_key"] == "deposit/1780826098942.json"
    ]
    assert len(qdep) == 24, (
        f"expected 24 quarantined deposit-JSON rows, got {len(qdep)}"
    )
    assert all(q["category"] == "platform_hot_wallet" for q in qdep)


def test_truth_gate_no_invalid_tron_in_census():
    """Every TRON census row (except the openly-truncated P1 partial) must
    be strict base58 — the live bucket decoders use the same alphabet, so
    anything failing this gate could never be corroborated on-chain."""
    bad = [
        r["address"]
        for r in _rows()
        if r["network"] == "TRON"
        and r["address"] != PARTIAL
        and not TRON_STRICT.match(r["address"])
    ]
    assert bad == [], f"{len(bad)} base58-invalid TRON rows in census: {bad[:5]}"


def test_truth_gate_no_upi_pseudo_addresses():
    """UPI transaction/reference IDs are not wallets. The 2 TQD numeric
    strings are quarantined; no upi_gateway row may remain in the census."""
    rows = _rows()
    assert [r["address"] for r in rows if r["category"] == "upi_gateway"] == []
    quar = _quarantined()
    tqd = [q for q in quar if q["category"] == "upi_gateway"]
    assert len(tqd) == 2, f"expected 2 quarantined UPI rows, got {len(tqd)}"


def test_c12_variant_quarantined_canonical_retained():
    """The base58-invalid C12 OCR variant is quarantined; the confirmed
    canonical TEeqmf address it derives from stays in the census."""
    by_addr = {r["address"]: r for r in _rows()}
    assert "TEeqmfN74hg8vP8umv8ubD30GAroVWRTFD" not in by_addr
    assert "TEeqmfN74hg8vP8umv8ubD3oGAroVwRTFD" in by_addr
    qaddrs = {q["address"] for q in _quarantined()}
    assert "TEeqmfN74hg8vP8umv8ubD30GAroVWRTFD" in qaddrs


def test_tavaw_flag_quarantined_not_census():
    """The 2026-09-21 docx flags a checksum-invalid B6 near-duplicate.

    It has a valid base58 alphabet and length, so the old alphabet-only gate
    would miss it. The document itself says it is not a separate wallet until
    the image is re-verified. It must be preserved in quarantine, never in the
    census or the LE operator master, while canonical B6 remains in the census.
    """
    flag = "TAVawJ7BF7L9XwSf5RJdtrigHSyVTzzCxf"
    canonical = "TAVawJ7BF7L9XwSf5RJdtr1gHSyVTzzCxf"
    census = {r["address"] for r in _rows()}
    quar = {q["address"]: q for q in _quarantined()}
    master = {
        r["address"]
        for r in csv.DictReader(
            open(
                REPO / "s3-live-monitor" / "reference" / "17-CRYPTO-WALLETS.csv",
                encoding="utf-8",
            )
        )
    }
    assert flag not in census
    assert flag not in master
    assert flag in quar
    assert quar[flag]["category"] == "operator_hot_variant"
    assert "checksum-invalid" in quar[flag]["quarantine_reason"]
    assert canonical in census
    assert canonical not in master


def test_bankdata_ids_resolve_to_full_keys():
    rows = _rows()
    haw = [r for r in rows if r["category"] == "hawala_collection"]
    assert len(haw) == 28
    bad = [r["address"] for r in haw if not (r["proof_s3_key"] or "").strip()]
    assert bad == [], f"{len(bad)} hawala rows without any proof key"
    # Double-role rows keep their stronger withdrawal proof; the rest resolve
    # bare bank_data IDs to full csv-files/data_<id>.xlsx mirror keys.
    sheet = [
        r["address"]
        for r in haw
        if re.fullmatch(r"csv-files/data_\d+\.xlsx", r["proof_s3_key"] or "")
    ]
    assert len(sheet) == 25, f"expected 25 sheet-keyed hawala rows, got {len(sheet)}"


def test_verified_true_key_corrections():
    """Pin the mirror-verified true keys (prefix/extension fixes, 2026-09-17)."""
    by_addr = {r["address"]: r for r in _rows()}
    assert (
        by_addr["TLbhuZ12zFwqKNDErNwwewBRvvUAFk1H4j"]["proof_s3_key"]
        == "chat_file/anshul_1769687598637_Screenshot_20260129_172259_Trust_Wallet.jpg"
    )
    assert (
        by_addr["TP5Rj2hz6BQhHTDigsxMZbs1b1ngpEC8xc"]["proof_s3_key"]
        == "chat_file/anshul_1769687598637_Screenshot_20260129_172259_Trust_Wallet.jpg"
    )
    assert (
        by_addr["TVHL9Wfc2rM7MaqDq6TL5E4yQ6biSP6gMU"]["proof_s3_key"]
        == "chat_file/nitrogen123_1743330811322_deposit_163 usdt.jpeg"
    )
    assert (
        by_addr["TD63G1qvWu7ke2ycW848BjMCj2sHEZZusa"]["proof_s3_key"]
        == "chat_file/07081990_1766755679252_Screenshot_20251226-185751.Binance.png"
    )
    assert (
        by_addr["TJ5yq6udNUQ5iQNaapbN5f5YMUvRJKP17B"]["proof_s3_key"]
        == "chat_file/inad_1757641435813_Screenshot_20250912-043626_KuCoin.png"
    )


def test_no_investigator_files_as_sources():
    """Every proof key must be a true original bucket object — never OCR,
    notes, bundles, or any investigator-created path."""
    bad = []
    for r in _rows():
        pk = (r.get("proof_s3_key") or "").strip()
        if not pk:
            continue
        if not pk.startswith(ORIGINAL_PREFIXES):
            bad.append((r["address"], pk, "bad-prefix"))
            continue
        low = pk.lower()
        if any(m.lower() in low for m in NON_SOURCE_MARKERS):
            bad.append((r["address"], pk, "investigator-file"))
    assert bad == [], f"{len(bad)} non-original sources: {bad[:5]}"
