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


# --------------------------------------------------------------------------
# Cross-lane alignment -- the invariant that makes "standardized" real
# --------------------------------------------------------------------------


def test_all_three_lanes_are_on_the_same_version() -> None:
    """The version number IS the contract version, in every lane.

    Before 1.0.0 the lanes drifted independently -- Python 0.4.0, Node 0.5.0,
    Go 0.3.1 -- so "which version am I on" had three different answers and
    none of them identified which metric catalogue you were getting.

    From 1.0.0 they move together. That is only true if something enforces it:
    the natural failure is bumping one lane for a lane-specific fix and
    quietly re-introducing the drift this replaced. Bumping one lane now means
    bumping all three, deliberately.

    If a change genuinely affects only one runtime, it still gets a version in
    all three -- the number identifies the CONTRACT, not the size of the diff.
    """
    py = python_pyproject_version()
    node = node_package_version()
    go = go_changelog_version()
    assert py == node == go, (
        f"lanes have drifted: python={py}, node={node}, go={go}. From 1.0.0 the "
        f"version number is the contract version and must match across all "
        f"three. Bump all three together, or the number stops identifying "
        f"which catalogue a consumer has."
    )


def test_version_matches_the_contract_version() -> None:
    """And that shared number is the one the contract declares."""
    from .contract import load_contract

    declared = load_contract()["contract_version"]
    assert python_pyproject_version() == declared, (
        f"packages are {python_pyproject_version()} but "
        f"spec/metrics-contract.json declares contract_version {declared}. "
        f"These are the same number by definition."
    )


# --------------------------------------------------------------------------
# Publish metadata -- only ever validated at publish time otherwise
# --------------------------------------------------------------------------


def test_node_package_declares_repository_for_provenance() -> None:
    """npm rejects a provenance publish whose repository.url does not match.

    `npm publish --provenance` has the registry verify the sigstore bundle
    against package.json, and a missing or empty repository.url fails with:

        E422 ... "repository.url" is "", expected to match
        "https://github.com/Simmons-Systems/simsys-metrics" from provenance

    Nothing before the publish step catches it -- not the build, not vitest,
    not `npm pack --dry-run`, not the typecheck. It surfaced only on a real
    tag push, after three other publish faults had been cleared, which is the
    most expensive possible moment to learn about a metadata field.
    """
    import json

    pkg = json.loads((ROOT / "node" / "package.json").read_text(encoding="utf-8"))
    repo = pkg.get("repository")
    assert isinstance(repo, dict), (
        "node/package.json has no `repository` object; npm provenance "
        "publication will fail E422 at tag time"
    )
    url = repo.get("url", "")
    assert "github.com/Simmons-Systems/simsys-metrics" in url, (
        f"repository.url is {url!r}; provenance verification requires it to "
        f"match the building repository"
    )
    assert repo.get("directory") == "node", (
        "repository.directory must be 'node' -- the package lives in a "
        "monorepo subdirectory, and npm uses this to resolve source links"
    )


# --------------------------------------------------------------------------
# Documented imports -- the READMEs are the package's public API surface
# --------------------------------------------------------------------------
#
# #50324 shipped `make_counter` / `make_gauge` / `make_histogram` as public
# exports because the README told users to import them and `__all__` did not
# carry them. That defect is fixed, but nothing stopped it recurring: a README
# can document any symbol it likes, and no test reads the READMEs.
#
# There are two ways for documentation and `__all__` to disagree, and only the
# first was named in #50326:
#
#   1. the README documents a symbol that is NOT exported -- the #50324 shape;
#      the copied snippet raises ImportError.
#   2. the README documents a PRIVATE path for a symbol that IS exported --
#      the snippet works, so nothing ever fails, while every consumer who
#      copies it is coupled to `simsys_metrics._registry`. Renaming a private
#      module is then a breaking change to code we told people to write.
#
# The second is the one the tree actually had (README.md:236,
# `from simsys_metrics._registry import make_counter`, two lines above a
# `from simsys_metrics import get_service` in the same snippet). It is the
# more dangerous of the two precisely because it cannot fail loudly.


IMPORT_LINE = re.compile(
    r"^from\s+(simsys_metrics(?:\.[A-Za-z_][A-Za-z0-9_]*)*)\s+import\s+(.+)$",
    re.MULTILINE,
)


def _documented_imports() -> list[tuple[str, str, str]]:
    """Every `from simsys_metrics... import ...` in every tracked markdown file.

    Returns `(relative_path, module, symbol)` triples, one per imported name,
    so a single `import a, b` line yields two entries.

    Markdown is scanned wholesale rather than only inside ```python fences:
    a snippet outside a fence is still an instruction to the reader, and a
    fence-aware parser would silently stop guarding the moment someone used
    an indented code block instead.
    """
    out: list[tuple[str, str, str]] = []
    for path in sorted(ROOT.rglob("*.md")):
        if "node_modules" in path.parts or ".venv" in path.parts:
            continue
        rel = path.relative_to(ROOT).as_posix()
        for module, names in IMPORT_LINE.findall(path.read_text(encoding="utf-8")):
            for name in names.split("#")[0].split(","):
                name = name.strip().split(" as ")[0].strip()
                if name and name != "(":
                    out.append((rel, module, name))
    return out


def test_documented_import_parser_finds_what_it_claims() -> None:
    """Positive control for both assertions below.

    Both checks are "no documented import violates X". If the parser matched
    nothing they would both pass while reading no documentation at all -- the
    exact failure mode `test_parsers_find_what_they_claim` exists to catch for
    the version extractors. `install` is the package's single entry point
    ("One install() call" -- README first paragraph), so a README set that
    does not document it means the parser, not the README, is broken.
    """
    found = _documented_imports()
    assert len(found) >= 3, (
        f"only {len(found)} documented `from simsys_metrics import` statements "
        f"found across all markdown; this guard is watching nothing. Fix the "
        f"pattern -- do not delete the assertion."
    )
    symbols = {sym for _, _, sym in found}
    assert "install" in symbols, (
        f"the parser found {sorted(symbols)} but not `install`, the package's "
        f"documented entry point. The parser is broken, not the docs."
    )


