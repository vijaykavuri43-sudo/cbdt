"""Tests for /api/object integrity gate + S3 repair (voice-note 0:00, 2026-09-05)."""

import asyncio
import hashlib
import json
import os
import tempfile
import unittest
from pathlib import Path

_tmp = tempfile.TemporaryDirectory()
os.environ["S3LM_DATA"] = _tmp.name
os.environ["S3LM_REFERENCE_ROOT"] = str(Path(_tmp.name) / "no-references")

from starlette.requests import Request  # noqa: E402

from . import app  # noqa: E402

GOOD_WEBM = bytes.fromhex(
    "1a45dfa39f4286810142f7810142f2810442f381084282847765626d42878104"
) + bytes(4000)
GARBAGE = b"\x00\x01\x02\x03not-a-real-media-file" * 200
WAV_KEY = "chat_file/spcbtfvip_hanumanjii_1788577293122.wav"
GOOD_PNG = bytes.fromhex("89504e470d0a1a0a") + bytes(5000)
PNG_KEY = "chat_file/hanumanjii_1_Account_Summary_Report_1.png"


class _FakeGCSMissing:
    """Pretends the GCS mirror copy hasn't landed yet (mirror lag)."""

    def get_object_stream(self, key, chunk_size=65536):
        raise RuntimeError(f"GetObject {key} -> not found in GCS mirror")


def _req():
    return Request({"type": "http", "method": "GET", "headers": []})


class _FakeResp:
    def __init__(self, payload: bytes):
        self._payload = payload

    def iter_bytes(self, _chunk=65536):
        yield self._payload

    def close(self):
        pass


class _FakeGCS:
    """Pretends to be the GCS mirror client serving corrupt bytes."""

    def __init__(self, payload: bytes, sha: str = ""):
        self.payload = payload
        self.sha = sha

    def get_object_stream(self, key, chunk_size=65536):
        return _FakeResp(self.payload), {
            "content_type": "audio/x-wav",
            "content_length": str(len(self.payload)),
            "etag": "fake-etag",
            "last_modified": "2026-09-05T00:00:00Z",
            "sha256": self.sha,
        }


def _body_of(resp):
    if not hasattr(resp, "body_iterator"):
        # FileResponse (cache-write path): read the served file
        return Path(resp.path).read_bytes()

    async def _collect():
        return b"".join([c async for c in resp.body_iterator])

    return asyncio.new_event_loop().run_until_complete(_collect())


class VerifyHelperTests(unittest.TestCase):
    def test_good_bytes_pass(self):
        sha = hashlib.sha256(GOOD_WEBM).hexdigest()
        self.assertEqual(
            app.verify_object_bytes(WAV_KEY, GOOD_WEBM, sha, len(GOOD_WEBM)), ""
        )

    def test_empty(self):
        self.assertEqual(app.verify_object_bytes(WAV_KEY, b"", "", 0), "empty")

    def test_hash_mismatch(self):
        self.assertEqual(
            app.verify_object_bytes(
                WAV_KEY, GARBAGE, hashlib.sha256(GOOD_WEBM).hexdigest(), 0
            ),
            "hash-mismatch",
        )

    def test_size_mismatch(self):
        self.assertEqual(
            app.verify_object_bytes(WAV_KEY, GOOD_WEBM, "", len(GOOD_WEBM) + 7),
            "size-mismatch",
        )

    def test_magic_mismatch_media(self):
        self.assertEqual(app.verify_object_bytes(WAV_KEY, GARBAGE), "magic-mismatch")
        self.assertEqual(
            app.verify_object_bytes("chat_file/x.png", GARBAGE), "magic-mismatch"
        )

    def test_garbage_non_media_not_flagged(self):
        self.assertEqual(app.verify_object_bytes("csv-files/a.csv", GARBAGE), "")

    def test_real_wav_magic_passes(self):
        riff = b"RIFF" + bytes(4) + b"WAVE" + bytes(100)
        self.assertEqual(app.verify_object_bytes("chat_file/a.wav", riff), "")

    def test_ext_parsing(self):
        self.assertEqual(app._ext_of_key("a/b/c.WAV"), ".wav")
        self.assertEqual(app._ext_of_key("noext"), "")


