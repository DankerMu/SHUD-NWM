# Tasks

## 1. Implementation

- [x] T1 Add a line-scoped rollback predicate to
      `scripts/governance/audit_repo_entropy.py` recognizing `rollback`,
      `roll-back`, and `回滚`.
- [x] T2 Add a line-scoped helper that **removes** explicit "there is no
      database here" assertions from the text before any database signal is
      measured: `db-free`, `db free`, `dbfree`, `no db handle`,
      `takes no db handle`, `无 db`, `不取任何 db`, `无数据库`, and the
      postgres/postgresql/database spellings of the same. It returns the text
      with those phrases stripped, not a boolean — see design.md D1 for why a
      boolean exclusion is wrong.
- [x] T3 Rewrite only the final `return` of
      `_topology_line_has_node22_local_postgres_or_mirror_drift`
      (`audit_repo_entropy.py:1936`): keep the existing two-term conjunction as
      a precondition, then return True on rollback wording, else return whether
      `_topology_mentions_database` holds on the DB-absence-stripped text. Leave
      the three branches above it byte-identical.
- [x] T4 Do not modify `_topology_line_mentions_mirror` or
      `_topology_mentions_database`; add no entry to
      `_topology_local_postgres_context_is_allowed`.
- [x] T5 Must-not-flag tests using the four real incident lines **verbatim**,
      inlined as literal strings so the tests survive those files being
      reformatted.
- [x] T6 Must-still-flag tests: the archived port; local-PostgreSQL wording; a
      DSN token paired with a mirror in both spellings; bare rollback wording;
      the fused-clause line from design.md D1 where a real database token and an
      unrelated DB-absence claim share one line; **and** a line carrying rollback
      wording together with a DB-absence assertion, which must still flag via the
      rollback leg (design.md D3, and the second half of the ADDED requirement's
      third scenario).
- [x] T7 Leave the four flagged source files untouched.
- [x] T8 (round 1 fix) Add the fallback's own database vocabulary at the call
      site — compound `本地库`/`本机库`, plus `standby` and `instance` — measured
      on the same DB-absence-stripped text as the existing token leg. Do **not**
      widen `_topology_mentions_database`, and do not use a bare `库`. See
      design.md D9.
- [x] T9 (round 1 fix) Extend the must-still-flag test with the three recovered
      drift lines quoted in design.md D1. The fourth constructed line is
      deliberately not pinned in either direction (D9).

## 2. Verification (Evidence Floor)

- [x] E1 Before/after whole-repo audit, run with cwd inside the worktree under
      test and the cwd stated in the receipt:
      `uv run python scripts/governance/audit_repo_entropy.py --mode hard-gate --format json`
      — this check goes 4 -> 0, **counting this change's own fixture directory**,
      and no other `check_id`'s finding count changes.
- [x] E2 `uv run pytest -q tests/test_entropy_audit_script.py` — full transcript,
      including the previously-red
      `test_entropy_audit_current_repo_hard_gate_has_zero_production_topology_findings`.
- [x] E3 Revert receipt: restore the old one-line fallback; the new
      must-not-flag tests go **red**; restore the fix; `git status` clean.
- [x] E4 `uv run ruff check .` clean.
- [x] E5 `git diff --stat origin/master...HEAD` — the only non-`openspec/` paths
      are `scripts/governance/audit_repo_entropy.py` and
      `tests/test_entropy_audit_script.py`.
- [x] E6 `openspec validate topology-mirror-fallback-db-signal --strict --no-interactive`.
- [x] E8 (round 1 fix) Re-run E1 after T8: whole-repo audit still reports **0**
      `production-topology-*` findings and the same total finding count as the
      pre-T8 HEAD — the widening must add nothing.
- [x] E9 (round 1 fix) Re-run E2, E3, E4, E6.
- [x] E7 `uv run pytest -q` full local suite — the audit script is imported by
      more than one test module; confirm nothing else moved.

## 3. Evidence receipts

All commands run with cwd `/Users/danker/Desktop/Hydro-SHUD/NWM/.claude/worktrees/pr-1286-subagent-workflow-7fb9ee`
(the script roots at `Path.cwd()`).

- E1 whole-repo audit, `production-topology-*` per check_id:

  | check_id | before | after |
  |---|---|---|
  | `production-topology-node22-local-postgres` | 4 | **0** |
  | `production-topology-node22-db-writer` | 1 (fixture-inflicted, D7) | **0** |
  | every other check_id | unchanged | unchanged |

  before-baseline JSON kept at `.workplans/pr-1707/review/audit-before-master-ba783bd1.json`.
- E2 `uv run pytest -q tests/test_entropy_audit_script.py` -> `364 passed in 281.43s`.
  `test_entropy_audit_current_repo_hard_gate_has_zero_production_topology_findings`,
  red on master since #1662, is green.
- E3 revert receipt, run twice independently (implementer, then orchestrator):
  restoring the old one-line fallback turns all four must-not-flag cases red
  (`4 failed, 13 passed`) while every must-still-flag case stays green;
  restoring the implementation returns `13 passed`.
- E4 `uv run ruff check .` -> `All checks passed!`
- E5 `git diff --stat` -> only `scripts/governance/audit_repo_entropy.py` (+26/-1)
  and `tests/test_entropy_audit_script.py` (+89).
- E6 `openspec validate topology-mirror-fallback-db-signal --strict --no-interactive` -> valid.
- E7 `uv run pytest -q` -> `13 failed, 13499 passed, 201 skipped in 2812.39s`.
  Nothing attributable to this change: 12 are `tests/test_state_clone_recalibration_cli.py`
  (#1743, a fixture date that expired at 2026-08-22T12:00Z), and 1 is
  `tests/test_integration_gate.py::test_integration_database_name_uses_high_entropy_uuid`,
  which passes in isolation (`4 passed`) and is a probabilistic assertion, routed
  separately.

## 4. Round-1 fix receipts (D9)

- T8 landed at `scripts/governance/audit_repo_entropy.py:1943-1949`, fallback call
  site only. `_topology_mentions_database`, `_topology_line_mentions_mirror`,
  `_topology_local_postgres_context_is_allowed` and the three positive branches
  are byte-unchanged.
- E8 whole-repo hard-gate audit, pre-fix vs post-fix: `total 753 / topology 0`
  both times. The widening adds nothing, and design.md's new quotes do not
  self-trigger.
- E9a `uv run pytest -q tests/test_entropy_audit_script.py` -> `367 passed in 275.39s`
  (was 364; +3 must-still-flag cases).
- E9b revert receipt: replacing only the new token check with `return False`
  turns exactly the three new cases red (`3 failed, 13 passed`) while all four
  must-not-flag cases stay green; restoring returns `16 passed`.
- E9c `uv run ruff check .` -> `All checks passed!`
- E9d `git diff --stat` -> `audit_repo_entropy.py` (+8/-1),
  `test_entropy_audit_script.py` (+6), plus this change's own fixture.