def test_documented_symbols_are_exported() -> None:
    """Every symbol a README tells users to import must be in `__all__`.

    This is the #50324 shape: a snippet that raises ImportError when copied.
    """
    import simsys_metrics

    exported = set(simsys_metrics.__all__)
    missing = sorted(
        {
            (rel, sym)
            for rel, module, sym in _documented_imports()
            if module == "simsys_metrics" and sym not in exported
        }
    )
    assert not missing, (
        f"these READMEs document symbols that are not in "
        f"simsys_metrics.__all__: {missing}. A reader copying the snippet gets "
        f"an ImportError. Either export the symbol or stop documenting it."
    )


def test_no_readme_documents_a_private_module_path() -> None:
    """No README may tell a user to import from a `_`-prefixed module.

    Private modules are an implementation detail this repo renames freely
    (`_registry`, `_http`, `_process`, `_baseline`). A documented import from
    one turns that freedom into a breaking change for every consumer who
    copied the snippet -- and unlike an unexported symbol, it never fails, so
    it survives indefinitely.

    If a symbol is worth documenting it is worth exporting; the fix is to add
    it to `__all__` and document the top-level path, never to allowlist a
    private module here.
    """
    offenders = sorted(
        {
            (rel, module, sym)
            for rel, module, sym in _documented_imports()
            if any(part.startswith("_") for part in module.split(".")[1:])
        }
    )
    assert not offenders, (
        f"these READMEs document imports from private modules: {offenders}. "
        f"Export the symbol from simsys_metrics and document `from "
        f"simsys_metrics import <name>` instead."
    )


# --------------------------------------------------------------------------
# Repository URL -- the org migration must not regress
# --------------------------------------------------------------------------


# Assembled at runtime so this file does not contain the needle it hunts.
# Spelling it out here would make the scanner flag its own source, and the
# obvious fix -- allowlisting this path -- would blind the guard to a
# regression landing in the guard's own directory.
STALE_ORG_URL = "Avicennasis" + "/simsys-metrics"


def _tracked_files() -> list[str]:
    """Git-tracked paths. Loud on failure rather than silently empty.

    `git ls-files` rather than a filesystem walk: the walk would have to guess
    at build output, virtualenvs and vendored trees, and a guessed exclusion
    that is too broad turns this guard off without saying so. CI runs
    actions/checkout, so git and a real index are both present.
    """
    import subprocess

    proc = subprocess.run(
        ["git", "-C", str(ROOT), "ls-files", "-z"],
        capture_output=True,
        text=True,
    )
    assert proc.returncode == 0, (
        f"`git ls-files` failed ({proc.returncode}): {proc.stderr.strip()!r}. "
        f"This guard cannot enumerate what it cannot list -- fix the "
        f"invocation rather than letting it pass on an empty file set."
    )
    files = [f for f in proc.stdout.split("\0") if f]
    assert len(files) > 50, (
        f"`git ls-files` returned only {len(files)} paths, which is not a "
        f"checkout of this repo. Refusing to report a clean scan."
    )
    return files


def _stale_org_hits() -> dict[str, int]:
    """`{path: occurrences}` for every tracked text file carrying the old org."""
    hits: dict[str, int] = {}
    for rel in _tracked_files():
        path = ROOT / rel
        try:
            text = path.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue  # binary or unreadable: cannot carry a URL we can read
        n = text.count(STALE_ORG_URL)
        if n:
            hits[rel] = n
    return hits


def test_stale_org_scanner_can_see_the_pattern() -> None:
    """Positive control: the scanner must find the ALLOWED occurrences.

    The assertion below is "no hits outside the changelogs". If the scanner
    read nothing -- wrong root, empty file list, an encoding guard swallowing
    every file -- that assertion passes trivially and reports a clean repo.

    The changelogs legitimately record the pre-migration path (the transfer is
    a historical fact), which makes them a guaranteed non-empty positive case:
    if this test finds zero, the scanner is broken, not the repo clean.
    """
    hits = _stale_org_hits()
    changelog_hits = {k: v for k, v in hits.items() if k.endswith("CHANGELOG.md")}
    assert changelog_hits, (
        f"the scanner found no {STALE_ORG_URL!r} anywhere, including in the "
        f"changelogs that are supposed to record the migration. It is not "
        f"reading the repo -- treat the companion test's pass as meaningless "
        f"until this one is green. Scanned files with any hit: {sorted(hits)}"
    )


def test_no_stale_org_url_outside_changelogs() -> None:
    """The repo moved to `Simmons-Systems/simsys-metrics`; only history may say otherwise.

    Two stale URLs were fixed by hand under #50326 (`node/src/index.ts:12`
    among them). Nothing prevented them coming back -- and a GitHub org
    redirect keeps a stale URL WORKING, so a regression here is invisible
    until the redirect is withdrawn.

    CHANGELOG.md files are exempt by design: an entry recording that the
    repository was transferred from the old org to Simmons-Systems would
    be false if rewritten.
    """
    offenders = {
        k: v for k, v in _stale_org_hits().items() if not k.endswith("CHANGELOG.md")
    }
    assert not offenders, (
        f"stale org URL {STALE_ORG_URL!r} outside the changelogs: {offenders}. "
        f"The canonical path is Simmons-Systems/simsys-metrics. Only "
        f"CHANGELOG.md may record the historical one."
    )
