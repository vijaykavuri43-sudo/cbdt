"""Disk snapshot cache tests (fast first paint after deploy, 2026-11-11).

A fresh uvicorn worker used to answer /api/status and /api/stats with inline
full-table builds, so every bucket switch / deploy showed a blank console for
seconds. Snapshots are now persisted per source and restored as stale so the
caller paints immediately while one background rebuild runs.
"""

import json
import tempfile
import time
import unittest
from pathlib import Path

from . import app as app_mod


class DiskCacheTests(unittest.TestCase):
    def setUp(self):
        self._orig_dir = app_mod._CACHE_DIR
        self._tmp = tempfile.TemporaryDirectory()
        app_mod._CACHE_DIR = Path(self._tmp.name)
        app_mod._stats_cache.clear()
        app_mod._persist_last.clear()
        app_mod._disk_lookup_at.clear()

    def tearDown(self):
        app_mod._CACHE_DIR = self._orig_dir
        app_mod._stats_cache.clear()
        app_mod._persist_last.clear()
        app_mod._disk_lookup_at.clear()
        self._tmp.cleanup()

    def _write_snapshot(self, key: str, value, saved: float | None = None):
        app_mod._CACHE_DIR.mkdir(parents=True, exist_ok=True)
        payload = {
            "key": key,
            "saved": time.time() if saved is None else saved,
            "value": value,
        }
        app_mod._cache_file(key).write_text(json.dumps(payload), encoding="utf-8")

    def test_restores_as_stale_and_returns_instantly(self):
        self._write_snapshot("status", {"objects_total": 123})
        before = time.monotonic()
        self.assertTrue(app_mod._load_cache_from_disk("status"))
        ent = app_mod._stats_cache["status"]
        self.assertEqual(ent[1], {"objects_total": 123})
        # Back-dated inside stale_grace: cached() serves it and rebuilds in
        # the background instead of blocking the request thread.
        self.assertGreaterEqual(time.monotonic() - ent[0], 600.0)
        self.assertLess(time.monotonic() - before, 2.0)

    def test_expired_snapshot_is_ignored(self):
        self._write_snapshot("status", {"objects_total": 1}, saved=time.time() - 2 * 86400)
        self.assertFalse(app_mod._load_cache_from_disk("status"))
        self.assertNotIn("status", app_mod._stats_cache)

    def test_non_persisted_key_is_ignored(self):
        self._write_snapshot("flows:whatever", {"rows": []})
        self.assertFalse(app_mod._load_cache_from_disk("flows:whatever"))

    def test_invalidate_removes_persisted_snapshot(self):
        self._write_snapshot("gateway:summary", {"brands": 1})
        self.assertTrue(app_mod._cache_file("gateway:summary").exists())
        app_mod._invalidate_prefix("gateway:")
        self.assertFalse(app_mod._cache_file("gateway:summary").exists())

    def test_missing_file_is_not_an_error(self):
        self.assertFalse(app_mod._load_cache_from_disk("stats"))


if __name__ == "__main__":
    unittest.main()
