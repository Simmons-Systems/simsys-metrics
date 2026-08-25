"""Couple `go/go.mod`'s module path to the major version `go/CHANGELOG.md` documents.

WHAT THIS EXISTS TO CATCH

Go requires a `/vN` suffix on the module path for every major version >= 2.
A module whose path lacks it cannot be fetched at that major AT ALL:

    go get github.com/Simmons-Systems/simsys-metrics/go@v2.0.0
    invalid version: module contains a go.mod file, so module path must
    match major version ("github.com/Simmons-Systems/simsys-metrics/go/v2")

`go/v2.0.0` was tagged and released with `module .../simsys-metrics/go` still
in go.mod. Every check the repo had passed: `go build`, `go vet`,
`go test -race`, staticcheck, the contract conformance suite, and the release
workflow itself all went green, because none of them RESOLVES the module the
way a consumer does. The failure surfaced only when the published tag was
fetched from proxy.golang.org into a clean module -- after publication, which
for a Go tag is the one moment that cannot be undone.

Cost was limited to a dangling tag purely by luck of timing: the proxy 404'd
on both `.../go/@v/v2.0.0.info` and `.../go/v2/@v/v2.0.0.info`, so nothing had
been cached and no consumer could hold a checksum. `go/v2.0.1` supersedes it.

WHY IT LIVES IN THE PYTEST LANE

Same reason as `test_go_ci_staticcheck_pins.py` and the cross-language guard:
the pytest lane is required on four Python versions and `test.yml` carries no
path filter, so this runs on every pull request. It is a claim about Go
packaging asserted in Python because that is where it can actually block a
merge.

Stdlib-only: `.github/requirements/*.txt` is hash-pinned and carries no PyYAML
or tomli, and this suite runs on 3.10.
"""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
GO_MOD = ROOT / "go" / "go.mod"
GO_CHANGELOG = ROOT / "go" / "CHANGELOG.md"

BASE_PATH = "github.com/Simmons-Systems/simsys-metrics/go"


def _module_path() -> str:
    text = GO_MOD.read_text(encoding="utf-8")
    hits = re.findall(r"^module\s+(\S+)\s*$", text, re.MULTILINE)
    assert len(hits) == 1, (
        f"go/go.mod has {len(hits)} `module` lines ({hits}); this guard cannot "
        f"say which one it validated"
    )
    return hits[0]


def _documented_major() -> int:
    """Major version of the newest `## [go/vX.Y.Z]` heading in go/CHANGELOG.md.

    The changelog rather than `git tag`: the tag list and the documented
    release history have disagreed before (#50032, go/v0.3.0), and what a
    consumer can read is the changelog.
    """
    for line in GO_CHANGELOG.read_text(encoding="utf-8").splitlines():
        if not line.startswith("## "):
            continue
        m = re.match(r"^## \[go/v(\d+)\.\d+\.\d+\]", line)
        if m:
            return int(m.group(1))
    raise AssertionError(
        "go/CHANGELOG.md has no `## [go/vX.Y.Z]` release heading; this guard "
        "is watching nothing. Fix the pattern rather than deleting it."
    )


def test_parsers_find_what_they_claim() -> None:
    """Positive control.

    Both assertions below compare an extracted module path against an
    extracted major. A parser that silently matched nothing would make them
    vacuously true.
    """
    path = _module_path()
    assert path.startswith(BASE_PATH), (
        f"go/go.mod module path is {path!r}, which does not start with the "
        f"expected base {BASE_PATH!r}. Either the repo moved or this guard is "
        f"reading the wrong file."
    )
    major = _documented_major()
    assert major >= 0, f"parsed an implausible major: {major}"


def test_module_path_major_suffix_matches_the_documented_release() -> None:
    """The rule Go actually enforces at `go get` time.

    v0 and v1 take the bare path; v2+ takes an explicit `/vN`. Getting this
    wrong does not degrade anything -- it makes the module unfetchable at that
    major, and only after the tag is published and immutable.
    """
    path = _module_path()
    major = _documented_major()
    expected = BASE_PATH if major < 2 else f"{BASE_PATH}/v{major}"
    assert path == expected, (
        f"go/CHANGELOG.md documents go/v{major}.x but go/go.mod declares\n"
        f"    {path}\n"
        f"Go requires\n"
        f"    {expected}\n"
        f"for that major. Publishing this combination produces a tag that "
        f"CANNOT be fetched -- `go get` refuses it outright -- and a Go tag "
        f"can never be re-pointed, so the only repair is a new version. This "
        f"is what happened to go/v2.0.0."
    )


def test_internal_imports_use_the_declared_module_path() -> None:
    """Every in-repo Go import of this module must match go.mod exactly.

    A stale import inside `go/` still COMPILES locally, because the local
    module resolves by directory. It breaks only for an external consumer, so
    the lanes stay green and the defect ships.
    """
    path = _module_path()
    stale: list[str] = []
    for src in sorted((ROOT / "go").rglob("*.go")):
        for i, line in enumerate(src.read_text(encoding="utf-8").splitlines(), 1):
            for hit in re.findall(rf'"({re.escape(BASE_PATH)}[^"]*)"', line):
                if hit != path:
                    stale.append(f"{src.relative_to(ROOT)}:{i}: {hit}")
    assert not stale, (
        f"these in-repo imports disagree with go/go.mod's module path "
        f"({path}): {stale}. They compile locally by directory resolution and "
        f"break only for an external consumer."
    )


def test_go_readme_install_snippets_use_the_declared_module_path() -> None:
    """`go get` lines a reader copies must name the fetchable path.

    go/README.md shipped `go get .../go@v2.0.0` for as long as go/v2.0.0
    existed -- an install command that errors for every reader who runs it.
    """
    path = _module_path()
    text = (ROOT / "go" / "README.md").read_text(encoding="utf-8")
    gets = re.findall(rf"go get ({re.escape(BASE_PATH)}[^\s@]*)@", text)
    assert gets, (
        "go/README.md has no `go get <path>@...` line; this guard is watching "
        "nothing. Fix the pattern rather than deleting it."
    )
    wrong = sorted({g for g in gets if g != path})
    assert not wrong, (
        f"go/README.md tells readers to `go get` {wrong}, but the module path "
        f"is {path}. Every one of those commands fails."
    )
