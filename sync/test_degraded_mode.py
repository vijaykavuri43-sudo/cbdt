"""Degraded write-blocked mode (review 2026-09-08): the sync daemon must
survive GCS write outages (e.g. 403 billing delinquent) instead of dying
into a docker restart loop."""

import importlib.util
import sys
from pathlib import Path


def _load_sync():
    mod_path = Path(__file__).resolve().parent / "s3-gcs-sync.py"
    spec = importlib.util.spec_from_file_location("s3_gcs_sync", mod_path)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules["s3_gcs_sync"] = mod
    spec.loader.exec_module(mod)
    return mod


sync = _load_sync()


class _FakeBlob:
    def __init__(self, fail=None):
        self.fail = fail
        self.metadata = None
        self.uploads = 0

    def upload_from_string(self, *a, **k):
        self.uploads += 1
        if self.fail is not None:
            raise self.fail


class _FakeBucket:
    def __init__(self, fail=None):
        self._blob = _FakeBlob(fail)

    def blob(self, name):
        return self._blob


def test_probe_writes_success_clears():
    sync.WRITE_STATE.update(blocked=True, reason="old")
    ok, err = sync.probe_writes(_FakeBucket(), _FakeBucket()._blob)
    assert ok is True and err == ""


def test_probe_writes_failure_never_raises():
    err = RuntimeError("403 billing disabled")
    ok, msg = sync.probe_writes(_FakeBucket(), _FakeBlob(fail=err))
    assert ok is False
    assert "403" in msg


def test_uploads_paused_reflects_state():
    sync.WRITE_STATE.update(blocked=True, reason="x")
    assert sync.uploads_paused() is True
    sync.WRITE_STATE.update(blocked=False, reason="")
    assert sync.uploads_paused() is False


class _FakeResp403:
    status_code = 403
    headers = {}

    def close(self):
        pass


class _FakeS3Denied:
    def build_request(self, *a, **k):
        return None

    def send(self, req, stream=False):
        return _FakeResp403()


class _NoopThrottle:
    def wait(self):
        pass


def _denied_worker(state_db):
    import threading

    w = sync.SyncWorker.__new__(sync.SyncWorker)
    w.s3 = _FakeS3Denied()
    w.bucket = _FakeBucket()
    w._db = state_db
    w.stats = {"errors": 0, "uploaded": 0, "bytes": 0, "notified": 0}
    w.stop = threading.Event()
    w.get_throttle = _NoopThrottle()
    return w


def test_get_403_terminal_tombstone_no_error():
    """Review 2026-09-22 (live b3: 182k cumulative errors): an S3 GET 403
    (listed but unreadable — upstream ACL) is a stable custody fact, not a
    transient failure. sync_one must tombstone it TERMINAL and return
    cleanly — no exception, no errors increment, excluded from retry loop."""
    import sqlite3

    state_db = sqlite3.connect(":memory:")
    state_db.execute(
        """CREATE TABLE sync_failures(
            key TEXT PRIMARY KEY, size INTEGER, etag TEXT,
            first_seen_utc TEXT, last_error TEXT, last_attempt_utc TEXT,
            fail_count INTEGER DEFAULT 0, terminal INTEGER DEFAULT 0)"""
    )
    w = _denied_worker(state_db)
    w.sync_one(("site-image/denied.apk", 1234, "etag0"))  # must not raise
    row = state_db.execute(
        "SELECT terminal, fail_count, last_error FROM sync_failures WHERE key=?",
        ("site-image/denied.apk",),
    ).fetchone()
    assert row is not None
    assert row[0] == 1  # terminal
    assert "403" in (row[2] or "")
    assert w.stats["errors"] == 0
    pending = state_db.execute(
        "SELECT COUNT(*) FROM sync_failures WHERE fail_count>0 AND terminal=0"
    ).fetchone()[0]
    assert pending == 0  # retry loop (terminal=0 filter) skips it
