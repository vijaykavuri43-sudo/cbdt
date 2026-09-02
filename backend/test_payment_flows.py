"""Regression tests for payment-rail extraction and directional normalization."""

import os
import tempfile
import unittest
from pathlib import Path

_tmp = tempfile.TemporaryDirectory()
os.environ["S3LM_DATA"] = _tmp.name
os.environ["S3LM_REFERENCE_ROOT"] = str(Path(_tmp.name) / "no-references")

from . import config, db  # noqa: E402
from .collector import Collector  # noqa: E402
from .enrich import extract_amount, extract_bank_accounts, extract_participants, extract_vpa_details, extract_vpas  # noqa: E402


class ExtractionTests(unittest.TestCase):
    def test_extracts_labeled_amount_account_ifsc_and_vpa(self):
        text = "Txn Amount 980.00 Account Number 35316820392 IFSC SBIN0001234 paid to Example Merchant merchant.pay@okaxis"
        self.assertEqual(extract_amount(text), 980.0)
        self.assertEqual(extract_vpas(text), ["merchant.pay@okaxis"])
        self.assertEqual(extract_bank_accounts(text)[0]["account_number"], "35316820392")
        self.assertEqual(extract_bank_accounts(text)[0]["ifsc"], "SBIN0001234")
        self.assertEqual(extract_participants(text)["beneficiary_name"], "Example Merchant")

    def test_does_not_treat_utr_as_unlabelled_amount(self):
        self.assertIsNone(extract_amount("UTR 123456789012 payment successful"))

    def test_vpa_roles_distinguish_payer_from_beneficiary(self):
        text = "Paid by Gambler payer@okaxis Paid to Merchant mule@ybl"
        roles = {item["vpa"]: item["role"] for item in extract_vpa_details(text)}
        self.assertEqual(roles["payer@okaxis"], "sender")
        self.assertEqual(roles["mule@ybl"], "beneficiary")


class MoneyFlowTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        config.REFERENCE_UTR_GAMBLER = Path(_tmp.name) / "missing-gambler.tsv"
        config.REFERENCE_UTR_VPA = Path(_tmp.name) / "missing-vpa.tsv"
        db.connect()

    @classmethod
    def tearDownClass(cls):
        db.close()

    def setUp(self):
        c = db.connect()
        c.execute("DELETE FROM payment_transactions")
        c.execute("DELETE FROM payment_flow_totals")
        c.execute("DELETE FROM payment_rail_summary")
        c.execute("DELETE FROM objects")
        c.execute("DELETE FROM utr_identity")
        c.commit()

    def _object(self, key):
        db.upsert_objects([{"key": key, "top_prefix": key.split('/')[0] + '/', "size": 100, "last_modified": "2026-09-01T10:00:00Z", "etag": "test"}])

    def test_deposit_maps_sender_to_receiving_upi(self):
        key = "deposit/d1.png"; self._object(key)
        db.mark_enriched(key, {"kind": "deposit", "sender_name": "Gambler One", "beneficiary_name": "Platform Merchant", "amount_inr": 980, "utr": "123456789012", "vpas": ["rail@okaxis"], "confidence": "medium"})
        item = db.money_flows("deposit")["items"][0]
        self.assertEqual(item["gambler"], "Gambler One")
        self.assertEqual(item["rail_id"], "rail@okaxis")
        self.assertEqual(item["account_holder"], "Platform Merchant")

    def test_withdrawal_maps_beneficiary_to_sending_bank(self):
        key = "withdrawal/w1.png"; self._object(key)
        db.mark_enriched(key, {"kind": "withdrawal", "sender_name": "Platform Settlement", "beneficiary_name": "Gambler Two", "amount_inr": 1500, "bank_accounts": [{"account_number": "1234567890", "ifsc": "HDFC0001234"}], "confidence": "medium"})
        item = db.money_flows("withdrawal", rail_type="bank_account")["items"][0]
        self.assertEqual(item["gambler"], "Gambler Two")
        self.assertEqual(item["account_holder"], "Platform Settlement")
        self.assertEqual(item["ifsc"], "HDFC0001234")

    def test_impossible_legacy_amount_is_not_summed(self):
        key = "withdrawal/bad-amount.png"; self._object(key)
        db.mark_enriched(key, {"kind": "withdrawal", "amount_inr": 123456789012, "utr": "123456789012"})
        flows = db.money_flows("withdrawal")
        self.assertEqual(flows["transactions"], 1)
        self.assertEqual(flows["sum_amount_inr"], 0)

    def test_materialized_summary_matches_live_rows(self):
        key = "deposit/materialized.png"; self._object(key)
        db.mark_enriched(key, {"kind": "deposit", "gambler": "G3", "amount_inr": 2000, "vpa": "rail3@okaxis"})
        db.refresh_payment_summaries()
        flows = db.money_flows("deposit", rail_type="upi")
        self.assertEqual(flows["transactions"], 1)
        self.assertEqual(flows["sum_amount_inr"], 2000)
        self.assertTrue(flows["summary_refreshed_utc"])

    def test_role_aware_deposit_excludes_payer_vpa(self):
        key = "deposit/two-party.png"; self._object(key)
        db.mark_enriched(key, {
            "kind": "deposit", "gambler": "G4", "amount_inr": 500,
            "vpa_details": [
                {"vpa": "gambler@okaxis", "role": "sender", "role_confidence": "high"},
                {"vpa": "receiver@ybl", "role": "beneficiary", "role_confidence": "high"},
            ],
        })
        rows = db.money_flows("deposit")["items"]
        self.assertEqual([row["rail_id"] for row in rows], ["receiver@ybl"])
        self.assertEqual(rows[0]["rail_role"], "beneficiary")

    def test_role_aware_withdrawal_excludes_beneficiary_vpa(self):
        key = "withdrawal/two-party.png"; self._object(key)
        db.mark_enriched(key, {
            "kind": "withdrawal", "gambler": "G5", "amount_inr": 750,
            "vpa_details": [
                {"vpa": "settlement@okaxis", "role": "sender", "role_confidence": "high"},
                {"vpa": "gambler@ybl", "role": "beneficiary", "role_confidence": "high"},
            ],
        })
        rows = db.money_flows("withdrawal")["items"]
        self.assertEqual([row["rail_id"] for row in rows], ["settlement@okaxis"])
        self.assertEqual(rows[0]["rail_role"], "sender")

    def test_mule_rail_aggregate_links_both_directions(self):
        for key, direction, role, gambler in (
            ("deposit/shared.png", "deposit", "beneficiary", "G6"),
            ("withdrawal/shared.png", "withdrawal", "sender", "G7"),
        ):
            self._object(key)
            db.mark_enriched(key, {
                "kind": direction, "gambler": gambler, "amount_inr": 1000,
                "vpa_details": [{"vpa": "shared@okaxis", "role": role, "role_confidence": "high"}],
            })
        item = db.mule_rails()["items"][0]
        self.assertEqual(item["deposit_transactions"], 1)
        self.assertEqual(item["withdrawal_transactions"], 1)
        self.assertEqual(item["gamblers"], 2)


