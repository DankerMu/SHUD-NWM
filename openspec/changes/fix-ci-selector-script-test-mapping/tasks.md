# Tasks: fix-ci-selector-script-test-mapping

Fixture level: compact · Repair intensity: light · Issue #1191

Triage note: single-file tooling change + tests, fully locally verifiable.
Risk axes: (1) the same-name rule must not DOWNGRADE any currently-selected
path — for every path currently matched by an explicit rule or the test
self-selection branch, the selected set must be a superset-or-equal of
today's; (2) fallback semantics: a same-name hit must suppress the
CORE_SMOKE fallback for that path, but paths with neither rule nor
same-name test must keep today's fallback exactly; (3) the completeness
guard must be mechanized (derive the pair list from the filesystem, not a
frozen literal list that re-creates the drift problem). Single review round.

Must preserve:
- Existing 22 selector tests green unmodified (unless an assertion pins the
  orphan behavior itself — none known; deviations reported).
- `--github-output` protocol, CHANGED_TEST_FILE_RULES, `_test_target_exists`
  silent-drop semantics (out of scope to change).
- Selector output for: changed test files, explicit-rule paths (pin the
  currently-uncovered different-named rule
  `scripts/validate_readonly_db_boundary.py` →
  `tests/test_readonly_db_validation.py` as the control), non-backend
  paths, unknown five-prefix `.py` without same-name test (still
  CORE_SMOKE).
- Same-name derivation applies to `scripts/**/*.py` ONLY. Other backend
  prefixes keep today's behavior even when a same-name test exists (13
  such paths today, e.g. `packages/common/state_qc.py` → CORE_SMOKE);
  pinned by a scope-guard case.

Must add:
- Same-name mapping with KNOWN semantics (issue acceptance: the 21 orphan
  pairs become reachable; verified via the completeness guard).
- Mechanized completeness guard test.
- Behavior cases per proposal item 2a.

## Implementation tasks

- [x] 1. Implement the same-name mapping in `scripts/select_ci_tests.py`
  per proposal; keep the diff minimal and inside the selection layer.
- [x] 2. Add tests per proposal item 2 to `tests/test_select_ci_tests.py`.
- [x] 3. Red proof (scratch copy, no git stash): (a) revert the selector
  change with new tests present → same-name behavior cases + completeness
  guard fail; (b) mutate the same-name rule to also mark the path known
  when the test file does NOT exist → the fallback-preserved case fails
  (probe path must be a script with neither explicit rule nor same-name
  test — a `validate_*` path would stay green since `matched` is already
  true); (c) mutate the rule to append the same-name test WITHOUT marking
  the path known → completeness guard's no-CORE_SMOKE clause fails.
  Record outputs.
- [x] 4. Oracle: `uv run pytest -q tests/test_select_ci_tests.py` green
  (22 baseline + new); CLI-level repro flips:
  `printf 'scripts/scheduler_state_index_copyback_replay.py\n' | uv run
  python scripts/select_ci_tests.py --repo-root .` now emits
  `tests/test_scheduler_state_index_copyback_replay.py` (not CORE_SMOKE);
  `uv run ruff check .`; `openspec validate
  fix-ci-selector-script-test-mapping --strict --no-interactive`.

## Required evidence

- Red-proof outputs for (a)/(b)/(c)
- Before/after CLI selector output for the repro path + one explicit-rule
  control path (unchanged)
- pytest counts, ruff, openspec validate outputs

## Non-goals

- #1182 fallback policy; #1138 .sh filter域; gate strength; CORE_SMOKE cost.
