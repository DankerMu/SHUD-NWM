## Why

Batch J2 (by explicit user instruction) covers four issues. Two things changed after the issues were written, and both change how they must be handled:

- The installed `subagent-workflow` skill (2026-09-22) ships only `fix_gate.py` and `run_unit_tests.py`. The old `review_gate.py`, `evidence_check.py` and `loop_log_audit.py` are gone.
- Nothing in the skill reads or writes `.review-gate-issues.json` (last write 2026-09-18, max issue 2472) or `docs/review-loop-log.jsonl` (frozen at 690 lines, last row 2026-09-18).

The cross-PR round-ceiling memory and the loop audit are therefore write-dead and read-dead, not merely buggy.

- **#2477**: `tests/test_loop_log_audit_attribution.py` module-level-skips in every checkout and CI run since `002ba4b59`. The #2036-fixed audit exists only in git history (`002ba4b59^`). ADR 0003 says twice that keep/cut should wait for #2477 to land and for the ratio to be recomputed.
- **#2261**: the committed memory was repaired by PR #2469. The residual acceptance items (a write-time structure guard, an in-vocabulary close outcome, a verdict hook, workflow wording) all targeted an untracked writer that no longer exists. That leaves the memory with no writer and no escalation reader.
- **#2533**: live requirements in three specs still name deleted monolith suites and a stale importer count (8, measured 11).
- **#2044**: needs a written ruling on "a cancelled full run leaves master unverified".

## What Changes

- **#2477**: restore the #2036 `loop_log_audit.py` from `002ba4b59^` as tracked `scripts/governance/loop_log_audit.py`.
  - Extract the `round_lenses` shape check (the only evidence-checker behaviour the suite needs) as a pure function there.
  - Retarget the suite to a plain import, so it runs with zero skips.
  - Route the script and `docs/review-loop-log.jsonl` to the suite: a selector rule plus a `ci.yml` backend exact literal for the log.
  - Run the audit once over the frozen log and append a Revisit to ADR 0003.
- **#2261**: add tracked `scripts/review_gate.py`, a small stdlib CLI over `.review-gate-issues.json` only:
  - `record` writes under `issues`, restricts `--outcome` to the four-value vocabulary, and loads through a structure guard that folds an unambiguous bare top-level key and exits nonzero on a conflicting one.
  - `check` is the cross-PR escalation read that `fix_gate.py` no longer does.
  - The existing guard test imports the CLI's vocabulary instead of copying it.
  - The invocation is documented in the tracked instruction source `instructions/agents/shared.md`, and `CLAUDE.md` / `AGENTS.md` are regenerated from it.
  - First real use: J1 (PR #2606) is recorded through the CLI.
- **#2533**: MODIFIED blocks restate every live requirement that names a deleted suite, using measured selections and derived importer counts.
- **#2044**: a written ruling only (in the PR body and on the issue); no workflow change.

## Impact

- **Specs**:
  - new capability `review-governance-records` (the two tracked tools);
  - `ci-contract-baseline`: ADDED routing requirements, plus MODIFIED requirements for #2533;
  - `orchestrator-structural-burndown` and `real-integration-test-matrix`: MODIFIED for #2533.
- **Code**:
  - new `scripts/review_gate.py` and `scripts/governance/loop_log_audit.py`;
  - `tests/test_loop_log_audit_attribution.py`, `tests/test_review_gate_issue_memory.py`, new `tests/test_review_gate_cli.py`;
  - `scripts/select_ci_tests.py`, `tests/test_select_ci_tests.py`, one `ci.yml` filter literal;
  - `instructions/agents/shared.md` with `CLAUDE.md` / `AGENTS.md` regenerated;
  - `docs/adr/0003-review-lens-rotation-keep.md`, `.review-gate-issues.json` (the J1 record).
- **Downstream**: the K batch (#2460 #2490 #2527 #2532) rebases on J1 and J2, which both touch the selector.
