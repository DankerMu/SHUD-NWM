## Why

After #874 was merged and deployed, node-22 proved real concurrent Slurm/SHUD
business execution, but the full 13-basin pass still ended
`submitted_partial`: `basins_keliya_shud` had no durable IFS forecast output, yet
the scheduler retried `state_save_qc` until it became permanently failed. That
is not unattended business operation for all registered basins.

## What Changes

- Reclassify downstream `parse`/`state_save_qc`/publish failures that have no
  durable forecast output as a forecast recompute, not a downstream permanent
  blocker.
- Preserve existing missing-forcing guards before the recompute is submitted.
- Prove node-22 can reach a non-partial business state for the current 13
  registered basins without manual per-basin intervention.

## Impact

- Affected code: scheduler candidate-state decision and failure evidence.
- Affected tests: focused production scheduler retry/recompute tests.
- Affected operation: node-22 compute scheduler service/timer and live evidence.
- No display/node-27 API, database migration, or frontend changes are intended.
