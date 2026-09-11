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
    extract_bank_accounts,
    extract_participants,
    extract_vpa_details,
    extract_vpas,
)
from . import db  # noqa: E402

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


class BankReceiptTests(unittest.TestCase):
    """Officer case 2026-09-11: PNB withdrawal receipt (withdrawal/1789105367390.jpg).
    Payee labeled 'Name X', bank A/C + IFSC present — all must surface."""

    SLIP = (
        "Transaction Successful Transaction ID 2902105 Customer No. 7301713510 "
        "Txn Amount =2000.0000 Name Pebba Rajesh | Bank Name PUNJAB NATIONAL BANK "
        "A/C 1583100100008388 | Tr. Date Time 9/11/2026 11:12:06 AM | IFSC Code "
        "PUNB0244200 | RRN 625411078687 Transfer Mode IMPS"
    )

    def test_beneficiary_from_name_label(self):
        self.assertEqual(
            extract_participants(self.SLIP).get("beneficiary_name"), "Pebba Rajesh"
        )

    def test_bank_name_not_taken_as_beneficiary(self):
        out = extract_participants(
            "Transaction Successful Bank Name PUNJAB NATIONAL BANK A/C 1583100100008388"
        )
        self.assertNotIn("PUNJAB", (out.get("beneficiary_name") or ""))

    def test_account_and_ifsc_extracted(self):
        accts = extract_bank_accounts(self.SLIP)
        self.assertTrue(accts)
        self.assertEqual(accts[0]["account_number"], "1583100100008388")
        self.assertEqual(accts[0]["ifsc"], "PUNB0244200")

    def test_equals_sign_amount_read(self):
        # Tesseract renders ₹ as '=' on this receipt ("Txn Amount =2000.0000").
        self.assertEqual(extract_amount(self.SLIP), 2000.0)


class UpiPaginationTests(unittest.TestCase):
    """Officer case 2026-09-11: UPI tab capped at newest-500 with no pages —
    10k+ VPAs unreachable. limit/offset/counts must page the full set."""

    PREFIX = "deposit/pg-20260911-"

    @classmethod
    def setUpClass(cls):
        for i in range(5):
            key = f"{cls.PREFIX}{i}.png"
            db.upsert_objects(
                [
                    {
                        "key": key,
                        "top_prefix": "deposit/",
                        "size": 100,
                        # distinct timestamps → deterministic newest-first order
                        "last_modified": f"2026-09-11T10:00:0{i}Z",
                        "etag": "test",
                    }
                ]
            )
            db.mark_enriched(
                key,
                {"kind": "deposit", "vpa": f"pguser{i}@okaxis", "amount_inr": 100 + i},
            )

    def _mine(self, rows):
        return [r for r in rows if (r.get("key") or "").startswith(self.PREFIX)]

    def test_feed_pages_are_disjoint_and_cover(self):
        p1 = self._mine(db.upi_feed(2, 0))
        p2 = self._mine(db.upi_feed(2, 2))
        p3 = self._mine(db.upi_feed(2, 4))
        # newest-first: i=4,3 on page 1 — but only among OUR rows after filtering
        self.assertEqual(len(p1) + len(p2) + len(p3), 5)
        keys = [r["key"] for r in p1 + p2 + p3]
        self.assertEqual(len(set(keys)), 5)
        self.assertEqual(p1[0]["key"], f"{self.PREFIX}4.png")

    def test_counts_drop_by_excluded_set(self):
        all5 = {f"pguser{i}@okaxis" for i in range(5)}
        before = db.upi_counts()
        after = db.upi_counts(all5)
        self.assertEqual(before["feed_total"] - after["feed_total"], 5)
        self.assertEqual(before["distinct_total"] - after["distinct_total"], 5)
        # excluded VPAs never surface, even on later pages
        hidden = self._mine(db.upi_feed(10**9, 10**9 - 5, all5))
        self.assertEqual(hidden, [])

    def test_which_limits_scans(self):
        c = db.upi_counts(which="feed")
        self.assertGreater(c["feed_total"], 0)
        self.assertEqual(c["distinct_total"], 0)


class Round2AmountGuardTests(unittest.TestCase):
    """Officer case 2026-09-11 round-2: withdrawal-tail garbage that survived
    round-1 (RRN/ref ids, credit-card-bill reminders, timestamp concats)."""

    def test_timestamp_frag_dropped_in_scan(self):
        flat = "Paid at 1:05 1 111 202 to Ritam Mahajanphone"
        hits = [d for d in _scan_amount_matches(flat) if d["value"] == 1111202.0]
        self.assertTrue(hits)
        self.assertTrue(all(d["dropped"] for d in hits))
        self.assertTrue(all(d["reason"] == "timestamp-frag" for d in hits))

    def test_timestamp_guard_exempts_prefixed_amount(self):
        flat = "Paid at 10:30 ₹500 to ACME acme@okaxis UTR 123456789012"
        vals = [d["value"] for d in _scan_amount_matches(flat) if not d["dropped"]]
        self.assertIn(500.0, vals)

    def test_bill_context_dropped_in_scan(self):
        flat = "Pay Postpaid now 1,328,572.62 for Credit Card - 1668"
        hits = [d for d in _scan_amount_matches(flat) if d["value"] == 1328572.62]
        self.assertTrue(hits)
        self.assertTrue(all(d["dropped"] for d in hits))

    def test_genuine_paid_bill_kept(self):
        flat = "Payment successful Paid postpaid bill ₹1,200 to AIRTEL airtel@okaxis"
        vals = [d["value"] for d in _scan_amount_matches(flat) if not d["dropped"]]
        self.assertIn(1200.0, vals)

    def test_transfer_amount_evidence_corrects(self):
        sample = "IMPS RRNNumber « = 6252477 Transfer Amount 1500 Beneficiary"
        hit = db._malformed_evidence(sample, 6252477.0)
        self.assertIsNotNone(hit)
        assert hit is not None
        self.assertEqual(hit["rule"], "transfer-amount")
        self.assertEqual(hit["correct"], 1500.0)

    def test_transfer_amount_matching_stored_not_flagged(self):
        sample = "Transfer Amount 1500 Beneficiary RRN 123"
        self.assertIsNone(db._malformed_evidence(sample, 1500.0))

    def test_timestamp_frag_evidence(self):
        hit = db._malformed_evidence("Paid at 1:05 1 111 202 to Ritam", 1111202.0)
        self.assertIsNotNone(hit)
        assert hit is not None
        self.assertEqual(hit["rule"], "timestamp-frag")

    def test_bill_context_evidence(self):
        hit = db._malformed_evidence("Pay Postpaid now 1,328,572.62 done", 1328572.62)
        self.assertIsNotNone(hit)
        assert hit is not None
        self.assertEqual(hit["rule"], "bill-context")

    def test_targeted_fixes_sane(self):
        for key, (val, _note) in db.TARGETED_AMOUNT_FIXES.items():
            self.assertTrue(key.startswith("deposit/"))
            self.assertGreater(val, 0)


if __name__ == "__main__":
    unittest.main()
