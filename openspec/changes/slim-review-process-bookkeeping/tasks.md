# Tasks

## 1. Writer

- [x] 1.1 `scripts/review_gate.py record`: a `merged` outcome without `--ceiling` exits 0, prints one line, writes nothing (D1); with `--ceiling` unchanged (D2). Update the module docstring.
- [x] 1.2 Tests in `tests/test_review_gate_cli.py`: the two new scenarios; existing tests that used a ceiling-less `merged` record as their vehicle move to a non-`merged` outcome or `--ceiling` without weakening what they assert.
  `test_conflicting_bare_key_refuses_every_subcommand_and_prints_both_records` keeps its ceiling-less `merged` record param (the no-op must still load and refuse); the fold test and the direct `cmd_record` call move to a non-`merged` outcome; the docstring phrase "persisted by the next `record`" becomes "by the next persisting `record`". Add a test that the no-op leaves a missing file missing.

## 2. Memory

- [x] 2.1 Prune `.review-gate-issues.json` by D3 (618 -> 17 issues); kept entries byte-for-byte unchanged; the file still round-trips through the loader.
- [x] 2.2 `tests/test_review_gate_issue_memory.py` mutants that index pruned keys (`1660`, `1736`) retarget kept entries (`1205` / PR `1286` for `closed[0]`, `1205` for `ceilingPrs` / `gateEntries`) with unchanged assertions.

## 3. Instructions

- [x] 3.1 `instructions/agents/shared.md`: the three rules of the proposal (archive in the implementation PR's final push; OpenSpec change only for product behaviour or an existing capability spec; `record` only for a non-`merged` close or a ceiling hit). Remove the "post-merge archive commit" wording.
  Amend the Issue 驱动开发 line: with a change the Evidence Floor is in its `tasks.md`; without one it is the issue body and `openspec validate` is not run. Keep every phrase the instruction tests pin (`tests/test_python_environment_truth.py`, `tests/test_node22_entrypoint_invariant.py`, `tests/test_role_boundary_static.py`); a new line containing `uv run` must start with `-`, `*`, `>` or `#`.
  Update the stale post-merge wording in the `.github/workflows/ci.yml` path-filter comment.
- [x] 3.2 `openspec/project-profile.md`: a short "Workflow overrides" section stating the same three rules for the subagent-workflow.
- [ ] 3.3 Regenerate `CLAUDE.md` and `AGENTS.md` from the sources (orchestrator).

## 4. Archive

- [ ] 4.1 `openspec archive slim-review-process-bookkeeping --yes` in the final push, after E5's `openspec validate`; fill the Purpose if it becomes TBD (orchestrator).

## Evidence Floor

- [x] E1 `uv run pytest -q tests/test_review_gate_cli.py tests/test_review_gate_issue_memory.py` passes.
- [x] E2 On the pruned file: `uv run python scripts/review_gate.py check --issue 1205 --pr 999999` exits 2; `check --issue 1660` exits 0.
- [x] E3 `uv run python scripts/review_gate.py record --issue 3218 --pr 1 --rounds 1 --outcome merged` leaves `git diff --stat .review-gate-issues.json` empty.
- [ ] E4 `CLAUDE.md` / `AGENTS.md` equal header + sources (`tests/test_node22_entrypoint_invariant.py`); `bash .claude/hooks/instruction-guard/instruction-guard.sh check CLAUDE.md AGENTS.md` reports nothing over 307 bytes per line / 32768 bytes per file.
- [ ] E5 `uv run ruff check . --no-cache`; `openspec validate slim-review-process-bookkeeping --strict --no-interactive`; `npx --yes markdownlint-cli2@0.17.2` on the changed markdown outside `openspec/changes`.
- [ ] E6 Every test that reads `instructions/agents/shared.md`, `CLAUDE.md`, `AGENTS.md` or `openspec/project-profile.md` still passes (`git grep -l` them and run those files).
- [x] E7 The pruned `issues` keys equal exactly {1185, 1186, 1205, 1338, 1351, 1368, 1369, 1398, 1571, 1596, 1627, 1980, 2008, 2013, 2035, 2238, 2668}, and each entry equals its `git show c18f131fc:.review-gate-issues.json` entry.
