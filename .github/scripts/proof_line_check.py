#!/usr/bin/env python3
"""PR Hygiene: a `fix:` pull request says whether its tests fail without the fix.

A test that still passes with the bug put back does not test the fix. `prove.py`
(in the kirocrew-prepare-pr skill) reverts the production hunks in a throwaway
worktree and re-runs the changed tests. This check asks the author to paste the
result as ONE line under the existing `## Tests` heading (no new section):

    Proof: PROVEN <the tests that fail without the fix> (head <sha12>)
    Proof: not provable -- <reason>

`<reason>` comes from a closed list, so a weekly report can count them:

    frontend test (prove.py runs Python only)
    test-only change
    copy-only change
    docs or config only
    prove.py timed out
    other: <20+ characters>

CI checks, for titles starting `fix` (a `revert` is out of scope):

1. `## Tests` exists (outside fences and HTML comments);
2. it holds exactly one `Proof:` line, and no `Proof:` line sits outside it;
3. the line has one of the two shapes above, with a reason from the list.

Locally, `diff_signals.py --check-body` also sets PROVE_RECORD and TREE_ID, and a
`PROVEN` line is compared with the record `prove.py` wrote for the same tree: a
stale or invented line fails in the agent flow. CI cannot do that (the record
lives in the author's git directory), so a hand-typed `PROVEN` line passes CI,
as `Reader:` lines do.

LIMIT, stated plainly: this checks that the line is present, well formed and in
the right section. It does not run the tests. A control test that passes both
ways stays valid: the rule asks for one failing test, not all.

Environment (read only as data, never interpolated into code):
    PR_TITLE        the pull request title
    PR_BODY         the pull request body
    PR_CREATED_AT   optional ISO-8601 time the pull request was opened
    PROVE_RECORD    optional path of prove.py's record (local runs)
    TREE_ID         optional tree id of HEAD, compared with the record's
    GITHUB_ACTIONS  when "true", findings print as workflow annotations

Mode: WARN until ENFORCE_FROM is set. A finding prints a warning and exits 0. Once
ENFORCE_FROM holds a date (YYYY-MM-DD), a pull request opened on or after it exits
1 on a finding; one opened before it keeps warning.

Exit: 0 pass, or findings in warn mode | 1 findings in enforce mode | 2 environment error
"""

from __future__ import annotations

import json
import os
import re
import sys
from datetime import date, datetime, timezone

# None = warn mode. Set to "YYYY-MM-DD" to turn the check red for pull requests
# opened on or after that date (RFC: review loop and escaped-bug reduction, D4).
ENFORCE_FROM: str | None = None

TITLE_RE = re.compile(r"^fix([(!:]|$)", re.IGNORECASE)
HEADING_RE = re.compile(r"^ {0,3}(#{1,6})( +.*|)$")
TESTS_RE = re.compile(r"^ {0,3}(#{2,}) +Tests *$", re.IGNORECASE)
FENCE_RE = re.compile(r"^ {0,3}(`{3,}|~{3,})")
COMMENT_RE = re.compile(r"<!--.*?-->", re.DOTALL)

BULLET = r"(?:[-*] +)?"
PROOF_PREFIX_RE = re.compile(BULLET + r"Proof *:", re.IGNORECASE)
PROVEN_RE = re.compile(
    BULLET + r"Proof: +PROVEN +(?P<tests>\S.*?) +\(head (?P<sha>[0-9a-f]{12})\)$"
)
NOT_PROVABLE_RE = re.compile(BULLET + r"Proof: +not provable +-- +(?P<reason>\S.*)$")

# The closed list of reasons (RFC Appendix D); `other:` takes 20+ characters.
REASONS = (
    "frontend test (prove.py runs Python only)",
    "test-only change",
    "copy-only change",
    "docs or config only",
    "prove.py timed out",
)
OTHER_RE = re.compile(r"other: (?P<why>\S.*)$")
OTHER_MIN = 20

SHAPES = (
    "'Proof: PROVEN <the tests that fail without the fix> (head <sha12>)' or "
    "'Proof: not provable -- <reason>'"
)


def strip_hidden(text: str) -> str:
    """Remove HTML comments and fenced blocks, so template help text never counts."""
    text = COMMENT_RE.sub("", text)
    kept: list[str] = []
    fence: str | None = None
    for line in text.splitlines():
        m = FENCE_RE.match(line)
        if fence is None:
            if m:
                fence = m.group(1)[0]
                continue
            kept.append(line)
        elif m and m.group(1)[0] == fence:
            fence = None
    return "\n".join(kept)


def split_sections(body: str) -> tuple[list[str] | None, list[str]]:
    """(lines of the `## Tests` section or None, every other visible line)."""
    lines = strip_hidden(body).splitlines()
    inside: list[str] | None = None
    outside: list[str] = []
    level = 0
    in_tests = False
    for line in lines:
        h = HEADING_RE.match(line)
        if in_tests and h and len(h.group(1)) <= level:
            in_tests = False
        m = TESTS_RE.match(line)
        if m and inside is None:
            in_tests = True
            level = len(m.group(1))
            inside = []
            continue
        if in_tests:
            assert inside is not None
            inside.append(line.strip())
        else:
            outside.append(line.strip())
    return inside, outside


