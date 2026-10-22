"""``pre_push_gate.py``: the optional, advisory pre-push gate runner (RFC decision D7).

The red path is a gate that fails or cannot run, and a ``Gate proof:`` line the
recorded proof contradicts. The green path is a proof written for the exact tree, a
skip that carries a code, and a body with no line at all, which never fails.
"""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import pytest
from skill_script_helpers import load_skill_script

from kiro_crew.platform.update_governance import _GIT_LOCATION_VARS

ROOT = Path(__file__).resolve().parent.parent
PREPARE_PR = ROOT / "src" / "kiro_crew" / "builtin_skills" / "kirocrew-dev" / "kirocrew-prepare-pr"
SCRIPT = PREPARE_PR / "scripts" / "pre_push_gate.py"

gate = load_skill_script("prepare_pr_pre_push_gate_under_test", SCRIPT)

OK_GATE = {"id": "ok", "cmd": ["{py}", "-c", "pass"], "ci": {"file": "ci.yml", "text": "x"}}
BAD_GATE = {
    "id": "bad",
    "cmd": ["{py}", "-c", "print('boom-one'); raise SystemExit(3)"],
    "ci": {"file": "ci.yml", "text": "x"},
}
BAD_GATE_2 = {
    "id": "bad2",
    "cmd": ["{py}", "-c", "print('boom-two'); raise SystemExit(4)"],
    "ci": {"file": "ci.yml", "text": "x"},
}
NO_TOOL_GATE = {
    "id": "needs-tool",
    "cmd": ["{py}", "-c", "raise SystemExit(9)"],
    "module": "no_such_module_for_the_gate_test",
    "ci": {"file": "ci.yml", "text": "x"},
}
BASE_GATE = {
    "id": "needs-base",
    "cmd": ["{py}", "-c", "import os, sys; sys.exit(0 if os.environ['X_BASE'] else 1)"],
    "base_env": "X_BASE",
    "ci": {"file": "ci.yml", "text": "x"},
}
GONE_GATE = {
    "id": "gone",
    "cmd": ["/no/such/executable/for/the/gate/test"],
    "ci": {"file": "ci.yml", "text": "x"},
}
OTHER_REASON = "other:the CI job needs a service this machine does not have"


def _git_env() -> dict[str, str]:
    env = {k: v for k, v in os.environ.items() if k not in _GIT_LOCATION_VARS}
    env.update(
        {
            "GIT_TEMPLATE_DIR": "",
            "GIT_AUTHOR_NAME": "Test",
            "GIT_AUTHOR_EMAIL": "test@example.invalid",
            "GIT_COMMITTER_NAME": "Test",
            "GIT_COMMITTER_EMAIL": "test@example.invalid",
            "GIT_CONFIG_GLOBAL": os.devnull,
            "GIT_CONFIG_SYSTEM": os.devnull,
        }
    )
    return env


def _git(cwd: Path, *args: str) -> str:
    done = subprocess.run(
        ["git", *args],
        cwd=cwd,
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
        env=_git_env(),
    )
    return done.stdout.strip()


@pytest.fixture
def repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A branch one commit past a synthetic ``origin/main``, with the cwd set to it."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    (repo / "README.md").write_text("hi\n")
    _git(repo, "add", ".")
    _git(repo, "commit", "-q", "-m", "base")
    _git(repo, "update-ref", "refs/remotes/origin/main", "HEAD")
    (repo / "mine.py").write_text("x = 1\n")
    _git(repo, "add", ".")
    _git(repo, "commit", "-q", "-m", "change")
    monkeypatch.chdir(repo)
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", os.devnull)
    monkeypatch.setenv("GIT_CONFIG_SYSTEM", os.devnull)
    return repo


