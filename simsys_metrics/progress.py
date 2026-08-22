"""Batch-job progress tracking.

Usage::

    from simsys_metrics import install, track_progress, ProgressOpts

    install(app, service="scanner", version="1.0.0")

    tracker = track_progress(ProgressOpts(operation="scan", total=input_count))
    for item in work:
        process(item)
        tracker.inc()
    tracker.stop()

Emits four metrics, all keyed on ``{service, operation}``:

* ``simsys_progress_processed_total`` — monotonic counter of items done
* ``simsys_progress_remaining`` — gauge of items not yet done
* ``simsys_progress_rate_per_second`` — EWMA-smoothed rate (window configurable)
* ``simsys_progress_estimated_completion_timestamp`` — Unix timestamp, wall clock

Designed for long-running batch work where ops want a live progress view.
See also ``track_queue`` (`_baseline.py`) for steady-state queue depth
observability; the two are complementary.
"""

from __future__ import annotations

import logging
import math
import threading
import time
from dataclasses import dataclass
from typing import Optional

from ._baseline import get_service
from ._registry import make_counter, make_gauge

_log = logging.getLogger("simsys_metrics")

progress_processed_total = make_counter(
    "simsys_progress_processed_total",
    "Items completed in a batch operation (monotonic counter).",
    labelnames=("service", "operation"),
)

progress_remaining = make_gauge(
    "simsys_progress_remaining",
    "Items not yet completed in a batch operation.",
    labelnames=("service", "operation"),
)

progress_rate_per_second = make_gauge(
    "simsys_progress_rate_per_second",
    "EWMA-smoothed processing rate in items per second.",
    labelnames=("service", "operation"),
)

progress_estimated_completion_timestamp = make_gauge(
    "simsys_progress_estimated_completion_timestamp",
    "Estimated completion time as a Unix timestamp (0 when unknown).",
    labelnames=("service", "operation"),
)


@dataclass
class ProgressOpts:
    """Options for :func:`track_progress`."""

    operation: str
    total: int
    window: float = 5.0  # EWMA smoothing window (seconds)
    interval: float = 5.0  # poll/update interval (seconds)


