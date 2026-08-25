## Why

File-journal state is being rewritten from public, redacted projections in two write legs, while cohort projection protects only one externally assigned terminal status. These gaps can erase durable failure attribution or silently replace an operator-visible terminal truth during an otherwise valid status/accounting update (#1652, #1630, #1629).

## What Changes

- Make the repository's private hydro-run lookup return the durable row; public redaction remains only at explicit return/query boundaries.
- Treat an unchanged public retry snapshot message as a round trip, not a new permanent-failure message override, while preserving genuinely new caller text.
- Preserve projection-reachable, non-derived cohort terminal statuses (`permanently_failed`, `cancelled`) across task reprojection without freezing task or Slurm observational evidence.
- Add durable-layer regression tests for raw and embedded URI attribution, successful-state clearing, permanent-failure message overrides, and cancelled-master reprojection.

## Capabilities

### New Capabilities

- None.

### Modified Capabilities

- `pipeline-job-persistence`: Extend durable/public boundary and cohort terminal-state preservation requirements to the three previously declared gaps.

## Impact

- Runtime: `services/orchestrator/file_orchestration_journal.py` only.
- Tests: `tests/test_file_orchestration_journal.py` and, only where the existing reconcile seam is needed, `tests/test_gateway_reconcile.py`.
- No API/schema/migration/dependency/DB/Slurm scheduling change; public redaction output remains unchanged.
