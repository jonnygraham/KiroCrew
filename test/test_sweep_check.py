"""Tests for sweep_check.py, the push-time check of the class sweep file.

The check runs inside ``push_guard.py --require-single-on-base``. It proves that the
mate's own search pattern leaves no hit that no site line accounts for, and it applies
the seam rules (3rd fix: change approach, 5th fix: narrow or defer) from the fix counts
the PR thread records. It never asks a person, and it is advisory while
``SWEEP_ENFORCED`` is False.
"""

from __future__ import annotations

import json
import os
import stat
import sys
from pathlib import Path

import push_guard_helpers
import pytest
from push_guard_helpers import (
    _commit_tree,
    _git,
    _inprocess,
    _land_on_main,
    _load_push_guard,
    _run_push_guard,
)
from skill_script_helpers import load_skill_script

_no_inherited_git_c = push_guard_helpers._no_inherited_git_c
_repo_pair_template = push_guard_helpers._repo_pair_template
repo_pair = push_guard_helpers.repo_pair

SCRIPT = push_guard_helpers.PUSH_GUARD_DIR / "sweep_check.py"
SPAN = "abcdef012345"
CLASS = "a git hook can come from a source other than core.hooksPath"
SEARCH = "git grep -nE 'hooksPath' -- src/kiro_crew"
A = "src/kiro_crew/a.py"
B = "src/kiro_crew/b.py"
REASON = "reads the name only, never runs the command"


def _mod():
    return load_skill_script("sweep_check_under_test", SCRIPT)


@pytest.fixture
def clone(repo_pair):
    """A feature branch that fixes a.py; b.py still names hooksPath; one test is added."""
    clone_dir, _ = repo_pair
    _land_on_main(
        clone_dir,
        {A: "x = 1\nhooksPath = 1\n", B: "def f():\n    return core.hooksPath\n"},
        "feat: base",
    )
    _git(clone_dir, "checkout", "-q", "-b", "feature/x")
    _commit_tree(
        clone_dir,
        {A: "x = 1\nhooks_path = 1\n", "test/test_a.py": "def test_hooks():\n    pass\n"},
        "fix: a",
    )
    return clone_dir


def _entry(
    *,
    span: str = SPAN,
    klass: str = CLASS,
    search: str = SEARCH,
    sites: tuple[str, ...] = (f"- {A}:2  fixed", f"- {B}:2  not-in-class -- {REASON}"),
    branches: tuple[str, ...] = (),
    extra: str = "",
) -> str:
    out = f"## span={span}\nclass: {klass}\nsearch: {search}\n{extra}"
    if sites:
        out += "sites:\n" + "\n".join(sites) + "\n"
    if branches:
        out += "branches:\n" + "\n".join(branches) + "\n"
    return out


def _check(mod, clone_dir, text, monkeypatch, *, enforced=True, counts=None):
    git_dir = _git(clone_dir, "rev-parse", "--absolute-git-dir")
    Path(git_dir, "prepare-pr-sweep.md").write_text(text, encoding="utf-8")
    monkeypatch.setattr(mod, "SWEEP_ENFORCED", enforced)
    return mod.run("main", git_dir, cwd=clone_dir, prior_counts=lambda: counts)


def _refused(mod, clone_dir, text, monkeypatch, capsys, **kw):
    rc = _check(mod, clone_dir, text, monkeypatch, **kw)
    return rc, capsys.readouterr().err


