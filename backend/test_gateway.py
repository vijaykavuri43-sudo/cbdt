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

    def test_network_urls_brands_and_qr_hosts(self):
        import json

        db.connect()
        c = db.connect()
        c.execute("DELETE FROM gateway_rows")
        c.execute("DELETE FROM objects")
        c.executemany(
            "INSERT INTO gateway_rows(key,row_no,kind,customer,amount,utr,website,ts,is_test)"
            " VALUES(?,?,?,?,?,?,?,?,?)",
            [
                (
                    "g1.csv",
                    1,
                    "gateway_deposit",
                    "u1",
                    1000.0,
                    "111111111111",
                    "newlotus",
                    "2026-01-01",
                    0,
                ),
                (
                    "g1.csv",
                    2,
                    "gateway_deposit",
                    "u2",
                    2000.0,
                    "222222222222",
                    "newlotus",
                    "2026-01-02",
                    0,
                ),
                (
                    "g1.csv",
                    3,
                    "gateway_deposit",
                    "t1",
                    1000.0,
                    "999",
                    "testbrand",
                    "2026-01-03",
                    1,
                ),
            ],
        )
        c.executemany(
            "INSERT INTO objects(key,size,etag,last_modified,top_prefix,extract_json) VALUES(?,?,?,?,?,?)",
            [
                (
                    "bankingqr/q1.png",
                    10,
                    "e1",
                    "2026-02-01",
                    "bankingqr/",
                    json.dumps(
                        {
                            "kind": "qr",
                            "raw_text": "upi://pay?pa=a@okaxis&pn=Shop https://pay.example.com/r/1",
                        }
                    ),
                ),
                (
                    "qrcode/q2.png",
                    10,
                    "e2",
                    "2026-02-02",
                    "qrcode/",
                    json.dumps(
                        {
                            "kind": "qr",
                            "raw_text": "upi://pay?pa=b@okaxis",
                            "expanded_url": "https://go.short/abc",
                        }
                    ),
                ),
            ],
        )
        c.commit()
        out = gateway.network_urls()
        by_src = {}
        for r in out["rows"]:
            by_src.setdefault(r["source"], []).append(r)
        brand = by_src["gateway-brand"][0]
        self.assertEqual(brand["host"], "newlotus")
        self.assertEqual(brand["txns"], 2)
        self.assertEqual(brand["amount_inr"], 3000.0)
        self.assertNotIn("testbrand", [r["host"] for r in out["rows"]])
        hosts = {(r["host"], r["source"]) for r in out["rows"]}
        self.assertIn(("pay.example.com", "qr-payload"), hosts)
        self.assertIn(("go.short", "qr-expanded"), hosts)
        self.assertEqual(out["counts"]["brands"], 1)

    def test_network_urls_repeat_host_last_seen(self):
        # Regression: second QR row for an already-seen host hit the else
        # branch, which read r["last_seen"] (not a selected column) and
        # raised IndexError -> /api/network-urls 500 on live.
        import json

        db.connect()
        c = db.connect()
        c.execute("DELETE FROM gateway_rows")
        c.execute("DELETE FROM objects")
        c.executemany(
            "INSERT INTO objects(key,size,etag,last_modified,top_prefix,extract_json) VALUES(?,?,?,?,?,?)",
            [
                (
                    "bankingqr/r1.png",
                    10,
                    "e1",
                    "2026-03-01",
                    "bankingqr/",
                    json.dumps({"kind": "qr", "raw_text": "https://dup.example/a"}),
                ),
                (
                    "bankingqr/r2.png",
                    10,
                    "e2",
                    "2026-03-05",
                    "bankingqr/",
                    json.dumps({"kind": "qr", "raw_text": "https://dup.example/b"}),
                ),
            ],
        )
        c.commit()
        out = gateway.network_urls()
        rows = [r for r in out["rows"] if r["host"] == "dup.example"]
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["hits"], 2)
        self.assertEqual(rows[0]["first_seen"], "2026-03-01")
        self.assertEqual(rows[0]["last_seen"], "2026-03-05")

    def test_normalize_brand_host(self):
        self.assertEqual(gateway.normalize_brand_host("reddy369"), "reddy369")
        self.assertEqual(
            gateway.normalize_brand_host("https://lotus365w.com"), "lotus365w.com"
        )
        self.assertEqual(
            gateway.normalize_brand_host("https://www.ambani247.com/home"),
            "www.ambani247.com",
        )
        self.assertEqual(
            gateway.normalize_brand_host("Mahadevbook777"), "mahadevbook777"
        )
        self.assertEqual(
            gateway.normalize_brand_host("https://Reddy247.co"), "reddy247.co"
        )
        self.assertEqual(gateway.normalize_brand_host(""), "")

    def test_is_valid_domain_host(self):
        self.assertTrue(gateway.is_valid_domain_host("bajrangi247.com"))
        self.assertTrue(gateway.is_valid_domain_host("lotus365w.co"))
        self.assertTrue(gateway.is_valid_domain_host("go.short"))
        self.assertFalse(gateway.is_valid_domain_host("reddy369"))
        self.assertFalse(gateway.is_valid_domain_host("click"))
        self.assertFalse(gateway.is_valid_domain_host("boi&pn=top"))
        self.assertFalse(
            gateway.is_valid_domain_host(
                "centralbankofindia.co.in28220010a00000052401047885520454995303356550201"
            )
        )

    def test_resolve_brand_alias_and_valid(self):
        r = gateway.resolve_brand("reddy369")
        self.assertFalse(r["is_valid_domain"])
        self.assertEqual(r["resolved_domain"], "reddy369.com")
        self.assertEqual(r["url"], "https://reddy369.com/")
        self.assertEqual(r["resolution"], "alias-resolved-live")
        v = gateway.resolve_brand("https://lotus365w.com")
        self.assertTrue(v["is_valid_domain"])
        self.assertEqual(v["host"], "lotus365w.com")
        self.assertEqual(v["url"], "https://lotus365w.com/")
        p = gateway.resolve_brand("click")
        self.assertEqual(p["resolution"], "placeholder")
        self.assertEqual(p["resolved_domain"], "")

    def test_network_urls_qr_glue_filtered(self):
        import json

        db.connect()
        c = db.connect()
        c.execute("DELETE FROM gateway_rows")
        c.execute("DELETE FROM objects")
        c.execute(
            "INSERT INTO objects(key,size,etag,last_modified,top_prefix,extract_json) VALUES(?,?,?,?,?,?)",
            (
                "bankingqr/glue.png",
                10,
                "e9",
                "2026-04-01",
                "bankingqr/",
                json.dumps(
                    {
                        "kind": "qr",
                        "raw_text": "upi://pay https://www.bankofindia.co.in&pa=boim-1@boi&pn=X "
                        "https://centralbankofindia.co.in28220010A00000052401047885520454995303356550201",
                    }
                ),
            ),
        )
        c.commit()
        out = gateway.network_urls()
        hosts = {r["host"] for r in out["rows"]}
        self.assertNotIn("boi&pn=x", hosts)
        self.assertFalse(any("centralbankofindia.co.in2822" in h for h in hosts))
        self.assertIn("counts", out)
        self.assertIn("valid_frontends", out["counts"])
        self.assertIn("alias_rows", out["counts"])


if __name__ == "__main__":
    unittest.main()
