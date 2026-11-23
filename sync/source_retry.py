"""Bounded source-access retry without container restart or worker recreation."""


def wait_for_source(probe, stop_event, publish, *, once=False, initial_delay=60, max_delay=300):
    """Return the probe result, or None on shutdown/one-shot failure.

    Publish at most once per failed attempt; waits are signal-interruptible.
    No retry sleep after success, so accessible buckets retain normal cadence.
    """
    attempt = 0
    while not stop_event.is_set():
        try:
            return probe()
        except Exception as exc:
            attempt += 1
            delay = min(max_delay, initial_delay * (2 ** min(attempt - 1, 10)))
            response = getattr(exc, "response", None)
            code = getattr(response, "status_code", None)
            try:
                publish({"state": "source_forbidden" if code == 403 else "source_unavailable",
                         "upstream_http_status": code, "source_retry_attempt": attempt,
                         "retry_after_sec": delay,
                         "error": f"Upstream HTTP {code}" if code else type(exc).__name__})
            except Exception:
                # A bookkeeping write failure must not trigger a restart loop.
                pass
            if once or stop_event.wait(delay):
                return None
    return None
