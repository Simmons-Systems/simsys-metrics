"""The HTTP latency bucket schedule is a cross-language contract.

Python, Node and Go are kept in sync by convention rather than codegen, and
nothing previously pinned this schedule — which is how the ceiling came to
matter without anyone noticing. These tests fail loudly if the Python schedule
changes, and assert the tail that was added on 2026-08-05.

Background: the schedule used to stop at 10.0, so any request slower than that
landed in +Inf and ``histogram_quantile`` could never return more than 10. On
the fleet that pinned voicestudio's p95 to exactly 10.00 with 23.3% of its
requests above the ceiling, and left an alert threshold of 15.0s on another
service structurally unable to fire.
"""

from prometheus_client import REGISTRY

EXPECTED_BUCKETS = (
    0.005,
    0.01,
    0.025,
    0.05,
    0.1,
    0.25,
    0.5,
    1.0,
    2.5,
    5.0,
    10.0,
    15.0,
    30.0,
    60.0,
)


def _observed_bucket_bounds(service: str) -> list[float]:
    """Finite ``le`` bounds actually emitted for a service, in order."""
    bounds = []
    for metric in REGISTRY.collect():
        if metric.name != "simsys_http_request_duration_seconds":
            continue
        for sample in metric.samples:
            if not sample.name.endswith("_bucket"):
                continue
            if sample.labels.get("service") != service:
                continue
            le = sample.labels["le"]
            if le != "+Inf":
                bounds.append(float(le))
    return bounds


def test_schedule_matches_the_cross_language_contract():
    from simsys_metrics._http import http_request_duration_seconds

    http_request_duration_seconds.labels(
        service="bucket_schedule_svc", method="GET", route="/x"
    ).observe(0.1)

    assert tuple(_observed_bucket_bounds("bucket_schedule_svc")) == EXPECTED_BUCKETS


def test_schedule_extends_past_ten_seconds():
    """The regression this file exists for: a ceiling at 10.0 hid every slow tail.

    Asserted against the buckets the registry actually emits — asserting against
    EXPECTED_BUCKETS would only be testing this file's own literal.
    """
    from simsys_metrics._http import http_request_duration_seconds

    http_request_duration_seconds.labels(
        service="ceiling_svc", method="GET", route="/x"
    ).observe(0.1)
    bounds = _observed_bucket_bounds("ceiling_svc")

    assert max(bounds) > 10.0, "schedule must resolve latency above 10s"
    assert [b for b in bounds if b > 10.0] == [15.0, 30.0, 60.0]


def test_schedule_is_strictly_increasing():
    from simsys_metrics._http import http_request_duration_seconds

    http_request_duration_seconds.labels(
        service="monotonic_svc", method="GET", route="/x"
    ).observe(0.1)
    bounds = _observed_bucket_bounds("monotonic_svc")

    assert bounds == sorted(bounds)
    assert len(set(bounds)) == len(bounds)


def test_a_slow_request_is_resolvable_below_the_top_bucket():
    """A 12s request must land in a finite bucket, not only in +Inf."""
    from simsys_metrics._http import http_request_duration_seconds

    http_request_duration_seconds.labels(
        service="slow_tail_svc", method="GET", route="/slow"
    ).observe(12.0)

    le15 = REGISTRY.get_sample_value(
        "simsys_http_request_duration_seconds_bucket",
        {"service": "slow_tail_svc", "method": "GET", "route": "/slow", "le": "15.0"},
    )
    le10 = REGISTRY.get_sample_value(
        "simsys_http_request_duration_seconds_bucket",
        {"service": "slow_tail_svc", "method": "GET", "route": "/slow", "le": "10.0"},
    )
    assert le10 == 0.0, "12s must not be counted at or below 10s"
    assert le15 == 1.0, "12s must be resolvable in the 15s bucket, not just +Inf"