class TestGreenPath:
    def test_an_accounted_sweep_passes(self, clone, monkeypatch, capsys):
        mod = _mod()
        rc = _check(mod, clone, _entry(), monkeypatch, counts={})
        assert rc == 0
        assert "1 sweep entry checked, no findings" in capsys.readouterr().out

    def test_no_sweep_file_passes_silently(self, clone, capsys):
        mod = _mod()
        git_dir = _git(clone, "rev-parse", "--absolute-git-dir")
        assert mod.run("main", git_dir, cwd=clone, prior_counts=lambda: {}) == 0
        assert capsys.readouterr() == ("", "")
        row = json.loads(Path(git_dir, "prepare-pr-metrics.jsonl").read_text().splitlines()[-1])
        assert row["outcome"] == "no-file" and row["entries"] == 0 and row["findings"] == 0

    def test_covered_by_a_test_in_the_diff_passes(self, clone, monkeypatch):
        sites = (f"- {A}:2  fixed", f"- {B}:2  covered-by test/test_a.py::test_hooks")
        assert _check(_mod(), clone, _entry(sites=sites), monkeypatch, counts={}) == 0

    def test_a_structural_class_with_two_branches_passes(self, clone, monkeypatch):
        text = _entry(
            search="none -- ordering across three writers, no common token",
            sites=(),
            branches=("- first PUT fails  fixed", "- slot mid-cycle  covered-by test/test_a.py"),
        )
        assert _check(_mod(), clone, text, monkeypatch, counts={}) == 0

    def test_a_metrics_line_is_appended(self, clone, monkeypatch):
        mod = _mod()
        _check(mod, clone, _entry(), monkeypatch, counts={})
        git_dir = _git(clone, "rev-parse", "--absolute-git-dir")
        row = json.loads(Path(git_dir, "prepare-pr-metrics.jsonl").read_text().splitlines()[-1])
        assert row["step"] == "sweep_check" and row["outcome"] == "ok" and row["entries"] == 1


class TestRefusals:
    def test_a_hit_no_site_accounts_for_is_refused_with_the_hit_list(
        self, clone, monkeypatch, capsys
    ):
        rc, err = _refused(
            _mod(), clone, _entry(sites=(f"- {A}:2  fixed",)), monkeypatch, capsys, counts={}
        )
        assert rc == 40
        assert "REFUSED" in err and "no site line accounts for" in err and f"{B}:2" in err

    def test_a_vacuous_pattern_is_refused(self, clone, monkeypatch, capsys):
        text = _entry(search="git grep -nE 'neverAppears' -- src/kiro_crew")
        rc, err = _refused(_mod(), clone, text, monkeypatch, capsys, counts={})
        assert rc == 40 and "matches nothing in the tree before the fix" in err

    def test_more_than_25_hits_is_too_broad(self, clone, monkeypatch, capsys):
        _commit_tree(clone, {B: "".join("hooksPath = %d\n" % i for i in range(30))}, "fix: b")
        rc, err = _refused(_mod(), clone, _entry(), monkeypatch, capsys, counts={})
        assert rc == 40 and "30 hits (limit 25): too broad" in err

    def test_covered_by_must_name_a_test_in_the_diff(self, clone, monkeypatch, capsys):
        for test_id, why in (
            ("test/test_other.py::test_x", "is not in the diff"),
            ("test/test_a.py::test_missing", "has no test_missing"),
        ):
            sites = (f"- {A}:2  fixed", f"- {B}:2  covered-by {test_id}")
            rc, err = _refused(_mod(), clone, _entry(sites=sites), monkeypatch, capsys, counts={})
            assert rc == 40 and why in err

    @pytest.mark.parametrize("test_id", ["test/test_old.py", "test/test_old.py::test_old"])
    def test_a_deleted_test_is_neither_coverage_nor_a_deferral_pin(
        self, clone, monkeypatch, capsys, test_id
    ):
        # main gains test_old.py; the feature branch lacks it, so it is in the diff but not in HEAD
        _land_on_main(clone, {"test/test_old.py": "def test_old():\n    pass\n"}, "test: old")
        _git(clone, "checkout", "-q", "feature/x")
        sites = (f"- {A}:2  fixed", f"- {B}:2  covered-by {test_id}")
        rc, err = _refused(_mod(), clone, _entry(sites=sites), monkeypatch, capsys, counts={})
        assert rc == 40 and "does not exist in HEAD (a deleted test covers nothing)" in err
        extra = f"resolution: deferred #12 pinned-by {test_id}\n"
        rc, err = _refused(
            _mod(), clone, _entry(extra=extra), monkeypatch, capsys, counts={SPAN: 4}
        )
        assert rc == 40 and "does not exist in HEAD" in err

    def test_fixed_must_name_a_path_the_diff_touches(self, clone, monkeypatch, capsys):
        sites = (f"- {A}:2  fixed", f"- {B}:2  fixed")
        rc, err = _refused(_mod(), clone, _entry(sites=sites), monkeypatch, capsys, counts={})
        assert rc == 40 and "`fixed` but the diff does not touch" in err

    def test_a_short_not_in_class_reason_is_refused(self, clone, monkeypatch, capsys):
        sites = (f"- {A}:2  fixed", f"- {B}:2  not-in-class -- harmless")
        rc, err = _refused(_mod(), clone, _entry(sites=sites), monkeypatch, capsys, counts={})
        assert rc == 40 and "needs a reason of 20+ characters" in err

    def test_search_none_needs_two_branch_rows(self, clone, monkeypatch, capsys):
        text = _entry(
            search="none -- ordering, no common token", sites=(), branches=("- one  fixed",)
        )
        rc, err = _refused(_mod(), clone, text, monkeypatch, capsys, counts={})
        assert rc == 40 and "two or more rows" in err

    @pytest.mark.parametrize(
        "search",
        [
            "git grep -nE '-oops' -- src/kiro_crew",
            "git grep -e hooksPath -- src/kiro_crew",
            "git grep --open-files-in-pager=touch hooksPath",
            "git grep --no-index hooksPath",
            "git grep -nE hooksPath -- :(top)src",
            "git grep hooksPath extra",
            "grep -rn hooksPath .",
            "git grep 'unterminated",
            "",
        ],
    )
    def test_an_unsafe_or_malformed_search_line_is_refused(
        self, clone, monkeypatch, capsys, search
    ):
        rc, err = _refused(_mod(), clone, _entry(search=search), monkeypatch, capsys, counts={})
        assert rc == 40 and "span=" + SPAN in err

    def test_the_pattern_reaches_git_as_data_not_through_a_shell(self, clone, monkeypatch, capsys):
        marker = Path(clone, "pwned")
        text = _entry(search=f"git grep -nE 'hooksPath;touch {marker}' -- src/kiro_crew")
        rc, _ = _refused(_mod(), clone, text, monkeypatch, capsys, counts={})
        assert rc == 40 and not marker.exists()

    def test_a_malformed_row_and_a_bad_span_heading_are_findings(self, clone, monkeypatch, capsys):
        text = _entry(sites=(f"- {A}:2  maybe",)) + "\n## span=xyz\nclass: y\n"
        rc, err = _refused(_mod(), clone, text, monkeypatch, capsys, counts={})
        assert rc == 40
        assert "row does not end in fixed" in err and "span id must be 12 hex" in err

    def test_a_missing_class_sentence_is_refused(self, clone, monkeypatch, capsys):
        rc, err = _refused(_mod(), clone, _entry(klass=""), monkeypatch, capsys, counts={})
        assert rc == 40 and "missing `class:` sentence" in err


