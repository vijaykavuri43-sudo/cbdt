"""Memory maintenance must run in HTTP-only workers as well as the leader."""

from unittest.mock import Mock, patch

from backend import memory_hygiene as memory


def test_loop_waits_before_reclaiming_and_stops_promptly():
    stop = Mock()
    stop.wait.side_effect = [False, False, True]
    with patch.object(memory, "reclaim_memory") as reclaim:
        memory._run(stop)
    assert reclaim.call_count == 2
    assert [call.args for call in stop.wait.call_args_list] == [(900,)] * 3


def test_stopped_worker_does_not_collect():
    stop = Mock()
    stop.wait.return_value = True
    with patch.object(memory, "reclaim_memory") as reclaim:
        memory._run(stop)
    reclaim.assert_not_called()


def test_collection_survives_missing_glibc():
    with patch.object(memory, "_rss_mb", return_value=None), patch.object(
        memory.gc, "collect"
    ) as collect, patch.object(memory.ctypes, "CDLL", side_effect=OSError):
        memory.reclaim_memory()
    collect.assert_called_once()


def test_collection_survives_gc_failure():
    with patch.object(memory.gc, "collect", side_effect=RuntimeError):
        memory.reclaim_memory()


def test_trim_and_reduction_logging(capsys):
    libc = Mock()
    with patch.object(memory, "_rss_mb", side_effect=[200.0, 100.0]), patch.object(
        memory.gc, "collect"
    ), patch.object(memory.ctypes, "CDLL", return_value=libc):
        memory.reclaim_memory()
    libc.malloc_trim.assert_called_once_with(0)
    assert "200 -> 100 MB" in capsys.readouterr().out


def test_start_is_idempotent_stop_interrupts_and_restart_works():
    try:
        memory.start()
        first = memory._thread
        assert first.is_alive()
        memory.start()
        assert memory._thread is first
        memory.stop()
        assert not first.is_alive()
        memory.start()
        assert memory._thread is not first
        assert memory._thread.is_alive()
    finally:
        memory.stop()
