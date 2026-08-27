## Why

Two scheduler retry diagnostics are incomplete on current `master`. The §8.7 quarantine breaker ignores a model that succeeded inside a `partially_failed` cohort, while the forced-resubmit cohort conjunction can silently veto another basin's requested resubmission and leave no bounded receipt explaining the zero-submit result.

## What Changes

- Count a provenance-stamped `partially_failed` forecast cohort master for one model only when that model's bounded `candidate_projections` entry says `array_task_outcome="succeeded"`.
- Preserve aggregate-success masters without projections, reject failed/unreadable/truncated per-model evidence, exclude reconciled per-model copies, and keep the accessor read-only/fail-toward-liveness.
- Keep the current forced-resubmit whitelist and all-basin conjunction byte-for-byte in meaning, but record the first decisive mixed-cohort veto as one fixed-shape typed record on the vetoing candidate's returned outcome.
- Carry that record through scheduler execution evidence and bounded candidate compaction, and document both operator-visible contracts.
- Do not import `replay_manual_retry_admission` from the archived, never-merged `feat/issue-1164-six-basin-replay` branch; current `master` has no such eligibility contract.

## Capabilities

### New Capabilities

- None.

### Modified Capabilities

- `file-state-snapshot-index`: make quarantine-breaker convergence counting model-aware for partially failed cohort masters.
- `dependency-chain-automation`: expose a bounded typed receipt when a mixed cohort vetoes terminal-stage forced resubmission without changing the decision.

## Impact

Affected surfaces are the file-journal occurrence accessor, forecast-chain cycle context and forced-resubmit gate, candidate-outcome/scheduler-evidence projection, bounded evidence compaction, focused orchestrator tests, and the scheduler typed-reason runbook. No database schema, API endpoint, Slurm submission template, decision whitelist, threshold, cadence, or production configuration changes.