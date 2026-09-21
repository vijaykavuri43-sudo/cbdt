"""Regression tests for crypto-wallet guards (CASE-2026-CBTF).

Verified live 2026-09-11 on two screenshot slips:
1. UPI slip deposit/1789040207329.jpg — "Transaction ID O NEF54[cent]3985c…f1c":
   a 29-char txid fragment was stored as a BITCOIN wallet because OCR junk
   ("O NEF54[cent]") sat between the label and the fragment, defeating the
   immediate-label guard.
2. Trust-Wallet USDT slip withdrawal/1789050709350.webp —
   "Transaction ID 8ce402586fedf9 ...111bcedb…f9F": a truncated-display tail
   was stored as BITCOIN, and the To-address got role=sender (bare "To"
   was not a beneficiary label).
"""

import os
import tempfile
import unittest
from pathlib import Path

_tmp = tempfile.TemporaryDirectory()
os.environ["S3LM_DATA"] = _tmp.name
os.environ["S3LM_REFERENCE_ROOT"] = str(Path(_tmp.name) / "no-references")

from . import db  # noqa: E402
from .enrich import (  # noqa: E402
    _after_ellipsis,
    _labeled_as_txid_loose,
    _recover_tron_ocr,
    _tron_checksum_ok,
    extract_wallets,
)

# Slip 1 (redacted to the wallet-relevant line): UPI Transaction ID whose
# OCR-glued remainder must never become a wallet.
SLIP1_TXID_LINE = "Transaction ID O NEF54\u00a23985c62964b1d8666d4c394fd7f1c"

# Slip 2 (redacted): truncated txid display + From/To TRON pair.
SLIP2_TEXT = (
    "Transaction ID 8ce402586fedf9 ...111bcedb2b29a8edd2c2b746f9F "
    "From TJXhSVWjd2Uo9uu1abhSdZztq9cGEMQPqx "
    "To TQtW5vRwRHYDftpK9ztgBwrtUjkBc27tu9"
)

REAL_TRON = "TJXhSVWjd2Uo9uu1abhSdZztq9cGEMQPqx"
REAL_TRON_TO = "TQtW5vRwRHYDftpK9ztgBwrtUjkBc27tu9"


class TxidFragmentGuardTests(unittest.TestCase):
    def test_upi_txid_remainder_is_not_a_wallet(self):
        self.assertEqual(extract_wallets(SLIP1_TXID_LINE), [])

    def test_truncated_display_tail_is_not_a_wallet(self):
        wallets = extract_wallets(SLIP2_TEXT)
        addrs = [w["address"] for w in wallets]
        self.assertNotIn("111bcedb2b29a8edd2c2b746f9F", addrs)

    def test_labeled_gap_guard_fires_on_slip1_shape(self):
        pos = SLIP1_TXID_LINE.find("3985c629")
        self.assertTrue(_labeled_as_txid_loose(SLIP1_TXID_LINE, pos, pos + 29))

    def test_ellipsis_guard_fires_on_slip2_shape(self):
        pos = SLIP2_TEXT.find("111bcedb")
        self.assertTrue(_after_ellipsis(SLIP2_TEXT, pos, pos + 27))

    def test_genuine_btc_unaffected(self):
        wallets = extract_wallets(
            "Send to 1A1zP1eP5QGefi2DMPTfTL5SLmv7DivfNa Amount Rs 500"
        )
        self.assertEqual(wallets[0]["network"], "BITCOIN")


class RailRoleTests(unittest.TestCase):
    def test_bare_to_is_beneficiary(self):
        wallets = extract_wallets(SLIP2_TEXT)
        roles = {w["address"]: (w["role"], w["role_confidence"]) for w in wallets}
        self.assertEqual(roles[REAL_TRON_TO], ("beneficiary", "high"))
        self.assertEqual(roles[REAL_TRON], ("sender", "high"))


