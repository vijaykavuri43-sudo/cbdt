"""Tests for cached() stampede protection (morning-outage fix, 2026-09-06).

Without singleflight + stale-serve, N tabs expiring a heavy aggregate
(ledger summary ~100s) each fire their own rebuild concurrently, filling the
threadpool and timing out every tab ("app not accessible").
"""

import threading
import time
import unittest

from .app import _invalidate_prefix, _stats_cache, cached


class CachedStampedeTests(unittest.TestCase):
    def setUp(self):
        _stats_cache.clear()

    def tearDown(self):
        _stats_cache.clear()
        _invalidate_prefix("t:")

    def test_fresh_and_ttl_hit(self):
        calls = []
        v1 = cached("t:a", 60.0, lambda: calls.append(1) or {"n": 1})
        v2 = cached("t:a", 60.0, lambda: calls.append(1) or {"n": 2})
        self.assertEqual(v1, {"n": 1})
        self.assertEqual(v2, {"n": 1})
        self.assertEqual(len(calls), 1)

    def test_concurrent_expiry_builds_once(self):
        # Seed a stale entry, then hammer from 10 threads: builder must run once.
        _stats_cache["t:b"] = (time.monotonic() - 1000.0, {"n": "stale"})
        started = threading.Event()
        release = threading.Event()
        runs = []

        def slow_builder():
            runs.append(1)
            started.set()
            release.wait(10)
            return {"n": "fresh"}

        outs = []
        ts = [
            threading.Thread(
                target=lambda: outs.append(cached("t:b", 1.0, slow_builder))
            )
            for _ in range(10)
        ]
        for t in ts:
            t.start()
        self.assertTrue(started.wait(10))
        time.sleep(0.3)  # let the herd arrive while rebuild is in-flight
        release.set()
        for t in ts:
            t.join(15)
        # Every caller got an answer; only one rebuild ran.
        self.assertEqual(len(outs), 10)
        for o in outs:
            self.assertIn(o, ({"n": "stale"}, {"n": "fresh"}))
        self.assertEqual(len(runs), 1)

    def test_stale_served_during_rebuild(self):
        _stats_cache["t:c"] = (time.monotonic() - 1000.0, {"n": "stale"})
        release = threading.Event()

        def slow_builder():
            release.wait(10)
            return {"n": "fresh"}

        bg = threading.Thread(target=lambda: cached("t:c", 1.0, slow_builder))
        bg.start()
        time.sleep(0.3)
        # Main thread must get the stale value instantly, not block 10s.
        t0 = time.monotonic()
        val = cached("t:c", 1.0, lambda: {"n": "other"})
        dt = time.monotonic() - t0
        self.assertEqual(val, {"n": "stale"})
        self.assertLess(dt, 5.0)
        release.set()
        bg.join(15)
        # After rebuild lands, fresh value is served.
        self.assertEqual(cached("t:c", 1000.0, lambda: {"n": "other"}), {"n": "fresh"})

    def test_invalidate_prefix_still_works(self):
        cached("t:d", 60.0, lambda: {"n": 1})
        _invalidate_prefix("t:")
        calls = []
        val = cached("t:d", 60.0, lambda: calls.append(1) or {"n": 2})
        self.assertEqual(val, {"n": 2})
        self.assertEqual(len(calls), 1)


if __name__ == "__main__":
    unittest.main()