class TestWarnPeriod:
    def test_the_same_findings_print_and_the_exit_is_zero(self, clone, monkeypatch, capsys):
        mod = _mod()
        assert mod.SWEEP_ENFORCED is False, "the check ships in its warn period"
        text = _entry(sites=(f"- {A}:2  fixed",))
        rc, err = _refused(mod, clone, text, monkeypatch, capsys, enforced=False, counts={})
        assert rc == 0
        assert "WARNING (warn period, not refused)" in err and f"{B}:2" in err
        git_dir = _git(clone, "rev-parse", "--absolute-git-dir")
        row = json.loads(Path(git_dir, "prepare-pr-metrics.jsonl").read_text().splitlines()[-1])
        assert row["outcome"] == "warned" and row["findings"] == 1 and row["enforced"] is False


class TestSeamRules:
    """The 3rd fix of one span changes approach; the 5th narrows or defers. No person."""

    def test_the_second_fix_needs_nothing_extra(self, clone, monkeypatch):
        assert _check(_mod(), clone, _entry(), monkeypatch, counts={SPAN: 1}) == 0

    def test_the_third_fix_needs_a_resolution_or_a_branches_table(self, clone, monkeypatch, capsys):
        rc, err = _refused(_mod(), clone, _entry(), monkeypatch, capsys, counts={SPAN: 2})
        assert rc == 40 and "this is the 3rd fix of one span" in err
        table = (
            "- final drain, first PUT fails  fixed",
            "- slot mid-cycle  covered-by test/test_a.py",
        )
        assert _check(_mod(), clone, _entry(branches=table), monkeypatch, counts={SPAN: 2}) == 0
        extra = "resolution: narrowed -- redo the hook lookup without the include path\n"
        assert _check(_mod(), clone, _entry(extra=extra), monkeypatch, counts={SPAN: 2}) == 0

    def test_the_fifth_fix_needs_a_resolution_and_a_table_is_not_enough(
        self, clone, monkeypatch, capsys
    ):
        table = (
            "- final drain, first PUT fails  fixed",
            "- slot mid-cycle  covered-by test/test_a.py",
        )
        rc, err = _refused(
            _mod(), clone, _entry(branches=table), monkeypatch, capsys, counts={SPAN: 4}
        )
        assert rc == 40 and "this is the 5th fix of one span: narrow the scope or defer" in err

    @pytest.mark.parametrize(
        "resolution",
        [
            "removed -- the second writer is gone and the hook list has one source",
            "narrowed -- only the hooksPath key is read now, includeIf is out of scope",
            "deferred #4711 pinned-by test/test_a.py::test_hooks",
        ],
    )
    def test_the_fifth_fix_passes_with_a_valid_resolution(self, clone, monkeypatch, resolution):
        extra = f"resolution: {resolution}\n"
        assert _check(_mod(), clone, _entry(extra=extra), monkeypatch, counts={SPAN: 4}) == 0

    @pytest.mark.parametrize(
        ("resolution", "why"),
        [
            ("narrowed -- short", "needs a reason of 20+ characters"),
            ("deferred later", "resolution must read"),
            ("deferred #12 pinned-by test/test_gone.py::test_x", "is not in the diff"),
            ("fixed again", "resolution must read"),
        ],
    )
    def test_an_invalid_resolution_is_refused_at_any_count(
        self, clone, monkeypatch, capsys, resolution, why
    ):
        extra = f"resolution: {resolution}\n"
        rc, err = _refused(_mod(), clone, _entry(extra=extra), monkeypatch, capsys, counts={})
        assert rc == 40 and why in err

    @pytest.mark.parametrize("risk", ["security", "data-loss", "corruption"])
    def test_a_sensitive_class_is_never_deferred(self, clone, monkeypatch, capsys, risk):
        extra = f"risk: {risk}\nresolution: deferred #4711 pinned-by test/test_a.py::test_hooks\n"
        rc, err = _refused(
            _mod(), clone, _entry(extra=extra), monkeypatch, capsys, counts={SPAN: 4}
        )
        assert rc == 40 and f"a {risk} class is never deferred" in err
        narrowed = f"risk: {risk}\nresolution: narrowed -- only the hooksPath key is read now\n"
        assert _check(_mod(), clone, _entry(extra=narrowed), monkeypatch, counts={SPAN: 4}) == 0

    def test_unreadable_rounds_skip_the_seam_rules_and_say_so(self, clone, monkeypatch, capsys):
        rc, err = _refused(_mod(), clone, _entry(), monkeypatch, capsys, counts=None)
        assert rc == 0
        assert "seam rules skipped" in err

    def test_counts_come_from_the_pr_thread_by_default(self, clone, monkeypatch):
        mod = _mod()
        seen: list = []
        monkeypatch.setattr(mod, "_prior_fix_counts", lambda cwd: seen.append(cwd) or {SPAN: 4})
        git_dir = _git(clone, "rev-parse", "--absolute-git-dir")
        Path(git_dir, "prepare-pr-sweep.md").write_text(_entry(), encoding="utf-8")
        monkeypatch.setattr(mod, "SWEEP_ENFORCED", True)
        assert mod.run("main", git_dir, cwd=clone) == 40
        assert [os.path.realpath(c) for c in seen] == [os.path.realpath(clone)]


