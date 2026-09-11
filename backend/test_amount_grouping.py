"""Regression tests: malformed-grouping guard + decoy-context skips.

Verified 2026-09-11 top-30 audit: bad-comma 10-100x inflation ('%714,0000'
stored 71.4L for true 7.14L), Navi loan-ad picks ('Get upto INR 20,00,000'
stored 20L), RRN/ref picks ('RRNNumber « = 6252477' over TRUE 1,500),
VPA-digit runs ('rs3689580-1@okaxis' stored 3.69M twice).
"""

import os
import tempfile
import unittest

_tmp = tempfile.TemporaryDirectory()
os.environ["S3LM_DATA"] = _tmp.name

from .db import _malformed_evidence  # noqa: E402
from .enrich import (  # noqa: E402
    _collect_amount_candidates,
    detect_bad_grouping,
    extract_amount,
)


class GroupingDetectorTests(unittest.TestCase):
    def test_bad_groupings_rejected(self):
        for tok in (
            "714,0000",
            "50080,00",
            "36,000,00",
            "350,0000",
            "31,000,00",
            "38,00000",
            "4,0000",
            "5,5009",
            "1,0000",
            "71,0000",
            "72,0000",
        ):
            self.assertTrue(detect_bad_grouping(tok), tok)

    def test_good_groupings_kept(self):
        for tok in (
            "7,14,000",
            "714,000",
            "21,00,000",
            "20,00,000",
            "60,000",
            "10,000",
            "1,500",
            "710000",
            "20000",
        ):
            self.assertFalse(detect_bad_grouping(tok), tok)


class CandidateSkipTests(unittest.TestCase):
    def test_bad_comma_candidate_dropped_true_survives(self):
        cands = _collect_amount_candidates("Paid to Le /21,000 = 8460704 view details")
        vals = [v for v, *_ in cands]
        self.assertNotIn(8460704.0, vals)
        self.assertIn(21000.0, vals)

    def test_token_glued_digits_dropped(self):
        cands = _collect_amount_candidates("Transfer towards7105960 Amount 120000")
        vals = [v for v, *_ in cands]
        self.assertNotIn(7105960.0, vals)
        self.assertIn(120000.0, vals)

    def test_vpa_digits_and_rrn_dropped_transfer_amount_survives(self):
        cands = _collect_amount_candidates(
            "Paid to rs3689580-1@okaxis RRNNumber 6252477 Transfer Amount 1500"
        )
        vals = [v for v, *_ in cands]
        self.assertNotIn(3689580.0, vals)
        self.assertNotIn(6252477.0, vals)
        self.assertIn(1500.0, vals)

    def test_navi_loan_ad_dropped(self):
        cands = _collect_amount_candidates(
            "Get upto INR 20,00,000.00 instant loan approval"
        )
        self.assertEqual(cands, [])

    def test_paytm_postpaid_ad_dropped_words_fallback(self):
        text = (
            "Rupees Four Hundred Fifty Only Paid To ******1910@okbizaxis "
            "Unlock Paytm Getupto Postpaid today! ~60,000 credit"
        )
        cands = _collect_amount_candidates(" ".join(text.split()))
        self.assertNotIn(60000.0, [v for v, *_ in cands])
        self.assertEqual(extract_amount(text), 450.0)


class MalformedEvidenceTests(unittest.TestCase):
    def test_bad_comma_evidence(self):
        hit = _malformed_evidence("Paid %714,0000 UPI", 7140000.0)
        self.assertEqual(hit["rule"], "bad-comma")

    def test_ref_evidence(self):
        hit = _malformed_evidence("Ref. No: 613726905 done", 6137269.0)
        self.assertEqual(hit["rule"], "ref-number")

    def test_ad_evidence(self):
        hit = _malformed_evidence("Get upto INR 20,00,000.00 instant", 2000000.0)
        self.assertEqual(hit["rule"], "ad-context")

    def test_vpa_evidence(self):
        hit = _malformed_evidence("to rs3689580-1@okaxis ok", 3689580.0)
        self.assertEqual(hit["rule"], "vpa-digits")

    def test_clean_amount_no_evidence(self):
        self.assertIsNone(_malformed_evidence("Paid To Raksha = 21,00,000", 2100000.0))

    def test_absent_culprit_no_evidence(self):
        self.assertIsNone(_malformed_evidence("some other text 500", 2100000.0))


if __name__ == "__main__":
    unittest.main()
