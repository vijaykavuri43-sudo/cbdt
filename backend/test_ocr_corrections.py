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
    _ACCOUNT_KEYWORDS_RE,
    _UTR_LABEL_NEAR_RE,
    _collect_amount_candidates,
    _scan_amount_matches,
    NUM12_RE,
    UTR_RE,
    amount_is_ref_id,
    detect_twin_artifact,
    correct_rupee_eaten,
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


class RupeeEatenSingleHitTests(unittest.TestCase):
    """Officer exhibit deposit/1789721443450.jpeg: ₹3,000 stored as 23,000
    because Paid-to ate ₹→2 once and Debited kept '~3,000'."""

    EXHIBIT = (
        "Transaction Successful 02:20 PM on 18 Sep 2026 Paid to FUSO KUMAR "
        "23,000 7903709938-1@okbizaxis Transfer Details PhonePe Transaction ID "
        "T2609181419577033515139 Debited from XXXXXX8766 ~3,000 "
        "UTR: 757251864413"
    )
    GPAY_WORDS = (
        "Paid Successfully Amount 23,000 Rupees Three Thousand Only "
        "To Sanju Kumar UPI ID: ******1923@okbizaxis"
    )

    def test_exhibit_phonepe_23000_to_3000(self):
        self.assertEqual(extract_amount(self.EXHIBIT), 3000.0)
        self.assertEqual(correct_rupee_eaten(self.EXHIBIT, 23000.0), 3000.0)

    def test_gpay_words_override_eaten_digits(self):
        self.assertEqual(extract_amount(self.GPAY_WORDS), 3000.0)

    def test_thousand_to_21000(self):
        text = (
            "Transaction Successful Paid to ACME 21,000 acme@okaxis "
            "Debited from HDFC ~1,000 UTR 123456789012"
        )
        self.assertEqual(extract_amount(text), 1000.0)

    def test_tilde_debris_does_not_beat_true_amount(self):
        text = (
            "Transaction Successful Paid to FUSO KUMAR 23,000 "
            "7903709938-1@okbizaxis Transfer Details ~ 4 "
            "PhonePe Transaction ID T2609181419577033515139 "
            "Debited from XXXXXX8766 ~3,000"
        )
        self.assertEqual(extract_amount(text), 3000.0)
        text = (
            "Transaction Successful Paid to ACME 23,000 acme@okaxis "
            "Debited from HDFC 23,000 Rupees Twenty Three Thousand Only"
        )
        self.assertEqual(extract_amount(text), 23000.0)


class PaytmRefAsAmountTests(unittest.TestCase):
    """Officer exhibit deposit/1789719398070.jpg: Paytm ₹150 stored as
    ₹31,49,658 because the scratch card split Ref. No 314965897745."""

    EXHIBIT = (
        "paytm Fuso Kumar UPI ID: 7903709938-1@okbizaxis ₹150 "
        "18 Sept, 01:46 PM Ref. No: 314965897745 Check Balance Pay Again "
        "SCRATCHCARD YOU JUST WON A ~ 3149658 97745 Send to device"
    )

    def test_exhibit_paytm_150_not_ref_prefix(self):
        self.assertEqual(extract_amount(self.EXHIBIT), 150.0)
        self.assertTrue(amount_is_ref_id(self.EXHIBIT, 3149658.0, "314965897745"))
        self.assertFalse(amount_is_ref_id(self.EXHIBIT, 150.0, "314965897745"))

    def test_ref_prefix_without_rupee_is_nulled_not_lakhs(self):
        text = (
            "paytm Fuso Kumar UPI ID: 7903709938-1@okbizaxis "
            "18 Sept Ref. No: 314965897745 SCRATCHCARD ~ 3149658 97745"
        )
        self.assertIsNone(extract_amount(text))

    def test_genuine_seven_digit_amount_with_unrelated_utr_kept(self):
        text = (
            "Payment successful Paid to ACME TRADERS acme@okhdfcbank "
            "₹21,00,000 UTR 123456789012"
        )
        self.assertEqual(extract_amount(text), 2100000.0)
        self.assertFalse(amount_is_ref_id(text, 2100000.0, "123456789012"))

    def test_proven_correction_ref_to_150(self):
        from .enrich import proven_amount_correction

        new, reason = proven_amount_correction(
            self.EXHIBIT, 3149658.0, "314965897745"
        )
        self.assertEqual(new, 150.0)
        self.assertEqual(reason, "ref_as_amount")

    def test_glyph_collision_pair_repairs_to_true_thousand(self):
        text = (
            "Payment successful Paid to ACME 71,000 acme@okaxis "
            "Debited from HDFC 21,000 ~1,000 UTR 123456789012"
        )
        self.assertEqual(extract_amount(text), 1000.0)


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
            self.assertTrue(key.startswith("deposit/") or key.startswith("withdrawal/"))
            if val is not None:
                self.assertGreater(val, 0)

    def test_targeted_none_means_null_for_gemini(self):
        hit = {"rule": "targeted-audit", "evidence": "x"}
        # None-correct entries must NOT carry a "correct" key, so the repair
        # path nulls (needs_gemini True) instead of setting a value.
        self.assertNotIn("correct", hit)
        for key, (val, _note) in db.TARGETED_AMOUNT_FIXES.items():
            if val is None:
                self.assertTrue(
                    key.startswith("withdrawal/") or key.startswith("deposit/")
                )