class TronChecksumTests(unittest.TestCase):
    def test_real_addresses_validate(self):
        self.assertTrue(_tron_checksum_ok(REAL_TRON))
        self.assertTrue(_tron_checksum_ok(REAL_TRON_TO))

    def test_garbage_fails(self):
        self.assertFalse(_tron_checksum_ok("T000000000000000000000000000000000"))

    def test_single_lookalike_error_recovers(self):
        degraded = REAL_TRON.replace("2Uo9uu", "2UO9uu", 1)
        self.assertEqual(_recover_tron_ocr(degraded), REAL_TRON)

    def test_unrecoverable_returns_none(self):
        self.assertIsNone(_recover_tron_ocr("T000000000000000000000000000000000"))

    # 2026-09-21 census dispute: 39 reported strings, all checksum-INVALID.
    # None may ever validate; the named lookalike classes must recover.
    DISPUTE_INVALID = [
        "TQAiceryW8Pbrpav4gvkNgevVEjpunqm8a",
        "TJXWJEDRrR7dCSRzVkyQbL2A9ZVh7VHdd1",
        "TBzz7udHadGokPXJ6TFUVPW6S7LfBEjF9D",
        "TANW4QKFYFBZX2qQ8yuZCoVNC4qrVUNJpQ",
        "TQYCXJBLEXbSYSEBriHHpeGvVS9FutyrZM",
        "THKa488cYP3dwiNikS9UW8N7nruVFtnhia",
        "TJNgidmSZir8aDWM1PRrTtw5PenMoFb9ss",
        "TCgLo2vsT4BNxXB5ZNKvW9vgv9tS4TvAkt",
    ]

    def test_dispute_strings_all_fail_checksum(self):
        for s in self.DISPUTE_INVALID:
            self.assertFalse(_tron_checksum_ok(s), s)

    def test_case_flip_pair_recovers(self):
        from .enrich import recover_tron_ocr

        self.assertEqual(
            recover_tron_ocr("TBzz7udHadGokPXJ6TFUVPW6S7LfBEjF9D"),
            "TBzz7udHadGokPXJ6TFUvPw6S7LfBEjF9D",
        )

    def test_extended_lookalike_classes_recover(self):
        from .enrich import recover_tron_ocr

        pairs = {
            # N/h (hawala#2), g/q (N4), E/6 (N5), i/1 (C10), S/5 (C7)
            "TANW4QKFYFBZX2qQ8yuZCoVNC4qrVUNJpQ": "TAhW4QKFYFBZX2qQ8yuZCoVNC4qrVUNJpQ",
            "TQAiceryW8Pbrpav4gvkNgevVEjpunqm8a": "TQAiceryW8Pbrpav4gvkNqevVEjpunqm8a",
            "TQYCXJBLEXbSYSEBriHHpeGvVS9FutyrZM": "TQYCXJBL6XbSYSEBriHHpeGvVS9FutyrZM",
            "TJNgidmSZir8aDWM1PRrTtw5PenMoFb9ss": "TJNg1dmSZir8aDWM1PRrTtw5PenMoFb9ss",
            "THKa488cYP3dwiNikS9UW8N7nruVFtnhia": "THKa488cYP3dwiNik59UW8N7nruVFtnhia",
        }
        for bad, good in pairs.items():
            got = recover_tron_ocr(bad)
            self.assertIsNotNone(got, bad)
            self.assertEqual(got, good, bad)
            assert got is not None
            self.assertTrue(_tron_checksum_ok(got))

    def test_both_variants_invalid_stays_unrecovered(self):
        from .enrich import recover_tron_ocr

        # N2: canonical and dispute variant are BOTH checksum-invalid —
        # no valid twin exists within edit distance; must stay flagged.
        self.assertIsNone(recover_tron_ocr("TCgLo2vsT4BNxXB5ZNKvW9vgv9tS4TvAkt"))


def _repair_row(key, wallets, sample):
    import json

    db.upsert_objects(
        [
            {
                "key": key,
                "size": 100,
                "last_modified": "2026-09-11T00:00:00Z",
                "etag": "e1",
            }
        ]
    )
    db.mark_enriched(
        key,
        {
            "kind": "deposit",
            "engine": "tesseract",
            "text_sample": sample,
            "wallets": wallets,
        },
    )
    res = db.repair_txid_wallets(dry_run=False)
    row = (
        db.connect()
        .execute("SELECT extract_json FROM objects WHERE key=?", (key,))
        .fetchone()
    )
    return res, json.loads(row[0])


class TxidWalletRepairPolicyTests(unittest.TestCase):
    def test_checksum_invalid_is_recovered_not_dropped(self):
        degraded = REAL_TRON.replace("2Uo9uu", "2UO9uu", 1)
        self.assertFalse(_tron_checksum_ok(degraded))
        _, p = _repair_row(
            "deposit/repair-test-recover.jpg",
            [
                {
                    "address": degraded,
                    "network": "TRON",
                    "standard": "TRC-20",
                    "confidence": "high",
                    "role": "unknown",
                    "role_confidence": "low",
                }
            ],
            "To " + degraded,
        )
        addrs = [w["address"] for w in p["wallets"]]
        # Recovered to the true on-chain address, never dropped.
        self.assertIn(REAL_TRON, addrs)
        self.assertTrue(any(w.get("ocr_recovered") for w in p["wallets"]))

    def test_txid_fragment_still_dropped(self):
        frag = "3985c62964b1d8666d4c394fd7f1c"
        _, p = _repair_row(
            "deposit/repair-test-drop.jpg",
            [
                {
                    "address": frag,
                    "network": "BITCOIN",
                    "standard": "BTC",
                    "confidence": "low",
                    "role": "unknown",
                    "role_confidence": "low",
                }
            ],
            SLIP1_TXID_LINE,
        )
        self.assertEqual(p["wallets"], [])


if __name__ == "__main__":
    unittest.main()