class RepairRouteTests(unittest.TestCase):
    def setUp(self):
        self._old = (
            app.object_store,
            app._actual_source,
            app._fetch_repair_bytes,
            app._OBJ_CACHE_DIR,
        )
        app._OBJ_CACHE_DIR = Path(_tmp.name) / "objcache-route" / self._testMethodName
        app._actual_source = lambda: "gcs"  # noqa: E731

    def tearDown(self):
        (
            app.object_store,
            app._actual_source,
            app._fetch_repair_bytes,
            app._OBJ_CACHE_DIR,
        ) = self._old

    def test_corrupt_mirror_repaired_from_s3(self):
        app.object_store = _FakeGCS(GARBAGE, hashlib.sha256(GOOD_WEBM).hexdigest())
        app._fetch_repair_bytes = lambda key: (GOOD_WEBM, "")  # noqa: E731
        resp = app.get_object(WAV_KEY, _req(), download=0, thumb=0, _=None)
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.media_type, "audio/webm")
        self.assertEqual(resp.headers.get("X-Cache-Status"), "MISS")
        # second request must be a healed disk HIT with identical bytes
        resp2 = app.get_object(WAV_KEY, _req(), download=0, thumb=0, _=None)
        self.assertEqual(resp2.headers.get("X-Cache-Status"), "HIT-disk")
        self.assertEqual(resp2.media_type, "audio/webm")

    def test_unrepairable_served_uncached_with_header(self):
        app.object_store = _FakeGCS(GARBAGE, hashlib.sha256(GOOD_WEBM).hexdigest())
        app._fetch_repair_bytes = lambda key: (None, "s3 404")  # noqa: E731
        resp = app.get_object(WAV_KEY, _req(), download=0, thumb=0, _=None)
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.headers.get("X-Object-Integrity"), "hash-mismatch")
        body = _body_of(resp)
        self.assertEqual(body, GARBAGE)
        # nothing cached: still a MISS next time
        resp2 = app.get_object(WAV_KEY, _req(), download=0, thumb=0, _=None)
        self.assertEqual(resp2.headers.get("X-Cache-Status"), "MISS")

    def test_poisoned_cache_entry_evicted_and_repaired(self):
        entry = app._objcache_entry(WAV_KEY, "", 0)
        entry.parent.mkdir(parents=True, exist_ok=True)
        entry.write_bytes(GARBAGE)
        entry.with_suffix(".json").write_text(json.dumps({"media": "audio/x-wav"}))
        app.object_store = _FakeGCS(GARBAGE, "")
        app._fetch_repair_bytes = lambda key: (GOOD_WEBM, "")  # noqa: E731
        resp = app.get_object(WAV_KEY, _req(), download=0, thumb=0, _=None)
        self.assertEqual(resp.media_type, "audio/webm")
        self.assertEqual(entry.read_bytes(), GOOD_WEBM)

    def test_small_download_verified(self):
        app.object_store = _FakeGCS(GOOD_WEBM, hashlib.sha256(GOOD_WEBM).hexdigest())
        resp = app.get_object(WAV_KEY, _req(), download=1, thumb=0, _=None)
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.media_type, "application/octet-stream")
        self.assertIn("attachment", resp.headers.get("Content-Disposition", ""))
        self.assertEqual(_body_of(resp), GOOD_WEBM)

    def test_mirror_miss_served_from_s3(self):
        app.object_store = _FakeGCSMissing()
        app._fetch_repair_bytes = lambda key: (GOOD_PNG, "")  # noqa: E731
        resp = app.get_object(PNG_KEY, _req(), download=0, thumb=0, _=None)
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.media_type, "image/png")
        self.assertEqual(resp.headers.get("X-Object-Source"), "s3-repair")
        self.assertEqual(_body_of(resp), GOOD_PNG)
        # repaired bytes are cached: repeat is a disk HIT, still marked
        resp2 = app.get_object(PNG_KEY, _req(), download=0, thumb=0, _=None)
        self.assertEqual(resp2.headers.get("X-Cache-Status"), "HIT-disk")
        self.assertEqual(resp2.media_type, "image/png")

    def test_mirror_miss_download_served_from_s3(self):
        app.object_store = _FakeGCSMissing()
        app._fetch_repair_bytes = lambda key: (GOOD_PNG, "")  # noqa: E731
        resp = app.get_object(PNG_KEY, _req(), download=1, thumb=0, _=None)
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.media_type, "application/octet-stream")
        self.assertIn("attachment", resp.headers.get("Content-Disposition", ""))
        self.assertEqual(resp.headers.get("X-Object-Source"), "s3-repair")
        self.assertEqual(_body_of(resp), GOOD_PNG)

    def test_mirror_and_s3_miss_stays_404(self):
        from fastapi import HTTPException

        app.object_store = _FakeGCSMissing()
        app._fetch_repair_bytes = lambda key: (None, "s3 404")  # noqa: E731
        with self.assertRaises(HTTPException) as ctx:
            app.get_object(PNG_KEY, _req(), download=0, thumb=0, _=None)
        self.assertEqual(ctx.exception.status_code, 404)


