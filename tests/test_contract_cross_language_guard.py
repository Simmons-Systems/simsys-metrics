"""Cross-language invariants for spec/metrics-contract.json, in the lane that gates merges.

WHY HERE. The `main-protection` ruleset requires `pytest (3.10-3.13)`, `ruff`,
`pre-commit` and the four `Analyze (...)` checks. The Node and Go *test* lanes
are NOT required, so a PR can merge with either red. Any invariant that must
block a merge therefore has to execute in the pytest lane. Same reasoning and
placement as `tests/test_go_ci_staticcheck_pins.py`, which parses go-ci.yml
with stdlib Python for exactly this reason.

WHY STDLIB. `.github/requirements/*.txt` is hash-pinned and carries no PyYAML.
A test importing it passes locally and fails in CI.

WHAT THIS DOES NOT DO. It does not check that a lane EMITS what the contract
declares -- that needs a live registry and lives in each lane's own
conformance test. This file checks the contract is internally coherent, that
each lane's source agrees with it on the values a parser can see, and that
the divergence list cannot quietly grow.

EXACTLY-ONE-HIT. Every extractor asserts its match count. Zero means the
guard stopped guarding; two means it cannot say which occurrence it checked.
Both are failures. `test_parsers_find_what_they_claim` is the positive
control: if the parsers break, it fails first rather than every check quietly
passing against nothing.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from .contract import CONTRACT_PATH, load_contract

ROOT = Path(__file__).resolve().parents[1]

VALID_TYPES = {"counter", "gauge", "histogram"}
VALID_RUNTIMES = {"python", "node", "go"}
VALID_TIERS = {"core", "extension"}


# --------------------------------------------------------------------------
# Contract self-validity
# --------------------------------------------------------------------------


def test_contract_is_valid_json_and_has_required_top_level_keys() -> None:
    c = load_contract()
    for key in (
        "contract_version",
        "prefix",
        "required_label",
        "enums",
        "bucket_schedules",
        "metrics",
        "divergences",
        "behaviors",
    ):
        assert key in c, f"contract is missing top-level key {key!r}"


def test_every_metric_is_well_formed() -> None:
    c = load_contract()
    prefix, required = c["prefix"], c["required_label"]
    for name, m in c["metrics"].items():
        assert name.startswith(prefix), f"{name} lacks the {prefix} prefix"
        assert m["type"] in VALID_TYPES, f"{name}: bad type {m['type']!r}"
        assert m["tier"] in VALID_TIERS, f"{name}: bad tier {m['tier']!r}"
        assert m["runtimes"], f"{name}: empty runtimes list"
        assert set(m["runtimes"]) <= VALID_RUNTIMES, f"{name}: unknown runtime"
        if m.get("status") == "divergent":
            continue  # label list lives under `current`, checked separately
        assert required in m["labels"], (
            f"{name} does not carry the mandatory {required!r} label. Every "
            f"dashboard in the fleet templates on it."
        )


def test_core_metrics_are_present_in_every_runtime() -> None:
    """`core` is a promise. A core metric missing a lane is a broken promise."""
    for name, m in load_contract()["metrics"].items():
        if m["tier"] == "core":
            assert set(m["runtimes"]) == VALID_RUNTIMES, (
                f"{name} is tier=core but only claims {m['runtimes']}. Either "
                f"implement it everywhere or declare it tier=extension -- core "
                f"is what a cross-runtime dashboard relies on unconditionally."
            )


def test_bucket_schedules_are_sane() -> None:
    for sched, body in load_contract()["bucket_schedules"].items():
        bounds = body["bounds"]
        assert bounds == sorted(bounds), f"{sched}: bounds not ascending"
        assert len(bounds) == len(set(bounds)), f"{sched}: duplicate bounds"
        assert all(b > 0 for b in bounds), f"{sched}: non-positive bound"


def test_http_schedule_still_covers_the_long_tail() -> None:
    """Regression pin for the p95 ceiling bug.

    The schedule once stopped at 10.0, so every slower request fell into +Inf
    and histogram_quantile could never exceed 10 -- p95 pinned at exactly
    10.00 for any service with a real tail. Now that all three lanes read
    their expectations from this file, a bad edit HERE would launder that
    regression back into every lane at once. This is the assertion that stops
    it.
    """
    bounds = load_contract()["bucket_schedules"]["http_request_duration_seconds"][
        "bounds"
    ]
    assert max(bounds) > 10.0, (
        "the HTTP bucket schedule has no bound above 10s; this reintroduces "
        "the ceiling that made p95 unknowable and a 15s alert unfirable"
    )


def test_every_enum_and_bucket_reference_resolves() -> None:
    c = load_contract()
    enums, scheds = set(c["enums"]), set(c["bucket_schedules"])
    for name, m in c["metrics"].items():
        for label, enum in (m.get("label_enums") or {}).items():
            assert enum in enums, f"{name}.{label} -> unknown enum {enum!r}"
        if m["type"] == "histogram":
            assert m.get("buckets") in scheds, (
                f"{name}: unknown schedule {m.get('buckets')!r}"
            )


# --------------------------------------------------------------------------
# The divergence budget -- what stops "declare it divergent" beating "fix it"
# --------------------------------------------------------------------------


def test_every_divergence_carries_a_ticket() -> None:
    """No ticket, no divergence.

    Without this, marking something `divergent` is strictly cheaper than
    filing the bug, and the contract becomes a place defects go to be
    legitimised rather than tracked.
    """
    c = load_contract()
    for name, m in c["metrics"].items():
        if m.get("status") == "divergent":
            assert m.get("ticket"), f"{name} is divergent with no ticket reference"
            assert "current" in m and "target" in m, (
                f"{name} is divergent but does not say what it does now and "
                f"what it should do -- both are required to ever close it"
            )
    for name, b in c["behaviors"].items():
        if name.startswith("$"):
            continue
        if b.get("status") == "divergent":
            assert b.get("ticket"), f"behavior {name} is divergent with no ticket"
            assert "current" in b and "target" in b, (
                f"behavior {name}: needs current+target"
            )


def test_divergence_list_matches_the_metrics_that_declare_it() -> None:
    c = load_contract()
    declared = {n for n, m in c["metrics"].items() if m.get("status") == "divergent"}
    listed = {e["metric"] for e in c["divergences"]["entries"]}
    assert declared == listed, (
        f"metrics marked divergent {sorted(declared)} do not match the "
        f"divergences list {sorted(listed)}. The list is the reviewable "
        f"summary; a divergence that is not in it is invisible."
    )


def test_divergence_count_is_budgeted() -> None:
    """Adding a divergence must be a deliberate edit to a NUMBER.

    A growing list in a long file is something reviewers skim past. A changed
    integer in a diff is something they have to acknowledge. If this fails
    because you added a divergence, raise the number ON PURPOSE and say why
    in the commit message.
    """
    c = load_contract()
    entries = c["divergences"]["entries"]
    assert len(entries) == c["divergences"]["expected_count"], (
        f"{len(entries)} divergences declared but expected_count is "
        f"{c['divergences']['expected_count']}"
    )


# --------------------------------------------------------------------------
# Contract vs each lane's source -- what a stdlib parser can see
# --------------------------------------------------------------------------


def _one(pattern: str, text: str, rel: str, what: str) -> str:
    hits = re.findall(pattern, text, re.MULTILINE | re.DOTALL)
    assert hits, (
        f"{rel}: found NO {what} matching {pattern!r}. Either the file moved "
        f"or was restructured and this guard now watches nothing. Fix the "
        f"pattern -- do not delete the assertion."
    )
    assert len(hits) == 1, (
        f"{rel}: {len(hits)} occurrences of {what}; this guard cannot say "
        f"which one it validated."
    )
    return hits[0]


def _floats(blob: str) -> list[float]:
    return [float(x) for x in re.findall(r"-?\d+\.?\d*", blob)]


# Patterns are per-file rather than uniform because the three lanes express
# the schedule differently: Go names it (HTTPBuckets/JobBuckets), Node and
# Python inline it at the metric definition. Each was verified against the
# real file rather than assumed -- an early version anchored on the metric
# name with a 400-character window and silently matched NOTHING in
# _http.py, where the literal sits ~500 characters below its metric name
# behind a long comment. A window is a filter, and a filter cannot prove
# absence; `_one` turns that into a loud failure instead of a vacuous pass.
BUCKET_SOURCES = {
    "http_request_duration_seconds": [
        # _http.py contains exactly one `buckets=`; unanchored is safer than a
        # window, and `_one` fails loudly if a second one ever appears.
        ("simsys_metrics/_http.py", r"buckets=\(([^)]*)\)"),
        (
            "node/src/registry.ts",
            r"simsys_http_request_duration_seconds[\s\S]{0,800}?buckets:\s*\[([^\]]*)\]",
        ),
        ("go/metrics.go", r"HTTPBuckets\s*=\s*\[\]float64\{([^}]*)\}"),
    ],
    "job_duration_seconds": [
        (
            "simsys_metrics/_baseline.py",
            r"simsys_job_duration_seconds[\s\S]{0,800}?buckets=\(([^)]*)\)",
        ),
        (
            "node/src/registry.ts",
            r"simsys_job_duration_seconds[\s\S]{0,800}?buckets:\s*\[([^\]]*)\]",
        ),
        ("go/metrics.go", r"JobBuckets\s*=\s*\[\]float64\{([^}]*)\}"),
    ],
}


@pytest.mark.parametrize("schedule", sorted(BUCKET_SOURCES))
def test_each_lane_bucket_literal_equals_the_contract(schedule: str) -> None:
    """The three hand-mirrored copies are gone; this proves they stay gone.

    Before the contract, each lane held its own literal copy of the schedule
    and ran in its own CI lane, so changing it in one language passed CI as
    long as that language's own literal was updated too. Nothing cross-checked
    the three. This is that cross-check.
    """
    expected = load_contract()["bucket_schedules"][schedule]["bounds"]
    for rel, pattern in BUCKET_SOURCES[schedule]:
        path = ROOT / rel
        if not path.is_file():
            pytest.fail(
                f"{rel} is missing; this guard cannot check what it cannot read"
            )
        got = _floats(
            _one(pattern, path.read_text(encoding="utf-8"), rel, f"{schedule} literal")
        )
        assert got == [float(x) for x in expected], (
            f"{rel} declares {schedule} as {got}, contract says {expected}. "
            f"Edit spec/metrics-contract.json first, then the lanes."
        )


def test_conformance_tests_exist_and_read_the_contract() -> None:
    """Stops the deep per-lane checks being deleted in an unrequired lane.

    The Node and Go conformance tests run in lanes that cannot block a merge,
    so their removal would otherwise be invisible from here.
    """
    for rel in (
        "tests/test_metrics_contract.py",
        "node/tests/contract-conformance.test.ts",
        "go/contract_conformance_test.go",
    ):
        path = ROOT / rel
        assert path.is_file(), (
            f"{rel} is missing -- a lane has stopped checking the contract"
        )
        assert "metrics-contract.json" in path.read_text(encoding="utf-8"), (
            f"{rel} no longer references the contract file; it may be asserting "
            f"against its own literals again, which is the state this replaced"
        )


def test_spec_is_wired_into_the_language_ci_lanes() -> None:
    """A contract-only edit must trigger the lanes that verify it.

    Without spec/** in the path filters, editing this file alone runs neither
    the Node nor the Go lane -- the guard would exist and never execute on the
    change most likely to break it.
    """
    for rel in (".github/workflows/test-node.yml", ".github/workflows/go-ci.yml"):
        text = (ROOT / rel).read_text(encoding="utf-8")
        assert text.count('"spec/**"') == 2, (
            f"{rel}: expected spec/** in BOTH the push and pull_request path "
            f"filters, found {text.count('spec/**')}"
        )


# --------------------------------------------------------------------------
# Positive control
# --------------------------------------------------------------------------


def test_parsers_find_what_they_claim() -> None:
    """If the extractors break, fail HERE rather than passing vacuously.

    Every comparison above is extracted-value vs extracted-value. A pattern
    that silently matched nothing would make those comparisons trivially true.
    """
    for schedule, sources in BUCKET_SOURCES.items():
        for rel, pattern in sources:
            blob = _one(
                pattern, (ROOT / rel).read_text(encoding="utf-8"), rel, "bucket literal"
            )
            vals = _floats(blob)
            assert len(vals) >= 5, (
                f"{rel}: parsed only {len(vals)} bucket bounds from {schedule}; "
                f"the pattern is matching the wrong thing"
            )
    assert CONTRACT_PATH.is_file()
    assert json.loads(CONTRACT_PATH.read_text(encoding="utf-8"))["metrics"], (
        "contract has no metrics"
    )