class _PagedStore:
    def __init__(self, objects):
        self.objects = sorted(objects, key=lambda o: o["key"])
        self.calls = 0

    def list_page(
        self,
        prefix="",
        continuation_token=None,
        start_after=None,
        max_keys=1000,
        **_kwargs,
    ):
        self.calls += 1
        filtered = [
            o
            for o in self.objects
            if o["key"].startswith(prefix)
            and (not start_after or o["key"] > start_after)
        ]
        offset = int(continuation_token or 0)
        page = filtered[offset : offset + max_keys]
        next_offset = offset + len(page)
        more = next_offset < len(filtered)
        return {
            "objects": [dict(o) for o in page],
            "prefixes": [],
            "is_truncated": more,
            "next_token": str(next_offset) if more else None,
        }


class CollectorAccuracyTests(unittest.TestCase):
    def setUp(self):
        c = db.connect()
        c.execute("DELETE FROM objects")
        c.execute("DELETE FROM meta")
        c.commit()

    @staticmethod
    def _obj(i: int, etag: str = "v1"):
        return {
            "key": f"chat_file/user_{i:04d}.png",
            "top_prefix": "chat_file/",
            "size": 100 + i,
            "etag": etag,
            "last_modified": "2026-09-02T10:00:00Z",
        }

    def test_crawl_detects_overwritten_known_key(self):
        original = [self._obj(1), self._obj(2)]
        db.upsert_objects(original)
        changed = [self._obj(1), self._obj(2, etag="v2")]
        collector = Collector(_PagedStore(changed))
        collector.crawl_once("chat_file/", cap=1000)
        row = db.get_object_row(changed[1]["key"])
        self.assertEqual(row["etag"], "v2")
        self.assertEqual(collector.state["new_since_start"], 1)

    def test_crawl_cap_bounds_already_known_pages_and_persists_cursor(self):
        objects = [self._obj(i) for i in range(2500)]
        db.upsert_objects(objects)
        store = _PagedStore(objects)
        collector = Collector(store)
        collector.crawl_once("chat_file/", cap=1000)
        self.assertEqual(store.calls, 1)
        self.assertEqual(
            db.get_meta("crawl:chat_file/"), "chat_file/user_0999.png"
        )


if __name__ == "__main__":
    unittest.main()
