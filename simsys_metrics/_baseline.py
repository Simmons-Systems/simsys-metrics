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
import sys
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

# #50319. From 2.0.0 a failing poller tick does NOT write its gauge, which
# means a broken depth_fn now looks like a FLAT LINE rather than a zero. That
# is the right shape -- an empty queue and a broken callback are operationally
# opposite -- but a flat line is only legible if something else says "this is
# stale". This counter is that something else.
#
# Cardinality is (queues + pools) per service, bounded because `collector` is
# a closed two-value enum in the contract.
collector_errors_total = make_counter(
    "simsys_collector_errors_total",
    "Poller callback failures, by collector kind and tracked name.",
    labelnames=("service", "collector", "name"),
)

_SERVICE: Optional[str] = None
_SERVICE_LOCK = threading.Lock()


def set_service(service: Optional[str]) -> None:
    """Set the process-wide service label. Called by install().

    Pass ``None`` (private) to clear; used by install rollback to restore
    pre-install state on partial failure.

    The value is stripped. ``service`` is the join key between this package
    and ``simsys-logevent`` -- an operator pivots from
    ``simsys_http_requests_total{service="x"}`` in Prometheus to
    ``{service="x"} | json`` in Loki -- and simsys-logevent strips its own
    copy. Without this, ``"  portal  "`` here and ``"portal"`` there are two
    different identities and the pivot silently returns nothing.

    Stripping is safe to do now rather than deferring to a major: all 36
    services currently reporting ``simsys_build_info`` were checked against
    live Prometheus on 2026-08-22 and none carries leading or trailing
    whitespace, so this is a measured no-op on the deployed fleet rather
    than a hoped-for one. A padded name is a caller typo in every observed
    case, and the stripped value is what the caller meant.

    An all-whitespace service strips to empty, which would emit
    ``service=""`` on every series. Python has no empty-service validation
    at all today (Go rejects it via ``ErrInvalidInstallOpts``), so that is
    warned about loudly here and the value is still set -- consistent with
    the warn-now/raise-in-the-next-major policy applied across this package.
    """
    global _SERVICE
    if service is not None:
        # A SECOND install() with a different service silently re-labels every
        # series (#50321). The per-app idempotence guard in fastapi.py/flask.py
        # is keyed on the app OBJECT, so install(app_a, "foo") followed by
        # install(app_b, "bar") never reaches it: trackers started for app_a
        # begin emitting under "bar" and app_a's process metrics vanish.
        #
        # ERROR, not WARNING -- this corrupts every series in the process, and
        # it is silent today at all three downstream sites (_process.py's
        # service_swap logs nothing at all). Behaviour is unchanged for now:
        # the fleet is swept for this marker before it becomes an exception in
        # the next major.
        #
        # Guarded on `_SERVICE is not None` so install-rollback's
        # set_service(None) and first-install both stay quiet.
        prior = _peek_service()
        if prior is not None and prior != service.strip():
            _log.error(
                "simsys-metrics: SERVICE IDENTITY CHANGE %r -> %r. One service "
                "identity per process is the contract. Trackers already "
                "started will now emit under the NEW service, and the prior "
                "service's process metrics disappear. If you genuinely need "
                "two identities, run two processes. This will raise in the "
                "next major version.",
                prior,
                service.strip(),
            )
        stripped = service.strip()
        if stripped != service:
            _log.warning(
                "simsys-metrics: service %r has leading/trailing whitespace; "
                "using %r. `service` is the join key with simsys-logevent, "
                "which strips its own copy -- an unstripped value here would "
                "not match in Loki.",
                service,
                stripped,
            )
        if not stripped:
            _log.warning(
                "simsys-metrics: service %r is empty after stripping. Every "
                'series will carry service="", which no dashboard template '
                "will match. This will raise in the next major version.",
                service,
            )
        service = stripped
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


class PollerThread(threading.Thread):
    """Daemon poll loop with a cooperative ``stop()``.

    Returned by :func:`track_queue` and :func:`track_pool`. Callers that
    tear down the polled resource (queue drained, pool closed, object
    about to be GC'd) should call ``stop()`` — the loop exits within one
    ``interval``. The loop also self-terminates once the interpreter
    begins finalizing, so user callbacks are never invoked after
    shutdown has started.
    """

    def __init__(self, *, name: str, interval: float, poll: Callable[[], None]):
        super().__init__(name=name, daemon=True)
        self._interval = interval
        self._poll = poll
        self._stop_event = threading.Event()

    def stop(self) -> None:
        """Ask the poll loop to exit; returns immediately."""
        self._stop_event.set()

    @property
    def stopped(self) -> bool:
        return self._stop_event.is_set()

    def run(self) -> None:  # pragma: no cover - exercised via integration tests
        while not self._stop_event.is_set():
            if sys.is_finalizing():
                return
            self._poll()
            if self._stop_event.wait(self._interval):
                return


