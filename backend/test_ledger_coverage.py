"""Regression: UTR slip-match coverage queries must use the partial UTR index.

2026-09-05 prod incident: ledger.summary() and gateway.summary() hung (>60s,
even >180s on-VM) with only ~4K ledger rows. Root cause: the correlated
EXISTS subquery `WHERE p.utr=<outer>` could not use the partial index
idx_pay_utr (WHERE utr != '') because the subquery never stated p.utr != '',
so SQLite planned SCAN p (165K rows) per outer UTR. Fix: add the redundant
`AND p.utr!=''` predicate. These tests lock in the fast plan + correctness.
"""

import os
import tempfile
import time
import unittest
from pathlib import Path

_tmp = tempfile.TemporaryDirectory()
os.environ["S3LM_DATA"] = _tmp.name
os.environ["S3LM_REFERENCE_ROOT"] = str(Path(_tmp.name) / "no-references")

from . import db, gateway, ledger  # noqa: E402


def _seed(c):
    c.execute("DELETE FROM ledger_rows")
    c.execute("DELETE FROM ledger_files")
    c.execute("DELETE FROM gateway_rows")
    c.execute("DELETE FROM gateway_files")
    c.execute("DELETE FROM payment_transactions")
    c.executemany(
        "INSERT INTO payment_transactions(source_key,direction,utr,rail_type,rail_id,amount_inr)"
        " VALUES(?,?,?,?,?,?)",
        [
            ("s1", "deposit", "UTR111", "upi", "a@ok", 100.0),
            ("s2", "deposit", "UTR222", "upi", "b@ok", 200.0),
            ("s3", "deposit", "", "upi", "c@ok", 50.0),
        ],
    )
    c.executemany(
        "INSERT INTO ledger_rows(key,row_no,type,sender,gambler,amount,bonus,cash,utr,remark,ts)"
        " VALUES(?,?,?,?,?,?,?,?,?,?,?)",
        [
            ("k1", 1, "Deposit", "br1", "g1", 100, 0, 100, "UTR111", "", "t"),
            ("k1", 2, "Withdraw", "", "g1", 40, 0, 40, "UTR999", "", "t"),
        ],
    )
    c.execute(
        "INSERT INTO ledger_files(key,etag,size,last_modified,kind,rows,status,synced_utc)"
        " VALUES(?,?,?,?,?,?,?,?)",
        ("k1", "e", 10, "2026-09-05T00:00:00Z", "transaction", 2, "indexed", "t"),
    )
    c.commit()


def _plan_has_full_scan(c, sql):
    plan = c.execute("EXPLAIN QUERY PLAN " + sql).fetchall()
    return any("SCAN p" in (r[3] if len(r) > 3 else r[-1]) for r in plan)


LEDGER_COV = (
    "SELECT COUNT(DISTINCT l.utr) FROM ledger_rows l"
    " WHERE l.utr!='' AND EXISTS (SELECT 1 FROM payment_transactions p"
    " WHERE p.utr=l.utr AND p.utr!='')"
)
GATEWAY_COV = (
    "SELECT COUNT(DISTINCT g.utr) FROM gateway_rows g"
    " WHERE g.utr!='' AND g.is_test=0"
    " AND EXISTS (SELECT 1 FROM payment_transactions p"
    " WHERE p.utr=g.utr AND p.utr!='')"
)


class CoveragePlanTests(unittest.TestCase):
    def test_no_full_scan_of_payment_transactions(self):
        db.connect()
        c = db.connect()
        _seed(c)
        self.assertFalse(
            _plan_has_full_scan(c, LEDGER_COV),
            "ledger coverage query must not full-scan payment_transactions",
        )
        self.assertFalse(
            _plan_has_full_scan(c, GATEWAY_COV),
            "gateway coverage query must not full-scan payment_transactions",
        )

    def test_summaries_fast_and_correct(self):
        db.connect()
        c = db.connect()
        _seed(c)
        t = time.time()
        s = ledger.summary()
        self.assertLess(time.time() - t, 10, "ledger.summary took too long")
        self.assertEqual(s["coverage"]["ledger_utrs"], 2)
        self.assertEqual(s["coverage"]["matched_in_flows"], 1)
        self.assertEqual(s["files"], {"indexed": 1, "total": 1})
        t = time.time()
        g = gateway.summary()
        self.assertLess(time.time() - t, 10, "gateway.summary took too long")
        self.assertEqual(g["coverage"]["gateway_utrs"], 0)
        self.assertEqual(g["coverage"]["matched_in_flows"], 0)


if __name__ == "__main__":
    unittest.main()
