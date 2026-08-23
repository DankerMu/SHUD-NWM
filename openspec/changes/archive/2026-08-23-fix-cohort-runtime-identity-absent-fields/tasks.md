# Tasks: fix-cohort-runtime-identity-absent-fields

## 1. Validator degrade + regression tests

- [x] 1.1 Rework the member-vs-`hydro_run` comparison in
  `services/orchestrator/file_orchestration_journal.py::forecast_cohort_runtime_identity_matches`:
  keep `run_id`/`model_id`/`scenario_id` (and the row-level
  `source_id`/`cycle_time`/`submission_attempt`) strict; for
  `candidate_id`/`basin_id`/`array_task_id` skip the comparison iff the
  `hydro_run` value is `None`, compare strictly otherwise.
  Evidence floor: new regression tests pass; existing
  `tests/test_gateway_reconcile.py` cohort suites stay green.

- [x] 1.2 Add regression test: cohort whose `hydro_run` rows carry `None`
  for all three degradable fields (production writer shape) reconciles to
  `terminal`/`succeeded` with reconciliation decision `matched_bound`.
  Evidence floor: test fails on pre-change code, passes post-change.

- [x] 1.3 Add regression test(s): a `hydro_run` row with a
  present-but-different `candidate_id`, `basin_id`, or `array_task_id`
  stays `identity_mismatch_blocked` with `durable_write_count == 0` and an
  unchanged master row.
  Evidence floor: parametrized over the three fields, all pass.

> **Superseded at head (2026-08-23, issue #1749).** Tasks 1.1 and 1.3 name
> three degradable fields and a three-way parametrization. Both were satisfied
> as written when they were checked off. At head there are two:
> `scope-cohort-identity-off-array-layout` removed the `array_task_id`
> comparison outright (it is a per-submission layout index, not an identity),
> so
> `tests/test_gateway_reconcile.py::test_file_cohort_present_but_different_runtime_identity_still_blocks`
> parametrizes over `candidate_id` and `basin_id` only.


## 2. Change-level verification floor

- [x] 2.1 `openspec validate fix-cohort-runtime-identity-absent-fields
  --strict --no-interactive` PASS.
- [x] 2.2 `uv run ruff check .` PASS.
- [x] 2.3 `uv run pytest -q tests/test_gateway_reconcile.py` PASS including
  the new regression tests.
