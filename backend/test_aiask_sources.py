"""Cross-source AI context and deterministic port/wallet answers.

Regression tests for the 2026-11-08 report: an officer on the bx-s3-dev-002
dashboard (port 83) asked about port 80 and the local-only assistant answered
0 for every wallet question. The assistant must now see all sibling sources
and explain incomplete extraction instead of claiming an empty bucket.
"""

import unittest

from . import aiask


def _b1() -> dict:
    return {
        "port": 80,
        "label": "bx-s3-dev-001",
        "bucket_url": "gs://evidence/s3-mirror/",
        "objects_total": 2480587,
        "enrichment": {
            "total": 2480587,
            "enriched_done": 2240946,
            "coverage_pct": 100.0,
            "slip_backlog": 0,
            "eta_hours": 0.0,
        },
        "wallets": {
            "wallet_hits": 1826,
            "distinct_wallets": 356,
            "by_network": [
                {"network": "TRON", "count": 1808},
                {"network": "EVM", "count": 12},
                {"network": "BITCOIN", "count": 6},
            ],
            "sample": [
                {
                    "address": "TCgLo2vsT4BNxXB5ZNKvW9vev9t54Tv4kt",
                    "network": "TRON",
                    "hits": 43,
                    "key": "deposit/1794123896524.jpg",
                }
            ],
        },
        "reachable": True,
    }


def _b4() -> dict:
    return {
        "port": 83,
        "label": "bx-s3-dev-002",
        "bucket_url": "gs://evidence/s3-mirror-bx-s3-dev-002/",
        "objects_total": 85285,
        "enrichment": {
            "total": 85138,
            "enriched_done": 2788,
            "coverage_pct": 3.27,
            "slip_backlog": 82344,
            "eta_hours": 32.5,
        },
        "wallets": {
            "wallet_hits": 0,
            "distinct_wallets": 0,
            "by_network": [],
            "sample": [],
        },
        "reachable": True,
    }


class SourceContextTests(unittest.TestCase):
    def setUp(self):
        self._saved = (
            aiask._peer_rows,
            aiask._local_source_row,
            aiask._local_peer,
        )
        aiask._peer_rows = lambda: [_b1(), _b4()]

    def tearDown(self):
        (
            aiask._peer_rows,
            aiask._local_source_row,
            aiask._local_peer,
        ) = self._saved

    def test_sources_block_lists_every_dashboard(self):
        txt, sources = aiask._ctx_sources("does port 83 have any crypto wallets?")
        self.assertIn("port 80", txt)
        self.assertIn("port 83", txt)
        self.assertIn("wallet_hits=0", txt)
        self.assertIn("does NOT mean", txt)  # b4 caveat, not "no wallets exist"
        self.assertTrue(any(s["kind"] == "sources" for s in sources))

    def test_fast_answer_for_remote_port(self):
        out = aiask._source_fast_answer(
            "does s3 bucket at port 83 have any crypto wallets?"
        )
        self.assertIsNotNone(out)
        self.assertEqual(out["model"], "local")
        self.assertIn("Port 83", out["answer"])
        self.assertIn("Distinct crypto wallets: 0", out["answer"])
        self.assertIn("extraction is still in progress", out["answer"])
        self.assertIn("82,344 slips queued", out["answer"])

    def test_fast_answer_lists_sample_for_local_port(self):
        aiask._local_peer = lambda: {"label": "bx-s3-dev-001", "port": 80}
        aiask._local_source_row = lambda peer: _b1()
        out = aiask._source_fast_answer(
            "give me crypto wallet found in port 80 bucket"
        )
        self.assertIsNotNone(out)
        self.assertIn("TCgLo2vsT4BNxXB5ZNKvW9vev9t54Tv4kt", out["answer"])
        self.assertIn("1826", out["answer"].replace(",", ""))
        self.assertIn("356", out["answer"])

    def test_fast_answer_this_bucket_uses_local_and_flags_incomplete(self):
        aiask._local_peer = lambda: {"label": "bx-s3-dev-002", "port": 83}
        aiask._local_source_row = lambda peer: _b4()
        out = aiask._source_fast_answer(
            "how many distinct crypto wallets are in this bucket?"
        )
        self.assertIsNotNone(out)
        self.assertIn("Port 83", out["answer"])
        self.assertIn("extraction is still in progress", out["answer"])

    def test_fast_answer_does_not_hijack_general_questions(self):
        self.assertIsNone(aiask._source_fast_answer("total deposits today?"))
        self.assertIsNone(aiask._source_fast_answer("list all wallets"))


if __name__ == "__main__":
    unittest.main()
