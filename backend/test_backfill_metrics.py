"""Regression tests for the slip-backlog visibility fix.

Bulk-copied payment slips sit with enriched=0 outside the live QR-tier
queue, so pending_enrichment_count() reports 0 while hundreds of thousands
of slips await decode. pending_slip_count() must surface them and
enrichment_progress() must fold them into eligible/coverage/ETA.
"""

import os
import tempfile
import unittest
from pathlib import Path

_tmp = tempfile.TemporaryDirectory()
os.environ["S3LM_DATA"] = _tmp.name
os.environ["S3LM_REFERENCE_ROOT"] = str(Path(_tmp.name) / "no-references")

from . import db  # noqa: E402


def _put(key, top, ext="", enriched=0, extract=None, retry=None):
    c = db.connect()
    c.execute(
        "INSERT INTO objects(key, top_prefix, ext, enriched, extract_json,"
        " enrich_retry_utc) VALUES (?,?,?,?,?,?)",
        (key, top, ext, enriched, extract, retry),
    )
    c.commit()


class SlipBacklogTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        _put("deposit/a.jpg", "deposit/", ".jpg")
        _put(
            "deposit/b.jpg",
            "deposit/",
            ".jpg",
            enriched=1,
            extract='{"kind":"deposit"}',
        )
        _put("bankingqr/c.jpg", "bankingqr/", ".jpg")
        _put("deposit/d.mp4", "deposit/", ".mp4")
        _put("deposit/e.jpg", "deposit/", ".jpg", retry="2999-01-01T00:00:00Z")
        _put("withdrawal/f.jpeg", "withdrawal/", "")

    def test_pending_slip_count_sees_only_due_slip_images(self):
        # a.jpg (ext branch) + f.jpeg (key-LIKE fallback branch).
        self.assertEqual(db.pending_slip_count(), 2)

    def test_progress_folds_backlog_into_eligible_and_eta(self):
        p = db.enrichment_progress()
        self.assertEqual(p["slip_backlog"], 2)
        self.assertEqual(
            p["eligible_total"],
            p["enriched_done"] + p["pending_now"] + p["slip_backlog"],
        )
        self.assertLess(p["coverage_pct"], 100.0)


if __name__ == "__main__":
    unittest.main()
