"""``gate_map.json`` is pinned to CI: a gate mirrors a CI step, a CI check is not forgotten.

``pre_push_gate.py`` runs what the map says. A map that drifts from the workflows would
write a proof for checks CI has dropped, or leave out one CI has added. Three
pins keep it honest: every gate names CI text that is still in the workflow, every
script a gate runs exists, and every ``check_*.py`` the workflows run is either a gate
or a ``ci_only`` entry with a reason.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
SCRIPTS = (
    ROOT
    / "src"
    / "kiro_crew"
    / "builtin_skills"
    / "kirocrew-dev"
    / "kirocrew-prepare-pr"
    / "scripts"
)
WORKFLOWS = ROOT / ".github" / "workflows"
GATE_MAP = json.loads((SCRIPTS / "gate_map.json").read_text(encoding="utf-8"))

# The CI steps this map has to account for: the repository's own checks.
_CI_CHECK = re.compile(
    r"scripts/(?:check_[a-z_]+\.py|docs_lint\.py|docs-lint\.sh|verify_vendor_manifest\.py)"
)


def _workflow_text() -> dict[str, str]:
    return {p.name: p.read_text(encoding="utf-8") for p in sorted(WORKFLOWS.glob("*.yml"))}


def _gate_ids() -> list[str]:
    return [g["id"] for g in GATE_MAP["gates"]]


def test_gate_ids_are_unique_and_never_also_ci_only() -> None:
    ids = _gate_ids() + [e["id"] for e in GATE_MAP["ci_only"]]
    assert len(ids) == len(set(ids))


@pytest.mark.parametrize("gate", GATE_MAP["gates"], ids=_gate_ids())
def test_every_gate_names_text_that_is_still_in_its_ci_workflow(gate) -> None:
    workflow = WORKFLOWS / gate["ci"]["file"]
    assert workflow.is_file(), "{} names no workflow".format(gate["id"])
    assert gate["ci"]["text"] in workflow.read_text(
        encoding="utf-8"
    ), "{}: CI no longer contains {!r}; update the gate or move it to ci_only".format(
        gate["id"], gate["ci"]["text"]
    )


@pytest.mark.parametrize("gate", GATE_MAP["gates"], ids=_gate_ids())
def test_every_gate_runs_a_file_that_exists_and_the_text_it_mirrors(gate) -> None:
    for token in gate["cmd"]:
        if token.startswith("scripts/"):
            assert (ROOT / token).is_file(), "{}: {} is not in the repository".format(
                gate["id"], token
            )
    if gate["ci"]["text"].startswith(("scripts/", "./scripts/")):
        assert gate["ci"]["text"].removeprefix("./") in gate["cmd"]


@pytest.mark.parametrize(
    "gate", [g for g in GATE_MAP["gates"] if g.get("base_env")], ids=lambda g: g["id"]
)
def test_a_gate_that_takes_a_base_reads_the_same_variable_as_ci(gate) -> None:
    assert gate["base_env"] in (WORKFLOWS / gate["ci"]["file"]).read_text(encoding="utf-8")


def test_every_check_ci_runs_is_a_gate_or_ci_only_with_a_reason() -> None:
    covered = " ".join(" ".join(g["cmd"]) for g in GATE_MAP["gates"])
    covered += " " + " ".join(e["needle"] for e in GATE_MAP["ci_only"])
    for gate in GATE_MAP["gates"]:
        if isinstance(gate.get("self_test"), list):
            covered += " " + " ".join(gate["self_test"])
    missing = sorted(
        {
            name
            for text in _workflow_text().values()
            for name in _CI_CHECK.findall(text)
            if name.removeprefix("./") not in covered
        }
    )
    assert not missing, (
        "CI runs {} and gate_map.json neither runs nor lists it: add a gate, or a "
        "ci_only entry with a reason".format(missing)
    )


@pytest.mark.parametrize("entry", GATE_MAP["ci_only"], ids=lambda e: e["id"])
def test_every_ci_only_entry_has_a_reason_and_is_still_in_ci(entry) -> None:
    assert len(entry["reason"]) >= 20
    assert any(
        entry["needle"] in text for text in _workflow_text().values()
    ), "{}: no workflow contains {!r}; delete the entry".format(entry["id"], entry["needle"])


@pytest.mark.parametrize(
    "gate", [g for g in GATE_MAP["gates"] if g.get("paths")], ids=lambda g: g["id"]
)
def test_a_path_gated_gate_is_selected_by_its_config_and_baseline_inputs(gate) -> None:
    """A change to a file the gate reads must run the gate, not only a change to a .py file."""
    patterns = [re.compile(p) for p in gate["paths"]]
    inputs = ["pyproject.toml", "setup.cfg"]
    for token in gate["cmd"]:
        script = ROOT / token
        if token.startswith("scripts/") and script.suffix == ".py":
            inputs += [
                ".github/" + name
                for name in re.findall(
                    r'"(?:\.github/)?([\w-]+-baseline\.txt)"', script.read_text(encoding="utf-8")
                )
            ]
    unselected = [i for i in inputs if not any(p.search(i) for p in patterns)]
    assert not unselected, "{}: paths miss its inputs {}".format(gate["id"], unselected)