class ProgressTracker:
    """Returned by :func:`track_progress`. Caller updates progress via :meth:`inc`.

    The tracker runs a daemon thread that recomputes rate + ETA at ``interval``
    seconds and updates the gauges. ``inc()`` is cheap (atomic counter bump);
    the background thread does the math.
    """

    def __init__(self, opts: ProgressOpts) -> None:
        # Reject NaN / +inf / -inf for interval and window — `<= 0`
        # check alone passes NaN (since nan <= 0 is False) and Infinity
        # (positive non-finite). Both lead to a daemon thread that
        # later crashes inside time.sleep / Event.wait, hours after
        # the misconfig was introduced.
        if not isinstance(opts.total, (int, float)) or not math.isfinite(opts.total):
            raise ValueError(
                f"ProgressOpts.total must be a finite number, got {opts.total!r}"
            )
        if opts.total < 0:
            raise ValueError(f"ProgressOpts.total must be >= 0, got {opts.total}")
        if (
            not isinstance(opts.window, (int, float))
            or not math.isfinite(opts.window)
            or opts.window <= 0
        ):
            raise ValueError(
                f"ProgressOpts.window must be a positive finite number, "
                f"got {opts.window!r}"
            )
        if (
            not isinstance(opts.interval, (int, float))
            or not math.isfinite(opts.interval)
            or opts.interval <= 0
        ):
            raise ValueError(
                f"ProgressOpts.interval must be a positive finite number, "
                f"got {opts.interval!r}"
            )

        # Empty operation: warn-only for now (#50322). Go rejects it outright
        # (go/progress.go:65) and Node throws (node/src/progress.ts:56), so
        # Python is the outlier -- ProgressOpts(operation="") silently emits an
        # operation="" series that no dashboard template matches. The message
        # text is deliberately identical to the other two lanes so promoting
        # this to a ValueError in the next major is a one-line change with no
        # wording drift.
        if not isinstance(opts.operation, str) or not opts.operation.strip():
            _log.warning(
                "simsys-metrics: track_progress opts.operation must be a "
                "non-empty string, got %r. The series is still emitted with "
                "operation=%r for backward compatibility; this will raise "
                "ValueError in the next major version.",
                opts.operation,
                opts.operation,
            )

        self._opts = opts
        self._service = get_service()
        self._lock = threading.Lock()
        self._processed = 0
        self._total = opts.total
        self._rate_ewma: Optional[float] = None
        self._last_processed = 0
        self._last_tick = time.monotonic()
        self._stop = threading.Event()

        # Seed the gauges so the series appears on /metrics even before any inc().
        progress_remaining.labels(service=self._service, operation=opts.operation).set(
            float(self._total)
        )
        progress_rate_per_second.labels(
            service=self._service, operation=opts.operation
        ).set(0.0)
        progress_estimated_completion_timestamp.labels(
            service=self._service, operation=opts.operation
        ).set(0.0)

        self._thread = threading.Thread(
            target=self._loop,
            name=f"simsys-metrics-progress-{opts.operation}",
            daemon=True,
        )
        self._thread.start()

    def inc(self, n: int = 1) -> None:
        """Record ``n`` items completed.

        Rejects ``nan`` / ``+inf`` / ``-inf`` at call time. Pre-fix, the
        plain ``< 0`` check let NaN through (since ``nan < 0`` is False
        per IEEE-754) and ``Infinity`` slipped through; both then
        propagated into the exported counter / gauge values, producing
        ``simsys_progress_processed_total ... nan`` lines on /metrics
        that downstream alerting cannot reason about.
        """
        if not isinstance(n, (int, float)) or isinstance(n, bool):
            raise ValueError(f"ProgressTracker.inc n must be a number, got {n!r}")
        if not math.isfinite(n):
            raise ValueError(
                f"ProgressTracker.inc n must be a finite number, got {n!r}"
            )
        if n < 0:
            raise ValueError(f"ProgressTracker.inc n must be >= 0, got {n}")
        if n == 0:
            return
        with self._lock:
            self._processed += n
        progress_processed_total.labels(
            service=self._service, operation=self._opts.operation
        ).inc(n)

    def set_total(self, total: int) -> None:
        """Update the denominator — useful when work grows mid-run.

        Rejects ``nan`` / ``+inf`` / ``-inf`` (same reasoning as
        :meth:`inc`).
        """
        if not isinstance(total, (int, float)) or isinstance(total, bool):
            raise ValueError(
                f"ProgressTracker.set_total must be a number, got {total!r}"
            )
        if not math.isfinite(total):
            raise ValueError(
                f"ProgressTracker.set_total must be a finite number, got {total!r}"
            )
        if total < 0:
            raise ValueError(f"ProgressTracker.set_total must be >= 0, got {total}")
        with self._lock:
            self._total = total

    def stop(self) -> None:
        """Stop the background thread and flush a final tick. Idempotent."""
        if self._stop.is_set():
            return
        self._stop.set()
        # Fire one final tick so the gauges reflect the end-state before the
        # thread exits — if the loop is sleeping we need to rejoin it first.
        self._thread.join(timeout=self._opts.interval + 1.0)
        self._tick()

    def _tick(self) -> None:
        """Recompute rate + ETA and update gauges. Called from loop and stop()."""
        now = time.monotonic()
        with self._lock:
            processed = self._processed
            total = self._total
        elapsed = now - self._last_tick
        if elapsed > 0:
            delta = processed - self._last_processed
            instant_rate = delta / elapsed
            # EWMA with alpha derived from (interval / window). interval equal
            # to window → alpha ≈ 0.63 (one time-constant of smoothing).
            alpha = min(1.0, elapsed / self._opts.window)
            if self._rate_ewma is None:
                self._rate_ewma = instant_rate
            else:
                self._rate_ewma = alpha * instant_rate + (1.0 - alpha) * self._rate_ewma
        self._last_processed = processed
        self._last_tick = now

        remaining = max(0, total - processed)
        progress_remaining.labels(
            service=self._service, operation=self._opts.operation
        ).set(float(remaining))

        rate = self._rate_ewma or 0.0
        progress_rate_per_second.labels(
            service=self._service, operation=self._opts.operation
        ).set(rate)

        if rate > 0 and remaining > 0:
            eta_seconds = remaining / rate
            completion_ts = time.time() + eta_seconds
        else:
            completion_ts = 0.0
        progress_estimated_completion_timestamp.labels(
            service=self._service, operation=self._opts.operation
        ).set(completion_ts)

    def _loop(self) -> None:
        seen_failure = False
        while not self._stop.wait(self._opts.interval):
            try:
                self._tick()
            except Exception as exc:
                # Don't let a metric-update failure kill the loop, but surface
                # the first failure so misbehaving collectors aren't silent.
                if not seen_failure:
                    _log.warning(
                        "simsys-metrics: progress tick for %r raised %r; "
                        "future failures will be silent.",
                        self._opts.operation,
                        exc,
                    )
                    seen_failure = True


def track_progress(opts: ProgressOpts) -> ProgressTracker:
    """Start tracking progress of a batch operation.

    See :class:`ProgressOpts` for configuration. Returns a :class:`ProgressTracker`
    the caller uses to report progress via :meth:`ProgressTracker.inc`.
    Don't forget to call :meth:`ProgressTracker.stop` when the work is done
    (or use a ``try/finally``).
    """
    return ProgressTracker(opts)
