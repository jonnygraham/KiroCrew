# The optional pre-push gate

`scripts/pre_push_gate.py` runs the cheap CI gates on your diff before you push, prints
every failure at once, and records a proof for the exact tree it checked. It is
**optional and advisory**. Nothing refuses a push because of it: no hook, no `require`
mode in `push_guard.py`, no CI job that fails when its line is missing. CI stays the
authority. A local run cannot reproduce CI on every machine (a tool that is not
installed, another operating system, no network), so a mandatory check would refuse
correct pushes and stall an unattended loop for a cause the author cannot fix there.

## Run it

```bash
python3 $SKILL_DIR/scripts/pre_push_gate.py                       # run the gates
python3 $SKILL_DIR/scripts/pre_push_gate.py --skip GATE=CODE      # do not run one, say why
python3 $SKILL_DIR/scripts/pre_push_gate.py --verify              # is there a proof for HEAD's tree?
```

The gates are in `scripts/gate_map.json`: one entry per CI step it mirrors, with the CI
file and the text of the step. A gate with `paths` runs only when a changed path matches; the patterns name every input of the gate (its source files, its tool config such as `pyproject.toml` and `setup.cfg`, and its baseline file), so a config-only change still runs it;
the others always run. A gate with `base_env` receives the merge base, as CI does. The
`ci_only` entries are checks the runner leaves to CI, each with its reason (needs
`website/node_modules`, needs the network, reads a full test run).
`test/test_gate_map_covers_ci.py` pins the map to the workflows.

## Exit codes

| Code | Meaning |
|---|---|
| 0 | every gate that ran passed; a proof is written (or `--verify` found one) |
| 50 | a gate failed; no proof is written |
| 51 | a gate could not run and no skip covers it, or the working tree has uncommitted changes, or HEAD or the tree changed during the run; no proof is written |
| 52 | `--verify` found no proof for this tree |
| 2 | not a git repository, a bad `--skip`, or an unreadable map |

`diff_signals.py --check-body` returns **25** only when a `Gate proof:` line is in the
body and the recorded proof contradicts it, or no proof backs it. A body with no line
never returns 25.

## Skips carry a code

| Code | Written by |
|---|---|
| `tool-missing:<tool>` | the runner, when a gate's tool or Python module is not installed |
| `unsupported-os` | the runner, when a gate does not run on this system |
| `no-network` | you, with `--skip` |
| `red-on-base:<sha>` | you: the gate is already red on the base commit |
| `other:<30 or more characters>` | you: say what stops the gate here |

A skipped gate is listed in the proof and in the `Gate proof:` line, so a reader sees
it. Failing a gate cannot be skipped away: `--skip` means the gate does not run.

## The proof and its line

Commit first: the gates check the working tree and a proof names a committed tree, so the runner refuses to run while `git status --porcelain` is not empty. Without a merge base with `origin/<base>` it runs every gate, not only those the diff touches.

The proof is `<git-dir>/prepare-pr-gate-proof.json`: `version`, `tree` (HEAD's tree),
`base`, `ran`, `skipped`. It is written only when nothing failed and every gate that did
not run has a code. Any change to the tree, a rebase or an amend included, leaves the
old proof naming a tree that is no longer HEAD's, so it stops counting. Run the tool
again.

The runner prints the line for the PR body, in `## Tests`:

```
Gate proof: tree 3fa91c0d2b7e; ran 21; skipped mypy=tool-missing:mypy
```

## Changing the map

Add a gate when CI adds a check the runner can run on any checkout; add a `ci_only`
entry, with a reason, when it cannot. The pin test fails for a check CI runs that the
map neither runs nor lists, and for a gate whose CI text is gone.
