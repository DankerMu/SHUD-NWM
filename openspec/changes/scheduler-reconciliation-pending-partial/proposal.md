## Why

A cycle whose submission result is still being reconciled can currently appear as final candidate success, while the nested partial-array retry path rewrites the same pending state into per-task failure and may continue retrying. The evidence plane and control plane must share one first-class reconciliation contract before the defer path can be made safe.

## What Changes

- Classify `reconciling`, `submit_result_ambiguous`, and `reconcile_unverified` as non-successful partial evidence, never failed or successful evidence.
- Make readiness recognize the family in both partial recount predicates and accept the `reconciling` pass status as review-blocked.
- Defer nested reconciliation-pending submissions without stamping pending tasks failed, minting another attempt, or running downstream stages.
- Preserve an already-confirmed first dispatch when a raw nested pending result replaces the prior stage; a bare pending token still does not imply submission.
- Attribute the final reconciliation defer span as neither submitted nor failed, while retaining the confirmed dispatch in scheduler evidence.
- Map nested stage statuses explicitly to the `reconciling` cycle terminal while preserving the existing duplicate-skip terminal.
- Preserve `submission_failed`, generic partial-retry, duplicate-skip, and durable reconciliation semantics outside this family.

## Capabilities

### New Capabilities

- None.

### Modified Capabilities

- `slurm-job-chain`: Treat nested reconciliation-pending results as a dedicated defer family with the correct cycle terminal.
- `production-scheduler-orchestration`: Treat reconciliation-pending candidate evidence as partial non-success.
- `readiness-evidence-schema`: Recount reconciliation-pending model-run rows consistently and keep the public readiness result review-blocked.

## Impact

- Shared orchestrator state-machine and retry/defer logic.
- Scheduler candidate quality, pass counts, and readiness validation vocabulary.
- No database migration, Slurm gateway protocol, production-status alias, public API, or frontend change.
- Pure local deterministic tests are the authoritative oracle; no sbatch/resource behavior changes.