@pytest.fixture
def use_gates(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    def _use(*gates: dict) -> None:
        path = tmp_path / "gate_map.json"
        path.write_text(json.dumps({"gates": list(gates), "ci_only": []}), encoding="utf-8")
        monkeypatch.setattr(gate, "MAP_PATH", str(path))

    return _use


def _proof(repo: Path) -> Path:
    return repo / ".git" / gate.PROOF_FILENAME


# ------------------------------------------------------------------ green path


def test_every_gate_green_writes_a_proof_for_this_tree(repo, use_gates, capsys) -> None:
    use_gates(OK_GATE)
    assert gate.main(["pre_push_gate.py"]) == 0
    proof = json.loads(_proof(repo).read_text(encoding="utf-8"))
    assert proof["tree"] == _git(repo, "rev-parse", "HEAD^{tree}")
    assert proof["ran"] == ["ok"]
    assert proof["skipped"] == []
    out = capsys.readouterr().out
    assert "Gate proof: tree {}; ran 1; skipped none".format(proof["tree"][:12]) in out


def test_a_missing_tool_is_skipped_with_its_code_and_the_proof_says_so(
    repo, use_gates, capsys
) -> None:
    use_gates(OK_GATE, NO_TOOL_GATE)
    assert gate.main(["pre_push_gate.py"]) == 0
    proof = json.loads(_proof(repo).read_text(encoding="utf-8"))
    assert proof["skipped"] == [
        {"gate": "needs-tool", "code": "tool-missing:no_such_module_for_the_gate_test"}
    ]
    assert "skipped needs-tool=tool-missing:no_such_module_for_the_gate_test" in (
        capsys.readouterr().out
    )


def test_a_skip_with_a_code_covers_a_gate_that_cannot_run(repo, use_gates) -> None:
    use_gates(OK_GATE, GONE_GATE)
    assert gate.main(["pre_push_gate.py", "--skip", "gone=no-network"]) == 0
    proof = json.loads(_proof(repo).read_text(encoding="utf-8"))
    assert proof["skipped"] == [{"gate": "gone", "code": "no-network"}]


def test_the_base_sha_reaches_a_gate_that_asks_for_it(repo, use_gates) -> None:
    use_gates(BASE_GATE)
    assert gate.main(["pre_push_gate.py"]) == 0


def test_a_gate_whose_paths_match_no_changed_file_does_not_run(repo, use_gates) -> None:
    only_docs = {**BAD_GATE, "id": "docs-only", "paths": [r"\.md$"]}
    use_gates(OK_GATE, only_docs)
    assert gate.main(["pre_push_gate.py"]) == 0
    assert json.loads(_proof(repo).read_text(encoding="utf-8"))["ran"] == ["ok"]


def test_the_self_test_runs_before_the_gate_and_a_red_one_fails_it(repo, use_gates) -> None:
    red_self_test = {**OK_GATE, "id": "st", "self_test": ["{py}", "-c", "raise SystemExit(1)"]}
    use_gates(red_self_test)
    assert gate.main(["pre_push_gate.py"]) == gate.EXIT_FAILED


def test_the_runner_self_test_passes(capsys) -> None:
    assert gate.main(["pre_push_gate.py", "--test"]) == 0
    assert "self-test: ok" in capsys.readouterr().out


# -------------------------------------------------------------------- red path


def test_every_failure_is_printed_at_once_and_exit_is_fifty(repo, use_gates, capsys) -> None:
    use_gates(OK_GATE, BAD_GATE, BAD_GATE_2)
    assert gate.main(["pre_push_gate.py"]) == gate.EXIT_FAILED == 50
    out = capsys.readouterr().out
    assert "FAIL  bad" in out and "boom-one" in out
    assert "FAIL  bad2" in out and "boom-two" in out
    assert not _proof(repo).exists(), "a red run records no proof"


def test_a_gate_that_cannot_start_and_has_no_skip_exits_fifty_one(repo, use_gates, capsys) -> None:
    use_gates(OK_GATE, GONE_GATE)
    assert gate.main(["pre_push_gate.py"]) == gate.EXIT_UNRUNNABLE == 51
    assert "CANNOT  gone" in capsys.readouterr().out
    assert not _proof(repo).exists()


def test_a_base_gate_without_a_merge_base_exits_fifty_one(
    repo, use_gates, capsys, monkeypatch
) -> None:
    _git(repo, "update-ref", "-d", "refs/remotes/origin/main")
    use_gates(BASE_GATE)
    assert gate.main(["pre_push_gate.py"]) == 51
    assert "CANNOT RUN  needs-base" in capsys.readouterr().out


def test_a_failed_gate_wins_over_an_unrunnable_one(repo, use_gates) -> None:
    use_gates(BAD_GATE, GONE_GATE)
    assert gate.main(["pre_push_gate.py"]) == 50


@pytest.mark.parametrize(
    "value",
    [
        "ok=nope",
        "ok=other:too short",
        "ok=tool-missing:",
        "ok=red-on-base:xyz",
        "ok=",
        "missing=no-network",
        "no-equals-sign",
    ],
)
def test_a_skip_without_a_valid_code_or_gate_is_refused(repo, use_gates, value) -> None:
    use_gates(OK_GATE)
    assert gate.main(["pre_push_gate.py", "--skip", value]) == gate.EXIT_ENV
    assert not _proof(repo).exists()


@pytest.mark.parametrize(
    "code",
    [
        "tool-missing:uv",
        "no-network",
        "unsupported-os",
        "red-on-base:0123abc",
        OTHER_REASON,
    ],
)
def test_every_documented_skip_code_is_accepted(code) -> None:
    assert gate.SKIP_CODE.match(code)


# ------------------------------------------------- what a proof may be about


def test_a_dirty_working_tree_refuses_the_run_and_writes_no_proof(repo, use_gates, capsys) -> None:
    use_gates(BAD_GATE)
    (repo / "mine.py").write_text("x = 2\n")  # an edit the gates would see and a tree would not
    assert gate.main(["pre_push_gate.py"]) == gate.EXIT_UNRUNNABLE
    out = capsys.readouterr().out
    assert "REFUSED: uncommitted changes" in out and "mine.py" in out
    assert "FAIL" not in out, "no gate runs against code the proof could not name"
    assert not _proof(repo).exists()


def test_a_new_untracked_file_also_refuses_the_run(repo, use_gates) -> None:
    use_gates(OK_GATE)
    (repo / "new.py").write_text("y = 1\n")
    assert gate.main(["pre_push_gate.py"]) == 51
    assert not _proof(repo).exists()


def test_a_clean_tree_after_the_edit_is_committed_gets_its_proof(repo, use_gates) -> None:
    use_gates(OK_GATE)
    (repo / "mine.py").write_text("x = 2\n")
    assert gate.main(["pre_push_gate.py"]) == 51
    _git(repo, "commit", "-qam", "edit")
    assert gate.main(["pre_push_gate.py"]) == 0
    assert json.loads(_proof(repo).read_text(encoding="utf-8"))["tree"] == _git(
        repo, "rev-parse", "HEAD^{tree}"
    )


def test_a_gate_that_leaves_a_file_behind_gets_no_proof(repo, use_gates, capsys) -> None:
    litter = {
        "id": "litter",
        "cmd": ["{py}", "-c", "open('litter.txt', 'w').write('x')"],
        "ci": {"file": "ci.yml", "text": "x"},
    }
    use_gates(litter)
    assert gate.main(["pre_push_gate.py"]) == 51
    assert "changed while the gates ran" in capsys.readouterr().out
    assert not _proof(repo).exists()


def test_a_gate_that_moves_head_gets_no_proof(repo, use_gates, capsys) -> None:
    mover = {
        "id": "mover",
        "cmd": [
            "git",
            "-c",
            "user.name=T",
            "-c",
            "user.email=t@example.invalid",
            "commit",
            "-q",
            "--allow-empty",
            "-m",
            "moved",
        ],
        "ci": {"file": "ci.yml", "text": "x"},
    }
    use_gates(mover)
    assert gate.main(["pre_push_gate.py"]) == 51
    assert "changed while the gates ran" in capsys.readouterr().out
    assert not _proof(repo).exists()


# ------------------------------------------------- no merge base: every gate runs


def test_without_a_merge_base_a_path_gated_gate_still_runs(repo, use_gates, capsys) -> None:
    _git(repo, "update-ref", "-d", "refs/remotes/origin/main")
    docs_only_red = {**BAD_GATE, "id": "docs-only", "paths": [r"\.md$"]}
    use_gates(OK_GATE, docs_only_red)
    assert gate.main(["pre_push_gate.py"]) == gate.EXIT_FAILED
    out = capsys.readouterr().out
    assert "running every gate" in out and "FAIL  docs-only" in out


def test_without_a_merge_base_a_passing_proof_lists_the_path_gated_gate(repo, use_gates) -> None:
    _git(repo, "update-ref", "-d", "refs/remotes/origin/main")
    gated = {**OK_GATE, "id": "gated", "paths": [r"\.md$"]}
    use_gates(OK_GATE, gated)
    assert gate.main(["pre_push_gate.py"]) == 0
    assert json.loads(_proof(repo).read_text(encoding="utf-8"))["ran"] == ["ok", "gated"]


def test_select_with_unknown_paths_chooses_every_gate_and_with_none_only_the_always_ones() -> None:
    gates = [{"id": "a"}, {"id": "b", "paths": [r"\.md$"]}]
    assert [g["id"] for g in gate.select(gates, None)] == ["a", "b"]
    assert [g["id"] for g in gate.select(gates, [])] == ["a"]


# ------------------------------------------------- config and baseline inputs


def _selected(paths: list[str]) -> set[str]:
    return {g["id"] for g in gate.select(gate.load_map()["gates"], paths)}


@pytest.mark.parametrize("config", ["pyproject.toml", "setup.cfg"])
def test_a_config_only_change_still_runs_the_python_lint_gates(config) -> None:
    assert {"black", "isort", "flake8", "mypy"} <= _selected([config])


@pytest.mark.parametrize(
    ("baseline", "gate_id"),
    [
        (".github/black-baseline.txt", "black"),
        (".github/subprocess-encoding-baseline.txt", "subprocess-encoding"),
        (".github/agent-sdk-boundary-baseline.txt", "agent-sdk-boundary"),
        (".github/sync-io-in-async-baseline.txt", "sync-io-in-async"),
        (".github/sync-io-in-async-fs-baseline.txt", "sync-io-in-async"),
    ],
)
def test_a_baseline_only_change_runs_the_gate_that_reads_it(baseline, gate_id) -> None:
    assert gate_id in _selected([baseline])


def test_an_unrelated_change_still_skips_the_python_gates() -> None:
    assert not {"black", "flake8", "mypy"} & _selected(["docs/guide.md"])


# ----------------------------------------------------------------------- verify


def test_verify_finds_no_proof_then_a_proof_then_none_after_the_tree_changes(
    repo, use_gates
) -> None:
    use_gates(OK_GATE)
    assert gate.main(["pre_push_gate.py", "--verify"]) == gate.EXIT_NO_PROOF == 52
    assert gate.main(["pre_push_gate.py"]) == 0
    assert gate.main(["pre_push_gate.py", "--verify"]) == 0
    (repo / "mine.py").write_text("x = 2\n")
    _git(repo, "commit", "-qam", "again")
    assert gate.main(["pre_push_gate.py", "--verify"]) == 52, "the proof names the old tree"


# ------------------------------------------------------- the Gate proof: line


TREE = "a" * 40


def _write_proof(git_dir: Path, **over) -> dict:
    proof = {
        "version": 1,
        "tree": TREE,
        "ran": ["x", "y"],
        "skipped": [{"gate": "z", "code": "no-network"}],
        **over,
    }
    (git_dir / gate.PROOF_FILENAME).write_text(json.dumps(proof), encoding="utf-8")
    return proof


def test_a_line_that_matches_the_proof_is_no_problem(tmp_path) -> None:
    proof = _write_proof(tmp_path)
    body = "## Tests\n\n{}\n".format(gate.proof_line(proof))
    assert gate.contradiction(body, str(tmp_path), TREE) == ""


def test_a_body_with_no_line_never_contradicts(tmp_path) -> None:
    assert gate.contradiction("## Tests\n\nran by hand\n", str(tmp_path), TREE) == ""
    _write_proof(tmp_path)
    assert gate.contradiction("## Tests\n\nran by hand\n", str(tmp_path), TREE) == ""


@pytest.mark.parametrize(
    "line",
    [
        "Gate proof: tree aaaaaaaaaaaa; ran 5; skipped z=no-network",
        "Gate proof: tree aaaaaaaaaaaa; ran 2; skipped none",
        "Gate proof: tree bbbbbbbbbbbb; ran 2; skipped z=no-network",
        "Gate proof: all green",
    ],
)
def test_a_line_the_proof_disagrees_with_is_reported(tmp_path, line) -> None:
    _write_proof(tmp_path)
    assert gate.contradiction(line, str(tmp_path), TREE)


def test_a_line_with_no_recorded_proof_is_reported(tmp_path) -> None:
    problem = gate.contradiction(
        "Gate proof: tree aaaaaaaaaaaa; ran 1; skipped none", str(tmp_path), TREE
    )
    assert "no recorded proof" in problem


@pytest.mark.parametrize("over", [{"tree": "b" * 40}, {"version": 2}, {"ran": "x"}])
def test_a_proof_for_another_tree_or_shape_is_no_proof(tmp_path, over) -> None:
    _write_proof(tmp_path, **over)
    assert gate.read_proof(str(tmp_path), TREE) is None


def test_a_corrupt_proof_file_is_no_proof(tmp_path) -> None:
    (tmp_path / gate.PROOF_FILENAME).write_text("{not json", encoding="utf-8")
    assert gate.read_proof(str(tmp_path), TREE) is None


# ------------------------------------------------------------------ the map


def test_the_shipped_map_loads_and_every_gate_names_what_it_runs() -> None:
    data = gate.load_map()
    assert len(data["gates"]) >= 20
    for entry in data["gates"]:
        assert entry["cmd"], entry["id"]


def test_a_map_with_a_repeated_id_is_refused(tmp_path) -> None:
    path = tmp_path / "m.json"
    path.write_text(json.dumps({"gates": [OK_GATE, OK_GATE], "ci_only": []}), encoding="utf-8")
    with pytest.raises(ValueError, match="listed twice"):
        gate.load_map(str(path))


def test_an_os_gate_off_its_platform_is_skipped_with_its_code(monkeypatch) -> None:
    monkeypatch.setattr(gate.sys, "platform", "win32")
    assert gate.unavailable({"os": ["linux", "darwin"]}) == "unsupported-os"
    assert gate.unavailable({}) == ""


def test_a_missing_executable_is_a_tool_missing_skip() -> None:
    assert gate.unavailable({"tool": "no-such-tool-for-the-gate-test"}) == (
        "tool-missing:no-such-tool-for-the-gate-test"
    )
