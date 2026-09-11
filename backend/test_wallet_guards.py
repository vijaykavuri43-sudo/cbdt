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


if __name__ == "__main__":
    unittest.main()
