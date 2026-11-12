"""Root folder tree fast path (2026-11-11 Explorer timeout fix).

subfolders("") used to substr-scan every object in the bucket. On the 758k-row
live table that exceeded the Explorer's 20s budget while the sidebar was
already materialized in prefix_stats_cache. Root must read that tiny table.
"""

import os
import tempfile
import unittest
from pathlib import Path

_tmp = tempfile.TemporaryDirectory()
os.environ["S3LM_DATA"] = _tmp.name
os.environ["S3LM_REFERENCE_ROOT"] = str(Path(_tmp.name) / "no-references")

from . import db  # noqa: E402


class RootTreeTests(unittest.TestCase):
    def setUp(self):
        c = db.connect()
        c.execute("DELETE FROM objects")
        c.execute("DELETE FROM prefix_stats_cache")
        c.commit()
        db._tree_cache.clear()
        db._list_total_cache.clear()

    def _seed_cache_only(self):
        c = db.connect()
        c.executemany(
            "INSERT INTO prefix_stats_cache(p,n,s,newest,refreshed_utc)"
            " VALUES(?,?,?,?,?)",
            [
                ("deposit/", 2, 200, "2026-11-11T00:00:00Z", "now"),
                ("withdrawal/", 1, 50, "2026-11-11T00:00:00Z", "now"),
                ("(root)", 1, 7, "2026-11-11T00:00:00Z", "now"),
            ],
        )
        c.commit()

    def test_root_reads_materialized_stats_without_touching_objects(self):
        self._seed_cache_only()
        out = db.subfolders("")
        self.assertEqual(
            out["dirs"],
            [
                {"seg": "deposit", "n": 2, "size": 200},
                {"seg": "withdrawal", "n": 1, "size": 50},
            ],
        )
        self.assertEqual(out["files_n"], 1)
        self.assertEqual(out["files_size"], 7)

        # Proof the fast path did not scan objects: clear the table and the
        # per-prefix tree cache; the materialized view must still answer.
        c = db.connect()
        c.execute("DELETE FROM objects")
        c.commit()
        db._tree_cache.clear()
        out2 = db.subfolders("")
        self.assertEqual(out2, out)

    def test_root_falls_back_to_live_aggregate_when_cache_empty(self):
        c = db.connect()
        c.executemany(
            "INSERT INTO objects(key,top_prefix,size,last_modified,etag,seen_utc,ext)"
            " VALUES(?,?,?,?,?,?,?)",
            [
                ("deposit/a.jpg", "deposit/", 10, "", "e1", "", ".jpg"),
                ("deposit/b.jpg", "deposit/", 20, "", "e2", "", ".jpg"),
                ("loose.txt", "(root)", 5, "", "e3", "", ".txt"),
            ],
        )
        c.commit()
        out = db.subfolders("")
        self.assertEqual(out["dirs"], [{"seg": "deposit", "n": 2, "size": 30}])
        self.assertEqual(out["files_n"], 1)
        self.assertEqual(out["files_size"], 5)

    def test_empty_listing_skips_page_scan(self):
        out = db.list_objects("does-not-exist/")
        self.assertEqual(out, {"total": 0, "items": []})

    def test_root_listing_uses_partial_index_and_correct_orders(self):
        c = db.connect()
        c.executemany(
            "INSERT INTO objects(key,top_prefix,size,last_modified,etag,seen_utc,ext)"
            " VALUES(?,?,?,?,?,?,?)",
            [
                ("z.txt", "(root)", 30, "2026-01-03T00:00:00Z", "e3",
                 "2026-01-05T00:00:00Z", ".txt"),
                ("a.txt", "(root)", 10, "2026-01-01T00:00:00Z", "e1",
                 "2026-01-01T00:00:00Z", ".txt"),
                ("m.txt", "(root)", 20, "2026-01-02T00:00:00Z", "e2",
                 "2026-01-03T00:00:00Z", ".txt"),
            ],
        )
        c.commit()
        db._list_total_cache.clear()

        def keys(order, offset=0, limit=200):
            out = db.list_objects("", limit=limit, offset=offset, order=order)
            self.assertEqual(out["total"], 3)
            return [i["key"] for i in out["items"]]

        self.assertEqual(keys("key"), ["a.txt", "m.txt", "z.txt"])
        self.assertEqual(keys("-modified"), ["z.txt", "m.txt", "a.txt"])
        self.assertEqual(keys("-seen"), ["z.txt", "m.txt", "a.txt"])
        self.assertEqual(keys("size"), ["z.txt", "m.txt", "a.txt"])
        self.assertEqual(keys("key", offset=1, limit=1), ["m.txt"])
        self.assertEqual(keys("size", offset=5, limit=1), [])


if __name__ == "__main__":
    unittest.main()
