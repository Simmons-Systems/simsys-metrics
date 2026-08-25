import time

import pytest
from prometheus_client import REGISTRY

from simsys_metrics import set_service, track_queue


def test_track_queue_updates_gauge():
    set_service("queue_test_svc")
    depth_value = [7]
    track_queue("inference", depth_fn=lambda: depth_value[0], interval=0.05)
    # Allow the daemon thread one poll cycle.
    deadline = time.time() + 2.0
    while time.time() < deadline:
        v = REGISTRY.get_sample_value(
            "simsys_queue_depth", {"service": "queue_test_svc", "queue": "inference"}
        )
        if v == 7:
            break
        time.sleep(0.05)
    v = REGISTRY.get_sample_value(
        "simsys_queue_depth", {"service": "queue_test_svc", "queue": "inference"}
    )
    assert v == 7


def test_track_queue_requires_install_first():
    with pytest.raises(RuntimeError, match="no service set"):
        track_queue("x", depth_fn=lambda: 1)


def test_track_queue_leaves_the_series_absent_when_the_first_tick_fails():
    """#50319, and the assertion here is DELIBERATELY INVERTED from 2.0.0.

    Until 2.0.0 this test read::

        # Gauge should default to 0 (int coercion of caught exception path).
        assert v == 0

    That comment described the defect approvingly. Reporting 0 makes a broken
    ``depth_fn`` indistinguishable from a genuinely drained queue, and only one
    of those two ever gets investigated -- a queue at 0 is the state everyone
    is hoping for.

    Flipping this is the contract change, not an incidental edit. A consumer
    whose depth_fn has been silently failing will see its series DISAPPEAR on
    upgrade rather than read 0, which is the entire point: absent means "never
    successfully measured", and 0 means "measured, and empty".
    """
    set_service("queue_test_svc")

    counter = [0]

    def bad():
        counter[0] += 1
        raise RuntimeError("boom")

    track_queue("broken", depth_fn=bad, interval=0.05)
    time.sleep(0.25)

    v = REGISTRY.get_sample_value(
        "simsys_queue_depth", {"service": "queue_test_svc", "queue": "broken"}
    )
    assert v is None, (
        f"the first tick failed, so simsys_queue_depth must have NO sample for "
        f"this queue -- got {v!r}. Seeding a value here is the #50319 lie."
    )

    # The failure is counted, so the absent series is legible rather than
    # merely missing.
    errors = REGISTRY.get_sample_value(
        "simsys_collector_errors_total",
        {"service": "queue_test_svc", "collector": "queue", "name": "broken"},
    )
    assert errors is not None and errors >= 2, (
        f"expected simsys_collector_errors_total to count every failed tick, "
        f"got {errors!r}"
    )

    # And the poller kept running (didn't crash after the first raise).
    assert counter[0] >= 2


def test_track_queue_preserves_the_last_known_value_when_a_later_tick_fails():
    """The other half of #50319: a gauge that HAS a value keeps it.

    First-tick failure leaves the series absent; a failure after a successful
    read must leave the last good value standing rather than resetting to 0.
    Without this the fix would only be half-implemented, and the half that is
    missing is the one a long-running service actually hits.
    """
    set_service("queue_test_svc")

    state = {"ok": True, "calls": 0}

    def flaky():
        state["calls"] += 1
        if state["ok"]:
            return 42
        raise RuntimeError("boom")

    track_queue("flaky", depth_fn=flaky, interval=0.05)

    labels = {"service": "queue_test_svc", "queue": "flaky"}
    deadline = time.time() + 2.0
    while time.time() < deadline:
        if REGISTRY.get_sample_value("simsys_queue_depth", labels) == 42:
            break
        time.sleep(0.02)
    assert REGISTRY.get_sample_value("simsys_queue_depth", labels) == 42, (
        "precondition: the gauge must be populated before we break the callback"
    )

    state["ok"] = False
    settled = state["calls"]
    while time.time() < deadline and state["calls"] < settled + 3:
        time.sleep(0.02)

    assert REGISTRY.get_sample_value("simsys_queue_depth", labels) == 42, (
        "a failing tick must leave the last known value in place, not reset to 0"
    )
    errors = REGISTRY.get_sample_value(
        "simsys_collector_errors_total",
        {"service": "queue_test_svc", "collector": "queue", "name": "flaky"},
    )
    assert errors is not None and errors >= 1, (
        f"the stale gauge must be annotated by an error count, got {errors!r}"
    )


def test_track_queue_rejects_zero_interval():
    """interval=0 would create a busy-loop in an unstoppable daemon thread.
    track_queue must reject it loudly at call time."""
    set_service("queue_test_svc")
    with pytest.raises(ValueError, match="positive finite number"):
        track_queue("zero_interval", depth_fn=lambda: 1, interval=0)


def test_track_queue_rejects_negative_interval():
    set_service("queue_test_svc")
    with pytest.raises(ValueError, match="positive finite number"):
        track_queue("neg_interval", depth_fn=lambda: 1, interval=-1.0)


def test_track_queue_rejects_nan_interval():
    """NaN passes `<= 0` vacuously (since `nan <= 0` is False per IEEE-754),
    so the previous check let it through. The daemon thread then crashes
    inside time.sleep(nan). Reject at call time."""
    import math

    set_service("queue_test_svc")
    with pytest.raises(ValueError, match="positive finite number"):
        track_queue("nan_interval", depth_fn=lambda: 1, interval=math.nan)


def test_track_queue_rejects_infinity_interval():
    import math

    set_service("queue_test_svc")
    with pytest.raises(ValueError, match="positive finite number"):
        track_queue("inf_interval", depth_fn=lambda: 1, interval=math.inf)


def test_track_queue_poller_is_stoppable():
    """FR-073/FR-104: pollers must expose stop() so orphaned trackers
    don't leak daemon threads for the life of the process."""
    set_service("queue_test_svc")
    calls = [0]

    def count():
        calls[0] += 1
        return 1

    t = track_queue("stoppable", depth_fn=count, interval=0.05)
    assert hasattr(t, "stop")
    # Let it poll at least once, then stop it.
    deadline = time.time() + 2.0
    while time.time() < deadline and calls[0] == 0:
        time.sleep(0.02)
    assert calls[0] >= 1
    t.stop()
    t.join(timeout=2.0)
    assert not t.is_alive()
    # No further polls after the thread has exited.
    settled = calls[0]
    time.sleep(0.2)
    assert calls[0] == settled


def test_track_queue_rejects_bool_interval():
    """`True` is an int (= 1) and `False` is 0; both should be rejected
    explicitly so consumers get a loud error instead of "1 second polling
    every iteration why is my dashboard so jittery"."""
    set_service("queue_test_svc")
    with pytest.raises(ValueError, match="positive finite number"):
        track_queue("true_interval", depth_fn=lambda: 1, interval=True)
    with pytest.raises(ValueError, match="positive finite number"):
        track_queue("false_interval", depth_fn=lambda: 1, interval=False)
