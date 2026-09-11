"""Regression tests for 2026-09-11 officer-reported OCR issues.

Covers: (1) lakh-eaten amounts — ₹12,000 hero read as '212,000' shown as
₹2,12,000; (2) empty amounts — 'Rs.2,000' without an 'amount' label missed;
(3) junk VPAs — '1336@completed' / 'clothe@completed' accepted as mule rails.
"""

import os
import tempfile
import unittest
from pathlib import Path

_tmp = tempfile.TemporaryDirectory()
os.environ["S3LM_DATA"] = _tmp.name
os.environ["S3LM_REFERENCE_ROOT"] = str(Path(_tmp.name) / "no-references")

from .enrich import (  # noqa: E402
    _collect_amount_candidates,
    _scan_amount_matches,
    detect_twin_artifact,
    extract_amount,
    extract_vpa_details,
    extract_vpas,
)

# Officer-verified Navi UPI slip (deposit/1789102839824.png): hero shows
# ₹12,000, tesseract ate ₹ into a leading 2 → '212,000' twice (hero +
# Paid-to row), true '12,000' in the Debited row.
NAVI_SLIP_TEXT = (
    "Payment successful 11 Sep 2026 10:30 AM HELP 212,000 "
    "Notes Paid via Navi UPI Paid to ROHAN KUMAR "
    "32230700012033@punbombqbo Debited from Bank of India - 0633 212,000 "
    "UPI transaction ID 625845873993 Navi transaction ID "
    "01317980360061675476200 12,000"
)


class LakhEatenAmountTests(unittest.TestCase):
    def test_six_digit_twin_repaired_to_true_amount(self):
        hit = detect_twin_artifact(NAVI_SLIP_TEXT, 212000.0)
        self.assertIsNotNone(hit)
        assert hit is not None
        self.assertEqual(hit["artifact"], "212000")
        self.assertEqual(hit["true_amount"], 12000)

    def test_genuine_lakh_amount_with_balance_sibling_untouched(self):
        # Genuine ₹2,12,000 whose only '12,000' is a balance readout.
        text = (
            "Payment successful 2,12,000 Paid to ACME TRADERS acme@okhdfcbank "
            "Debited from HDFC Bank 2,12,000 Balance: ₹12,000"
        )
        self.assertIsNone(detect_twin_artifact(text, 212000.0))

    def test_single_occurrence_never_repaired(self):
        text = "Payment successful 2,12,000 Paid to ACME acme@okhdfcbank"
        self.assertIsNone(detect_twin_artifact(text, 212000.0))

    def test_five_digit_twin_still_works(self):
        text = (
            "Payment successful 71,000 Paid to ACME acme@okhdfcbank "
            "Debited from HDFC 71,000 UTR 123456789012 1,000"
        )
        hit = detect_twin_artifact(text, 71000.0)
        self.assertIsNotNone(hit)
        assert hit is not None
        self.assertEqual(hit["true_amount"], 1000)


class RsPrefixedAmountTests(unittest.TestCase):
    def test_rs_prefix_without_amount_label(self):
        text = (
            "Payment successful Paid to ACME acme@okhdfcbank Rs.2,000 UTR 123456789012"
        )
        self.assertEqual(extract_amount(text), 2000.0)

    def test_inr_prefix_without_amount_label(self):
        text = "Payment successful INR 5000 Paid to ACME acme@okaxis"
        self.assertEqual(extract_amount(text), 5000.0)


class JunkVpaTests(unittest.TestCase):
    def test_status_word_handles_rejected(self):
        self.assertEqual(
            extract_vpas("1336@completed clothe@completed Payment successful"), []
        )

    def test_real_mobile_vpa_kept(self):
        self.assertEqual(
            extract_vpas("Paid to 6239199904@okbizaxis Payment successful"),
            ["6239199904@okbizaxis"],
        )

    def test_copyright_sign_recovered_to_at(self):
        self.assertEqual(
            extract_vpas("Paid to 6239199904©okbizaxis Payment successful"),
            ["6239199904@okbizaxis"],
        )


class AdDecoyTests(unittest.TestCase):
    """Officer case 2026-09-11: true deposit ₹2,000 + iPhone ad ₹50,000 —
    the ad must never win, wherever the banner sits."""

    AD_FAR = (
        "Payment successful 11 Sep 2026 ₹2,000 Paid to ACME acme@okaxis "
        "Debited from HDFC 2,000 UTR 123456789012 "
        "iPhone 16 Pro ₹50,000 Buy now EMI available"
    )
    AD_NEAR = (
        "Payment successful iPhone 16 Pro ₹50,000 Buy now with EMI "
        "₹2,000 Paid to ACME acme@okaxis Debited from HDFC 2,000 UTR 123456789012"
    )

    def test_ad_far_from_cue_loses(self):
        self.assertEqual(extract_amount(self.AD_FAR), 2000.0)

    def test_ad_beside_header_loses(self):
        # Pre-fix this returned 50000.0 (ad scored 6+20 on header proximity).
        self.assertEqual(extract_amount(self.AD_NEAR), 2000.0)

    def test_cashback_decoy_loses(self):
        text = (
            "Payment successful ₹2,000 Paid to ACME acme@okaxis "
            "Get ₹500 cashback on next pay UTR 123456789012"
        )
        self.assertEqual(extract_amount(text), 2000.0)

    def test_premium_payee_not_an_ad(self):
        # Word-boundary guard: "premium" must not trigger the EMI rule.
        text = "Payment successful ₹2,000 Paid to PREMIUM STORE prem@okaxis UTR 123456789012"
        self.assertEqual(extract_amount(text), 2000.0)


class SharedSweepTests(unittest.TestCase):
    def test_collect_matches_best_first_scan(self):
        flat = " ".join(NAVI_SLIP_TEXT.split())
        scanned = [d for d in _scan_amount_matches(flat) if not d["dropped"]]
        scanned.sort(key=lambda d: (-d["score"], d["s"], d["value"]))
        collected = _collect_amount_candidates(flat)
        self.assertEqual(
            [(d["value"], d["score"]) for d in scanned],
            [(c[0], c[1]) for c in collected],
        )

    def test_vpa_details_reuses_allowed_set(self):
        text = "Paid to 6239199904@okbizaxis Payment successful UTR 123456789012"
        vpas = extract_vpas(text)
        self.assertEqual(
            extract_vpa_details(text, set(vpas)), extract_vpa_details(text)
        )

    def test_twin_ignores_cashback_sibling(self):
        # Genuine 71234-class amount; only same-suffix sibling is a cashback readout.
        text = (
            "Payment successful 71,234 Paid to ACME acme@okhdfcbank "
            "Debited from HDFC 71,234 Get ₹1,234 cashback on next pay"
        )
        self.assertIsNone(detect_twin_artifact(text, 71234.0))
        assert True


if __name__ == "__main__":
    unittest.main()
