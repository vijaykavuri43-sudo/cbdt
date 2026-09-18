"""Officer Query date-bound money_flows filters."""

import os
import tempfile
import unittest
from pathlib import Path

_tmp = tempfile.TemporaryDirectory()
os.environ["S3LM_DATA"] = _tmp.name
os.environ["S3LM_REFERENCE_ROOT"] = str(Path(_tmp.name) / "no-references")

from . import db  # noqa: E402


class MoneyFlowBoundsTests(unittest.TestCase):
    def setUp(self):
        c = db.connect()
        c.execute("DELETE FROM payment_transactions")
        c.execute("DELETE FROM objects")
        c.executemany(
            "INSERT INTO objects(key,top_prefix,size,last_modified,etag,seen_utc,ext) VALUES (?,?,?,?,?,?,?)",
            [
                ("deposit/a.jpg", "deposit/", 10, "2026-09-10T12:00:00Z", "e1", "2026-09-10T12:05:00Z", ".jpg"),
                ("deposit/b.jpg", "deposit/", 10, "2026-09-15T12:00:00Z", "e2", "2026-09-15T12:05:00Z", ".jpg"),
                ("withdrawal/c.jpg", "withdrawal/", 10, "2026-09-12T12:00:00Z", "e3", "2026-09-12T12:05:00Z", ".jpg"),
            ],
        )
        c.executemany(
            "INSERT INTO payment_transactions("
            "source_key,direction,gambler,amount_inr,utr,rail_type,rail_id,"
            "account_holder,ifsc,network,confidence,rail_role,role_confidence,last_modified,evidence_json"
            ") VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            [
                ("deposit/a.jpg", "deposit", "g1", 100, "U1", "upi", "a@ybl", "", "", "", "high", "beneficiary", "high", "2026-09-10T12:00:00Z", "{}"),
                ("deposit/b.jpg", "deposit", "g2", 200, "U2", "upi", "b@ybl", "", "", "", "high", "beneficiary", "high", "2026-09-15T12:00:00Z", "{}"),
                ("withdrawal/c.jpg", "withdrawal", "g3", 50, "U3", "bank_account", "123456", "Holder", "SBIN0001", "", "medium", "sender", "medium", "2026-09-12T12:00:00Z", "{}"),
            ],
        )
        c.commit()

    def test_bound_ts_inclusive_day(self):
        self.assertEqual(db._bound_ts("2026-09-10"), "2026-09-10T00:00:00Z")
        self.assertEqual(db._bound_ts("2026-09-10", end=True), "2026-09-10T23:59:59Z")

    def test_date_range_and_seen_utc(self):
        out = db.money_flows(
            direction="deposit",
            rail_type="upi",
            since="2026-09-14",
            until="2026-09-16",
            limit=50,
        )
        self.assertEqual(out["rows_total"], 1)
        self.assertEqual(out["items"][0]["rail_id"], "b@ybl")
        self.assertEqual(out["items"][0]["seen_utc"], "2026-09-15T12:05:00Z")

    def test_both_directions_export(self):
        rows = db.money_flows_export_rows(
            direction="both",
            since="2026-09-10",
            until="2026-09-13",
            source="object",
        )
        keys = {r["source_key"] for r in rows}
        self.assertEqual(keys, {"deposit/a.jpg", "withdrawal/c.jpg"})


    def test_folder_deposit_not_withdrawal(self):
        dep = db.money_flows(direction="both", folder="deposit", since="2026-09-01", until="2026-09-30", limit=50)
        self.assertEqual(dep["rows_total"], 2)
        self.assertTrue(all(i["source_key"].startswith("deposit/") for i in dep["items"]))
        wd = db.money_flows(direction="both", folder="withdrawal", since="2026-09-01", until="2026-09-30", limit=50)
        self.assertEqual(wd["rows_total"], 1)
        self.assertEqual(wd["items"][0]["source_key"], "withdrawal/c.jpg")

    def test_folder_all_includes_deposit_and_withdrawal(self):
        out = db.money_flows(direction="both", folder="", since="2026-09-01", until="2026-09-30", limit=50)
        keys = {i["source_key"] for i in out["items"]}
        self.assertEqual(keys, {"deposit/a.jpg", "deposit/b.jpg", "withdrawal/c.jpg"})
        also = db.money_flows(direction="both", folder="all", since="2026-09-01", until="2026-09-30", limit=50)
        self.assertEqual({i["source_key"] for i in also["items"]}, keys)

    def test_unique_upi(self):
        out = db.money_flows_unique(folder="deposit", rail_type="upi", since="2026-09-01", until="2026-09-30")
        self.assertEqual(out["rows_total"], 2)
        ids = {r["rail_id"] for r in out["items"]}
        self.assertEqual(ids, {"a@ybl", "b@ybl"})

    def test_canonical_rail_type(self):
        self.assertEqual(db._canonical_rail_type("upi", "name@oksbi"), "upi")
        self.assertEqual(db._canonical_rail_type("upi", "123456789012"), "bank_account")
        self.assertEqual(db._canonical_rail_type("bank_account", "T" + "A" * 33), "crypto_wallet")

    def test_bank_filter_excludes_vpa(self):
        out = db.money_flows(direction="both", rail_type="bank_account", since="2026-09-01", until="2026-09-30")
        self.assertTrue(all("@" not in (i["rail_id"] or "") for i in out["items"]))
        self.assertEqual(out["rows_total"], 1)
        self.assertEqual(out["items"][0]["rail_id"], "123456")


if __name__ == "__main__":
    unittest.main()
