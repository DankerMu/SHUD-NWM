# Tasks

Risk packs:
- Error handling / retry policy: selected -> 1.1, 1.2, 1.3.
- Legacy compatibility: selected - the #1161 / #1313 refusal sets and existing raw-repair behavior -> 1.2, 1.3.
- Concurrency / shared state: not selected - pure functions of (candidate, state).
- Public API / CLI, Config, File IO, Schema, Auth: not selected - no such surface is touched.

## 1. Raw-repair channels

- [x] 1.1 Red-before, on unmodified source, for both channels (`repair_missing_raw_manifest`,
      `retry_downstream_after_raw_repair`) and the codes `FORCING_PACKAGE_CHECKSUM_MISMATCH`,
      `FORCING_CHECKSUM_READ_FAILED`, `SHUD_FORCING_CSV_MISSING`, `FORCING_FAILED`: on the default lane the
      decision is a `retry_failed` raw-repair retry; on the strict lane the candidate is scheduled as a retry
      (effective restart `forecast` for the downstream channel). Expected green on both lanes: blocked,
      `permanent_failure_guard`. Test inputs: the forcing package is witnessed present (otherwise the
      missing-forcing blocker answers); on the strict lane warm start is ready (otherwise warm admission blocks
      first); the seeded `forecast_cycle.manifest_uri` must survive as a raw-manifest object URI (the file journal
      redacts a recorded one and both channels then abstain - reuse the withdrawn patch's `_raw_repair_pass` seeding).
- [x] 1.2 Both channels return `None` for the predicate (`failure["permanent"]` and native SHUD stage and
      forcing-input); a non-permanent failure keeps today's decision (preserve test for the reader-disagreement
      shape); `_REMEDY_NON_CAUSAL_*` sets unchanged (pinned by test).
- [x] 1.3 Preserve (green before and after): raw-repair retry of `SHUD_FAILED` at `forecast`; of a post-forecast
      failure; of `failed_stage="forcing"` + `FORCING_FAILED`; of `DIRECT_GRID_TSD_FORC_TOO_LARGE`; a top-level
      non-forcing `error_code` with a stale forcing code elsewhere; existing raw-repair, #2439, #2670, #2719 tests.

## 2. Verification

- [x] 2.1 `uv run ruff check .`; `uv run pytest -q tests/test_production_scheduler.py tests/test_manual_retry_failed_stage_restart.py`
- [x] 2.2 `openspec validate raw-repair-refuses-forcing-input-forecast-failure --strict --no-interactive`
- [ ] 2.3 node-27, no database: the two test files of 2.1 (`TMPDIR=/home/nwm/tmp`, `uv run --no-sync`).
