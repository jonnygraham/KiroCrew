"""``prove.py`` prints the ``Proof:`` line a fix pastes under ``## Tests`` and records it.

``diff_signals.py --check-body`` compares a ``PROVEN`` line with the record for the
same tree, so a stale or invented line fails in the agent flow. These tests pin the
line, the record (tree-keyed), exit 11 for frontend tests, and the closed list of
"not provable" reasons the CI check accepts.
"""

from __future__ import annotations

import json
import re
import subprocess
import sys
from pathlib import Path

from skill_script_helpers import load_skill_script

ROOT = Path(__file__).resolve().parent.parent
PREPARE_PR = ROOT / "src" / "kiro_crew" / "builtin_skills" / "kirocrew-dev" / "kirocrew-prepare-pr"
PROVE = PREPARE_PR / "scripts" / "prove.py"
CHECK = ROOT / ".github" / "scripts" / "proof_line_check.py"

prove = load_skill_script("prove_line_under_test", PROVE)
check = load_skill_script("proof_line_check_for_prove_test", CHECK)

LINE_RE = re.compile(r"^Proof: PROVEN (?P<tests>.+) \(head (?P<sha>[0-9a-f]{12})\)$", re.MULTILINE)


def _git(repo: Path, *args: str) -> str:
    out = subprocess.run(
        ["git", *args],
        cwd=str(repo),
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    return out.stdout.strip()


def _repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    _git(repo, "config", "user.email", "prove@test")
    _git(repo, "config", "user.name", "prove")
    (repo / "mod.py").write_text("def last_char(s):\n    return s[len(s) - 2]\n")
    (repo / "test_mod.py").write_text(
        "from mod import last_char\n\n\ndef test_smoke():\n    assert last_char('abc') == 'b'\n"
    )
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "base")
    _git(repo, "checkout", "-q", "-b", "feature")
    return repo


def _run(repo: Path) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, "-B", str(PROVE), "--base", "main"],
        cwd=str(repo),
        capture_output=True,
        text=True,
        encoding="utf-8",
    )


def _record(repo: Path) -> dict:
    gitdir = _git(repo, "rev-parse", "--absolute-git-dir")
    return json.loads((Path(gitdir) / prove.RECORD_FILENAME).read_text(encoding="utf-8"))


def _fix_with_control_test(repo: Path) -> None:
    """A fix with one test that fails without it and one control that passes both ways."""
    (repo / "mod.py").write_text("def last_char(s):\n    return s[len(s) - 1]\n")
    (repo / "test_mod.py").write_text(
        "from mod import last_char\n\n\n"
        "def test_last():\n    assert last_char('abc') == 'c'\n\n\n"
        "def test_returns_a_str():\n    assert isinstance(last_char('abc'), str)\n"
    )
    _git(repo, "commit", "-aqm", "fix off-by-one")


# --------------------------------------------------------------- green path


