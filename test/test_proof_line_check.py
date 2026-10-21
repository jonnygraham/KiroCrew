"""``.github/scripts/proof_line_check.py``: a fix states, inside ``## Tests``, whether its tests fail without it.

Pinned here: the line shapes and the closed list of reasons (green and red), that
the line must sit inside ``## Tests`` and nowhere else, the mode (warn now, red only
for pull requests opened on or after ``ENFORCE_FROM``), the CLI, and the wiring
(template text inside the existing ``## Tests`` comment, no new heading, a workflow
step that reads the script from the base ref). CI checks presence, place and shape;
a hand-typed ``PROVEN`` line passes it, and only the local comparison with
``prove.py``'s record (test_prepare_pr_prove_line.py) catches that.
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
from pathlib import Path

import pytest
import yaml
from skill_script_helpers import load_skill_script

ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / ".github" / "scripts" / "proof_line_check.py"
WORKFLOW = ROOT / ".github" / "workflows" / "code-review.yml"
TEMPLATE = ROOT / ".github" / "PULL_REQUEST_TEMPLATE.md"
DESC_CHECK = ROOT / ".github" / "scripts" / "pr-description-check.sh"

mod = load_skill_script("proof_line_check_under_test", SCRIPT)

PROVEN = "Proof: PROVEN test/test_a.py::test_x (head 0123456789ab)"
NOT = "Proof: not provable -- test-only change"


def _body(tests: str, extra: str = "") -> str:
    return f"## Problem / Motivation\n\n**Goal:** x\n\n## Tests\n\n{tests}\n\n## Manual verification\n\nN/A\n{extra}"


def _problems(tests: str, title: str = "fix: stop the leak", extra: str = "") -> list[str]:
    return mod.check(title, _body(tests, extra))[0]


# ------------------------------------------------------------ green path


@pytest.mark.parametrize(
    "tests",
    [
        PROVEN,
        "test/test_a.py locks the boundary.\n\n" + PROVEN,
        "- " + PROVEN,
        "Proof: PROVEN test/test_a.py::test_x, test/test_a.py::test_y[a, b] (head 0123456789ab)",
        "Proof: not provable -- frontend test (prove.py runs Python only)",
        "Proof: not provable -- test-only change",
        "Proof: not provable -- copy-only change",
        "Proof: not provable -- docs or config only",
        "Proof: not provable -- prove.py timed out",
        "Proof: not provable -- other: the fix is a one-line data migration, tested by hand",
        "Proof: not provable -- TEST-ONLY CHANGE",
        "### Cases\n\n" + PROVEN,  # a sub-heading stays inside the section
    ],
)
def test_well_formed_lines_pass(tests: str) -> None:
    assert _problems(tests) == []


@pytest.mark.parametrize(
    "title", ["feat: x", "docs: x", "chore(deps): bump", "revert: x", "refactor: fix the thing"]
)
def test_other_titles_are_not_checked(title: str) -> None:
    assert mod.check(title, "## Tests\n\nnothing\n") == ([], None)


@pytest.mark.parametrize("title", ["fix: x", "fix(chat): x", "fix!: x", "Fix: x"])
def test_fix_titles_are_checked(title: str) -> None:
    assert mod.check(title, "## Tests\n\nnothing\n")[0]


def test_a_proven_line_is_returned_for_the_record_comparison() -> None:
    problems, proven = mod.check("fix: x", _body(PROVEN))
    assert problems == []
    assert proven.group("sha") == "0123456789ab"
    assert proven.group("tests") == "test/test_a.py::test_x"
    assert mod.check("fix: x", _body(NOT))[1] is None


# -------------------------------------------------------------- red path


def test_no_tests_section_is_one_finding_that_shows_the_shapes() -> None:
    (finding,) = mod.check("fix: x", "## What changed\n\nx\n")[0]
    assert "'## Tests'" in finding and "Proof: PROVEN" in finding and "not provable" in finding


def test_a_tests_section_with_no_line_is_reported() -> None:
    (finding,) = _problems("Added test_a.py, which locks the boundary.")
    assert "no Proof line" in finding


def test_the_unfilled_template_does_not_pass() -> None:
    body = TEMPLATE.read_text(encoding="utf-8")
    (finding,) = mod.check("fix: x", body)[0]
    assert "no Proof line" in finding


def test_a_fenced_example_or_comment_does_not_count() -> None:
    assert _problems(f"```\n{PROVEN}\n```")
    assert _problems(f"<!--\n{PROVEN}\n-->")


def test_a_line_outside_tests_is_reported_and_does_not_satisfy_the_check() -> None:
    problems = _problems("Added tests.", extra=f"\n## Pattern harvest\n\n{PROVEN}\n")
    assert len(problems) == 1 and "outside '## Tests'" in problems[0]


def test_a_line_outside_tests_does_not_hide_a_missing_one_inside() -> None:
    body = f"{PROVEN}\n\n## Tests\n\nAdded tests.\n"
    problems = mod.check("fix: x", body)[0]
    assert len(problems) == 1 and "outside '## Tests'" in problems[0]


def test_two_lines_inside_tests_are_reported() -> None:
    assert any("2 Proof lines" in p for p in _problems(PROVEN + "\n" + NOT))


@pytest.mark.parametrize(
    "line, fragment",
    [
        ("Proof: PROVEN", "does not parse"),
        ("Proof: PROVEN (head 0123456789ab)", "does not parse"),
        ("Proof: PROVEN test/test_a.py::t", "does not parse"),
        ("Proof: PROVEN test/test_a.py::t (head 0123)", "does not parse"),
        ("Proof: PROVEN test/test_a.py::t (head 0123456789AB)", "does not parse"),
        ("Proof: yes", "does not parse"),
        ("Proof: not provable", "does not parse"),
        ("Proof: not provable -- ", "does not parse"),
        ("Proof: not provable -- it is hard", "the reason must be one of"),
        ("Proof: not provable -- other: too short", "the reason must be one of"),
        ("Proof: not provable -- other:", "the reason must be one of"),
        ("Proof: not provable -- frontend test", "the reason must be one of"),
    ],
)
def test_malformed_lines_are_reported(line: str, fragment: str) -> None:
    problems = _problems(line)
    assert any(fragment in p for p in problems), problems


def test_other_needs_twenty_characters() -> None:
    assert not mod.reason_ok("other: " + "x" * 19)
    assert mod.reason_ok("other: " + "x" * 20)


# ------------------------------------------------------------------ mode


def test_ships_in_warn_mode() -> None:
    assert mod.ENFORCE_FROM is None
    assert mod.enforced("2030-01-01T00:00:00Z") is False
    assert mod.enforced(None) is False


@pytest.mark.parametrize(
    "created, expected",
    [
        ("2026-11-04T00:00:00Z", True),
        ("2026-11-05T09:30:00+00:00", True),
        ("2026-11-03T23:59:59Z", False),
    ],
)
def test_enforcement_applies_to_pull_requests_opened_on_or_after_the_date(
    created: str, expected: bool
) -> None:
    assert mod.enforced(created, enforce_from="2026-11-04") is expected


# ------------------------------------------------------------ CLI / env


def _run(title: str, body: str, **env: str) -> tuple[int, str]:
    full = {k: v for k, v in os.environ.items() if not k.startswith(("PR_", "PROVE", "TREE_"))}
    full.update({"PR_TITLE": title, "PR_BODY": body, "GITHUB_ACTIONS": ""})
    full.update(env)
    proc = subprocess.run(
        [sys.executable, "-B", str(SCRIPT)],
        env=full,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    return proc.returncode, proc.stdout + proc.stderr


def test_cli_green() -> None:
    rc, out = _run("fix: x", _body(PROVEN))
    assert rc == 0 and "Proof line present." in out


def test_cli_missing_line_warns_and_passes_in_warn_mode() -> None:
    rc, out = _run("fix: x", _body("Added tests."), GITHUB_ACTIONS="true")
    assert rc == 0
    assert out.startswith("::warning::Proof line: ")
    assert "advisory for now" in out


def test_cli_local_run_prints_plain_lines() -> None:
    rc, out = _run("fix: x", _body("Added tests."))
    assert rc == 0 and "PROOF LINE (warning):" in out and "::warning::" not in out


def test_cli_not_a_fix_is_skipped() -> None:
    rc, out = _run("feat: x", "")
    assert rc == 0 and "not required" in out


def test_cli_compares_a_proven_line_with_the_record_only_when_asked(tmp_path: Path) -> None:
    body = _body(PROVEN)
    rc, out = _run("fix: x", body)  # CI shape: no record, a hand-typed line passes
    assert rc == 0 and "Proof line present." in out
    rc, out = _run("fix: x", body, PROVE_RECORD=str(tmp_path / "none.json"), TREE_ID="t")
    assert rc == 0 and "no prove.py record" in out  # warn mode: printed, not failed


def test_enforce_mode_turns_a_finding_into_exit_one(monkeypatch, capsys) -> None:
    monkeypatch.setattr(mod, "ENFORCE_FROM", "2026-01-01")
    for key, value in {
        "PR_TITLE": "fix: x",
        "PR_BODY": _body("Added tests."),
        "PR_CREATED_AT": "2026-02-01T00:00:00Z",
        "GITHUB_ACTIONS": "true",
    }.items():
        monkeypatch.setenv(key, value)
    assert mod.main() == 1
    assert capsys.readouterr().out.startswith("::error::Proof line: ")
    monkeypatch.setenv("PR_CREATED_AT", "2025-12-31T00:00:00Z")  # opened before: still a warning
    assert mod.main() == 0
    monkeypatch.setenv("PR_CREATED_AT", "2026-02-01T00:00:00Z")
    monkeypatch.setenv("PR_BODY", _body(NOT))
    assert mod.main() == 0


# ---------------------------------------------------------------- wiring


def _steps() -> list[dict]:
    return yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))["jobs"]["pr-hygiene"]["steps"]


def _step_index(fragment: str) -> int:
    for i, step in enumerate(_steps()):
        if fragment.lower() in str(step.get("name", "")).lower():
            return i
    raise AssertionError(f"no pr-hygiene step named like {fragment!r}")


def test_the_proof_text_lives_inside_the_existing_tests_section() -> None:
    text = TEMPLATE.read_text(encoding="utf-8")
    headings = re.findall(r"^## .*$", text, re.MULTILINE)
    assert "## Proof" not in headings  # folded into ## Tests, no new heading
    block = text.split("\n## Tests\n", 1)[1].split("\n## ", 1)[0]
    assert "Proof: PROVEN <the tests that fail without the fix> (head <sha12>)" in block
    assert "Proof: not provable -- <reason>" in block
    for reason in mod.REASONS:
        assert reason in block
    # help text only: an unfilled template carries no uncommented Proof line
    assert not [
        ln for ln in re.sub(r"<!--.*?-->", "", block, flags=re.DOTALL).splitlines() if ln.strip()
    ]


def test_tests_stays_a_required_heading_and_proof_adds_none() -> None:
    required = DESC_CHECK.read_text(encoding="utf-8")
    assert "Tests" in required
    assert "Proof" not in required


def test_the_step_runs_after_pattern_harvest_and_reads_the_base_script() -> None:
    idx = _step_index("Proof line")
    assert idx > _step_index("Pattern harvest")
    step = _steps()[idx]
    run = step["run"]
    assert 'git show "$BASE_TIP:.github/scripts/proof_line_check.py"' in run
    assert "cp .github/scripts/proof_line_check.py" in run
    assert "${{" not in run  # untrusted text arrives through env only
    for key in ("PR_TITLE", "PR_BODY", "PR_CREATED_AT", "BASE_TIP"):
        assert key in step["env"]
    assert "::error::" not in run
    assert "continue-on-error" not in step
