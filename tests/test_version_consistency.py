"""Pin every published version number in the repo to a single authority per lane.

This repo distributes by git tag and release tarball rather than through a
package registry, so a README install snippet IS the package-manager UX. A
stale version there is a functional bug: the consumer who copies it installs
something other than what the changelog describes.

That has now happened three times. Redmine FR-070 ("README Node version
example stale, says 0.3.8, actual 0.4.4") and FR-101 ("README version table
says Node v0.3.8 but package.json is 0.4.4") were both filed and closed on
2026-07-06, and by 2026-08-22 `node/README.md` pointed at `node-v0.4.3` while
`node/package.json` was `0.5.0` -- with Python simultaneously drifted four
ways. Two manual fixes did not hold, so this file exists to make the third
one stick.

WHY IT LIVES IN THE PYTEST LANE
    The `main-protection` ruleset (id 18541630... see repo settings) requires
    `pytest (3.10-3.13)`, `ruff`, `pre-commit` and `Analyze (actions)`. The
    Go and Node lanes are NOT required checks, so a PR can merge with either
    red. A cross-lane invariant asserted anywhere else would not gate a
    merge. Same reasoning, and same placement, as
    `tests/test_go_ci_staticcheck_pins.py`.

WHY IT IS STDLIB-ONLY
    `.github/requirements/*.txt` is hash-pinned and does not carry PyYAML or
    tomli. `tomllib` is 3.11+, and this suite runs on 3.10, so `pyproject.toml`
    is read with a regex rather than a TOML parser. A test that imports a
    package absent from the CI lockfile passes locally and fails in CI, which
    is the exact trap `test_go_ci_staticcheck_pins.py` documents.

ON EXACTLY-ONE-HIT
    Every extractor below asserts on its match count. Zero hits means the
    guard has stopped guarding (the file was restructured and this parser now
    watches nothing); two or more means it cannot say which occurrence it
    checked. Both are failures, never silent passes. `test_parsers_find_what_
    they_claim` is the positive control: if the parsers themselves break,
    that test fails first and loudly, rather than every version check
    quietly succeeding against nothing.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]

SEMVER = r"\d+\.\d+\.\d+"


def _read(rel: str) -> str:
    path = ROOT / rel
    assert path.is_file(), (
        f"{rel} is missing -- this guard cannot verify what it cannot read"
    )
    return path.read_text(encoding="utf-8")


def _one(pattern: str, text: str, rel: str, what: str) -> str:
    """Return the single capture for `pattern`, failing on 0 or 2+ matches."""
    hits = re.findall(pattern, text, re.MULTILINE)
    assert hits, (
        f"{rel}: found NO {what} matching {pattern!r}. Either the file was "
        f"restructured and this guard is now watching nothing, or the value "
        f"was deleted. Fix the pattern -- do not delete the assertion."
    )
    assert len(hits) == 1, (
        f"{rel}: found {len(hits)} occurrences of {what} ({sorted(set(hits))}); "
        f"this guard cannot say which one it validated. Narrow the pattern."
    )
    return hits[0]


def _all(pattern: str, text: str, rel: str, what: str) -> list[str]:
    """Return every capture for `pattern`, failing if there are none."""
    hits = re.findall(pattern, text, re.MULTILINE)
    assert hits, (
        f"{rel}: found NO {what} matching {pattern!r}. This guard is watching "
        f"nothing. Fix the pattern -- do not delete the assertion."
    )
    return hits


def _newest_heading(rel: str, heading_pattern: str) -> str:
    """First matching '## [...]' heading in a Keep-a-Changelog file.

    Entries are reverse-chronological by convention, so the first match below
    `[Unreleased]` is the newest release. Asserted rather than assumed: the
    test below checks the file actually starts with `[Unreleased]`.
    """
    text = _read(rel)
    for line in text.splitlines():
        if not line.startswith("## "):
            continue
        m = re.match(heading_pattern, line)
        if m:
            return m.group(1)
    raise AssertionError(
        f"{rel}: no release heading matched {heading_pattern!r}. The changelog "
        f"heading format changed; update this pattern rather than removing it."
    )


# --------------------------------------------------------------------------
# Authorities
# --------------------------------------------------------------------------


def python_pyproject_version() -> str:
    # tomllib is 3.11+; this suite runs on 3.10. Anchored to the [project]
    # table's `version =` and asserted unique so a stray `version` key in
    # another table cannot be picked up instead.
    return _one(
        rf'^version = "({SEMVER})"$',
        _read("pyproject.toml"),
        "pyproject.toml",
        "project version",
    )


def python_dunder_version() -> str:
    return _one(
        rf'^__version__ = "({SEMVER})"$',
        _read("simsys_metrics/__init__.py"),
        "simsys_metrics/__init__.py",
        "__version__",
    )


def node_package_version() -> str:
    return _one(
        rf'^  "version": "({SEMVER})",$',
        _read("node/package.json"),
        "node/package.json",
        "package version",
    )


# --------------------------------------------------------------------------
# Python lane
# --------------------------------------------------------------------------


def test_python_authorities_agree() -> None:
    """pyproject.toml and __version__ must not drift from each other."""
    assert python_pyproject_version() == python_dunder_version(), (
        "pyproject.toml `version` and simsys_metrics.__version__ disagree. "
        "These are the two things a consumer can read at install time and at "
        "runtime; they must be the same number."
    )


def test_python_changelog_documents_current_version() -> None:
    newest = _newest_heading(
        "CHANGELOG.md",
        rf"^## \[python-v({SEMVER})\]",
    )
    assert newest == python_pyproject_version(), (
        f"CHANGELOG.md's newest Python entry is {newest} but pyproject.toml is "
        f"{python_pyproject_version()}. Either the release is undocumented or "
        f"the version was never bumped."
    )


def test_python_readme_install_snippets_are_current() -> None:
    """Every `python-v<ver>` in the root README must be the current version.

    Includes the version-table example row: FR-101 was filed against exactly
    that row, so it is in scope by precedent.
    """
    expected = python_pyproject_version()
    pins = _all(
        rf"python-v({SEMVER})", _read("README.md"), "README.md", "python-v pins"
    )
    stale = sorted({p for p in pins if p != expected})
    assert not stale, (
        f"README.md pins python-v{stale} but the package is {expected}. "
        f"Consumers copy these lines verbatim to install. Occurrences: {len(pins)}."
    )


# --------------------------------------------------------------------------
# Node lane
# --------------------------------------------------------------------------


def test_node_changelog_documents_current_version() -> None:
    newest = _newest_heading(
        "node/CHANGELOG.md",
        rf"^## \[(?:node-v)?({SEMVER})\]",
    )
    assert newest == node_package_version(), (
        f"node/CHANGELOG.md's newest entry is {newest} but node/package.json is "
        f"{node_package_version()}."
    )


def test_node_readme_release_tag_is_current() -> None:
    """The `node-v<ver>` path segment of the install URL."""
    expected = node_package_version()
    tag = _one(
        rf"/releases/download/node-v({SEMVER})/",
        _read("node/README.md"),
        "node/README.md",
        "release tag in the install URL",
    )
    assert tag == expected, (
        f"node/README.md install URL points at release tag node-v{tag} but "
        f"node/package.json is {expected}."
    )


def test_node_readme_tarball_filename_is_current() -> None:
    """The `simsys-metrics-<ver>.tgz` filename -- a SEPARATE number.

    The install URL carries two independently-editable version strings: the
    release tag and the tarball filename. Checking only one lets the other
    rot, and a URL whose tag and filename disagree 404s rather than
    installing the wrong version -- a different and louder failure, but a
    failure the tag check alone cannot see.
    """
    expected = node_package_version()
    tarball = _one(
        rf"/simsys-metrics-({SEMVER})\.tgz",
        _read("node/README.md"),
        "node/README.md",
        "tarball filename in the install URL",
    )
    assert tarball == expected, (
        f"node/README.md install URL names simsys-metrics-{tarball}.tgz but "
        f"node/package.json is {expected}."
    )


def test_root_readme_node_pin_is_current() -> None:
    expected = node_package_version()
    pins = _all(rf"node-v({SEMVER})", _read("README.md"), "README.md", "node-v pins")
    stale = sorted({p for p in pins if p != expected})
    assert not stale, (
        f"README.md pins node-v{stale} but node/package.json is {expected}."
    )


# --------------------------------------------------------------------------
# Go lane
# --------------------------------------------------------------------------


def go_changelog_version() -> str:
    """Newest DOCUMENTED Go release.

    Deliberately the changelog and not `git tag`: Redmine #50032 records that
    `go/v0.3.0` is tagged on main with no changelog entry at all, so the tag
    list and the documented release history disagree. This guard tracks what
    consumers can actually read; #50032 resolves the tag itself.
    """
    return _newest_heading("go/CHANGELOG.md", rf"^## \[go/v({SEMVER})\]")


def test_go_readme_pin_is_current() -> None:
    expected = go_changelog_version()
    pins = _all(
        rf"@v({SEMVER})", _read("go/README.md"), "go/README.md", "go get version args"
    )
    stale = sorted({p for p in pins if p != expected})
    assert not stale, (
        f"go/README.md pins @v{stale} but the newest documented Go release is "
        f"v{expected}."
    )


def test_root_readme_go_pin_is_current() -> None:
    expected = go_changelog_version()
    text = _read("README.md")
    tag_pins = _all(rf"go/v({SEMVER})", text, "README.md", "go/v pins")
    get_pins = _all(rf"go get [^\n`]*@v({SEMVER})", text, "README.md", "go get pins")
    stale = sorted({p for p in tag_pins + get_pins if p != expected})
    assert not stale, (
        f"README.md pins go v{stale} but the newest documented Go release is v{expected}."
    )


# --------------------------------------------------------------------------
# Positive control
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "extractor",
    [
        python_pyproject_version,
        python_dunder_version,
        node_package_version,
        go_changelog_version,
    ],
    ids=lambda f: f.__name__,
)
def test_parsers_find_what_they_claim(extractor) -> None:
    """The guard's own instruments must work before its verdicts mean anything.

    Every check above compares an extracted value against another extracted
    value. If BOTH extractors silently returned the same wrong thing -- or the
    file layout changed so a pattern matched nothing -- the comparisons could
    pass while validating nothing. This asserts each extractor returns a
    plausible semver, so a broken parser fails here first.
    """
    value = extractor()
    assert re.fullmatch(SEMVER, value), (
        f"{extractor.__name__} returned {value!r}, not a semver"
    )


def test_changelogs_have_an_unreleased_section() -> None:
    """`_newest_heading` assumes reverse-chronological order.

    Keep a Changelog puts `[Unreleased]` first, and every release below it in
    descending order. If a changelog loses that structure, "first matching
    heading" stops meaning "newest release" and every check above silently
    validates against the wrong entry.
    """
    for rel in ("CHANGELOG.md", "node/CHANGELOG.md", "go/CHANGELOG.md"):
        text = _read(rel)
        headings = [ln for ln in text.splitlines() if ln.startswith("## ")]
        assert headings, f"{rel}: no '## ' headings at all"
        assert headings[0].startswith("## [Unreleased]"), (
            f"{rel}: first heading is {headings[0]!r}, not '## [Unreleased]'. "
            f"The newest-release detection above depends on reverse-chronological "
            f"ordering; verify that still holds before changing this."
        )
