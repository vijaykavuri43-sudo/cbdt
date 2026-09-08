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
