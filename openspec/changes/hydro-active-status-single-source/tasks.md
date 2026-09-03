# Tasks

Fixture level: expanded · repair intensity: standard · origin: PR #1995 residuals (no new issue by user instruction).
Line cites against `origin/master` `d7fe213b`; symbol names are authoritative.

## 0. Evidence Floor

Oracles: local pytest (macOS) for red/green, greps and the scratch-dir sweep proofs; node-27 for the real-DB test (D3) and the same suites at the final head; CI status read at every head.

- [ ] Red proof (A, SQL): with the aliases in place and `tests/test_orchestration_chain.py:6679` unedited, `test_psycopg_has_active_pipeline_includes_queued_pipeline_rows` fails on the five-member parameter; passes after the assertion gains `"pending"`
- [ ] Red proof (A, parity): the old `test_active_hydro_status_divergence_is_locked_not_adjudicated` fails against the aliased modules before it is replaced
- [ ] Red proof (A, journal): the three task-1.8 pins fail at `d7fe213b` (`has_active_pipeline` → `False`; `permit_pipeline_job_retry` leaves the `pending` row untouched; `project_forecast_cohort_tasks` leaves it untouched) and pass at the final head
- [ ] Red proof (A, real DB, node-27): the D3 test returns `False` at `d7fe213b` and `True` at the final head (`/home/nwm/tmp/` log cited in the PR body)
- [ ] Red proof (B): a scratch copy of the reducer with `include_direct_jobs: bool = True` re-added turns the signature pin red
- [ ] Red proof (C): scratch migrations dir (copy of `db/migrations` + probe file) with `ALTER TYPE hydro."run_status" ADD VALUE IF NOT EXISTS 'complete'` — old regex green, new regex red on the enum assertion; scratch `ALTER TYPE hydro.run_status RENAME VALUE 'succeeded' TO 'done'` — old sweep green, new sweep fails closed naming the file
- [ ] Identity: `chain.ACTIVE_HYDRO_STATUSES is chain_repository.ACTIVE_HYDRO_STATUSES is file_orchestration_journal.ACTIVE_HYDRO_STATUSES is scheduler_state_types.ACTIVE_HYDRO_STATUSES` and the value is `{"created","staged","pending","submitted","running"}`
- [ ] `grep -rn 'ACTIVE_HYDRO_STATUSES = {' services/` → exactly one hit (`scheduler_state_types.py`); `grep -n 'ACTIVE_HYDRO_STATUSES' services/orchestrator/file_orchestration_journal.py` shows the import from `scheduler_state_types`
- [ ] `uv run pytest -q tests/test_hydro_status_set_parity.py tests/test_orchestration_chain.py tests/test_file_orchestration_journal.py tests/test_production_scheduler.py tests/test_retry.py tests/test_retry_cancel_consistency.py tests/test_scheduler_backfill_predecessor.py tests/test_orchestrator_demote_reclaim_lifecycle.py` green locally (counts in the PR body)
- [ ] `uv run pytest -q tests/test_select_ci_tests.py` green (no selector edit expected: `chain.py` / `chain_repository.py` / the journal already route to the parity suite; `tests/test_real_database_integration.py` already exists)
- [ ] `uv run ruff check .` clean; `openspec validate hydro-active-status-single-source --strict --no-interactive` valid
- [ ] node-27 receipt at the final head: the suites above plus `tests/test_real_database_integration.py -k pending` (real DB; command lines + counts in the PR body)

## 1. A — single definition

- [ ] 1.1 `scheduler_state_types.py:29-32`: replace the UNADJUDICATED comment with the ruling (D1: `pending` = retry submitted = in flight; SQL probe and journal aligned)
- [ ] 1.2 `chain.py:208-210`: `ACTIVE_HYDRO_STATUSES = ACTIVE_HYDRO_STATUSES` imported from `scheduler_state_types` (same object), comment states it has no in-module consumer and is kept as an importable name
- [ ] 1.3 `chain_repository.py:19-21`: same alias; comment states the hydro arm of `has_active_pipeline` (`:93`) now includes `pending` and why, and that the journal probe's terminal-completion suppression (#1472) is not mirrored
- [ ] 1.4 `tests/test_orchestration_chain.py:6679`: the parameter set gains `"pending"`; docstring/comment names the requirement
- [ ] 1.5 `tests/test_hydro_status_set_parity.py`: divergence test replaced by the four-site identity + value test; module docstring updated; the enum-membership test keeps its `<= enum_members` lines (add the journal site)
- [ ] 1.6 `tests/test_real_database_integration.py`: D3 test on `throwaway_database_url` — `apply_migrations_from_zero`, `seed_issue_126_data`, set `FORECAST_RUN_ID` to `pending`, delete `it126_forecast_job` (assert zero `ops.pipeline_job` rows for `gfs_2026050300`), assert `has_active_pipeline` True; no cleanup obligation beyond the throwaway database (never the session-scoped `integration_database_url`)
- [ ] 1.7 `file_orchestration_journal.py:70-71`: import `ACTIVE_HYDRO_STATUSES` from `scheduler_state_types` instead of `chain_repository` (keep the `COMPLETED_HYDRO_STATUSES` import where it is — #1581 pinned that surface)
- [ ] 1.8 `tests/test_file_orchestration_journal.py`: three `pending` pins per D1 — `has_active_pipeline` on a `pending` latest view with no jobs → `True`; `permit_pipeline_job_retry` marks a `pending` member row of the lost attempt `failed` / `SLURM_RESERVATION_LOST` (the call then returns 2 — master plus the rewritten hydro row — where the existing permit tests assert 1); `project_forecast_cohort_tasks` rewrites a `pending` row to `succeeded` on a succeeded task; each modelled on the nearest existing test of that method

## 2. B — signature pin

- [ ] 2.1 `tests/test_file_orchestration_journal.py` next to `:15222`: `inspect.signature` pin on `_cycle_rows_by_model_unlocked` (keyword-only `source_id`, `cycle_time`, `model_ids`; nothing else)

## 3. C — sweep hardening

- [ ] 3.1 `tests/test_hydro_status_set_parity.py:30-37`: both regexes accept optional double quotes around each identifier segment
- [ ] 3.2 same file: `_RENAME_RUN_STATUS_RE`; the helper asserts no `RENAME VALUE` / `RENAME TO` for the type anywhere in the tree, message names the files and says the oracle does not model renames
- [ ] 3.3 docstring of `_hydro_run_status_enum_members` states both forms and the fail-closed rule

## 4. Spec + docs

- [ ] 4.1 spec deltas (MODIFIED requirements in `production-scheduler-orchestration` and `pipeline-job-persistence`) reflect the ruling, the journal-lane consequences and the new pins; `openspec validate` valid
- [ ] 4.2 PR body: 变更摘要 states the DB-lane and journal-lane behaviour changes in two sentences and names the one operator-visible regression (re-triggering a cycle whose run sits at `pending` is refused; manual retry remains the remedy); oracle-integrity clause lists the two requirement-driven assertion edits; 偏离记录; merge is NOT pre-authorised for this PR — stop at the gate
