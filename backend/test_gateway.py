"""Tests for payment-gateway CSV ingestion — real schemas observed 2026-09-04."""

import os
import tempfile
import unittest
from pathlib import Path

_tmp = tempfile.TemporaryDirectory()
os.environ["S3LM_DATA"] = _tmp.name
os.environ["S3LM_REFERENCE_ROOT"] = str(Path(_tmp.name) / "no-references")

from . import db, gateway  # noqa: E402

DEPOSIT_V1 = (
    "Customer Name,Merchant OrderID,Amount,Mode,Website,Client Name,ActionBy,Receipt,Type,Approved At,Created At\n"
    "i100pay,ecce8abb6d46f5b0427a48e5d4064247e6336d94671bda2e71ec7fb91c2a97ab,1001,,bhavaniexch,ma1tr1cl2,ma1tr1,"
    "ZZUDR8VFJW,payin,Sat Oct 26 2024 01:08:44 GMT+0530 (India Standard Time),Sat Oct 26 2024 00:54:18 GMT+0530 (India Standard Time)\n"
)

DEPOSIT_V2 = (
    "Customer Name,UTR,Amount,Mode,Website,Client Name,ActionBy,Receipt,Type,Approved At,Created At\n"
    "vikash098,393101367400,1700,UPI,newlotus,newlotus,trader01,WKNYMQR2YC,payin,01/12/2025 11:11 AM,01/12/2025 11:02 AM\n"
    "master,12345678908765432,1000,UPI,4xexch,testclient,testtrader,E6IA7AS5XY,payin,08/06/2026 11:21 AM,08/06/2026 11:21 AM\n"
)

PAYOUT = (
    "Holder Name,Account Number,IFSC Number,Transaction Id,Amount\n"
    "FITNESS HOUSE,24030200001617,FDRL0000001,3355419327,100\n"
    "Bank Holder Name,121212121,PUNB0999900,3355422285,100\n"
)


class GatewayParseTests(unittest.TestCase):
    def test_deposit_v1_32hex_orderid_not_utr(self):
        rows = gateway.parse_gateway_deposit(DEPOSIT_V1)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["utr"], "")  # 32-hex is an order id, not a UTR
        self.assertEqual(rows[0]["website"], "bhavaniexch")
        self.assertEqual(rows[0]["amount"], 1001.0)
        self.assertEqual(rows[0]["is_test"], 0)

    def test_deposit_v2_utr_and_test_flag(self):
        rows = gateway.parse_gateway_deposit(DEPOSIT_V2)
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0]["utr"], "393101367400")
        self.assertEqual(rows[0]["website"], "newlotus")
        self.assertEqual(rows[0]["is_test"], 0)
        self.assertEqual(rows[1]["is_test"], 1)  # testclient/testtrader synthetic

    def test_payout_accounts_and_test_flag(self):
        rows = gateway.parse_gateway_payout(PAYOUT)
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0]["account"], "24030200001617")
        self.assertEqual(rows[0]["ifsc"], "FDRL0000001")
        self.assertEqual(rows[0]["is_test"], 0)
        self.assertEqual(rows[1]["is_test"], 1)  # placeholder holder/account

    def test_sync_and_summary_roundtrip(self):
        class FakeResp:
            def __init__(self, data):
                self._d = data

            def read(self):
                return self._d

            def close(self):
                pass

        class FakeStore:
            def __init__(self, blobs):
                self.blobs = blobs

            def get_object_stream(self, key):
                return FakeResp(self.blobs[key]), None

        db.connect()
        c = db.connect()
        c.execute("DELETE FROM gateway_rows")
        c.execute("DELETE FROM gateway_files")
        c.execute("DELETE FROM objects")
        c.executemany(
            "INSERT INTO objects(key,size,etag,last_modified,top_prefix) VALUES(?,?,?,?,?)",
            [
                (
                    "paymentgateway/csv-files/deposit_1.csv",
                    10,
                    "e1",
                    "2026-01-01T00:00:00.000Z",
                    "paymentgateway/",
                ),
                (
                    "paymentgateway/csv-files/payout_1.csv",
                    10,
                    "e2",
                    "2026-01-02T00:00:00.000Z",
                    "paymentgateway/",
                ),
            ],
        )
        c.commit()
        store = FakeStore(
            {
                "paymentgateway/csv-files/deposit_1.csv": DEPOSIT_V2.encode(),
                "paymentgateway/csv-files/payout_1.csv": PAYOUT.encode(),
            }
        )
        out = gateway.sync_gateway(store, limit_files=10)
        self.assertEqual(out["indexed"], 2)
        s = gateway.summary()
        self.assertEqual(s["deposits"]["rows"], 1)  # test row excluded
        self.assertEqual(s["deposits"]["brands"], 1)
        self.assertEqual(s["brands"][0]["website"], "newlotus")
        self.assertEqual(s["test_rows_excluded"], 2)
        self.assertEqual(s["payouts"]["accounts"], 1)
        self.assertEqual(s["files"]["bucket_total"], 2)


if __name__ == "__main__":
    unittest.main()
