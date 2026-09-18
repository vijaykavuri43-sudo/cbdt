"""Tests for AI-investigator actual-data context (2026-09-18 fix).

Officers asked "list all the wallet address here directly" and got only
aggregate counts. The _ctx_*_list gatherers must return verbatim identifiers
from the DB instead.
"""

import unittest

from . import aiask, db


def _wallets(**kw):
    return [
        {
            "address": "TXYZ123abc",
            "network": "TRON",
            "hits": 12,
            "first_seen": "2026-09-01T00:00:00Z",
            "last_seen": "2026-09-17T00:00:00Z",
            "key": "deposit/a.jpg",
        },
        {
            "address": "0xabcDEF123",
            "network": "EVM",
            "hits": 3,
            "first_seen": "2026-09-02T00:00:00Z",
            "last_seen": "2026-09-16T00:00:00Z",
            "key": "deposit/b.jpg",
        },
    ]


class ActualDataContextTests(unittest.TestCase):
    def setUp(self):
        self._saved = (
            db.wallet_distinct,
            db.mule_rails,
            db.list_objects,
        )

    def tearDown(self):
        (
            db.wallet_distinct,
            db.mule_rails,
            db.list_objects,
        ) = self._saved

    def test_wallet_list_returns_verbatim_addresses(self):
        db.wallet_distinct = _wallets
        txt, sources = aiask._ctx_wallet_list("list all the wallet address here")
        self.assertIn("TXYZ123abc", txt)
        self.assertIn("0xabcDEF123", txt)
        self.assertIn("TRON", txt)
        self.assertTrue(sources)

    def test_wallet_list_ignores_unrelated_questions(self):
        txt, sources = aiask._ctx_wallet_list("total deposits today?")
        self.assertEqual((txt, sources), ("", []))

    def test_upi_list_returns_handles(self):
        db.mule_rails = lambda limit=60: {
            "total": 1,
            "items": [
                {
                    "rail_type": "vpa",
                    "rail_id": "mule@okbank",
                    "transactions": 40,
                    "deposit_amount_inr": 100000,
                    "withdrawal_amount_inr": 0,
                    "gamblers": 9,
                    "account_holders": "MULE X",
                }
            ],
        }
        txt, sources = aiask._ctx_upi_list("top mule upi handles?")
        self.assertIn("mule@okbank", txt)
        self.assertIn("1,00,000", txt)  # Indian grouping
        self.assertTrue(sources)

    def test_objects_prefix_detection(self):
        seen = {}

        def _fake_list(prefix, limit=15, order="-modified", flat=True):
            seen["prefix"] = prefix
            return {
                "total": 5,
                "items": [
                    {
                        "key": "deposit/x.jpg",
                        "size": 100,
                        "last_modified": "2026-09-17T00:00:00Z",
                    }
                ],
            }

        db.list_objects = _fake_list
        txt, _ = aiask._ctx_objects(
            "newest files in deposit/ folder", "newest files in deposit/ folder"
        )
        self.assertEqual(seen["prefix"], "deposit/")
        self.assertIn("deposit/x.jpg", txt)

    def test_ledger_gatherer_compacts_summary(self):
        from . import ledger as _ledger

        def _fake_summary():
            return {
                "by_type": [
                    {
                        "type": "Deposit",
                        "txns": 10,
                        "cash": 50000,
                        "bonus": 5000,
                        "gamblers": 4,
                        "utrs": 9,
                    }
                ],
                "by_sender": [{"sender": "GAMBLER1", "txns": 6, "cash": 30000}],
                "coverage": {"ledger_utrs": 9, "matched_in_flows": 7},
                "files": {"indexed": 3, "total": 3},
            }

        saved = _ledger.summary
        _ledger.summary = _fake_summary
        try:
            txt, sources = aiask._ctx_ledger("ledger xls totals by sender?")
        finally:
            _ledger.summary = saved
        self.assertIn("GAMBLER1", txt)
        self.assertIn("Deposit", txt)
        self.assertTrue(sources)


if __name__ == "__main__":
    unittest.main()
