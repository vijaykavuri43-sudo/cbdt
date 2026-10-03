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
                (
                    "deposit/a.jpg",
                    "deposit/",
                    10,
                    "2026-09-10T12:00:00Z",
                    "e1",
                    "2026-09-10T12:05:00Z",
                    ".jpg",
                ),
                (
                    "deposit/b.jpg",
                    "deposit/",
                    10,
                    "2026-09-15T12:00:00Z",
                    "e2",
                    "2026-09-15T12:05:00Z",
                    ".jpg",
                ),
                (
                    "withdrawal/c.jpg",
                    "withdrawal/",
                    10,
                    "2026-09-12T12:00:00Z",
                    "e3",
                    "2026-09-12T12:05:00Z",
                    ".jpg",
                ),
            ],
        )
        c.executemany(
            "INSERT INTO payment_transactions("
            "source_key,direction,gambler,amount_inr,utr,rail_type,rail_id,"
            "account_holder,ifsc,network,confidence,rail_role,role_confidence,last_modified,evidence_json"
            ") VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            [
                (
                    "deposit/a.jpg",
                    "deposit",
                    "g1",
                    100,
                    "U1",
                    "upi",
                    "a@ybl",
                    "",
                    "",
                    "",
                    "high",
                    "beneficiary",
                    "high",
                    "2026-09-10T12:00:00Z",
                    "{}",
                ),
                (
                    "deposit/b.jpg",
                    "deposit",
                    "g2",
                    200,
                    "U2",
                    "upi",
                    "b@ybl",
                    "",
                    "",
                    "",
                    "high",
                    "beneficiary",
                    "high",
                    "2026-09-15T12:00:00Z",
                    "{}",
                ),
                (
                    "withdrawal/c.jpg",
                    "withdrawal",
                    "g3",
                    50,
                    "U3",
                    "bank_account",
                    "123456",
                    "Holder",
                    "SBIN0001",
                    "",
                    "medium",
                    "sender",
                    "medium",
                    "2026-09-12T12:00:00Z",
                    "{}",
                ),
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
        dep = db.money_flows(
            direction="both",
            folder="deposit",
            since="2026-09-01",
            until="2026-09-30",
            limit=50,
        )
        self.assertEqual(dep["rows_total"], 2)
        self.assertTrue(
            all(i["source_key"].startswith("deposit/") for i in dep["items"])
        )
        wd = db.money_flows(
            direction="both",
            folder="withdrawal",
            since="2026-09-01",
            until="2026-09-30",
            limit=50,
        )
        self.assertEqual(wd["rows_total"], 1)
        self.assertEqual(wd["items"][0]["source_key"], "withdrawal/c.jpg")

    def test_folder_all_includes_deposit_and_withdrawal(self):
        out = db.money_flows(
            direction="both",
            folder="",
            since="2026-09-01",
            until="2026-09-30",
            limit=50,
        )
        keys = {i["source_key"] for i in out["items"]}
        self.assertEqual(keys, {"deposit/a.jpg", "deposit/b.jpg", "withdrawal/c.jpg"})
        also = db.money_flows(
            direction="both",
            folder="all",
            since="2026-09-01",
            until="2026-09-30",
            limit=50,
        )
        self.assertEqual({i["source_key"] for i in also["items"]}, keys)

    def test_unique_direction_split_deposit_vs_withdrawal(self):
        # Deposit side: UPI/VPA + wallets used to COLLECT in range.
        dep = db.money_flows_unique(
            rail_type="upi",
            direction="deposit",
            since="2026-09-01",
            until="2026-09-30",
        )
        self.assertEqual({r["rail_id"] for r in dep["items"]}, {"a@ybl", "b@ybl"})
        # Withdrawal side: no UPI rails paid out in range.
        wd_upi = db.money_flows_unique(
            rail_type="upi",
            direction="withdrawal",
            since="2026-09-01",
            until="2026-09-30",
        )
        self.assertEqual(wd_upi["rows_total"], 0)
        # Withdrawal side bank accounts used to PAY gamblers.
        wd_bank = db.money_flows_unique(
            rail_type="bank_account",
            direction="withdrawal",
            since="2026-09-01",
            until="2026-09-30",
        )
        self.assertEqual([r["rail_id"] for r in wd_bank["items"]], ["123456"])
        self.assertEqual(wd_bank["items"][0]["account_holder"], "Holder")
        # Narrow single-day window keeps only that day's rails.
        one = db.money_flows_unique(
            rail_type="upi",
            direction="deposit",
            since="2026-09-10T00:00:00Z",
            until="2026-09-10T23:59:59Z",
        )
        self.assertEqual([r["rail_id"] for r in one["items"]], ["a@ybl"])

    def test_unique_export_rows_direction_split(self):
        dep = db.money_flows_unique_export_rows(
            direction="deposit", since="2026-09-01", until="2026-09-30"
        )
        self.assertEqual({r["rail_id"] for r in dep}, {"a@ybl", "b@ybl"})
        wd = db.money_flows_unique_export_rows(
            direction="withdrawal", since="2026-09-01", until="2026-09-30"
        )
        self.assertEqual([r["rail_id"] for r in wd], ["123456"])

    def test_unique_hits_direction_split(self):
        dep = db.money_flows_unique_hits(
            rail_type="upi",
            rail_id="a@ybl",
            direction="deposit",
            since="2026-09-01",
            until="2026-09-30",
        )
        self.assertEqual(dep["rows_total"], 1)
        wd = db.money_flows_unique_hits(
            rail_type="upi",
            rail_id="a@ybl",
            direction="withdrawal",
            since="2026-09-01",
            until="2026-09-30",
        )
        self.assertEqual(wd["rows_total"], 0)

    def test_unique_excludes_ocr_junk_vpas(self):
        # Live b2/b3 2026-10-02: amount artifacts + template junk topped lists.
        c = db.connect()
        c.executemany(
            "INSERT INTO payment_transactions("
            "source_key,direction,gambler,amount_inr,utr,rail_type,rail_id,"
            "account_holder,ifsc,network,confidence,rail_role,role_confidence,last_modified,evidence_json"
            ") VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            [
                (
                    "deposit/j1.jpg",
                    "deposit",
                    "g9",
                    10,
                    "UJ1",
                    "upi",
                    "000.00@tess",
                    "",
                    "",
                    "",
                    "low",
                    "beneficiary",
                    "low",
                    "2026-09-11T12:00:00Z",
                    "{}",
                ),
                (
                    "deposit/j2.jpg",
                    "deposit",
                    "g9",
                    10,
                    "UJ2",
                    "upi",
                    "only@foodstall",
                    "",
                    "",
                    "",
                    "low",
                    "beneficiary",
                    "low",
                    "2026-09-11T12:00:00Z",
                    "{}",
                ),
                (
                    "deposit/j3.jpg",
                    "deposit",
                    "g9",
                    10,
                    "UJ3",
                    "upi",
                    "receipt@contact",
                    "",
                    "",
                    "",
                    "low",
                    "beneficiary",
                    "low",
                    "2026-09-11T12:00:00Z",
                    "{}",
                ),
                (
                    "deposit/j4.jpg",
                    "deposit",
                    "g9",
                    10,
                    "UJ4",
                    "upi",
                    "share@need",
                    "",
                    "",
                    "",
                    "low",
                    "beneficiary",
                    "low",
                    "2026-09-11T12:00:00Z",
                    "{}",
                ),
            ],
        )
        c.commit()
        out = db.money_flows_unique(
            rail_type="upi",
            direction="deposit",
            since="2026-09-01",
            until="2026-09-30",
        )
        ids = {r["rail_id"] for r in out["items"]}
        self.assertNotIn("000.00@tess", ids)
        self.assertNotIn("only@foodstall", ids)
        self.assertNotIn("receipt@contact", ids)
        self.assertNotIn("share@need", ids)
        self.assertEqual(ids, {"a@ybl", "b@ybl"})

    def test_unique_excludes_time_fragments_and_bank_words(self):
        c = db.connect()
        c.executemany(
            "INSERT INTO payment_transactions("
            "source_key,direction,gambler,amount_inr,utr,rail_type,rail_id,"
            "account_holder,ifsc,network,confidence,rail_role,role_confidence,last_modified,evidence_json"
            ") VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            [
                (
                    "deposit/t1.jpg",
                    "deposit",
                    "g9",
                    10,
                    "UT1",
                    "upi",
                    "04pm@kotak",
                    "",
                    "",
                    "",
                    "low",
                    "beneficiary",
                    "low",
                    "2026-09-11T12:00:00Z",
                    "{}",
                ),
                (
                    "deposit/t2.jpg",
                    "deposit",
                    "g9",
                    10,
                    "UT2",
                    "upi",
                    "06am@state",
                    "",
                    "",
                    "",
                    "low",
                    "beneficiary",
                    "low",
                    "2026-09-11T12:00:00Z",
                    "{}",
                ),
                (
                    "deposit/t3.jpg",
                    "deposit",
                    "g9",
                    10,
                    "UT3",
                    "upi",
                    "gupta@bank",
                    "",
                    "",
                    "",
                    "low",
                    "beneficiary",
                    "low",
                    "2026-09-11T12:00:00Z",
                    "{}",
                ),
                (
                    "deposit/t4.jpg",
                    "deposit",
                    "g9",
                    10,
                    "UT4",
                    "upi",
                    "9876543210@kotak",
                    "",
                    "",
                    "",
                    "high",
                    "beneficiary",
                    "high",
                    "2026-09-11T12:00:00Z",
                    "{}",
                ),
            ],
        )
        c.commit()
        out = db.money_flows_unique(
            rail_type="upi",
            direction="deposit",
            since="2026-09-01",
            until="2026-09-30",
        )
        ids = {r["rail_id"] for r in out["items"]}
        self.assertNotIn("04pm@kotak", ids)
        self.assertNotIn("06am@state", ids)
        self.assertNotIn("gupta@bank", ids)
        self.assertIn("9876543210@kotak", ids)

    def test_rail_quality_labels(self):
        q = db.rail_quality
        self.assertEqual(q("upi", "9876543210@okbizaxis"), "full")
        self.assertEqual(q("upi", "vinod.mungekar3258@uboi"), "full")
        self.assertEqual(q("upi", "xxxxxx7922@ptyes"), "masked")
        self.assertEqual(q("upi", "0000008301-q251@ybl"), "masked")
        self.assertEqual(q("upi", "0767@okbizaxis"), "partial")
        self.assertEqual(q("upi", "borichatridevhajabhai@okax"), "partial")
        self.assertEqual(q("upi", "sahil@jio"), "partial")
        self.assertEqual(q("upi", "-1220841560649@okbizaxis"), "garble")
        self.assertEqual(q("upi", "0001559@okbizaxis"), "masked")
        self.assertEqual(q("upi", "0097-1@okbizaxis"), "masked")
        self.assertEqual(q("upi", "0048272@okbizaxis"), "masked")
        self.assertEqual(q("upi", "00e1925@okbizaxis"), "masked")
        self.assertEqual(q("upi", "0e3679@okbizaxis"), "masked")
        self.assertEqual(q("upi", "x8211@uboi"), "masked")
        self.assertEqual(q("upi", "1021-xf2b@ibl"), "masked")
        self.assertEqual(q("upi", "11240718451@okbizaxis"), "masked")
        self.assertEqual(q("upi", "6204435919-1@okbizaxis"), "full")
        self.assertEqual(q("upi", "919876543210@okaxis"), "full")
        self.assertEqual(q("upi", "000.@powered"), "garble")
        self.assertEqual(q("bank_account", "123456"), "full")
        out = db.money_flows_unique(
            rail_type="upi",
            direction="deposit",
            since="2026-09-01",
            until="2026-09-30",
        )
        quals = {r["rail_quality"] for r in out["items"]}
        self.assertTrue(quals <= {"full", "masked", "partial", "garble"})
        by = {r["rail_id"]: r for r in out["items"]}
        self.assertEqual(by["a@ybl"]["rail_quality"], "partial")

    def test_unique_upi(self):
        out = db.money_flows_unique(
            folder="deposit", rail_type="upi", since="2026-09-01", until="2026-09-30"
        )
        self.assertEqual(out["rows_total"], 2)
        ids = {r["rail_id"] for r in out["items"]}
        self.assertEqual(ids, {"a@ybl", "b@ybl"})
        by = {r["rail_id"]: r for r in out["items"]}
        self.assertEqual(by["a@ybl"]["utrs"], "U1")
        self.assertEqual(by["a@ybl"]["utr_count"], 1)
        self.assertEqual(by["b@ybl"]["utrs"], "U2")

    def test_canonical_rail_type(self):
        self.assertEqual(db._canonical_rail_type("upi", "name@oksbi"), "upi")
        self.assertEqual(db._canonical_rail_type("upi", "123456789012"), "bank_account")
        self.assertEqual(
            db._canonical_rail_type("bank_account", "T" + "A" * 33), "crypto_wallet"
        )

    def test_bank_filter_excludes_vpa(self):
        out = db.money_flows(
            direction="both",
            rail_type="bank_account",
            since="2026-09-01",
            until="2026-09-30",
        )
        self.assertTrue(all("@" not in (i["rail_id"] or "") for i in out["items"]))
        self.assertEqual(out["rows_total"], 1)
        self.assertEqual(out["items"][0]["rail_id"], "123456")

    def test_unique_crypto_unions_decoded_images(self):
        hit_only = "T" + "B" * 33
        both = "T" + "C" * 33
        c = db.connect()
        c.execute(
            "INSERT INTO objects(key,top_prefix,size,last_modified,etag,seen_utc,ext) VALUES (?,?,?,?,?,?,?)",
            (
                "bankingqr/q.png",
                "bankingqr/",
                10,
                "2026-09-12T12:00:00Z",
                "e4",
                "2026-09-12T12:05:00Z",
                ".png",
            ),
        )
        c.execute(
            "INSERT INTO objects(key,top_prefix,size,last_modified,etag,seen_utc,ext) VALUES (?,?,?,?,?,?,?)",
            (
                "deposit/d-crypto.jpg",
                "deposit/",
                10,
                "2026-09-12T12:00:00Z",
                "e5",
                "2026-09-12T12:05:00Z",
                ".jpg",
            ),
        )
        c.execute(
            "INSERT INTO payment_transactions("
            "source_key,direction,gambler,amount_inr,utr,rail_type,rail_id,"
            "account_holder,ifsc,network,confidence,rail_role,role_confidence,last_modified,evidence_json"
            ") VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                "deposit/d-crypto.jpg",
                "deposit",
                "",
                1,
                "",
                "crypto_wallet",
                both,
                "",
                "",
                "TRON",
                "high",
                "",
                "",
                "2026-09-12T12:00:00Z",
                "{}",
            ),
        )
        c.execute(
            "INSERT OR REPLACE INTO wallet_hits(address,key,last_modified,network) VALUES (?,?,?,?)",
            (hit_only, "bankingqr/q.png", "2026-09-12T12:00:00Z", "TRON"),
        )
        c.execute(
            "INSERT OR REPLACE INTO wallet_hits(address,key,last_modified,network) VALUES (?,?,?,?)",
            (both, "deposit/d-crypto.jpg", "2026-09-12T12:00:00Z", "TRON"),
        )
        c.commit()
        out = db.money_flows_unique(
            rail_type="crypto_wallet", folder="", since="2026-09-01", until="2026-09-30"
        )
        ids = {r["rail_id"] for r in out["items"]}
        self.assertIn(hit_only, ids)
        self.assertIn(both, ids)
        self.assertGreaterEqual(out["rows_total"], 2)
        by = {r["rail_id"]: r for r in out["items"]}
        self.assertGreaterEqual(by[hit_only]["image_hits"], 1)
        self.assertEqual(by[hit_only]["slips"], 0)
        self.assertGreaterEqual(by[both]["slips"], 1)

    def test_unique_crypto_direction_scopes_image_wallets(self):
        # bankingqr/q.png decode must NOT appear on the withdrawal-pay side.
        hit_only = "T" + "B" * 33
        c = db.connect()
        c.execute(
            "INSERT OR REPLACE INTO wallet_hits(address,key,last_modified,network) VALUES (?,?,?,?)",
            (hit_only, "bankingqr/q.png", "2026-09-12T12:00:00Z", "TRON"),
        )
        c.commit()
        dep = db.money_flows_unique(
            rail_type="crypto_wallet",
            direction="deposit",
            since="2026-09-01",
            until="2026-09-30",
        )
        wd = db.money_flows_unique(
            rail_type="crypto_wallet",
            direction="withdrawal",
            since="2026-09-01",
            until="2026-09-30",
        )
        self.assertIn(hit_only, {r["rail_id"] for r in dep["items"]})
        self.assertNotIn(hit_only, {r["rail_id"] for r in wd["items"]})

    def test_unique_hits_upi_and_bank(self):
        upi = db.money_flows_unique_hits(
            rail_type="upi",
            rail_id="a@ybl",
            since="2026-09-01",
            until="2026-09-30",
        )
        self.assertEqual(upi["rows_total"], 1)
        self.assertEqual(upi["items"][0]["source_key"], "deposit/a.jpg")
        self.assertEqual(upi["items"][0]["folder"], "deposit")
        self.assertIn("ocr_slip", upi["items"][0]["origins"])
        bank = db.money_flows_unique_hits(
            rail_type="bank_account",
            rail_id="123456",
            since="2026-09-01",
            until="2026-09-30",
        )
        self.assertEqual(bank["items"][0]["source_key"], "withdrawal/c.jpg")
        self.assertEqual(bank["items"][0]["folder"], "withdrawal")
        none = db.money_flows_unique_hits(
            rail_type="upi",
            rail_id="a@ybl",
            folder="withdrawal",
            since="2026-09-01",
            until="2026-09-30",
        )
        self.assertEqual(none["rows_total"], 0)

    def test_unique_hits_crypto_unions_all_source_paths(self):
        hit_only = "T" + "B" * 33
        both = "T" + "C" * 33
        extra = "T" + "D" * 33
        c = db.connect()
        c.executemany(
            "INSERT INTO objects(key,top_prefix,size,last_modified,etag,seen_utc,ext) VALUES (?,?,?,?,?,?,?)",
            [
                (
                    "bankingqr/q.png",
                    "bankingqr/",
                    10,
                    "2026-09-12T12:00:00Z",
                    "e4",
                    "2026-09-12T12:05:00Z",
                    ".png",
                ),
                (
                    "deposit/d-crypto.jpg",
                    "deposit/",
                    10,
                    "2026-09-12T12:00:00Z",
                    "e5",
                    "2026-09-12T12:05:00Z",
                    ".jpg",
                ),
                (
                    "chat_file/w.webp",
                    "chat_file/",
                    10,
                    "2026-09-13T12:00:00Z",
                    "e6",
                    "2026-09-13T12:05:00Z",
                    ".webp",
                ),
                (
                    "withdrawal/w-crypto.jpg",
                    "withdrawal/",
                    10,
                    "2026-09-14T12:00:00Z",
                    "e7",
                    "2026-09-14T12:05:00Z",
                    ".jpg",
                ),
            ],
        )
        c.execute(
            "INSERT INTO payment_transactions("
            "source_key,direction,gambler,amount_inr,utr,rail_type,rail_id,"
            "account_holder,ifsc,network,confidence,rail_role,role_confidence,last_modified,evidence_json"
            ") VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                "deposit/d-crypto.jpg",
                "deposit",
                "",
                1,
                "",
                "crypto_wallet",
                both,
                "",
                "",
                "TRON",
                "high",
                "",
                "",
                "2026-09-12T12:00:00Z",
                "{}",
            ),
        )
        c.executemany(
            "INSERT OR REPLACE INTO wallet_hits(address,key,last_modified,seen_utc,network) VALUES (?,?,?,?,?)",
            [
                (
                    hit_only,
                    "bankingqr/q.png",
                    "2026-09-12T12:00:00Z",
                    "2026-09-12T12:05:00Z",
                    "TRON",
                ),
                (
                    both,
                    "deposit/d-crypto.jpg",
                    "2026-09-12T12:00:00Z",
                    "2026-09-12T12:05:00Z",
                    "TRON",
                ),
                (
                    extra,
                    "chat_file/w.webp",
                    "2026-09-13T12:00:00Z",
                    "2026-09-13T12:05:00Z",
                    "TRON",
                ),
                (
                    extra,
                    "withdrawal/w-crypto.jpg",
                    "2026-09-14T12:00:00Z",
                    "2026-09-14T12:05:00Z",
                    "TRON",
                ),
            ],
        )
        c.commit()
        image_only = db.money_flows_unique_hits(
            rail_type="crypto_wallet",
            rail_id=hit_only,
            since="2026-09-01",
            until="2026-09-30",
        )
        self.assertEqual(
            {i["source_key"] for i in image_only["items"]}, {"bankingqr/q.png"}
        )
        self.assertEqual(image_only["items"][0]["origins"], ["image_hit"])
        merged = db.money_flows_unique_hits(
            rail_type="crypto_wallet",
            rail_id=both,
            since="2026-09-01",
            until="2026-09-30",
        )
        self.assertEqual(merged["rows_total"], 1)
        self.assertEqual(set(merged["items"][0]["origins"]), {"image_hit", "ocr_slip"})
        all_paths = db.money_flows_unique_hits(
            rail_type="crypto_wallet",
            rail_id=extra,
            since="2026-09-01",
            until="2026-09-30",
        )
        self.assertEqual(
            {i["source_key"] for i in all_paths["items"]},
            {"chat_file/w.webp", "withdrawal/w-crypto.jpg"},
        )
        folders = {i["folder"] for i in all_paths["items"]}
        self.assertEqual(folders, {"chat_file", "withdrawal"})
        deposit_only = db.money_flows_unique_hits(
            rail_type="crypto_wallet",
            rail_id=extra,
            folder="deposit",
            since="2026-09-01",
            until="2026-09-30",
        )
        self.assertEqual(deposit_only["rows_total"], 0)


if __name__ == "__main__":
    unittest.main()