class TestPriorFixCounts:
    def test_a_missing_gh_or_pr_means_unknown_not_zero(self, tmp_path, monkeypatch):
        mod = _mod()
        monkeypatch.setenv("PATH", str(tmp_path))
        assert mod._prior_fix_counts(str(tmp_path)) is None

    def test_a_fix_counts_once_per_distinct_head_and_only_fixes_count(self):
        mod = _mod()
        heads = ["1" * 40, "2" * 40, "3" * 40]

        def rec(cid, head, span):
            return {"comment_id": cid, "head": head, "spans": [span], "malformed": False}

        records = [
            rec(1, heads[0], SPAN),
            rec(2, heads[0], SPAN),
            rec(3, heads[1], SPAN),
            rec(4, heads[2], SPAN),
            rec(5, heads[0], "bbbbbbbbbbbb"),
            rec(6, heads[1], "bbbbbbbbbbbb"),
            rec(7, heads[0], "cccccccccccc"),
        ]
        bodies = {
            1: "**fixed** -- x\nclass: a hook can come from another source\n",
            2: "**fixed** -- again\nclass: a hook can come from another source\n",
            3: "**fixed** -- x\nclass: a hook can come from another source\n",
            4: "**rebutted** -- not a fix\n",
            5: "**fixed** -- x\nclass: the export drops the trailing newline\n",
            6: "**fixed** -- x\nclass: retry loop sleeps while holding the lock\n",
            7: "> fixed in an earlier head, quoted\nclass: a thing\n",
        }
        counts = mod.fix_counts_from(records, bodies)
        assert counts == {SPAN: 2, "bbbbbbbbbbbb": 2}, "heads are counted; class lines are not read"
        assert mod.fix_counts_from(records, bodies, skip_head=heads[0])[SPAN] == 1

    def test_class_lines_never_change_the_count(self):
        mod = _mod()
        classes = ["a hook can come from another source"] + ["the export drops the newline"] * 3
        records = [
            {"comment_id": i, "head": str(i + 1) * 40, "spans": [SPAN], "malformed": False}
            for i in range(4)
        ]
        bodies = {i: f"**fixed** -- x\nclass: {c}\n" for i, c in enumerate(classes)}
        assert mod.fix_counts_from(records, bodies) == {SPAN: 4}
        assert "difflib" not in SCRIPT.read_text(encoding="utf-8")

    def test_every_span_of_a_fixing_record_is_kept(self):
        mod = _mod()
        records = [
            {
                "comment_id": 1,
                "head": "1" * 40,
                "spans": [SPAN, "bbbbbbbbbbbb"],
                "malformed": False,
            },
            {
                "comment_id": 2,
                "head": "2" * 40,
                "spans": ["bbbbbbbbbbbb", SPAN],
                "malformed": False,
            },
            {"comment_id": 3, "head": "3" * 40, "spans": [SPAN], "malformed": True},
        ]
        bodies = {1: "**fixed** -- x\n", 2: "**fixed** -- y\n", 3: "**fixed** -- z\n"}
        assert mod.fix_counts_from(records, bodies) == {SPAN: 2, "bbbbbbbbbbbb": 2}

    def test_a_rebuttal_that_quotes_a_fixed_bullet_is_not_a_fix(self):
        mod = _mod()
        quote = "> - **fixed** in head 1\n> Outcome: fixed in abc123\n"
        records = [
            {"comment_id": i, "head": str(i + 1) * 40, "spans": [SPAN], "malformed": False}
            for i in range(4)
        ]
        bodies = {i: f"**rebutted** -- span={SPAN}\n{quote}" for i in range(4)}
        assert mod.fix_counts_from(records, bodies) == {}

    def test_a_bold_title_with_no_outcome_line_counts_nothing(self):
        mod = _mod()
        records = [
            {"comment_id": i, "head": str(i + 1) * 40, "spans": [SPAN], "malformed": False}
            for i in range(4)
        ]
        bodies = {
            i: f"**Fixed point iteration is slow** span={SPAN}\n> rationale\n" for i in range(4)
        }
        assert mod.fix_counts_from(records, bodies) == {}

    @pytest.mark.parametrize(
        ("body", "word"),
        [
            ("<!-- m -->\n**fixed** -- span=aaaaaaaaaaa1\n", "fixed"),
            ("<!-- m -->\n- **span=aaaaaaaaaaa1** fixed in head 1083c00c5\n", "fixed"),
            ("<!-- m -->\ntitle\n\n> Outcome: fixed in 7ff6176c3d. Detail\n", "fixed"),
            ("<!-- m -->\ntitle\n\n> Outcome: checked, no change needed.\n", "checked"),
            ("<!-- m -->\n**rebutted** -- x\n> - **fixed** quoted\n", "rebutted"),
            ("<!-- m -->\n**rebutted** -- x\n```\n**fixed** -- y\n```\n", "rebutted"),
            ("<!-- m -->\n> a quoted line that says fixed\n", ""),
            ("<!-- m -->\n**Refused activation puts a message in the origin chat**\n", ""),
            ("<!-- m -->\n**Retry guard**\n> Outcome: fixed in abc1234\n", "fixed"),
            ("<!-- m -->\n**Retry guard**\n> Outcome: rebutted, by design.\n", "rebutted"),
            ("<!-- m -->\n**deferred**\n> Outcome: fixed in abc1234\n", "deferred"),
            ("<!-- m -->\n- **span=aaaaaaaaaaa1** Retry guard fails\n", ""),
        ],
    )
    def test_the_declared_outcome_is_read_from_the_forms_dispositions_use(self, body, word):
        assert _mod().declared_outcome(body) == word


