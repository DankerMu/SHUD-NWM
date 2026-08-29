## Why

Two latent file-journal compatibility paths violate the operator-authority closed world delivered by #1564 and #1748. Legacy accepted-submit writers can mint `operator_verified_absence` without the typed audited demotion, while the manual-retry clone can copy `operator_recovery_attested_at` into a fresh attempt; both defects are currently unreachable from production callers but become active under plausible future compatibility work.

## What Changes

- Reject `operator_verified_absence` at both legacy accepted-submit compatibility writers with the existing typed-authority error and zero durable mutation.
- Strip `operator_recovery_attested_at` when a manual-retry successor is constructed, while preserving the source attestation and ordinary retry behavior.
- Pin `reservation_lost` outside the generic manual-retry source set and prove the cloned successor cannot satisfy the operator-recovery consumer predicate.
- Preserve legitimate legacy reconciliation decisions, the dedicated typed demotion/recovery APIs, and existing retry lineage.

## Capabilities

### New Capabilities

- None.

### Modified Capabilities

- `production-scheduler-orchestration`: extend the operator-decision writer closed world across the legacy transition and reconciliation compatibility APIs.
- `job-retry-mechanism`: prevent an operator-recovery attestation from crossing into a distinct manual-retry attempt.

## Impact

- Code: `services/orchestrator/file_orchestration_journal.py` only.
- Tests: focused file-journal authority and manual-retry regressions.
- Specifications: the two existing capabilities above.
- No API signature, database, schema migration, dependency, frontend, Slurm configuration, or node-22 runtime change.
