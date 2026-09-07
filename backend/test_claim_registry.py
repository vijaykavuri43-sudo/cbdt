"""Claim registry: two drains must never OCR the same object concurrently.

Regression test for the 2026-09-07 hang — overlapping queues (pending vs
wallet/slip) fetched + OCR'd identical keys 2-4x at once, and crashed
batches silently re-queued the same keys every loop.
"""

from backend.enrich import _claimed, claim_keys, release_keys


def test_claim_skips_in_flight_keys():
    release_keys(["a", "b", "c"])
    try:
        assert claim_keys(["a", "b"]) == ["a", "b"]
        # second drain asking for overlap gets only fresh keys
        assert claim_keys(["b", "c"]) == ["c"]
        assert claim_keys(["a", "b", "c"]) == []
    finally:
        release_keys(["a", "b", "c"])
    assert not _claimed
    # after release everything is claimable again
    assert claim_keys(["a", "b", "c"]) == ["a", "b", "c"]
    release_keys(["a", "b", "c"])
    assert not _claimed


def test_claim_empty_and_release_missing():
    assert claim_keys([]) == []
    release_keys(["never-claimed"])  # must not raise
    assert not _claimed
