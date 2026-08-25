"""Python-lane conformance against spec/metrics-contract.json.

Asserts against a LIVE registry rather than against the source that defines
the metrics. Reading the definitions back would be a tautology: the test
would agree with the implementation by construction and could never catch a
metric that fails to register.

Three directions, and the third is the one that catches drift nobody filed:
  1. every core metric is present with the declared type and label set
  2. every extension claiming python is present
  3. NO simsys_-prefixed metric is emitted that the contract does not declare
Direction 3 is what would have caught simsys_pool_* shipping in all three
lanes while appearing in none of the three catalogues.
"""

from __future__ import annotations

import pytest
from prometheus_client import REGISTRY

import simsys_metrics
from simsys_metrics._baseline import _reset_for_tests

from .contract import labels_for, load_contract, metrics_for

# prometheus_client emits a companion `_created` gauge alongside every counter
# and histogram. They are a client-library artifact, not part of the contract,
# and no other lane emits them.
_CLIENT_ARTIFACT_SUFFIXES = ("_created",)

# Synthetic labels Prometheus itself adds to sample rows: `le` on histogram
# buckets, `quantile` on summaries. They are not part of any metric's declared
# label set and no lane "chooses" them, so comparing them against the contract
# would fail every histogram for a reason that has nothing to do with drift.
_SYNTHETIC_LABELS = {"le", "quantile"}

_PROM_TYPE = {"counter": "counter", "gauge": "gauge", "histogram": "histogram"}


@pytest.fixture(scope="module")
def emitted() -> dict[str, tuple[str, set[str]]]:
    """{name: (type, label-name set)} for every simsys_ metric in the registry.

    A real install plus every opt-in helper, because registration is split:
    queue/job/pool/progress collectors appear on `import simsys_metrics`, but
    build_info and the process/runtime collectors only appear on `install()`.
    Asserting against an import-only registry would report half the catalogue
    missing and look like a contract error rather than a fixture error.

    The helpers are also *driven* so real series exist -- a collector with no
    samples exposes no label names, and a label check against zero series
    passes without testing anything.
    """
    _reset_for_tests()

    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    app = FastAPI()

    @app.get("/items/{item_id}")
    def _item(item_id: int):  # pragma: no cover - driven via TestClient
        return {"id": item_id}

    simsys_metrics.install(app, service="contract-conformance", version="0.0.0")

    # Drive one request so the HTTP families emit labelled series.
    TestClient(app).get("/items/1")

    # Exercise every opt-in helper so its labels become observable.
    simsys_metrics.track_queue("cq", depth_fn=lambda: 1, interval=3600)
    simsys_metrics.track_pool(
        "cp", active_fn=lambda: 1, idle_fn=lambda: 1, interval=3600
    )

    # simsys_collector_errors_total only ever gets a series when a poller
    # callback FAILS (#50319), so the fixture has to break one on purpose.
    # Without this the counter has no samples, `test_declared_labels_match`
    # SKIPS, and its label set ships unverified -- a skip is not a pass.
    def _boom() -> int:
        raise RuntimeError("contract fixture: deliberate depth_fn failure")

    _broken = simsys_metrics.track_queue("cq-broken", depth_fn=_boom, interval=3600)
    _broken.stop()

    with simsys_metrics.track_job("cj"):
        pass
    simsys_metrics.track_progress(
        simsys_metrics.ProgressOpts(operation="cop", total=1, window=60, interval=3600)
    ).stop()

    out: dict[str, tuple[str, set[str]]] = {}
    for fam in REGISTRY.collect():
        if not fam.name.startswith("simsys_"):
            continue
        labels: set[str] = set()
        for s in fam.samples:
            if s.name.endswith(_CLIENT_ARTIFACT_SUFFIXES):
                continue
            labels |= set(s.labels) - _SYNTHETIC_LABELS
        out[fam.name] = (fam.type, labels)
    _reset_for_tests()
    return out


def _family_key(name: str, mtype: str) -> str:
    """prometheus_client reports counters without the _total suffix."""
    if mtype == "counter" and name.endswith("_total"):
        return name[: -len("_total")]
    return name


@pytest.mark.parametrize("name", sorted(metrics_for("python")))
def test_declared_metric_is_registered(name, emitted) -> None:
    m = load_contract()["metrics"][name]
    key = _family_key(name, m["type"])
    assert key in emitted, (
        f"contract declares {name} for python but the registry does not emit "
        f"it. Either implement it or remove it from `runtimes`."
    )
    got_type, _ = emitted[key]
    assert got_type == _PROM_TYPE[m["type"]], (
        f"{name}: contract says {m['type']}, registry emits {got_type}"
    )


@pytest.mark.parametrize("name", sorted(metrics_for("python")))
def test_declared_labels_match(name, emitted) -> None:
    """Label NAMES, not values.

    A label set that silently differs by runtime is the failure mode that
    breaks a shared dashboard without breaking anything visibly -- a panel
    with a matcher on a missing label just renders empty.
    """
    m = load_contract()["metrics"][name]
    key = _family_key(name, m["type"])
    if key not in emitted:
        pytest.skip("covered by test_declared_metric_is_registered")
    _, got = emitted[key]
    if not got:
        pytest.skip("no series emitted yet; labels unobservable without samples")
    assert got == set(labels_for(m, "python")), (
        f"{name}: contract declares {sorted(labels_for(m, 'python'))}, "
        f"registry emits {sorted(got)}"
    )


def test_no_undeclared_simsys_metric_is_emitted(emitted) -> None:
    """The additive-drift check.

    A metric added to the code but not the contract fails HERE. This is the
    direction that was missing entirely before: simsys_pool_* shipped in all
    three lanes and appeared in none of the three README catalogues, and
    nothing anywhere noticed.
    """
    declared = set()
    for name, m in load_contract()["metrics"].items():
        declared.add(name)
        declared.add(_family_key(name, m["type"]))
    undeclared = sorted(set(emitted) - declared)
    assert not undeclared, (
        f"these simsys_ metrics are emitted but not declared in "
        f"spec/metrics-contract.json: {undeclared}. Add them to the contract "
        f"(and to the README catalogues) or stop emitting them."
    )


def test_python_only_declares_what_it_can_emit() -> None:
    """Negative control on the runtimes array.

    Node-only and Go-only metrics must NOT claim python. Without this, a
    contract listing every runtime on every metric would pass direction 3 and
    look complete while meaning nothing.
    """
    py = metrics_for("python")
    for name in ("simsys_process_uptime_seconds", "simsys_runtime_goroutines"):
        assert name not in py, f"{name} is not a python metric but claims python"
