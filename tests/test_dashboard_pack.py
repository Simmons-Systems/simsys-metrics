"""The shipped dashboard and alert pack must stay consistent with the contract.

Two failure modes this exists for:

1. A metric gets renamed and the dashboard keeps querying the old name. Grafana
   renders an empty panel; nothing errors. Same silent-empty class as an alert
   threshold above the highest bucket bound, which shipped once here.

2. Internal fleet topology leaks into a public repo. These files were extracted
   from a production rule set that named real services, carried measured fleet
   statistics, and referenced an internal issue tracker and an Ansible
   inventory path. A denylist is cruder than review but it does not get tired.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from .contract import load_contract

ROOT = Path(__file__).resolve().parents[1]
DASHBOARD = ROOT / "dashboards" / "simsys-services.json"
RULES = ROOT / "dashboards" / "simsys-services.rules.yml"

# Service names, measured statistics, tracker IDs and infra paths that were in
# the source files. Any of these reappearing means an un-sanitized re-extract.
INTERNAL_TOKENS = [
    "simsys-in-house",
    "widgets",
    "whisper",
    "voicestudio",
    "stargazer",
    "diagramstudio",
    "waxseal",
    "wazuh-exporter",
    "51,038",
    "88 crossings",
    "ansible/inventory",
    "redmine-bridge",
    "host_vars",
]


def test_both_artifacts_exist_and_parse() -> None:
    assert DASHBOARD.is_file(), "dashboards/simsys-services.json is missing"
    assert RULES.is_file(), "dashboards/simsys-services.rules.yml is missing"
    json.loads(DASHBOARD.read_text(encoding="utf-8"))  # raises on malformed


def _referenced_metrics() -> set[str]:
    """Every simsys_ metric name either artifact queries."""
    text = DASHBOARD.read_text(encoding="utf-8") + RULES.read_text(encoding="utf-8")
    # [A-Za-z0-9_] not [a-z_]: a lenient class silently matches the VALID
    # PREFIX of a renamed metric and stops, so `simsys_build_infoX` reads as
    # `simsys_build_info` and the rename is invisible. Caught by mutation --
    # the first version of this guard passed against exactly that rename.
    names = set(re.findall(r"\bsimsys_[A-Za-z0-9_]+", text))
    assert names, (
        "found ZERO simsys_ metric references across the dashboard and rules. "
        "Either the files are empty or this parser is broken -- it cannot be "
        "that a metrics dashboard queries no metrics."
    )
    return names


def test_every_referenced_metric_exists_in_the_contract() -> None:
    """A panel querying a metric that does not exist renders empty, not red."""
    declared = set(load_contract()["metrics"])
    # Prometheus suffixes on histogram families are query syntax, not names.
    suffixes = ("_bucket", "_count", "_sum")

    unknown = []
    for name in sorted(_referenced_metrics()):
        base = name
        for s in suffixes:
            if base.endswith(s):
                base = base[: -len(s)]
                break
        if base not in declared:
            unknown.append(name)

    assert not unknown, (
        f"the dashboard/rules reference metrics the contract does not declare: "
        f"{unknown}. Either they were renamed and these files were not updated "
        f"(the panel will silently render empty), or the contract is missing them."
    )


@pytest.mark.parametrize("token", INTERNAL_TOKENS)
def test_no_internal_token_leaked(token: str) -> None:
    for path in (DASHBOARD, RULES):
        text = path.read_text(encoding="utf-8")
        assert token not in text, (
            f"{path.name} contains {token!r}. These artifacts ship in a public "
            f"repo; internal service names, measured fleet statistics, tracker "
            f"IDs and infra paths must be stripped on extraction."
        )


def test_dashboard_is_templated_not_hardcoded() -> None:
    d = json.loads(DASHBOARD.read_text(encoding="utf-8"))
    names = {v["name"] for v in d["templating"]["list"]}
    for required in ("service", "job", "datasource"):
        assert required in names, (
            f"dashboard has no ${required} template variable; an adopter would "
            f"have to hand-edit panels, which is how a shipped dashboard stops "
            f"being adopted"
        )


def test_memory_panels_filter_on_rss() -> None:
    """The one cross-runtime trap in the whole catalogue.

    `rss` is the only memory type every runtime emits. Python and Go also emit
    `vms`; Node emits `heapUsed`, `heapTotal` and `external`. An unfiltered
    query double-counts on Node, where heapUsed is a subset of heapTotal is a
    subset of rss.
    """
    for path in (DASHBOARD, RULES):
        text = path.read_text(encoding="utf-8")
        for match in re.finditer(r"simsys_process_memory_bytes(\{[^}]*\})?", text):
            selector = match.group(1) or ""
            assert "rss" in selector, (
                f"{path.name}: simsys_process_memory_bytes is queried without a "
                f'type="rss" filter ({match.group(0)!r}). This silently '
                f"double-counts on Node."
            )


def test_rules_substitution_placeholder_is_documented() -> None:
    """{{JOB}} must be explained, or an adopter ships it literally."""
    text = RULES.read_text(encoding="utf-8")
    if "{{JOB}}" in text:
        assert "SUBSTITUTE BEFORE USE" in text, (
            "the rules file contains a {{JOB}} placeholder but no substitution "
            "instructions; an adopter would apply it verbatim and every rule "
            "would match nothing"
        )
