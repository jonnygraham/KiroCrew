#!/usr/bin/env python3
"""PR Hygiene: a fix or revert must list the other places with the same defect.

A fix that repairs one site and leaves its siblings is the commonest way a bug
comes back (`partial_fix` is 25% of fix pull requests' review loops). The PR
body therefore carries a `## Sibling sweep` section, directly before `## Pattern
harvest`, in one of two shapes:

    ## Sibling sweep

    Swept: <optional: the search you ran>
    Sibling: <path>[:<line>] -- changed here | tracked in #<n> | not affected -- <why>

or, when the defect has no second site:

    Siblings: none -- <why, 8+ characters>

Checks, none needing a network call or a model:

1. the heading exists (outside fences and HTML comments);
2. at least one `Sibling:` line, or one `Siblings: none -- <why>` line, and not both;
3. every `Sibling:` line parses; `not affected` and `none` carry 8+ characters of why;
4. a `changed here` path is in the pull request's changed-file list;
5. every path exists in the head tree or the diff (stops invented paths);
6. no duplicate path, and at most MAX_LINES `Sibling:` lines.

LIMIT, stated plainly: this checks that the list is present and well formed. It
does not prove the list is complete, that "not affected" is true, or that `#N`
exists. Judging that is the reviewers' job (like `pr-description-check.sh`, which
verifies that parts are present, not that they are well written).

Titles other than `fix` / `revert` are not checked. The section is NOT one of
`pr-description-check.sh`'s REQUIRED_SECTIONS: that list is matched on every open
pull request, and a copy outside the repo must match it.

Usage:  sibling_list_check.py            (inputs come from the environment)

Environment (read only as data, never interpolated into code):
    PR_TITLE        the pull request title
    PR_BODY         the pull request body
    MERGE_BASE      merge-base commit of the pull request
    HEAD_SHA        head commit of the pull request
    PR_CREATED_AT   optional ISO-8601 time the pull request was opened
    GITHUB_ACTIONS  when "true", findings print as workflow annotations

Mode: WARN until ENFORCE_FROM is set. A finding prints a warning and exits 0, so
no open fix pull request turns red for a section it never had. Once ENFORCE_FROM
holds a date (YYYY-MM-DD), a pull request opened on or after it exits 1 on a
finding; one opened before it keeps warning.

Exit: 0 pass, or findings in warn mode | 1 findings in enforce mode | 2 environment error
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
from datetime import date, datetime, timezone

# None = warn mode. Set to "YYYY-MM-DD" to turn the check red for pull requests
# opened on or after that date (RFC: review loop and escaped-bug reduction, D3).
ENFORCE_FROM: str | None = None

TITLE_RE = re.compile(r"^(fix|revert)([(!:]|$)", re.IGNORECASE)
HEADING_RE = re.compile(r"^ {0,3}(#{1,6})( +.*|)$")
SECTION_RE = re.compile(r"^ {0,3}(#{2,}) +Sibling sweep\b", re.IGNORECASE)
FENCE_RE = re.compile(r"^ {0,3}(`{3,}|~{3,})")
COMMENT_RE = re.compile(r"<!--.*?-->", re.DOTALL)

MIN_WHY = 8
MAX_LINES = 40

BULLET = r"(?:[-*] +)?"
NONE_RE = re.compile(BULLET + r"Siblings *: *none\b *(?:-- *(?P<why>.*))?$", re.IGNORECASE)
SIBLING_PREFIX_RE = re.compile(BULLET + r"Siblings? *:", re.IGNORECASE)
SIBLING_RE = re.compile(
    BULLET + r"Sibling: +(?P<path>[^\s:]+)(?::(?P<line>\d+))? +-- +(?P<rest>\S.*)$"
)
TRACKED_RE = re.compile(r"tracked in #\d+$")
NOT_AFFECTED_RE = re.compile(r"not affected +-- +(?P<why>\S.*)$")


def strip_hidden(text: str) -> str:
    """Remove HTML comments and fenced blocks, so template help text never counts."""
    text = COMMENT_RE.sub("", text)
    kept: list[str] = []
    fence: tuple[str, int] | None = None  # (character, opening length)
    for line in text.splitlines():
        m = FENCE_RE.match(line)
        if fence is None:
            if m:
                fence = (m.group(1)[0], len(m.group(1)))
                continue
            kept.append(line)
        elif (
            m
            and m.group(1)[0] == fence[0]
            and len(m.group(1)) >= fence[1]
            and not line.strip().strip(fence[0])
        ):
            fence = None
    return "\n".join(kept)


def sweep_section(body: str) -> list[str] | None:
    """Lines of the `## Sibling sweep` section, or None when the heading is absent."""
    lines = strip_hidden(body).splitlines()
    for i, line in enumerate(lines):
        m = SECTION_RE.match(line)
        if not m:
            continue
        level = len(m.group(1))
        out: list[str] = []
        for nxt in lines[i + 1 :]:
            h = HEADING_RE.match(nxt)
            if h and len(h.group(1)) <= level:
                break
            out.append(nxt.strip())
        return out
    return None


def is_in_scope(title: str) -> bool:
    return bool(TITLE_RE.match(title.strip()))


def check(title: str, body: str, changed: set[str], exists) -> list[str]:
    """Findings for one pull request; an empty list passes.

    ``changed`` is the set of paths in the diff (deleted ones included) and
    ``exists(path)`` says whether a path is in the head tree.
    """
    if not is_in_scope(title):
        return []
    lines = sweep_section(body)
    if lines is None:
        return [
            "fix/revert pull requests must carry a '## Sibling sweep' section "
            "(before '## Pattern harvest'): list every other place with the same defect "
            "as 'Sibling: <path>[:<line>] -- changed here | tracked in #<n> | not affected "
            "-- <why>', or write 'Siblings: none -- <why the defect has no second site>'"
        ]

    problems: list[str] = []
    siblings: list[tuple[str, str]] = []  # (path, disposition)
    none_lines = 0
    for line in lines:
        if not line:
            continue
        none = NONE_RE.match(line)
        if none:
            none_lines += 1
            why = (none.group("why") or "").strip()
            if len(why) < MIN_WHY:
                problems.append(
                    f"'{line}': 'Siblings: none' needs a reason of {MIN_WHY}+ characters "
                    "after '--'"
                )
            continue
        if not SIBLING_PREFIX_RE.match(line):
            continue  # Swept: lines and prose are free text.
        m = SIBLING_RE.match(line)
        if not m:
            problems.append(
                f"'{line}' does not parse: write 'Sibling: <path>[:<line>] -- changed here | "
                "tracked in #<n> | not affected -- <why>'"
            )
            continue
        rest = m.group("rest").strip()
        if rest == "changed here":
            disposition = "changed"
        elif TRACKED_RE.match(rest):
            disposition = "tracked"
        else:
            na = NOT_AFFECTED_RE.match(rest)
            if not na:
                problems.append(
                    f"'{line}': after '--' write 'changed here', 'tracked in #<n>' or "
                    f"'not affected -- <why>'"
                )
                continue
            if len(na.group("why").strip()) < MIN_WHY:
                problems.append(f"'{line}': 'not affected' needs a reason of {MIN_WHY}+ characters")
                continue
            disposition = "not affected"
        siblings.append((m.group("path"), disposition))

    if not siblings and not none_lines:
        problems.append(
            "the '## Sibling sweep' section has no 'Sibling:' line and no "
            "'Siblings: none -- <why>' line"
        )
    if siblings and none_lines:
        problems.append("'Siblings: none' contradicts the 'Sibling:' lines; keep one")
    if none_lines > 1:
        problems.append("more than one 'Siblings: none' line")
    if len(siblings) > MAX_LINES:
        problems.append(
            f"{len(siblings)} 'Sibling:' lines (limit {MAX_LINES}): the pattern is too broad; "
            "name the search in 'Swept:' and list the sites that matter"
        )

    seen: set[str] = set()
    for path, disposition in siblings:
        if path in seen:
            problems.append(f"'{path}' is listed twice")
            continue
        seen.add(path)
        if path.startswith("/") or ".." in path.split("/"):
            problems.append(f"'{path}' must be a repo-relative path")
            continue
        if path not in changed and not exists(path):
            problems.append(f"'{path}' is not in the head tree or the diff")
            continue
        if disposition == "changed" and path not in changed:
            problems.append(f"'{path}' is marked 'changed here' but is not in the diff")
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


def _git(args: list[str]) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", *args], capture_output=True, text=True, encoding="utf-8", errors="replace"
    )


def changed_paths(merge_base: str, head: str) -> set[str]:
    r = _git(["diff", "--name-only", "--no-renames", merge_base, head])
    if r.returncode != 0:
        raise RuntimeError(f"git diff failed: {r.stderr.strip()}")
    return {p for p in r.stdout.splitlines() if p}


def main() -> int:
    title = os.environ.get("PR_TITLE", "")
    body = os.environ.get("PR_BODY", "")
    merge_base = os.environ.get("MERGE_BASE", "")
    head = os.environ.get("HEAD_SHA", "")
    if not is_in_scope(title):
        print("Not a fix/revert pull request; Sibling sweep section not required.")
        return 0
    if not merge_base or not head:
        print("sibling_list_check: MERGE_BASE and HEAD_SHA are required", file=sys.stderr)
        return 2
    try:
        changed = changed_paths(merge_base, head)
    except (RuntimeError, OSError) as exc:
        print(f"sibling_list_check: {exc}", file=sys.stderr)
        return 2

    def exists(path: str) -> bool:
        return _git(["cat-file", "-e", f"{head}:{path}"]).returncode == 0

    problems = check(title, body, changed, exists)
    if not problems:
        print("Sibling sweep section present.")
        return 0
    try:
        red = enforced(os.environ.get("PR_CREATED_AT"))
    except ValueError as exc:
        print(f"sibling_list_check: bad date ({exc})", file=sys.stderr)
        return 2
    level = "error" if red else "warning"
    in_ci = os.environ.get("GITHUB_ACTIONS") == "true"
    for problem in problems:
        if in_ci:
            print(f"::{level}::Sibling sweep: {problem}")
        else:
            print(f"SIBLING SWEEP ({level}): {problem}")
    if not red:
        print("Sibling sweep is advisory for now: this does not fail the check.")
    return 1 if red else 0


if __name__ == "__main__":
    sys.exit(main())
