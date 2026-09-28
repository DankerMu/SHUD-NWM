## 1. Writer guard

- [x] 1.1 `_is_legacy_unversioned_reserved_forecast_master(row)` (forecast rule: `stage`, or `job_type` when `stage` is empty) plus the refusal at the lowest record-level append funnel(s) every `pipeline_job` record passes. Named error `file_journal_legacy_unversioned_reserved_forecast_master`, zero bytes (design Decisions 1-2): check at the `_write_pipeline_job_unlocked` entry before the conflict check, sequence allocation and anchor sync, plus the record-level append funnels.
- [x] 1.3 `import_historical_scheduler_state` pre-scans job rows after the row-limit gate and before the root is created or verified; a (c) row fails the whole import (design Decision 3).
- [x] 1.2 Confirm by grep that no other `pipeline_job` append bypasses the guard, and list every append site in the PR.

## 2. Tests

- [x] 2.1 Per-writer refusal tests E1-E6 (including the reconcile-inventory zero-byte check in E1 and E4), red before and green after.
- [x] 2.1b Import pre-scan test E7 (task 1.3): named error, no journal root created, nothing imported; red before, green after.
- [x] 2.2 Bypass appenders unchanged (E8), non-(c) writes unchanged (E9), and the caller pin on `_reserve_cycle_stage` (E10).
- [x] 2.3 A test-only helper seeds a pre-existing (c) row. Move the reconcile, bind and listing suites that build (c) through a public writer onto it (E11, E12).
- [x] 2.4 Register any new suite in `scripts/select_ci_tests.py` / `tests/test_select_ci_tests.py`.

## 3. Docs

- [x] 3.1 `failed-basin-retry.md` (held-disposition row for `legacy_unversioned_read_only` and the `legacy_unversioned_unsupported` refusal row) and `scheduler-dbfree-typed-reasons.md` (#2666 section): shape (c) is unreachable from current writers since #2674; a pre-existing legacy row is escalated and never hand-edited; production held 0 such rows on 2026-09-28.

## 4. Verification

- [x] 4.1 `uv run ruff check .`; `openspec validate legacy-unversioned-held-master-guard --strict --no-interactive`; focused suites: new suites, `tests/test_gateway_reconcile_*.py`, `tests/test_orchestrator_demote_*.py`, `tests/test_file_orchestration_migration*.py` (or the import suite), `tests/test_orchestrator_bind_reserved_job_*.py`, `tests/test_operator_action_listing*.py`, `tests/test_scheduler_held_reservation_block.py`, retry and journal suites touched by the guard.
- [ ] 4.2 node-27 focused run at the PR head, plus the full `uv run pytest -q`.
- [ ] 4.3 Record the node-22 production (c) count in the PR, with the exact scan script, the journal root and the predicate: design Context gives 0 on both journal segments and direct rows (2026-09-28T05:21Z).

## Risk packs considered (core)

- Public API / CLI / script entry: not selected - no new command; the historical import only gains a named refusal (2.1).
- Config / project setup: not selected.
- File IO / path safety / overwrite: selected - guard at the `_write_pipeline_job_unlocked` entry (ahead of the anchor sync) and at the append funnels, zero-byte refusal including `reconcile-inventory/` (2.1).
- Schema / columns / units / field names: selected - one new error code; no persisted field change (2.1).
- Auth / permissions / secrets: not selected.
- Concurrency / shared state / ordering: not selected - the guard is a pure predicate on the row being written, inside the existing locks.
- Resource limits / large input / discovery: not selected.
- Legacy compatibility / examples: selected - pre-existing legacy rows stay readable and keep their reconcile, bind and listing handling; non-(c) legacy writes unchanged (2.2, 2.3).
- Error handling / rollback / partial outputs: selected - the import fails closed before root creation (1.3, 2.1b); a refused write leaves no reconcile-inventory anchor (2.1).
- Release / packaging / dependency compatibility: not selected.
- Documentation / migration notes: selected (3.1).

## Domain risk packs considered

- Slurm production lifecycle: selected - the guard prevents a row whose reconcile would be blind to cohort accounting (design Context); production count recorded (4.3).
- Geospatial, hydro-met windows, SHUD numerics, PostGIS/Timescale, providers, published artifacts, alerting lanes: not selected - untouched.
