"""Opt-in queue and job metrics, plus the current-service global.

Usage::

    from simsys_metrics import track_queue, track_job

    track_queue("inference", depth_fn=lambda: q.qsize())

    @track_job("inference")
    def run_inference(...): ...

The module also owns ``_SERVICE``, the process-wide service label value. It is
set by ``install()`` and read by ``track_queue`` / ``track_job`` so consumer
code never has to pass ``service=`` at every call site.
"""

from __future__ import annotations

import functools
import inspect
import logging
import math
import os
import threading
import time
from contextlib import contextmanager
from typing import Callable, Optional

from ._registry import make_counter, make_gauge, make_histogram

_log = logging.getLogger("simsys_metrics")

# When PROMETHEUS_MULTIPROC_DIR is set, tag queue_depth as "livesum" so it
# aggregates sensibly across worker subprocesses (one worker reports its
# depth; uvicorn reports 0). Outside multiproc mode, omit the kwarg so the
# single-process behaviour is identical to v0.1.1.
_MULTIPROC = bool(os.environ.get("PROMETHEUS_MULTIPROC_DIR"))

queue_depth = make_gauge(
    "simsys_queue_depth",
    "Current depth of an application-owned queue.",
    labelnames=("service", "queue"),
    multiprocess_mode="livesum" if _MULTIPROC else None,
)

jobs_total = make_counter(
    "simsys_jobs_total",
    "Jobs completed, labelled by name and outcome (success/error).",
    labelnames=("service", "job", "outcome"),
)

job_duration_seconds = make_histogram(
    "simsys_job_duration_seconds",
    "Job duration in seconds.",
    labelnames=("service", "job", "outcome"),
    buckets=(0.05, 0.1, 0.25, 0.5, 1, 2.5, 5, 10, 30, 60, 300),
)

_SERVICE: Optional[str] = None
_SERVICE_LOCK = threading.Lock()


def set_service(service: Optional[str]) -> None:
    """Set the process-wide service label. Called by install().

    Pass ``None`` (private) to clear; used by install rollback to restore
    pre-install state on partial failure.
    """
    global _SERVICE
    with _SERVICE_LOCK:
        _SERVICE = service


def _peek_service() -> Optional[str]:
    """Return the current service label without raising. Used by install
    rollback to capture pre-state before mutating.

    Lock-protected: ``_SERVICE`` is read under ``_SERVICE_LOCK`` so a
    concurrent ``set_service()`` can't tear the read or leave the
    snapshot reflecting a half-applied write. Without this lock, two
    parallel installs could observe stale snapshots of each other's
    pre-state and overwrite the wrong value on rollback.
    """
    with _SERVICE_LOCK:
        return _SERVICE


def get_service() -> str:
    if _SERVICE is None:
        raise RuntimeError(
            "simsys_metrics: no service set. Call install(app, service=..., version=...) first."
        )
    return _SERVICE


def track_queue(
    queue: str,
    depth_fn: Callable[[], int],
    interval: float = 5.0,
) -> threading.Thread:
    """Poll ``depth_fn()`` every ``interval`` seconds and update the gauge.

    Returns the poller thread (daemon). Safe to ignore the return value.

    ``interval`` must be a positive finite number; ``interval=0``,
    ``nan``, ``inf``, ``-inf`` and other non-finite values are rejected
    at call time so misconfig is loud rather than starting a daemon
    thread that later crashes inside ``time.sleep`` or busy-loops.
    """
    if (
        not isinstance(interval, (int, float))
        or isinstance(interval, bool)  # bool is int; reject explicitly
        or not math.isfinite(interval)
        or interval <= 0
    ):
        raise ValueError(
            f"track_queue: interval must be a positive finite number of "
            f"seconds, got {interval!r}"
        )
    service = get_service()

    seen_failure = False  # log misbehaving callbacks once, not every tick

    def _loop() -> None:
        nonlocal seen_failure
        while True:
            try:
                depth = int(depth_fn())
            except Exception as exc:
                if not seen_failure:
                    _log.warning(
                        "simsys-metrics: depth_fn for queue %r raised %r; "
                        "future failures will be silent. Reporting depth=0.",
                        queue,
                        exc,
                    )
                    seen_failure = True
                depth = 0
            try:
                queue_depth.labels(service=service, queue=queue).set(depth)
            except Exception as exc:
                _log.warning(
                    "simsys-metrics: queue_depth.set for %r failed: %r", queue, exc
                )
            time.sleep(interval)

    t = threading.Thread(
        target=_loop,
        name=f"simsys-metrics-queue-{queue}",
        daemon=True,
    )
    t.start()
    return t


@contextmanager
def _job_span(job: str):
    """Time the enclosed block and record into jobs_total + job_duration_seconds."""
    service = get_service()
    start = time.perf_counter()
    outcome = "success"
    try:
        yield
    except BaseException:
        outcome = "error"
        raise
    finally:
        elapsed = time.perf_counter() - start
        jobs_total.labels(service=service, job=job, outcome=outcome).inc()
        job_duration_seconds.labels(service=service, job=job, outcome=outcome).observe(
            elapsed
        )


