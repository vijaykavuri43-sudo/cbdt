"""Tests for the memory-hygiene loop helper (OOM-hardening, 2026-09-08)."""

import unittest

from .collector import _rss_mb


class RssMbTests(unittest.TestCase):
    def test_returns_float_or_none_never_raises(self):
        v = _rss_mb()
        self.assertTrue(v is None or isinstance(v, float))
        if v is not None:
            self.assertGreater(v, 0)


if __name__ == "__main__":
    unittest.main()