class TestPushGuardWiring:
    def test_a_refusal_from_the_check_refuses_the_push(self, clone, monkeypatch):
        rc, out, _ = _inprocess(
            monkeypatch,
            clone,
            extra_args=["--require-single-on-base"],
            overrides={"_sweep_check": lambda base, git_dir: 40},
        )
        assert rc == 40 and "SAFE TO PUSH" not in out

    def test_a_clean_check_leaves_the_push_safe(self, clone, monkeypatch):
        rc, out, _ = _inprocess(
            monkeypatch,
            clone,
            extra_args=["--require-single-on-base"],
            overrides={"_sweep_check": lambda base, git_dir: 0},
        )
        assert rc == 0 and "SAFE TO PUSH (single commit on base)" in out

    def test_the_real_check_is_advisory_in_the_warn_period(self, clone):
        git_dir = _git(clone, "rev-parse", "--absolute-git-dir")
        Path(git_dir, "prepare-pr-sweep.md").write_text(
            _entry(sites=(f"- {A}:2  fixed",)), encoding="utf-8"
        )
        rc, out, err = _run_push_guard(clone, ["--require-single-on-base"])
        assert rc == 0, err
        assert "SAFE TO PUSH" in out
        assert "sweep_check WARNING (warn period, not refused)" in err

    def test_glue_returns_the_refusal_once_the_check_is_enforced(self, clone, monkeypatch):
        pg = _load_push_guard()
        loader = pg._NoBytecodeSourceLoader
        real = loader.exec_module

        def enforced(self, module):
            real(self, module)
            module.SWEEP_ENFORCED = True
            module._prior_fix_counts = lambda cwd: {}

        monkeypatch.setattr(loader, "exec_module", enforced)
        monkeypatch.chdir(clone)
        git_dir = _git(clone, "rev-parse", "--absolute-git-dir")
        sweep = Path(git_dir, "prepare-pr-sweep.md")
        sweep.write_text(_entry(sites=(f"- {A}:2  fixed",)), encoding="utf-8")
        assert pg._sweep_check("main", git_dir) == pg.EXIT_REFUSED
        sweep.write_text(_entry(), encoding="utf-8")
        assert pg._sweep_check("main", git_dir) == 0

    def test_a_check_that_cannot_load_warns_and_does_not_block(self, clone, monkeypatch, capsys):
        pg = _load_push_guard()

        def boom(self, module):
            raise ImportError("broken copy")

        monkeypatch.setattr(pg._NoBytecodeSourceLoader, "exec_module", boom)
        assert pg._sweep_check("main", _git(clone, "rev-parse", "--absolute-git-dir")) == 0
        assert "could not run (broken copy); not blocking the push" in capsys.readouterr().err


