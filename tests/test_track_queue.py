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


def test_track_queue_swallows_depth_fn_exception():
    set_service("queue_test_svc")

    counter = [0]

    def bad():
        counter[0] += 1
        raise RuntimeError("boom")

    track_queue("broken", depth_fn=bad, interval=0.05)
    time.sleep(0.25)
    # Gauge should default to 0 (int coercion of caught exception path).
    v = REGISTRY.get_sample_value(
        "simsys_queue_depth", {"service": "queue_test_svc", "queue": "broken"}
    )
    assert v == 0
    # And the poller kept running (didn't crash after the first raise).
    assert counter[0] >= 2


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
