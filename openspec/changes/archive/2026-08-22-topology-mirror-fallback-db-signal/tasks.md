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

- [x] T10 (round 2 fix) Generalize `_topology_line_mentions_rollback` to the
      whole lexeme — `roll back`, `rolled back`, `rolling back`, `rolls back`,
      hyphenated and spaced — plus `回退` alongside `回滚`. design.md D3/D10.
- [x] T11 (round 2 fix) Extend the fallback's own vocabulary with the standard
      database-role nouns: `replica`, `secondary`, `从库`, `备库`, `生产库`
      (`主库` was proposed and removed in round 3 as dead — D10). Still at the call site; `_topology_mentions_database` stays
      untouched. design.md D9/D10.
- [x] T12 (round 2 fix) Extend the must-still-flag test with the six round-2
      lines quoted in design.md D1.

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
- [x] E10 (round 2 fix) Whole-repo audit after T10+T11 still reports **0**
      `production-topology-*` findings and total **753** — the widenings must be
      free. Plus a predicate-level check that none of the four real findings is
      resurrected, and a revert receipt for each of the two new legs.
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
- E5 `git diff --stat` at the round-0 SHA `2d842981` -> only
  `scripts/governance/audit_repo_entropy.py` (+26/-1) and
  `tests/test_entropy_audit_script.py` (+89). **Cumulative final total** across
  all three rounds, measured on the merge base: `audit_repo_entropy.py` +52/-1
  and `test_entropy_audit_script.py` +117/-0. The per-round numbers in §4 and §5
  are deltas, not totals; this line is the total.
- E6 `openspec validate topology-mirror-fallback-db-signal --strict --no-interactive` -> valid.
- E7 `uv run pytest -q` -> `13 failed, 13499 passed, 201 skipped in 2812.39s`.
  Nothing attributable to this change: 12 are `tests/test_state_clone_recalibration_cli.py`
  (#1743, a fixture date that expired at 2026-08-22T12:00Z), and 1 is
  `tests/test_integration_gate.py::test_integration_database_name_uses_high_entropy_uuid`,
  which passes in isolation (`4 passed`) and is a probabilistic assertion, routed
  separately.

## 4. Round-1 fix receipts (D9)

- T8 landed in the fallback call site of
  `_topology_line_has_node22_local_postgres_or_mirror_drift` in
  `scripts/governance/audit_repo_entropy.py` (the `return any(token in stripped ...)`
  block; two later fix rounds shifted its absolute position twice, so it is cited
  by symbol here rather than by line). `_topology_mentions_database`, `_topology_line_mentions_mirror`,
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
- E9d `git diff --stat` -> `audit_repo_entropy.py` (+7/-1),
  `test_entropy_audit_script.py` (+6), plus this change's own fixture.

## 5. Round-2 fix receipts (D10)

- T10 `_topology_line_mentions_rollback` (`audit_repo_entropy.py:2005-2010`) now
  matches the whole lexeme via `roll(?:ed|ing|s)?[\s-]?back|回滚|回退`. The
  helper has exactly one caller (the fallback leg at `:1941`), verified, so the
  widening does not leak into any other check.
- T11 fallback token tuple (`:1946-1965`) gains `replica`, `secondary`, `从库`,
  `备库`, `生产库` (`主库` proposed here and removed in round 3 as dead, see §6).
  `_topology_mentions_database`,
  `_topology_line_mentions_mirror`, `_topology_local_postgres_context_is_allowed`
  and the three positive branches are byte-unchanged.
- E10a whole-repo hard gate: `total 753 / topology 0`, identical to the pre-fix
  baseline — **both widenings are free**, and design.md's six new drift quotes
  do not self-trigger.
- E10b the four real findings, read verbatim from disk and passed to the
  predicate: all four `False`. No false positive is resurrected.
- E10c two independent revert receipts, one per leg: restoring the old
  three-token rollback body turns exactly the three rollback cases red
  (`3 failed, 19 passed`); stubbing the T11 tokens turns exactly the three
  replication cases red (`3 failed, 19 passed`). The red sets are disjoint and
  the four must-not-flag cases stay green throughout. Restoring each returns
  `22 passed`, and the restored file diffs byte-identical to the measured state.
- E10d `uv run pytest -q tests/test_entropy_audit_script.py` -> `373 passed in 285.88s`
  (367 -> 373).
- E10e `uv run ruff check .` -> `All checks passed!`
- E10f `git diff --stat` -> `audit_repo_entropy.py` (+29/-6),
  `test_entropy_audit_script.py` (+12), plus this change's own fixture.
- Recorded deviation: the two red receipts were run against the two topology
  parametrized functions (22 cases) rather than the whole 373-case file. The
  helper's single-caller property was verified first and both stubs only
  *narrow* the predicate, so no new finding is reachable; the full-file 373 pass
  is recorded separately at the final state.

## 6. Round-3 fix receipts

Round 3 returned no P0/P1. Two P2s were fixed as ride-alongs rather than
deferred, because one of them made a decision record false; four P3s were
citation staleness in this fixture and were corrected by the orchestrator.

- F1 `主库` removed from the fallback token tuple. It is already in
  `_topology_mentions_database`'s list, and the tuple is only reached after that
  helper returns False, so the entry could never decide anything. Nine tokens
  remain. Behavior pinned by a new must-still-flag case at the behavior level.
- F2 `_topology_line_mentions_rollback` gains a **left** word boundary:
  `\broll(?:ed|ing|s)?[\s-]?back|回滚|回退`. `scrollback`, `payrollbacklog`,
  `controllback` no longer match; `rollbacks` still does, which is why there is
  no right boundary. Pinned by a new must-not-flag case.
- G1 whole-repo hard gate pre and post: `total 753 / topology 0` both times.
- G2 the four real findings, read verbatim from disk: all four still `False`.
- G3 revert receipts. (i) Putting `主库` back leaves the new 主库 test **green** —
  which is the proof that the token was dead, since the database leg returns
  before the tuple is reached. (ii) Dropping the `\b` turns exactly the new
  scrollback case red (`1 failed, 374 passed`) and nothing else.
- G4 `uv run pytest -q tests/test_entropy_audit_script.py` -> `375 passed in 284.86s`
  (373 -> 375).
- G5 `uv run ruff check .` -> `All checks passed!`
- G6 `git diff --stat` -> the same two non-`openspec/` files.
- Recorded deviation: the implementer added decision comments not asked for in
  the brief, matching the `#1707 D3/D9/D10` commentary style already in the file.
  Behavior unaffected, ruff clean, and the 主库 test is otherwise inexplicable to
  a future reader since it pins behavior no longer routed through the token it
  names.
