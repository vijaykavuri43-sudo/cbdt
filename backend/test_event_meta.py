"""Regression test: `enriched` SSE events must carry size/last_modified.

A sizeless event made the Live Feed modal coerce size to 0 and render a
real 336KB slip as an "Empty object (0 bytes)" folder marker (2026-09-11).
db.get_object_sizes() is the batch lookup backing the event fields.
"""

import os
import tempfile
import unittest
from pathlib import Path

_tmp = tempfile.TemporaryDirectory()
os.environ["S3LM_DATA"] = _tmp.name
os.environ["S3LM_REFERENCE_ROOT"] = str(Path(_tmp.name) / "no-references")

from . import db  # noqa: E402


class EventMetaTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        c = db.connect()
        c.execute(
            "INSERT INTO objects(key, top_prefix, ext, size, last_modified)"
            " VALUES (?,?,?,?,?)",
            ("probe/slip.jpg", "probe/", ".jpg", 336363, "2026-09-10T07:42:27Z"),
        )
        c.commit()

    def test_get_object_sizes_returns_size_and_modified(self):
        got = db.get_object_sizes(["probe/slip.jpg", "probe/missing.jpg"])
        self.assertEqual(got["probe/slip.jpg"]["size"], 336363)
        self.assertEqual(got["probe/slip.jpg"]["last_modified"], "2026-09-10T07:42:27Z")
        self.assertNotIn("probe/missing.jpg", got)

    def test_get_object_sizes_empty_input(self):
        self.assertEqual(db.get_object_sizes([]), {})


if __name__ == "__main__":
    unittest.main()
