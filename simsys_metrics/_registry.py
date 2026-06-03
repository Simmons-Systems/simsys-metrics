"""Prefix-guarded metric factories.

Every metric created through these helpers is forced to start with `simsys_`.
The guard makes it impossible to accidentally ship a metric under a bare name
or any other prefix when using this package.

The factories also WARN (don't error) when ``service`` is missing from
``labelnames``. The shared ``$service``-templated Grafana dashboards
filter on the service label; metrics without it can't participate in
the cross-service contract. The warning is logged once per metric name
so library code that knows what it's doing isn't drowned in noise.
"""

from __future__ import annotations

import logging
from typing import Iterable, Optional, Sequence

from prometheus_client import Counter, Gauge, Histogram

_log = logging.getLogger("simsys_metrics")

PREFIX = "simsys_"

# Metric names we've already warned about — keep the warning to once
# per (process, name) so a misuse is loud the first time and silent
# after that. Library-built metrics like simsys_*_total without
# `service` (none currently — but defensive) won't spam logs.
_warned_missing_service: set[str] = set()


def _guard_name(name: str) -> str:
    if not isinstance(name, str) or not name.startswith(PREFIX):
        raise ValueError(
            f"simsys-metrics refuses to register metric {name!r}: "
            f"all metric names must start with {PREFIX!r}."
        )
    return name


def _warn_if_missing_service(name: str, labelnames: Sequence[str]) -> None:
    """Log a warning when a custom metric's labelnames omit `service`.

    The shared cross-service dashboards filter on the service label;
    metrics without it can't participate in the contract. We don't
    raise — consumer code that legitimately doesn't need `service`
    (rare, but possible) shouldn't be broken — but we do make the
    deviation visible.
    """
    if "service" in labelnames:
        return
    if name in _warned_missing_service:
        return
    _warned_missing_service.add(name)
    _log.warning(
        "simsys-metrics: metric %r registered without 'service' in "
        "labelnames=%r — it will NOT participate in $service-templated "
        "dashboards. Add 'service' to labelnames if you want cross-service "
        "queries to work for this metric.",
        name,
        tuple(labelnames),
    )


def make_counter(
    name: str, documentation: str, labelnames: Sequence[str] = ()
) -> Counter:
    _warn_if_missing_service(name, labelnames)
    return Counter(_guard_name(name), documentation, labelnames=tuple(labelnames))


def make_gauge(
    name: str,
    documentation: str,
    labelnames: Sequence[str] = (),
    multiprocess_mode: Optional[str] = None,
) -> Gauge:
    """Create a Gauge with the simsys_ prefix guard.

    ``multiprocess_mode`` is forwarded to ``prometheus_client.Gauge`` only
    when non-None; omitting it preserves the stdlib default (``"all"``)
    and keeps the constructor call identical in single-process mode.
    """
    _warn_if_missing_service(name, labelnames)
    kwargs: dict[str, object] = {}
    if multiprocess_mode is not None:
        kwargs["multiprocess_mode"] = multiprocess_mode
    return Gauge(
        _guard_name(name), documentation, labelnames=tuple(labelnames), **kwargs
    )


scrape_duration_seconds = make_gauge(
    "simsys_scrape_duration_seconds",
    "Time taken to generate the /metrics response.",
    labelnames=("service",),
)

scrape_errors_total = make_counter(
    "simsys_scrape_errors_total",
    "Errors encountered while generating the /metrics response.",
    labelnames=("service",),
)


def make_histogram(
    name: str,
    documentation: str,
    labelnames: Sequence[str] = (),
    buckets: Iterable[float] | None = None,
) -> Histogram:
    _warn_if_missing_service(name, labelnames)
    kwargs = {}
    if buckets is not None:
        kwargs["buckets"] = tuple(buckets)
    return Histogram(
        _guard_name(name), documentation, labelnames=tuple(labelnames), **kwargs
    )
