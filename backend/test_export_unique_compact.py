"""Crypto CSV consistency: unique-address and every-slip exports share columns."""

import os
import tempfile
import unittest
from pathlib import Path

_tmp = tempfile.TemporaryDirectory()
os.environ["S3LM_DATA"] = _tmp.name
os.environ["S3LM_REFERENCE_ROOT"] = str(Path(_tmp.name) / "no-references")

from . import app as app_module  # noqa: E402
from . import db  # noqa: E402

EXPECTED = (
    "deposit_or_withdrawal",
    "gambler",
    "amount_inr",
    "rail_id",
    "transaction hash",
    "tx_status",
    "tx_time_utc",
    "tx_amount",
    "counterparty",
    "source_object_key",
    "fact_type",
)


class CompactColumnTests(unittest.TestCase):
    def test_both_crypto_exports_use_the_same_columns(self):
        self.assertEqual(app_module._MONEY_FLOW_COMPACT_COLUMNS, EXPECTED)

    def test_counterparty_is_the_side_opposite_rail_id(self):
        deposit = {
            "rail_id": "TQAiceryW8Pbrpav4gvkNqevVEjpunqm8a",
            "direction": "deposit",
            "tx_from": "TVHL9Wfc2rM7MaqDq6TL5E4yQ6biSP6gMU",
            "tx_to": "TQAiceryW8Pbrpav4gvkNqevVEjpunqm8a",
        }
        withdrawal = {
            "rail_id": "TQAiceryW8Pbrpav4gvkNqevVEjpunqm8a",
            "direction": "withdrawal",
            "tx_from": "TQAiceryW8Pbrpav4gvkNqevVEjpunqm8a",
            "tx_to": "TVHL9Wfc2rM7MaqDq6TL5E4yQ6biSP6gMU",
        }
        self.assertEqual(app_module._flow_counterparty(deposit), "TVHL9Wfc2rM7MaqDq6TL5E4yQ6biSP6gMU")
        self.assertEqual(app_module._flow_counterparty(withdrawal), "TVHL9Wfc2rM7MaqDq6TL5E4yQ6biSP6gMU")


class UniqueRepresentativeTests(unittest.TestCase):
    def setUp(self):
        c = db.connect()
        c.execute("DELETE FROM payment_transactions")
        c.execute("DELETE FROM objects")
        c.executemany(
            "INSERT INTO objects(key,top_prefix,size,last_modified,etag,seen_utc,ext)"
            " VALUES (?,?,?,?,?,?,?)",
            [
                ("deposit/old.jpg", "deposit/", 10, "2026-01-01T00:00:00Z", "e1", "", ".jpg"),
                ("deposit/new.jpg", "deposit/", 10, "2026-02-01T00:00:00Z", "e2", "", ".jpg"),
            ],
        )
        c.executemany(
            "INSERT INTO payment_transactions"
            "(source_key,direction,rail_type,rail_id,txid,tx_status,tx_time,"
            " tx_amount,tx_from,tx_to,gambler,amount_inr,last_modified)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            [
                (
                    "deposit/old.jpg",
                    "deposit",
                    "crypto_wallet",
                    "TQAiceryW8Pbrpav4gvkNqevVEjpunqm8a",
                    "aa" * 32,
                    "derived",
                    "2026-01-01T00:00:00Z",
                    10.0,
                    "TVHL9Wfc2rM7MaqDq6TL5E4yQ6biSP6gMU",
                    "TQAiceryW8Pbrpav4gvkNqevVEjpunqm8a",
                    "g1",
                    500.0,
                    "2026-01-01T00:00:00Z",
                ),
                (
                    "deposit/new.jpg",
                    "deposit",
                    "crypto_wallet",
                    "TQAiceryW8Pbrpav4gvkNqevVEjpunqm8a",
                    "bb" * 32,
                    "verified",
                    "2026-02-01T00:00:00Z",
                    20.0,
                    "TJXhSVWjd2Uo9uu1abhSdZztq9cGEMQPqx",
                    "TQAiceryW8Pbrpav4gvkNqevVEjpunqm8a",
                    "g1",
                    900.0,
                    "2026-02-01T00:00:00Z",
                ),
            ],
        )
        c.commit()

    def test_newest_transaction_wins_and_is_coherent(self):
        reps = db.crypto_unique_representatives(["TQAiceryW8Pbrpav4gvkNqevVEjpunqm8a"])
        self.assertIn("TQAiceryW8Pbrpav4gvkNqevVEjpunqm8a", reps)
        rep = reps["TQAiceryW8Pbrpav4gvkNqevVEjpunqm8a"]
        self.assertEqual(rep["txid"], "bb" * 32)
        self.assertEqual(rep["tx_amount"], 20.0)
        self.assertEqual(rep["tx_from"], "TJXhSVWjd2Uo9uu1abhSdZztq9cGEMQPqx")
        self.assertEqual(rep["tx_to"], "TQAiceryW8Pbrpav4gvkNqevVEjpunqm8a")
        self.assertEqual(rep["source_key"], "deposit/new.jpg")

    def test_unknown_address_returns_nothing(self):
        self.assertEqual(db.crypto_unique_representatives(["TNLnEzdc3dnmHonxeDLS4RRgvGLDc6jN7c"]), {})
        self.assertEqual(db.crypto_unique_representatives([]), {})


if __name__ == "__main__":
    unittest.main()
