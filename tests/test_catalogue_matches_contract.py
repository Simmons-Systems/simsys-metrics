"""The README catalogue is DERIVED from spec/metrics-contract.json.

This is the check that would have caught the original defect: `simsys_pool_*`
shipped in all three lanes and appeared in none of the three READMEs, and
`simsys_scrape_*` / `simsys_runtime_*` / `simsys_process_threads` were missing
too. `CONTRIBUTING.md` step 2 already *required* updating the catalogue, so the
process existed and was simply skipped -- which is the argument for a check
rather than a firmer reminder.

Runs in the pytest lane because that is what gates a merge.
"""

from __future__ import annotations

import re
from pathlib import Path

from .contract import load_contract

ROOT = Path(__file__).resolve().parents[1]
README = ROOT / "README.md"

BEGIN = "<!-- BEGIN GENERATED CATALOGUE"
END = "<!-- END GENERATED CATALOGUE -->"

RUNTIME_ABBR = {"python": "Py", "node": "Node", "go": "Go"}


def _catalogue_block() -> str:
    text = README.read_text(encoding="utf-8")
    assert text.count(BEGIN) == 1, (
        f"README.md has {text.count(BEGIN)} generated-catalogue markers; this "
        f"guard cannot say which block it validated"
    )
    assert END in text, "README.md catalogue block is not closed"
    return text[text.index(BEGIN) : text.index(END)]


def _rows() -> dict[str, list[str]]:
    """{metric name: [type, labels, runtimes, tier, source]} from the table."""
    rows: dict[str, list[str]] = {}
    for line in _catalogue_block().splitlines():
        m = re.match(r"^\|\s*`(simsys_[a-z_]+)`\s*\|(.*)\|\s*$", line)
        if m:
            rows[m.group(1)] = [c.strip() for c in m.group(2).split("|")]
    assert rows, (
        "parsed ZERO metric rows out of the README catalogue block. The table "
        "format changed and this guard is now watching nothing -- fix the "
        "pattern, do not delete the assertion."
    )
    return rows


def test_catalogue_lists_exactly_the_contract_metrics() -> None:
    contract = set(load_contract()["metrics"])
    documented = set(_rows())
    missing = sorted(contract - documented)
    extra = sorted(documented - contract)
    assert not missing, (
        f"these metrics are in the contract but NOT in the README catalogue: "
        f"{missing}. This is the exact drift that let simsys_pool_* ship "
        f"undocumented in all three lanes."
    )
    assert not extra, (
        f"the README catalogue documents metrics the contract does not declare: {extra}"
    )


def test_catalogue_types_match_the_contract() -> None:
    contract = load_contract()["metrics"]
    for name, cells in _rows().items():
        declared = contract[name]["type"]
        # build_info is rendered "Gauge = 1" because its value is always 1.
        got = cells[0].replace(" = 1", "").strip().lower()
        assert got == declared, (
            f"{name}: README says {cells[0]!r}, contract says {declared!r}"
        )


def test_catalogue_runtimes_match_the_contract() -> None:
    """The Runtimes column is what tells a dashboard author what they may rely on.

    A wrong entry here is worse than a missing row: it is an affirmative claim
    that a metric exists somewhere it does not.
    """
    contract = load_contract()["metrics"]
    for name, cells in _rows().items():
        expected = {RUNTIME_ABBR[r] for r in contract[name]["runtimes"]}
        got = set(cells[2].split())
        assert got == expected, (
            f"{name}: README claims runtimes {sorted(got)}, contract declares "
            f"{sorted(expected)}"
        )


def test_catalogue_tiers_match_the_contract() -> None:
    contract = load_contract()["metrics"]
    for name, cells in _rows().items():
        expected = contract[name]["tier"]
        got = cells[3].replace("*", "").strip()
        got = "extension" if got == "ext" else got
        assert got == expected, (
            f"{name}: README tier {got!r}, contract tier {expected!r}"
        )


def test_divergent_metrics_are_flagged_in_the_catalogue() -> None:
    """A reader of the table must be able to see a divergence without opening the contract."""
    contract = load_contract()["metrics"]
    rows = _rows()
    for name, m in contract.items():
        if m.get("status") != "divergent":
            continue
        row = " ".join(rows[name])
        assert m["ticket"] in row, (
            f"{name} is a tracked divergence but its catalogue row does not "
            f"reference ticket #{m['ticket']}. A dashboard author reading the "
            f"table would not know the label schema changes by runtime."
        )


def test_parser_finds_what_it_claims() -> None:
    """Positive control.

    Every check above compares parsed-value against contract-value. A pattern
    matching nothing would make them all vacuously true, so assert the parser
    found a plausible number of rows and that a known metric is among them.
    """
    rows = _rows()
    assert len(rows) >= 20, (
        f"parsed only {len(rows)} catalogue rows; expected the full catalogue"
    )
    assert "simsys_http_requests_total" in rows, (
        "parser missed a metric that is certainly present"
    )
    assert len(rows["simsys_http_requests_total"]) >= 5, (
        "row split produced too few columns"
    )