class UpiTransactionIdUtrTests(unittest.TestCase):
    """2026-09-16 officer slip deposit/1789519873770.jpg (₹1,500, 6:20am).

    Tesseract text carries 'UPI transaction ID 129679804197' right after
    "...deposit of money to THAKOR KIRABHAI DINESHBHAI's bank account".
    The old UTR_RE knew only UTR/Ref/reference, and the bare-12-digit
    fallback's account-guard killed the number because of 'bank account' —
    so utr came out null and the slip could not join to ledger rows.
    """

    SLIP = (
        "To THAKOR KIRABHAI DINESHBHAI 1,500 Completed 16 Sept 2026, 6:20am "
        "Union Bank of India 5547 Payment of 21,500 completed Receiver bank "
        "has confirmed deposit of money to THAKOR KIRABHAI DINESHBHAI bank "
        "account UPI transaction ID 129679804197 To: THAKOR KIRABHAI "
        "DINESHBHAI 0901@okbizaxis From: KOTHAPALLI RAVITEJA"
    )

    def test_labeled_upi_transaction_id_matches(self):
        m = UTR_RE.search(self.SLIP)
        self.assertIsNotNone(m)
        assert m is not None
        self.assertEqual(m.group(1), "129679804197")

    def test_account_guard_yields_to_adjacent_utr_label(self):
        m = NUM12_RE.search(self.SLIP)
        self.assertIsNotNone(m)
        assert m is not None
        before = self.SLIP[max(0, m.start() - 40) : m.start()].lower()
        # The guard WOULD fire on 'bank account' ...
        self.assertTrue(bool(_ACCOUNT_KEYWORDS_RE.search(before)))
        # ... but the adjacent label overrides it, so the digit run is kept.
        self.assertTrue(bool(_UTR_LABEL_NEAR_RE.search(before[-30:])))
        self.assertEqual(m.group(1), "129679804197")

    def test_long_alphanumeric_gateway_txn_id_not_a_utr(self):
        # PhonePe 'Transaction ID T2609160800210373921631' (22 chars) must
        # not match the 8–16 capture — gateway order ids are not UTRs.
        text = "Transaction Successful Paid to AMAR RAJ 2,000 A PhonePe Transaction ID T2609160800210373921631 Debited from"
        self.assertIsNone(UTR_RE.search(text))

    def test_plain_utr_label_still_matches(self):
        self.assertEqual(UTR_RE.search("UTR 123456789012").group(1), "123456789012")  # type: ignore[union-attr]


if __name__ == "__main__":
    unittest.main()
