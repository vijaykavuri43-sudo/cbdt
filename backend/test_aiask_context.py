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


class FileContentSearchTests(unittest.TestCase):
    def test_find_ipv4_validates_octets(self):
        self.assertEqual(
            aiask._find_ipv4("server 8.231.108.145 and 999.1.1.1 ok"),
            ["8.231.108.145"],
        )
        self.assertEqual(aiask._find_ipv4("a 1.2.3.4 b 1.2.3.4"), ["1.2.3.4"])

    def test_content_term_picks_literal(self):
        self.assertEqual(
            aiask._content_term("file containing '64431313@okhdfc' ?"),
            "64431313@okhdfc",
        )
        self.assertEqual(
            aiask._content_term("any file with 8.231.108.145"), "8.231.108.145"
        )

    def test_no_trigger_no_scan(self):
        txt, sources = aiask._ctx_file_content(
            "total deposits today?", "total deposits today?", object()
        )
        self.assertEqual((txt, sources), ("", []))

    def test_live_grep_reports_hit_keys(self):
        from . import enrich as _enrich

        saved = (db.list_objects, db.search, db.connect, _enrich.fetch_bytes)
        db.list_objects = lambda *a, **k: {
            "items": [
                {"key": "csv-files/n1.csv", "size": 100},
                {"key": "csv-files/n2.csv", "size": 100},
            ]
        }
        db.search = lambda **k: []
        db.connect = lambda: _NoGateway()

        _enrich.fetch_bytes = lambda store, key: (
            b"nothing here" if key.endswith("n1.csv") else b"callback 8.231.108.145 ok"
        )
        try:
            txt, sources = aiask._ctx_file_content(
                "is there any file containing ip address",
                "is there any file containing ip address",
                object(),
            )
        finally:
            db.list_objects, db.search, db.connect = saved[0], saved[1], saved[2]
            _enrich.fetch_bytes = saved[3]
        self.assertIn("8.231.108.145", txt)
        self.assertIn("csv-files/n2.csv", txt)
        self.assertTrue(any(s["ref"] == "csv-files/n2.csv" for s in sources))


class _NoGateway:
    def execute(self, *a, **k):
        return self

    def fetchall(self):
        return []


if __name__ == "__main__":
    unittest.main()