def test_the_script_and_the_skill_state_the_same_limits():
    mod = _mod()
    doc = mod.__doc__ or ""
    assert mod.MAX_HITS == 25 and mod.MIN_REASON == 20
    assert "more than 25 hits" in doc and "under 20 characters" in doc
    assert "exit" in doc.lower() and "No rule asks a person" in doc


def test_agents_md_and_the_skill_point_at_the_sweep():
    root = push_guard_helpers.REPO_ROOT
    agents = (root / "AGENTS.md").read_text(encoding="utf-8")
    assert "### A review finding is a class, not a line" in agents
    assert "On the fifth fix only narrowing or deferral remain." in agents
    skill = (push_guard_helpers.PUSH_GUARD_DIR.parent / "SKILL.md").read_text(encoding="utf-8")
    assert "`prepare-pr-sweep.md`" in skill
    assert len(skill.encode("utf-8")) <= 56000


def _fake_gh(tmp_path, monkeypatch, *, heads, head_now="9" * 40, klass=CLASS):
    """A gh stand-in script that serves one PR, its dispositions and a writer."""
    comments = [
        {
            "id": 100 + i,
            "user": {"login": "writer"},
            "body": (
                f"<!-- ai-review-disposition target=gpt head={h} -->\n"
                f"**fixed** -- span={SPAN}\nclass: {klass}\n> rationale\n"
            ),
        }
        for i, h in enumerate(heads)
    ]
    pr = {"number": 7, "url": "https://github.com/o/r/pull/7", "headRefOid": head_now}
    data = tmp_path / "gh.json"
    data.write_text(json.dumps({"pr": pr, "comments": comments}))
    gh = tmp_path / "fake_gh.py"
    gh.write_text(
        "import json, os, sys\n"
        "data = json.load(open(os.environ['FAKE_GH']))\n"
        "a = sys.argv[1:]\n"
        "if a[:2] == ['pr', 'view']:\n"
        "    print(json.dumps(data['pr']))\n"
        "elif a[0] == 'api' and '/comments' in a[1]:\n"
        "    print(json.dumps(data['comments']))\n"
        "elif a[0] == 'api' and '/collaborators/' in a[1]:\n"
        "    print(json.dumps({'permission': 'write'}))\n"
        "else:\n"
        "    sys.exit(1)\n"
    )
    monkeypatch.setenv("FAKE_GH", str(data))
    monkeypatch.setenv("FAKE_GH_SCRIPT", str(gh))


