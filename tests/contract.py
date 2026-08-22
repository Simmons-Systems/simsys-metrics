"""Loader for spec/metrics-contract.json. Stdlib only.

Deliberately not a package import: the Go and Node lanes read the same file
with their own loaders, and the contract is the shared artifact rather than
any lane's code. Keeping this a plain module means the Python conformance
tests read the file the same way the other two do.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

CONTRACT_PATH = Path(__file__).resolve().parents[1] / "spec" / "metrics-contract.json"


def load_contract() -> dict[str, Any]:
    assert CONTRACT_PATH.is_file(), (
        f"{CONTRACT_PATH} is missing. Every conformance test in all three lanes "
        f"reads it; without it they would silently have nothing to assert."
    )
    return json.loads(CONTRACT_PATH.read_text(encoding="utf-8"))


def metrics_for(runtime: str) -> dict[str, dict[str, Any]]:
    """Every metric the contract says `runtime` must emit."""
    c = load_contract()
    return {n: m for n, m in c["metrics"].items() if runtime in m["runtimes"]}


def labels_for(metric: dict[str, Any], runtime: str) -> list[str]:
    """Declared label list, honouring a tracked per-runtime divergence.

    A metric marked `status: divergent` carries `current.<runtime>.labels`
    describing what that lane ACTUALLY emits today. Conformance asserts
    against reality so it stays green and keeps meaning something; the
    divergence is tracked separately and gated on a ticket.
    """
    if metric.get("status") == "divergent":
        cur = metric.get("current", {}).get(runtime)
        assert cur is not None, (
            f"divergent metric has no `current` entry for runtime {runtime!r}; "
            f"a divergence must describe every runtime it claims to be in"
        )
        return list(cur["labels"])
    return list(metric["labels"])
