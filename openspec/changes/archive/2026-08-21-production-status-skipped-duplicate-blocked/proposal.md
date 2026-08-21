## Why

The reserve gate emits `skipped_duplicate_submission` when another pass already owns the same in-flight reservation, but `production_status_for` does not recognize that status and renders it as `failed`. This contradicts the readiness plane's existing `blocked` classification and turns normal concurrency deferral into misleading failure evidence.

## What Changes

- Map `skipped_duplicate_submission` to the existing production status `blocked`.
- Lock both stage-evidence projection paths to that mapping.
- Preserve the fail-closed `unknown -> failed` fallback and every existing alias.

## Capabilities

### New Capabilities

- None.

### Modified Capabilities

- `slurm-job-chain`: Add the production-evidence projection contract for duplicate-submission deferral.

## Impact

- `services/orchestrator/production_contract.py`
- `services/orchestrator/scheduler_candidate_execution_evidence.py` consumers, behavior only
- `tests/test_production_scheduler.py` and `tests/test_orchestration_chain.py`
- No schema migration, API change, or runtime submission behavior change