def _range_req(spec: str):
    return Request(
        {
            "type": "http",
            "method": "GET",
            "headers": [(b"range", spec.encode())],
        }
    )


class ParseRangeTests(unittest.TestCase):
    def test_full_span(self):
        self.assertEqual(app._parse_range("bytes=0-99", 1000), (0, 99))

    def test_open_end_clamped(self):
        self.assertEqual(app._parse_range("bytes=900-", 1000), (900, 999))

    def test_suffix(self):
        self.assertEqual(app._parse_range("bytes=-100", 1000), (900, 999))

    def test_bad_inputs_none(self):
        self.assertIsNone(app._parse_range("", 1000))
        self.assertIsNone(app._parse_range("items=0-10", 1000))
        self.assertIsNone(app._parse_range("bytes=2000-3000", 1000))
        self.assertIsNone(app._parse_range("bytes=abc-def", 1000))


class RangeRouteTests(unittest.TestCase):
    """Voice-note seek (2026-09-11): audio Range requests get 206 slices."""

    def setUp(self):
        self._old = (
            app.object_store,
            app._actual_source,
            app._fetch_repair_bytes,
            app._OBJ_CACHE_DIR,
        )
        app._OBJ_CACHE_DIR = Path(_tmp.name) / "objcache-range" / self._testMethodName
        app._actual_source = lambda: "gcs"  # noqa: E731

    def tearDown(self):
        (
            app.object_store,
            app._actual_source,
            app._fetch_repair_bytes,
            app._OBJ_CACHE_DIR,
        ) = self._old

    def test_audio_range_returns_206_slice(self):
        app.object_store = _FakeGCS(GOOD_WEBM, hashlib.sha256(GOOD_WEBM).hexdigest())
        app._fetch_repair_bytes = lambda key: (None, "unused")  # noqa: E731
        resp = app.get_object(
            WAV_KEY, _range_req("bytes=0-99"), download=0, thumb=0, _=None
        )
        self.assertEqual(resp.status_code, 206)
        self.assertEqual(
            resp.headers.get("Content-Range"), f"bytes 0-99/{len(GOOD_WEBM)}"
        )
        self.assertEqual(resp.headers.get("Accept-Ranges"), "bytes")
        self.assertEqual(_body_of(resp), GOOD_WEBM[:100])

    def test_audio_no_range_returns_200(self):
        app.object_store = _FakeGCS(GOOD_WEBM, hashlib.sha256(GOOD_WEBM).hexdigest())
        app._fetch_repair_bytes = lambda key: (None, "unused")  # noqa: E731
        resp = app.get_object(WAV_KEY, _req(), download=0, thumb=0, _=None)
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.headers.get("Accept-Ranges"), "bytes")
        self.assertEqual(_body_of(resp), GOOD_WEBM)

    def test_bad_range_falls_back_to_200(self):
        app.object_store = _FakeGCS(GOOD_WEBM, hashlib.sha256(GOOD_WEBM).hexdigest())
        app._fetch_repair_bytes = lambda key: (None, "unused")  # noqa: E731
        resp = app.get_object(
            WAV_KEY, _range_req("bytes=99999-100000"), download=0, thumb=0, _=None
        )
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(_body_of(resp), GOOD_WEBM)


if __name__ == "__main__":
    unittest.main()