def is_in_scope(title: str) -> bool:
    return bool(TITLE_RE.match(title.strip()))


def reason_ok(reason: str) -> bool:
    reason = reason.strip()
    if reason.lower() in {r.lower() for r in REASONS}:
        return True
    other = OTHER_RE.match(reason)
    return bool(other and len(other.group("why").strip()) >= OTHER_MIN)


def check(title: str, body: str) -> tuple[list[str], re.Match | None]:
    """Findings for one pull request, and the parsed `PROVEN` match if there is one."""
    if not is_in_scope(title):
        return [], None
    inside, outside = split_sections(body)
    if inside is None:
        return [
            "fix pull requests must carry a '## Tests' section holding one Proof line: " + SHAPES
        ], None
    problems: list[str] = []
    proofs = [ln for ln in inside if PROOF_PREFIX_RE.match(ln)]
    stray = [ln for ln in outside if PROOF_PREFIX_RE.match(ln)]
    for line in stray:
        problems.append(f"'{line}' is outside '## Tests': move the Proof line under that heading")
    if not proofs:
        if not stray:
            problems.append("'## Tests' has no Proof line: add " + SHAPES)
        return problems, None
    if len(proofs) > 1:
        problems.append(f"'## Tests' has {len(proofs)} Proof lines; keep one")
    line = proofs[0]
    proven = PROVEN_RE.match(line)
    if proven:
        return problems, proven
    not_provable = NOT_PROVABLE_RE.match(line)
    if not_provable:
        if not reason_ok(not_provable.group("reason")):
            problems.append(
                f"'{line}': the reason must be one of: " + "; ".join(REASONS) + "; "
                f"other: <{OTHER_MIN}+ characters>"
            )
        return problems, None
    problems.append(f"'{line}' does not parse: write " + SHAPES)
    return problems, None


def compare_record(match: re.Match, record_path: str, tree: str) -> list[str]:
    """Findings from comparing a `PROVEN` line with prove.py's record for this tree."""
    try:
        with open(record_path, encoding="utf-8") as fh:
            record = json.load(fh)
    except (OSError, ValueError):
        return [
            f"the PROVEN line has no prove.py record ({record_path}): run prove.py and paste "
            "the line it prints"
        ]
    if not isinstance(record, dict):
        return ["the prove.py record is not a JSON object: re-run prove.py"]
    problems: list[str] = []
    if record.get("tree") != tree:
        problems.append(
            "the prove.py record is for a different tree (the code changed since): re-run "
            "prove.py and paste the line it prints"
        )
        return problems
    if record.get("verdict") != "PROVEN":
        problems.append(
            f"prove.py recorded {record.get('verdict')} for this tree, not PROVEN: the tests "
            "do not fail without the fix"
        )
        return problems
    head = str(record.get("head", ""))[:12]
    if match.group("sha") != head:
        problems.append(
            f"the line names head {match.group('sha')}, prove.py recorded {head or 'none'}"
        )
    remainder = match.group("tests").replace("<&#33;--", "<!--")  # prove.py's escape
    tests = [t for t in record.get("tests", []) if isinstance(t, str) and t]
    named = 0
    for test in sorted(tests, key=len, reverse=True):
        if test in remainder:
            named += 1
            remainder = remainder.replace(test, "")
    if not named or remainder.strip(" ,;\t"):
        problems.append(
            "the line names tests the prove.py record does not list: paste the line it prints"
        )
    return problems


def enforced(created_at: str | None, enforce_from: str | None = None) -> bool:
    """Whether a pull request opened at ``created_at`` is held to the check.

    A pull request with no known opening time counts as opened now.
    """
    cutoff = enforce_from if enforce_from is not None else ENFORCE_FROM
    if not cutoff:
        return False
    start = date.fromisoformat(cutoff)
    if created_at:
        text = created_at.strip()
        if text.endswith("Z"):
            text = text[:-1] + "+00:00"
        opened = datetime.fromisoformat(text).date()
    else:
        opened = datetime.now(timezone.utc).date()
    return opened >= start


def main() -> int:
    title = os.environ.get("PR_TITLE", "")
    body = os.environ.get("PR_BODY", "")
    if not is_in_scope(title):
        print("Not a fix pull request; Proof line not required.")
        return 0
    problems, proven = check(title, body)
    record_path = os.environ.get("PROVE_RECORD", "")
    tree = os.environ.get("TREE_ID", "")
    if proven is not None and record_path and tree:
        problems += compare_record(proven, record_path, tree)
    if not problems:
        print("Proof line present.")
        return 0
    try:
        red = enforced(os.environ.get("PR_CREATED_AT"))
    except ValueError as exc:
        print(f"proof_line_check: bad date ({exc})", file=sys.stderr)
        return 2
    level = "error" if red else "warning"
    in_ci = os.environ.get("GITHUB_ACTIONS") == "true"
    for problem in problems:
        if in_ci:
            print(f"::{level}::Proof line: {problem}")
        else:
            print(f"PROOF LINE ({level}): {problem}")
    if not red:
        print("The Proof line is advisory for now: this does not fail the check.")
    return 1 if red else 0


if __name__ == "__main__":
    sys.exit(main())
