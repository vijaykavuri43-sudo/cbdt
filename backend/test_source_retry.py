import importlib.util
from pathlib import Path

import httpx

spec = importlib.util.spec_from_file_location("source_retry", Path(__file__).parents[1] / "sync/source_retry.py")
retry = importlib.util.module_from_spec(spec)
spec.loader.exec_module(retry)


class Stop:
    def __init__(self, stop_after=20):
        self.waits = []
        self.stop_after = stop_after
    def is_set(self):
        return len(self.waits) >= self.stop_after
    def wait(self, delay):
        self.waits.append(delay)
        return self.is_set()


def forbidden():
    response = httpx.Response(403, request=httpx.Request("GET", "https://example.invalid/"))
    response.raise_for_status()


def test_forbidden_backoff_does_not_restart_and_is_bounded():
    stop, statuses = Stop(7), []
    assert retry.wait_for_source(forbidden, stop, statuses.append) is None
    assert stop.waits == [60, 120, 240, 300, 300, 300, 300]
    assert len(statuses) == 7
    assert all(s["state"] == "source_forbidden" and s["upstream_http_status"] == 403 for s in statuses)


def test_recovery_returns_immediately_without_normal_poll_delay():
    stop, statuses = Stop(), []
    calls = []
    def probe():
        calls.append(1)
        if len(calls) <= 2:
            forbidden()
        return ["deposit/"]
    assert retry.wait_for_source(probe, stop, statuses.append) == ["deposit/"]
    assert stop.waits == [60, 120]


def test_accessible_source_has_no_wait_and_no_status_write():
    stop = Stop()
    assert retry.wait_for_source(lambda: [], stop, lambda _: (_ for _ in ()).throw(AssertionError())) == []
    assert stop.waits == []


def test_oneshot_and_status_publish_failure_are_safe():
    stop = Stop()
    def bad_publish(_):
        raise RuntimeError("GCS not reachable")
    assert retry.wait_for_source(forbidden, stop, bad_publish, once=True) is None
    assert stop.waits == []