class TestSeamPathReadsThePrThread:
    """The 3rd/5th-fix rules end to end: no stub of the fix counts, a gh that serves a thread."""

    def _run(self, mod, clone_dir, text, monkeypatch):
        git_dir = _git(clone_dir, "rev-parse", "--absolute-git-dir")
        Path(git_dir, "prepare-pr-sweep.md").write_text(text, encoding="utf-8")
        monkeypatch.setattr(mod, "SWEEP_ENFORCED", True)
        # Only the gh executable is swapped (a script run by this Python, so it
        # works on every platform); the fix counting under test is the real code.
        real = mod._exe_argv

        def argv(args):
            if args[0] == "gh":
                return [sys.executable, os.environ["FAKE_GH_SCRIPT"], *args[1:]]
            return real(args)

        monkeypatch.setattr(mod, "_exe_argv", argv)
        return mod.run("main", git_dir, cwd=clone_dir)

    def test_four_fixed_heads_make_the_next_fix_its_fifth(
        self, clone, tmp_path, monkeypatch, capsys
    ):
        _fake_gh(tmp_path, monkeypatch, heads=["1" * 40, "2" * 40, "3" * 40, "4" * 40])
        mod = _mod()
        assert self._run(mod, clone, _entry(), monkeypatch) == 40
        assert "this is the 5th fix of one span" in capsys.readouterr().err
        extra = "resolution: narrowed -- only the hooksPath key is read now\n"
        assert self._run(mod, clone, _entry(extra=extra), monkeypatch) == 0

    def test_two_fixed_heads_make_the_next_fix_its_third(
        self, clone, tmp_path, monkeypatch, capsys
    ):
        _fake_gh(tmp_path, monkeypatch, heads=["1" * 40, "2" * 40])
        assert self._run(_mod(), clone, _entry(), monkeypatch) == 40
        assert "this is the 3rd fix of one span" in capsys.readouterr().err

    def test_the_head_being_fixed_is_not_counted_twice(self, clone, tmp_path, monkeypatch):
        # its disposition may already be posted before the push
        _fake_gh(tmp_path, monkeypatch, heads=["1" * 40, "2" * 40], head_now="2" * 40)
        assert self._run(_mod(), clone, _entry(), monkeypatch) == 0

    def test_one_fix_so_far_is_the_second_fix(self, clone, tmp_path, monkeypatch):
        _fake_gh(tmp_path, monkeypatch, heads=["1" * 40])
        assert self._run(_mod(), clone, _entry(), monkeypatch) == 0


