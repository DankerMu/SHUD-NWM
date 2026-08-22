# Tasks

## 1. Implementation

- [x] T1 In `tests/integration_helpers.py::_clear_issue_126_rows`, put **one**
      `SELECT min(valid_time), max(valid_time)` read in front of the
      `hydro.river_timeseries` DELETE (`:420-428`), reusing that statement's
      existing `run_key IN (SELECT run_key FROM hydro.hydro_run WHERE run_id IN
      (%s, %s))` identity predicate verbatim. When `min` is `NULL`, skip the
      DELETE entirely; otherwise append `AND valid_time >= %s AND valid_time <= %s`.
      No separate existence probe — `min` over zero rows already returns `NULL`
      (design.md D1). Both statements stay **before** the `hydro.hydro_run`
      deletion on the following line, which the subquery depends on.
- [x] T2 Same shape for the `met.forcing_station_timeseries` DELETE (`:434-437`),
      reusing its `forcing_version_id LIKE %s` predicate.
- [x] T3 Do not call `check_batch_targets_uncompressed` (design.md D1). Do not
      import it into this module.
- [x] T4 Leave the other seventeen `cursor.execute` call sites, and the
      statement order, byte-identical.
- [x] T5 Keep the `river_timeseries` DELETE's literal `WHERE run_key IN (`
      substring intact and add no text-identity column. The new probe must use
      the same identity predicate and likewise carry no text-identity column.
- [x] T5b Update the two registers in
      `tests/test_river_ts_text_identity_cleanup.py` that the probe moves, and
      **add** the probe's own shape pin (design.md D4):
      - `RIVER_TABLE_CENSUS["tests/integration_helpers.py"]` `2 -> 3`, and the
        annotation at `:162` extended to name the third statement.
      - `test_integration_helpers_delete_targets_the_run_key` (`:1109-1119`):
        `assert len(statements) == 1` becomes `== 2`; select the DELETE **by
        content**, not by list index, since the probe now precedes it; keep both
        existing assertions on the DELETE; add the same two assertions for the
        probe.
      This edit must be a net gain in assertions. Removing or relaxing any
      existing assertion in that file is out of bounds — stop and report instead.

## 2. Tests

- [x] T6 A recording fake cursor driven through `_clear_issue_126_rows`,
      asserting the emitted statement sequence for the **rows-present** path:
      both hypertable DELETEs carry `valid_time >=` and `valid_time <=`, and the
      bound parameters are the probed min/max.
- [x] T7 Same harness for the **no-rows** path: no `DELETE FROM
      hydro.river_timeseries` and no `DELETE FROM met.forcing_station_timeseries`
      is emitted at all. This is an explicit acceptance criterion of #1640.
- [x] T8 A case where the probed window is wider than
      `VALID_TIME_1`/`VALID_TIME_2`, pinning design.md D2 — the bound follows the
      table, not the fixture constants.

## 3. Verification (Evidence Floor)

- [x] E1 `uv run pytest -q tests/test_river_ts_text_identity_cleanup.py` — green.
      The file **is** edited (T5b); show `git diff` on it and confirm the diff
      only raises counts and adds assertions, deleting none.
- [x] E2 `uv run pytest -q` on the new test module plus
      `tests/test_timescale_write_guard_wire_site_invariant.py`.
- [x] E3 Revert receipt, two parts. (i) Restore either unbounded DELETE and show
      the new bound/skip assertions go red; restore. (ii) Remove the probe but
      leave `RIVER_TABLE_CENSUS` at 3 — the census test must go **red**, proving
      the number tracks the file rather than whatever made the suite pass;
      restore.
- [x] E4 `uv run ruff check .` clean.
- [x] E5 `git diff --stat origin/master...HEAD` — the only non-`openspec/` paths
      are `tests/integration_helpers.py`, the new test module, and
      `tests/test_river_ts_text_identity_cleanup.py` (T5b only).
- [x] E6 `openspec validate teardown-bounded-delete-guarded-hypertables --strict --no-interactive`.
- [ ] E7 **node-27 receipt**, the terminal oracle:
      `NHMS_RUN_INTEGRATION=1 NHMS_INTEGRATION_DATABASE_URL=... uv run pytest -q -m integration
      tests/test_real_database_integration.py tests/test_display_coverage_residual_debt_integration.py`
      — teardown path passes, both the session-scoped and the throwaway-database
      caller. No full suite is run for this change.

## 4. Evidence receipts

- T1/T2 landed in `tests/integration_helpers.py::_clear_issue_126_rows`. One
  `SELECT min(valid_time) AS valid_time_min, max(valid_time) AS valid_time_max`
  per table, reusing each statement's own identity predicate verbatim; the
  DELETE is skipped when `valid_time_min` is `NULL`. Both probes sit before the
  `hydro.hydro_run` deletion. `check_batch_targets_uncompressed` is neither
  called nor imported. The other seventeen `cursor.execute` sites and the
  statement order are byte-identical.
- **Found during implementation, not in the fixture**: the only real caller opens
  its connection with `RealDictCursor`, so `cursor.fetchone()` returns a mapping,
  not a tuple. A positional `row[0]` probe would have raised on node-27 and
  nowhere else. The probes therefore alias the aggregates and read by key, and
  the recording fake returns dict rows so it mirrors the only shape the real
  cursor produces.
- T5b `tests/test_river_ts_text_identity_cleanup.py`: census
  `2 -> 3` with the annotation extended, and
  `test_integration_helpers_delete_targets_the_run_key` now selects by content
  (`DELETE FROM` / `min(valid_time)`) rather than by index. Net **+4** assertions,
  none removed or relaxed.
- E1 `uv run pytest -q tests/test_river_ts_text_identity_cleanup.py` -> `41 passed`.
- E2 new module + wire-site invariant -> `11 passed`; orchestrator re-ran the
  three files together -> `52 passed in 2.63s`.
- E3(i) revert receipt taken for **both** statements, not one: restoring the
  unbounded forcing DELETE -> `4 failed, 2 passed`; restoring the unbounded river
  DELETE -> `3 failed, 3 passed`; each restored -> `6 passed`.
- E3(ii) probe removed with the census left at 3 -> `2 failed, 39 passed`
  (`census says 3`, plus the new shape pin). The number tracks the file rather
  than whatever made the suite pass. Restored -> `41 passed`.
- E4 `uv run ruff check .` -> `All checks passed!`
- E5 non-`openspec/` paths: `tests/integration_helpers.py`,
  `tests/test_integration_helpers_bounded_teardown.py` (new),
  `tests/test_river_ts_text_identity_cleanup.py`.
- E6 `openspec validate ... --strict --no-interactive` -> valid.
- Recorded deviation: the implementer ran one text edit through a bare `python3`
  heredoc before switching back to `uv run python`. Pure string replacement on a
  test file, result verified by the E1 run under `uv`. No impact.
