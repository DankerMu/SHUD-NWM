## Why

The db-free scheduler can already have committed a retry reservation or decided a duplicate-submission skip, then let a derived projection or evidence-write failure escape as if the business decision had failed. That false failure can strand an active `reserved` row, turn the next automatic-absence cycle into `PIPELINE_ALREADY_ACTIVE`, or replace a correct no-submit result with an exception.

## What Changes

- Extend committed projection containment from the operator-only reclaim instance to the generic file-journal reservation/reclaim write boundaries, including automatic `absence_retry_permitted` recovery.
- Preserve append-before-projection commit semantics: pre-append failures remain fail-closed; post-append direct/inventory/latest failures return the committed row, emit bounded non-secret fault evidence, and do not permit a second submit.
- Make duplicate-submission skip evidence best-effort for both scheduler and file-journal exception families, while preserving the typed `skipped_duplicate_submission` result and zero-sbatch decision.
- Add public-cycle, repository-boundary, observability, pre-commit control, normal-path compatibility, and handler-inventory regression evidence for issues #1796 and #1568.

## Capabilities

### New Capabilities

None.

### Modified Capabilities

- `job-retry-mechanism`: define the commit-point and evidence-failure containment contract for db-free reservations, automatic-absence reclaim, and duplicate-submission skip decisions.

## Impact

- Affected code: `services/orchestrator/file_orchestration_journal.py`, `services/orchestrator/chain_forecast_submission.py`, and only the smallest helper/caller surfaces required by the selected design.
- Affected tests: focused file-journal and forecast-cycle regressions at the repository and public orchestration seams.
- No database migration, public API/schema change, Slurm gateway change, new status vocabulary, or node-22 runtime action is required; validation is local-only per the issue labels and stated verification floors.
