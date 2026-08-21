"""Couple the staticcheck job's Go toolchain to its staticcheck release.

The staticcheck job in .github/workflows/go-ci.yml runs
dominikh/staticcheck-action with ``install-go: false`` and
``GOTOOLCHAIN=local``, so the staticcheck binary is BUILT and RUN with exactly
the toolchain ``actions/setup-go`` installs in that job. That makes the two
pins a matched pair, bounded on both sides:

    lower   staticcheck's own go.mod sets a minimum. 2026.2 declares
            ``go 1.26.0``, so a 1.25 toolchain cannot build it at all:
            "requires go >= 1.26.0 (running go 1.25.0; GOTOOLCHAIN=local)".
    upper   a staticcheck release can only decode export data from the
            toolchains it shipped with, so a toolchain NEWER than the pinned
            release fails every stdlib import with "export data version N is
            greater than maximum supported version M".

Move one pin without the other and the job breaks. That has happened three
times: Renovate floated the toolchain in 33db5b2 and f7f90cd, and #80 raised it
to 1.27.x against staticcheck 2026.1. Each time the symptom was the cryptic
export-data error above, and twice the "fix" was to float the pin again.

renovate.json now stops the BOT from moving it. This file stops a HUMAN from
moving it, which renovate.json cannot. It lives in the pytest lane on purpose:
the go-ci.yml jobs are NOT among the repository's required status checks, so a
PR can merge with staticcheck red -- which is exactly how #80 landed. pytest is
required on four Python versions and test.yml carries no path filter, so this
assertion runs on every pull request and can actually block the merge.

Deliberately stdlib-only: PyYAML is importable on a dev box but is absent from
both the declared test extra and the hash-pinned .github/requirements, so a
test that imported it would pass locally and fail in CI.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

WORKFLOW = Path(__file__).resolve().parents[1] / ".github" / "workflows" / "go-ci.yml"

# The matched pair currently in the workflow. Changing either pin means
# changing this table in the same commit -- that is the whole point.
EXPECTED_GO_TOOLCHAIN = "1.27.x"
EXPECTED_STATICCHECK = "2026.2"

# (minimum buildable Go minor, maximum supported Go minor) per staticcheck
# release. The minimum is the `go` directive in that release's own go.mod; the
# maximum is the newest Go it can decode export data for. Adding a row here is
# a claim about both bounds -- read the release notes before adding one.
STATICCHECK_GO_BOUNDS = {
    "2026.1": ((1, 25), (1, 26)),
    "2026.2": ((1, 26), (1, 27)),
}


def _read() -> str:
    assert WORKFLOW.is_file(), f"workflow not found: {WORKFLOW}"
    return WORKFLOW.read_text(encoding="utf-8")


def _job_block(text: str, job: str) -> str:
    """Return the body of a top-level job, excluding comment lines.

    Scoped on purpose: the `test` job also carries a `go-version:` key (a
    matrix and a `${{ matrix.go-version }}` reference), so an unscoped search
    would match the wrong pin. Comments are stripped because this job's
    comments quote both `go 1.26.0` and `2026.2` while explaining the bounds.
    """
    lines = text.splitlines()
    start = None
    for i, line in enumerate(lines):
        if re.match(rf"^  {re.escape(job)}:\s*$", line):
            start = i + 1
            break
    assert start is not None, f"job {job!r} not found in {WORKFLOW.name}"

    end = len(lines)
    for j in range(start, len(lines)):
        if re.match(r"^  \S", lines[j]):  # next job at the same indent
            end = j
            break

    body = lines[start:end]
    assert body, f"job {job!r} is empty"
    return "\n".join(ln for ln in body if not ln.lstrip().startswith("#"))


def _scalar(block: str, key: str) -> str:
    """Extract exactly one double-quoted scalar for `key`.

    Requires exactly one hit. Zero means the key moved or was renamed and this
    guard has quietly stopped guarding; more than one means the block grew a
    second pin and the test can no longer say which it checked. Both are
    failures, not silent passes.
    """
    hits = re.findall(rf'^\s*{re.escape(key)}:\s*"([^"]+)"\s*$', block, re.M)
    assert len(hits) == 1, (
        f"expected exactly one {key!r} in the staticcheck job, found {len(hits)}: {hits}"
    )
    return hits[0]


def _minor(version: str) -> tuple[int, int]:
    m = re.match(r"^(\d+)\.(\d+)", version)
    assert m, f"cannot parse a Go minor from {version!r}"
    return int(m.group(1)), int(m.group(2))


@pytest.fixture(scope="module")
def staticcheck_job() -> str:
    return _job_block(_read(), "staticcheck")


def test_parser_finds_the_pins_it_claims_to_check(staticcheck_job: str) -> None:
    """Positive control.

    Every assertion below is a comparison against something this parser
    extracted; a parser that silently matched nothing would make them vacuous.
    Prove it finds both pins and the action before trusting any of them.
    """
    assert _scalar(staticcheck_job, "go-version")
    assert _scalar(staticcheck_job, "version")
    assert "dominikh/staticcheck-action" in staticcheck_job


def test_pinned_toolchain_is_within_the_staticcheck_release_bounds(
    staticcheck_job: str,
) -> None:
    """The actual rule, read from the workflow rather than from constants here.

    This is the assertion that would have caught #80. Both operands come out of
    go-ci.yml, so it stays true to the artifact even if the recorded pair below
    is edited -- comparing EXPECTED_GO_TOOLCHAIN against
    STATICCHECK_GO_BOUNDS[EXPECTED_STATICCHECK] would pass no matter what the
    workflow said.
    """
    toolchain = _scalar(staticcheck_job, "go-version")
    release = _scalar(staticcheck_job, "version")

    assert release in STATICCHECK_GO_BOUNDS, (
        f"no recorded Go bounds for staticcheck {release!r}. Add a "
        "STATICCHECK_GO_BOUNDS row (minimum buildable, maximum supported) taken "
        "from that release's go.mod and release notes."
    )
    low, high = STATICCHECK_GO_BOUNDS[release]
    pinned = _minor(toolchain)
    assert low <= pinned <= high, (
        f"Go {toolchain} is outside staticcheck {release}'s supported range "
        f"{low[0]}.{low[1]}-{high[0]}.{high[1]}. Below the floor it cannot build "
        "staticcheck at all; above the ceiling every stdlib import fails with "
        '"export data version N is greater than maximum supported version M".'
    )


def test_go_toolchain_pin_matches_the_recorded_pair(staticcheck_job: str) -> None:
    found = _scalar(staticcheck_job, "go-version")
    assert found == EXPECTED_GO_TOOLCHAIN, (
        f"staticcheck job's go-version is {found!r}, expected {EXPECTED_GO_TOOLCHAIN!r}.\n"
        "This pin is bounded on BOTH sides by the staticcheck release below it. If "
        "you are moving it deliberately, move the staticcheck `version:` pin in the "
        "same change and update EXPECTED_GO_TOOLCHAIN / STATICCHECK_GO_BOUNDS here."
    )


def test_staticcheck_release_pin_matches_the_recorded_pair(
    staticcheck_job: str,
) -> None:
    found = _scalar(staticcheck_job, "version")
    assert found == EXPECTED_STATICCHECK, (
        f"staticcheck version is {found!r}, expected {EXPECTED_STATICCHECK!r}.\n"
        "Renovate does not extract this input, so it only moves by hand. Raise the "
        "go-version pin above it in the same change and update EXPECTED_STATICCHECK / "
        "STATICCHECK_GO_BOUNDS here."
    )


def test_toolchain_is_not_derived_from_go_mod(staticcheck_job: str) -> None:
    """go/go.mod's floor is the MODULE floor, not the linter toolchain.

    Deriving this from go/go.mod (currently `go 1.25.0`) puts the toolchain
    below staticcheck 2026.2's minimum, so it fails outright. This was tried.
    """
    assert "go-version-file" not in staticcheck_job, (
        "the staticcheck job must pin go-version explicitly, not use "
        "go-version-file: go/go.mod -- the module floor is below staticcheck's "
        "minimum buildable Go and the job fails outright."
    )
