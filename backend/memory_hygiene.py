"""Best-effort heap reclamation in every API process, not only the collector."""

import ctypes
import gc
import threading


def _rss_mb():
    """Process RSS in MB, or None when /proc is unavailable."""
    try:
        with open("/proc/self/status", encoding="utf-8") as f:
            for line in f:
                if line.startswith("VmRSS:"):
                    return float(line.split()[1]) / 1024.0
    except Exception:
        pass
    return None


def reclaim_memory():
    """Release unreachable objects and free allocator pages; keep live caches."""
    try:
        before = _rss_mb()
        gc.collect()
        try:
            ctypes.CDLL("libc.so.6").malloc_trim(0)
        except Exception:
            pass  # Non-glibc platforms still benefit from collection.
        after = _rss_mb()
        if before is not None and after is not None and before - after >= 50:
            print(
                f"[memory-hygiene] RSS {before:.0f} -> {after:.0f} MB",
                flush=True,
            )
    except Exception:
        pass  # Maintenance must never take down request/OCR workers.


def _run(stop, interval=900):
    # Wait first: don't contend with startup warmup or run on shutdown.
    while not stop.wait(interval):
        reclaim_memory()


_lock = threading.Lock()
_stop = threading.Event()
_thread = None


def start():
    """One maintenance thread per API worker, independent of leader election."""
    global _thread, _stop
    with _lock:
        if _thread is not None and _thread.is_alive():
            return
        _stop = threading.Event()
        _thread = threading.Thread(
            target=_run, args=(_stop,), daemon=True, name="memory-hygiene"
        )
        _thread.start()


def stop():
    with _lock:
        _stop.set()
        if _thread is not None:
            _thread.join(timeout=5)