def _bump_collector_error(service: str, collector: str, name: str) -> None:
    """Count one failed poller tick (#50319).

    Deliberately swallows its own errors: this runs on the failure path, and a
    metrics package that raises out of its own error accounting turns a
    degraded collector into a crashed poller thread. The gauge's silence is
    already the primary signal; this counter is the annotation on it.
    """
    try:
        collector_errors_total.labels(
            service=service, collector=collector, name=name
        ).inc()
    except Exception:  # pragma: no cover - defensive, see docstring
        pass


def _warn_empty_name(kind: str, name: object) -> None:
    """Warn-only guard for an empty queue/pool name (#50322).

    An empty name emits a series labelled queue="" / pool="", which no
    dashboard template matches and which is indistinguishable from a
    mislabelled one. Node has the same gap; Go validates. Warn now, raise in
    the next major -- the series is still emitted so no deployed consumer
    breaks on upgrade.
    """
    if not isinstance(name, str) or not name.strip():
        _log.warning(
            "simsys-metrics: track_%s name must be a non-empty string, got %r. "
            "The series is still emitted for backward compatibility; this will "
            "raise ValueError in the next major version.",
            kind,
            name,
        )


def track_queue(
    queue: str,
    depth_fn: Callable[[], int],
    interval: float = 5.0,
) -> PollerThread:
    """Poll ``depth_fn()`` every ``interval`` seconds and update the gauge.

    Returns the poller thread (daemon, a :class:`PollerThread`). Safe to
    ignore the return value; keep it and call ``.stop()`` to end polling
    when the tracked queue goes away.

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
    _warn_empty_name("queue", queue)
    service = get_service()

    seen_failure = False  # log misbehaving callbacks once, not every tick

    def _poll() -> None:
        nonlocal seen_failure
        try:
            depth = int(depth_fn())
        except Exception as exc:
            # #50319: do NOT write the gauge. Until 2.0.0 this reported 0,
            # which is indistinguishable from a genuinely drained queue -- and
            # only one of those two gets investigated. Returning early leaves
            # the last known value standing, and leaves the series ABSENT
            # entirely if the very first tick failed, which is the honest
            # representation of "we have never successfully read this queue".
            _bump_collector_error(service, "queue", queue)
            if not seen_failure:
                _log.warning(
                    "simsys-metrics: depth_fn for queue %r raised %r; the gauge "
                    "keeps its last known value (absent if this was the first "
                    "tick) and simsys_collector_errors_total is incremented. "
                    "Future failures will be silent.",
                    queue,
                    exc,
                )
                seen_failure = True
            return
        try:
            queue_depth.labels(service=service, queue=queue).set(depth)
        except Exception as exc:
            _log.warning(
                "simsys-metrics: queue_depth.set for %r failed: %r", queue, exc
            )

    t = PollerThread(
        name=f"simsys-metrics-queue-{queue}",
        interval=interval,
        poll=_poll,
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
) -> PollerThread:
    """Poll pool stat callbacks every ``interval`` seconds and update gauges.

    Returns the poller thread (daemon, a :class:`PollerThread`). Safe to
    ignore the return value; keep it and call ``.stop()`` to end polling
    when the tracked pool goes away.
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
    _warn_empty_name("pool", name)
    service = get_service()

    if max_size is not None:
        pool_max.labels(service=service, pool=name).set(float(max_size))

    seen_failure = False

    def _poll() -> None:
        nonlocal seen_failure
        # #50319: read EVERY callback before writing ANY gauge. The previous
        # shape interleaved reads and writes inside one try, so if idle_fn
        # raised, pool_active had already been set for this tick -- a pool
        # reporting a fresh `active` beside a stale `idle`, which is a
        # self-inconsistent snapshot that no consumer can detect. Computing
        # first makes the tick all-or-nothing.
        try:
            values = [
                (pool_active, float(active_fn())),
                (pool_idle, float(idle_fn())),
            ]
            if waiting_fn is not None:
                values.append((pool_waiting, float(waiting_fn())))
        except Exception as exc:
            _bump_collector_error(service, "pool", name)
            if not seen_failure:
                _log.warning(
                    "simsys-metrics: pool callback for %r raised %r; the gauges "
                    "keep their last known values (absent if this was the first "
                    "tick) and simsys_collector_errors_total is incremented. "
                    "Future failures will be silent.",
                    name,
                    exc,
                )
                seen_failure = True
            return
        for gauge, value in values:
            gauge.labels(service=service, pool=name).set(value)

    t = PollerThread(
        name=f"simsys-metrics-pool-{name}",
        interval=interval,
        poll=_poll,
    )
    t.start()
    return t


def _reset_for_tests() -> None:
    """Test-only: clear the process-wide service global.

    track_queue()/track_pool() pollers are PollerThread instances —
    cooperatively stoppable via .stop() since the FR-073/FR-104 fix, and
    self-terminating at interpreter finalization. Tests that start
    pollers should stop() them; anything left running dies with the
    interpreter. (The old module-level _QUEUE_THREADS reference list was
    removed in v0.3.5 as an unbounded leak source.)
    """
    global _SERVICE
    with _SERVICE_LOCK:
        _SERVICE = None
