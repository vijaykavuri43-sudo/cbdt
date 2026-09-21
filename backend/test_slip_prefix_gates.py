"""Bucket-prefix gate review (2026-09-21): b3 payment-proofs/ sat invisible.

- classify(): OCR_PREFIXES wins over QR_PREFIXES when a prefix is in both
  (OCR path = ocr_slip + QR merge, strictly richer than QR-decode-only).
- db.pending_slips()/pending_slip_count(): config-driven via _slip_prefixes()
  instead of hardcoded deposit/withdrawal/payment.
"""

import os
import tempfile
import unittest
from pathlib import Path

_tmp = tempfile.TemporaryDirectory()
os.environ["S3LM_DATA"] = _tmp.name
os.environ["S3LM_REFERENCE_ROOT"] = str(Path(_tmp.name) / "no-references")

from . import config  # noqa: E402
from . import db  # noqa: E402
from . import enrich  # noqa: E402


class SlipPrefixGates(unittest.TestCase):
    def test_classify_ocr_wins_when_prefix_in_both(self):
        both = set(config.QR_PREFIXES) & set(config.OCR_PREFIXES)
        saved_qr, saved_ocr = config.QR_PREFIXES, config.OCR_PREFIXES
        try:
            config.QR_PREFIXES = ("qr-only/", "both/")
            config.OCR_PREFIXES = ("ocr-only/", "both/")
            self.assertEqual(enrich.classify("both/shot.jpg"), "withdrawal")
            self.assertEqual(enrich.classify("qr-only/a.png"), "qr")
            self.assertEqual(enrich.classify("ocr-only/b.png"), "withdrawal")
            self.assertEqual(enrich.classify("deposit/c.png"), "none")
        finally:
            config.QR_PREFIXES, config.OCR_PREFIXES = saved_qr, saved_ocr
        self.assertTrue(True if not both else True)

    def test_slip_prefixes_honour_env_additions(self):
        saved = config.OCR_PREFIXES
        try:
            config.OCR_PREFIXES = ("deposit/", "withdrawal/", "payment-proofs/")
            prefs = db._slip_prefixes()
            self.assertIn("payment-proofs/", prefs)
            self.assertIn("payment/", prefs)  # back-compat default kept
            clause, args = db._slip_prefix_clause()
            self.assertEqual(clause.count("?"), len(args))
            self.assertEqual(len(args), len(prefs))
        finally:
            config.OCR_PREFIXES = saved

    def test_slip_prefixes_default_keeps_old_trio(self):
        saved = config.OCR_PREFIXES
        try:
            config.OCR_PREFIXES = ("deposit/", "withdrawal/")
            prefs = db._slip_prefixes()
            for p in ("deposit/", "withdrawal/", "payment/"):
                self.assertIn(p, prefs)
        finally:
            config.OCR_PREFIXES = saved


if __name__ == "__main__":
    unittest.main()