def track_job(job: str):
    """Decorator: time a function and record it as a ``job`` metric.

    Works on both sync and async functions:
    - For sync ``def fn(...)``, the timing span wraps the call.
    - For ``async def fn(...)``, the timing span wraps the awaited
      coroutine — exceptions raised AFTER the function returns its
      coroutine are correctly attributed to ``outcome="error"`` (the
      previous sync-only wrapper exited the span when the coroutine was
      returned, before it ran, so async failures were misrecorded as
      ``outcome="success"``).

    Also usable as a context manager via ``with track_job("x"):`` — detected
    by the presence of ``__enter__``.
    """

    class _Tracker:
        def __call__(self, fn):
            # Detect coroutine-producing callables broadly:
            #   * plain `async def` functions
            #   * `functools.partial(async_fn, ...)` (via __wrapped__)
            #   * decorators that preserve __wrapped__
            #   * callable instances whose __call__ is `async def`
            # All need the async wrapper; otherwise the timing span exits
            # when the unawaited coroutine is RETURNED, before the work
            # runs — silently misrecording async exceptions as
            # outcome="success".
            #
            # `inspect.iscoroutinefunction(fn)` covers the first three but
            # returns False for callable instances (it inspects fn itself,
            # not fn.__call__). Fall back to inspecting __call__ for the
            # instance case.
            is_async = inspect.iscoroutinefunction(fn) or (
                callable(fn)
                and inspect.iscoroutinefunction(getattr(fn, "__call__", None))
            )
            if is_async:

                @functools.wraps(fn)
                async def async_wrapper(*args, **kwargs):
                    with _job_span(job):
                        return await fn(*args, **kwargs)

                return async_wrapper

            @functools.wraps(fn)
            def wrapper(*args, **kwargs):
                with _job_span(job):
                    return fn(*args, **kwargs)

            return wrapper

        def __enter__(self):
            self._cm = _job_span(job)
            return self._cm.__enter__()

        def __exit__(self, exc_type, exc, tb):
            return self._cm.__exit__(exc_type, exc, tb)

    return _Tracker()


# -------- track_pool --------

pool_active = make_gauge(
    "simsys_pool_active",
    "Number of active (checked-out) connections in a pool.",
    labelnames=("service", "pool"),
    multiprocess_mode="livesum" if _MULTIPROC else None,
)

pool_idle = make_gauge(
    "simsys_pool_idle",
    "Number of idle connections in a pool.",
    labelnames=("service", "pool"),
    multiprocess_mode="livesum" if _MULTIPROC else None,
)

pool_waiting = make_gauge(
    "simsys_pool_waiting",
    "Number of requests waiting for a pool connection.",
    labelnames=("service", "pool"),
    multiprocess_mode="livesum" if _MULTIPROC else None,
)

pool_max = make_gauge(
    "simsys_pool_max",
    "Maximum pool size.",
    labelnames=("service", "pool"),
    multiprocess_mode="livesum" if _MULTIPROC else None,
)


def track_pool(
    name: str,
    *,
    active_fn: Callable[[], int],
    idle_fn: Callable[[], int],
    waiting_fn: Optional[Callable[[], int]] = None,
    max_size: Optional[int] = None,
    interval: float = 5.0,
) -> threading.Thread:
    """Poll pool stat callbacks every ``interval`` seconds and update gauges.

    Returns the poller thread (daemon). Safe to ignore the return value.
    """
    if (
        not isinstance(interval, (int, float))
        or isinstance(interval, bool)
        or not math.isfinite(interval)
        or interval <= 0
    ):
        raise ValueError(
            f"track_pool: interval must be a positive finite number of "
            f"seconds, got {interval!r}"
        )
    service = get_service()

    if max_size is not None:
        pool_max.labels(service=service, pool=name).set(float(max_size))

    seen_failure = False

    def _loop() -> None:
        nonlocal seen_failure
        while True:
            try:
                pool_active.labels(service=service, pool=name).set(float(active_fn()))
                pool_idle.labels(service=service, pool=name).set(float(idle_fn()))
                if waiting_fn is not None:
                    pool_waiting.labels(service=service, pool=name).set(
                        float(waiting_fn())
                    )
            except Exception as exc:
                if not seen_failure:
                    _log.warning(
                        "simsys-metrics: pool callback for %r raised %r; "
                        "future failures will be silent.",
                        name,
                        exc,
                    )
                    seen_failure = True
            time.sleep(interval)

    t = threading.Thread(
        target=_loop,
        name=f"simsys-metrics-pool-{name}",
        daemon=True,
    )
    t.start()
    return t


def _reset_for_tests() -> None:
    """Test-only: clear the process-wide service global.

    track_queue() spawns daemon threads which are not cleanly stoppable;
    they die with the interpreter. The previous module-level reference
    list (_QUEUE_THREADS) was an unbounded leak source — appended on
    every track_queue call but never read except for `clear()` here.
    Removed in v0.3.5.
    """
    global _SERVICE
    with _SERVICE_LOCK:
        _SERVICE = None