class TestSearchSemantics:
    """The search means what it meant when the mate ran it, in both checks."""

    def test_git_default_basic_regex_alternation_is_kept(self, clone, monkeypatch):
        text = _entry(search=r"git grep -n 'hooksPath\|nothingElse' -- src/kiro_crew")
        assert _check(_mod(), clone, text, monkeypatch, counts={}) == 0

    def test_the_same_text_under_dash_e_is_a_literal_pipe_and_vacuous(
        self, clone, monkeypatch, capsys
    ):
        text = _entry(search=r"git grep -nE 'hooksPath\|nothingElse' -- src/kiro_crew")
        rc, err = _refused(_mod(), clone, text, monkeypatch, capsys, counts={})
        assert rc == 40 and "matches nothing in the tree before the fix" in err

    def test_dash_e_alternation_still_works(self, clone, monkeypatch):
        text = _entry(search="git grep -nE 'hooksPath|nothingElse' -- src/kiro_crew")
        assert _check(_mod(), clone, text, monkeypatch, counts={}) == 0

    def test_a_fixed_string_search_is_literal(self, clone, monkeypatch, capsys):
        text = _entry(search="git grep -nF 'hooks.Path' -- src/kiro_crew")
        rc, err = _refused(_mod(), clone, text, monkeypatch, capsys, counts={})
        assert rc == 40 and "matches nothing" in err

    def test_the_branch_as_last_pushed_is_a_pre_fix_tree(self, clone, monkeypatch):
        # the token exists only in a head that was pushed, then removed by the fix
        _commit_tree(clone, {A: "x = 1\nlegacy_token = 1\nhooks_path = 1\n"}, "fix: a2")
        _git(clone, "push", "-q", "origin", "feature/x")
        _commit_tree(clone, {A: "x = 1\nhooks_path = 1\n"}, "fix: a3")
        text = _entry(
            search="git grep -nE legacy_token -- src/kiro_crew", sites=(f"- {A}:2  fixed",)
        )
        assert _check(_mod(), clone, text, monkeypatch, counts={}) == 0

    def test_an_unpushed_token_is_vacuous(self, clone, monkeypatch, capsys):
        text = _entry(search="git grep -nE hooks_path -- src/kiro_crew", sites=(f"- {A}:2  fixed",))
        rc, err = _refused(_mod(), clone, text, monkeypatch, capsys, counts={})
        assert rc == 40 and "matches nothing" in err


class TestRunsFromTheRepositoryRoot:
    def test_a_check_started_in_a_subdirectory_reads_root_relative_searches(
        self, clone, monkeypatch
    ):
        sub = Path(clone, "src", "kiro_crew")
        assert _check(_mod(), str(sub), _entry(), monkeypatch, counts={}) == 0

    def test_a_check_started_in_a_nested_directory_refuses_the_same_findings(
        self, clone, monkeypatch, capsys
    ):
        sub = Path(clone, "src")
        sites = (f"- {A}:2  fixed",)
        rc, err = _refused(_mod(), str(sub), _entry(sites=sites), monkeypatch, capsys, counts={})
        assert rc == 40 and f"{B}:2" in err


class TestExecutableResolution:
    """git and gh come from PATH's absolute entries, never from the checkout."""

    @pytest.mark.parametrize("name", ["git", "gh"])
    def test_a_program_planted_in_the_checkout_is_never_run(self, tmp_path, monkeypatch, name):
        planted = tmp_path / name
        marker = tmp_path / "ran"
        planted.write_text(f"#!/bin/sh\ntouch '{marker}'\n")
        planted.chmod(planted.stat().st_mode | stat.S_IXUSR)
        monkeypatch.setenv("PATH", ".")
        rc, _, err = _mod()._run([name, "--version"], cwd=str(tmp_path))
        assert rc == 127 and not marker.exists()
        assert err.startswith(name + ":")

    def test_the_resolved_git_is_absolute(self):
        argv = _mod()._exe_argv(["git", "status"])
        assert os.path.isabs(argv[0]) and argv[1:] == ["status"]

    def test_only_git_and_gh_may_be_run(self):
        with pytest.raises(ValueError):
            _mod()._exe_argv(["sh", "-c", "true"])

    def test_the_resolver_is_push_guards_own(self):
        assert _mod()._sibling("push_guard.py")._which("git") == _load_push_guard()._which_git()