def test_proven_prints_the_line_naming_only_the_tests_that_fail(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    _fix_with_control_test(repo)
    r = _run(repo)
    assert r.returncode == 0, r.stdout + r.stderr
    m = LINE_RE.search(r.stdout)
    assert m, r.stdout
    assert m.group("tests") == "test_mod.py::test_last"  # the control test is not named
    assert m.group("sha") == _git(repo, "rev-parse", "HEAD")[:12]
    assert r.stdout.rstrip().splitlines()[-1] == m.group(0)


def test_the_record_is_keyed_by_tree_and_names_the_line(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    _fix_with_control_test(repo)
    r = _run(repo)
    record = _record(repo)
    assert record["verdict"] == "PROVEN" and record["exit"] == 0
    assert record["tree"] == _git(repo, "rev-parse", "HEAD^{tree}")
    assert record["head"] == _git(repo, "rev-parse", "HEAD")
    assert record["tests"] == ["test_mod.py::test_last"]
    assert record["line"] in r.stdout


def test_a_record_written_by_prove_satisfies_the_check(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    _fix_with_control_test(repo)
    line = LINE_RE.search(_run(repo).stdout).group(0)
    gitdir = _git(repo, "rev-parse", "--absolute-git-dir")
    record_path = str(Path(gitdir) / prove.RECORD_FILENAME)
    tree = _git(repo, "rev-parse", "HEAD^{tree}")
    body = f"## Tests\n\n{line}\n"
    problems, proven = check.check("fix: x", body)
    assert problems == [] and proven is not None
    assert check.compare_record(proven, record_path, tree) == []


def test_a_message_only_amend_keeps_the_record_valid(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    _fix_with_control_test(repo)
    line = LINE_RE.search(_run(repo).stdout).group(0)
    _git(repo, "commit", "-q", "--amend", "-m", "fix: a better message")
    gitdir = _git(repo, "rev-parse", "--absolute-git-dir")
    _, proven = check.check("fix: x", f"## Tests\n\n{line}\n")
    assert (
        check.compare_record(
            proven,
            str(Path(gitdir) / prove.RECORD_FILENAME),
            _git(repo, "rev-parse", "HEAD^{tree}"),
        )
        == []
    )


# ----------------------------------------------------------------- red path


def test_a_stale_record_fails_the_comparison_after_the_code_changes(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    _fix_with_control_test(repo)
    line = LINE_RE.search(_run(repo).stdout).group(0)
    (repo / "mod.py").write_text("def last_char(s):\n    return s[-1]\n")
    _git(repo, "commit", "-aqm", "rewrite")
    gitdir = _git(repo, "rev-parse", "--absolute-git-dir")
    _, proven = check.check("fix: x", f"## Tests\n\n{line}\n")
    problems = check.compare_record(
        proven, str(Path(gitdir) / prove.RECORD_FILENAME), _git(repo, "rev-parse", "HEAD^{tree}")
    )
    assert problems and "different tree" in problems[0]


def test_an_invented_line_fails_the_comparison(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    _fix_with_control_test(repo)
    _run(repo)
    gitdir = _git(repo, "rev-parse", "--absolute-git-dir")
    path = str(Path(gitdir) / prove.RECORD_FILENAME)
    tree = _git(repo, "rev-parse", "HEAD^{tree}")
    head = _git(repo, "rev-parse", "HEAD")[:12]
    for line, fragment in [
        (f"Proof: PROVEN test_mod.py::test_invented (head {head})", "does not list"),
        (f"Proof: PROVEN test_mod.py::test_last (head {'0' * 12})", "recorded"),
        (
            f"Proof: PROVEN test_mod.py::test_last, test_mod.py::test_ghost (head {head})",
            "does not list",
        ),
    ]:
        _, proven = check.check("fix: x", f"## Tests\n\n{line}\n")
        problems = check.compare_record(proven, path, tree)
        assert any(fragment in p for p in problems), (line, problems)


def test_a_missing_record_fails_the_comparison(tmp_path: Path) -> None:
    _, proven = check.check(
        "fix: x", "## Tests\n\nProof: PROVEN test_a.py::t (head abcdef012345)\n"
    )
    problems = check.compare_record(proven, str(tmp_path / "nope.json"), "tree")
    assert problems and "no prove.py record" in problems[0]


def test_a_not_proven_run_prints_no_line_and_records_the_verdict(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    (repo / "mod.py").write_text("def last_char(s):\n    return s[len(s) - 1]\n")
    (repo / "test_mod.py").write_text(
        "from mod import last_char\n\n\ndef test_a_str():\n    assert isinstance(last_char('abc'), str)\n"
    )
    _git(repo, "commit", "-aqm", "fix with a weak test")
    r = _run(repo)
    assert r.returncode == 20
    assert "Proof:" not in r.stdout
    record = _record(repo)
    assert record["verdict"] == "NOT_PROVEN" and record["line"] is None
    # a PROVEN line pasted anyway is refused by the comparison
    gitdir = _git(repo, "rev-parse", "--absolute-git-dir")
    _, proven = check.check(
        "fix: x", "## Tests\n\nProof: PROVEN test_mod.py::t (head abcdef012345)\n"
    )
    problems = check.compare_record(
        proven, str(Path(gitdir) / prove.RECORD_FILENAME), _git(repo, "rev-parse", "HEAD^{tree}")
    )
    assert problems and "NOT_PROVEN" in problems[0]


# ------------------------------------------------- not provable (closed list)


def test_frontend_tests_exit_eleven_and_print_the_frontend_reason(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    web = repo / "website" / "src"
    web.mkdir(parents=True)
    (web / "thing.ts").write_text("export const n = 1;\n")
    (web / "thing.test.ts").write_text("import { n } from './thing';\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "fix(ui): thing")
    r = _run(repo)
    assert r.returncode == 11, r.stdout + r.stderr
    assert r.stdout.rstrip().splitlines()[-1] == (
        "Proof: not provable -- frontend test (prove.py runs Python only)"
    )
    assert _record(repo)["verdict"] == "FRONTEND_TESTS"


def test_test_only_and_docs_only_changes_print_their_reason(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    (repo / "test_mod.py").write_text(
        "from mod import last_char\n\n\ndef test_smoke():\n    assert last_char('abc') == 'b'\n\n\n"
        "def test_more():\n    assert last_char('xy') == 'x'\n"
    )
    _git(repo, "commit", "-aqm", "test only")
    r = _run(repo)
    assert r.returncode == 10
    assert r.stdout.rstrip().splitlines()[-1] == "Proof: not provable -- test-only change"

    (tmp_path / "docs").mkdir()
    docs = _repo(tmp_path / "docs")
    (docs / "NOTES.md").write_text("hello\n")
    _git(docs, "add", "-A")
    _git(docs, "commit", "-qm", "docs")
    r = _run(docs)
    assert r.returncode == 10
    assert r.stdout.rstrip().splitlines()[-1] == "Proof: not provable -- docs or config only"


def test_code_with_no_test_prints_no_line(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    (repo / "mod.py").write_text("def last_char(s):\n    return s[len(s) - 1]\n")
    _git(repo, "commit", "-aqm", "fix with no test")
    r = _run(repo)
    assert r.returncode == 10
    assert "Proof:" not in r.stdout


def test_every_reason_prove_prints_is_in_the_checks_closed_list() -> None:
    for reason in (
        prove.REASON_FRONTEND,
        prove.REASON_TEST_ONLY,
        prove.REASON_DOCS_OR_CONFIG,
        prove.REASON_TIMED_OUT,
    ):
        assert check.reason_ok(reason), reason
        problems, _ = check.check("fix: x", f"## Tests\n\nProof: not provable -- {reason}\n")
        assert problems == []


def test_the_frontend_test_shape_is_narrow() -> None:
    assert prove.is_frontend_test_path("website/src/a/b.test.tsx")
    assert prove.is_frontend_test_path("website/src/a/__tests__/b.ts")
    assert not prove.is_frontend_test_path("website/src/a/b.tsx")
    assert not prove.is_frontend_test_path("src/kiro_crew/x.test.ts")


def test_the_line_names_at_most_five_tests_and_the_record_keeps_all() -> None:
    ids = [f"test_a.py::t{i}" for i in range(7)]
    line = prove.proof_line({"verdict": "PROVEN", "named": ids}, "a" * 40)
    assert line == "Proof: PROVEN " + ", ".join(ids[:5]) + " (head " + "a" * 12 + ")"
    assert prove.proof_line({"verdict": "PROVEN", "named": []}, "a" * 40) is None
    assert prove.proof_line({"verdict": "NOT_PROVEN"}, "a" * 40) is None


# ------------------------------------------- the proof is of the commit it started on


def test_a_head_that_moves_during_the_run_does_not_change_what_was_proven(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    """The author commits while pytest runs: the line and record keep the pinned ids."""
    repo = _repo(tmp_path)
    _fix_with_control_test(repo)
    pinned_commit = _git(repo, "rev-parse", "HEAD")
    pinned_tree = _git(repo, "rev-parse", "HEAD^{tree}")

    real_run_pytest = prove.run_pytest
    moved: list[str] = []

    def commit_then_run(worktree: Path, test_files: list[str], report: Path):
        if not moved:  # during the first (baseline) run
            (repo / "later.py").write_text("x = 1\n")
            _git(repo, "add", "later.py")
            _git(repo, "commit", "-qm", "work done while the proof ran")
            moved.append(_git(repo, "rev-parse", "HEAD"))
        return real_run_pytest(worktree, test_files, report)

    monkeypatch.setattr(prove, "run_pytest", commit_then_run)
    monkeypatch.setattr(sys, "argv", ["prove.py", "--base", "main"])
    monkeypatch.chdir(repo)
    assert prove.main() == 0
    out = capsys.readouterr().out

    assert moved and moved[0] != pinned_commit
    assert _git(repo, "rev-parse", "HEAD") == moved[0]  # HEAD did move
    record = _record(repo)
    assert record["head"] == pinned_commit
    assert record["tree"] == pinned_tree
    assert record["tree"] != _git(repo, "rev-parse", "HEAD^{tree}")
    assert f"(head {pinned_commit[:12]})" in out
    assert moved[0][:12] not in out
    # the later tree is correctly stale against this record
    _, proven = check.check("fix: x", f"## Tests\n\n{record['line']}\n")
    gitdir = _git(repo, "rev-parse", "--absolute-git-dir")
    path = str(Path(gitdir) / prove.RECORD_FILENAME)
    assert check.compare_record(proven, path, pinned_tree) == []
    assert check.compare_record(proven, path, _git(repo, "rev-parse", "HEAD^{tree}"))


def test_the_proof_runs_on_the_pinned_commit_not_on_head(tmp_path: Path) -> None:
    """A later commit that breaks the fix must not turn a proven fix into a failure."""
    repo = _repo(tmp_path)
    _fix_with_control_test(repo)
    commit, _ = prove.pin_commit(repo)
    (repo / "mod.py").write_text("def last_char(s):\n    return 'broken'\n")
    _git(repo, "commit", "-aqm", "later work")
    result = prove.prove(repo, "main", False, commit)
    assert result["verdict"] == "PROVEN"
    assert result["named"] == ["test_mod.py::test_last"]


# ------------------------------------------- ids that look like HTML comments


def test_a_test_id_with_a_comment_marker_survives_the_body(tmp_path: Path) -> None:
    ids = ["test_x[<!--kirocrew:docked height=180-->-180]", "test_y"]
    line = prove.proof_line({"verdict": "PROVEN", "named": ids}, "a" * 40)
    assert "<!--" not in line and "<&#33;--" in line
    problems, proven = check.check("fix: x", f"## Tests\n\n{line}\n")
    assert problems == [] and proven is not None
    record = tmp_path / "rec.json"
    record.write_text(
        json.dumps({"tree": "t", "head": "a" * 40, "verdict": "PROVEN", "tests": ids}),
        encoding="utf-8",
    )
    assert check.compare_record(proven, str(record), "t") == []
    # a name the record does not hold is still refused
    bad = line.replace("test_y", "test_z")
    _, proven_bad = check.check("fix: x", f"## Tests\n\n{bad}\n")
    assert check.compare_record(proven_bad, str(record), "t")
