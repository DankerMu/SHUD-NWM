## Why

Five scheduler state and evidence gaps can still charge retries to the wrong stage, lose retry truth before DB projection, claim that no Slurm submit occurred after a gateway-crossed ambiguous attempt, ingest a permanent tracker as pass evidence, and omit the only unreadable-file cause from registry receipts. The gaps are independently reachable, and the submit-proof defect has already appeared in production evidence, so the shared contracts must close together without creating more state/evidence vocabularies.

## What Changes

- Make every explicit stage-scoped retry-attempt read ignore the candidate-level flat retry count and derive only from matching rows plus the existing carried stage floors; preserve stage-less flat-first behavior.
- Make the DB-backed candidate-state path project the complete candidate job population before applying `job_limit`, report the true job total/truncation state, and reuse the same stage/attempt owner as the file-journal path.
- Preserve `unknown_after_attempt` when a producer-proven gateway submission attempt ends ambiguously without a confirmed Slurm identity, while keeping `submitted=false` and keeping token-only pending state non-submitted and proven-no-submit.
- Give readiness discovery and retention one pass-evidence filename predicate so `no-progress-tracker.json` and other non-pass JSON files cannot enter root-scan validation or consume the discovery cap.
- Extend the registry skip-cause contract atomically through publisher diagnostics, refresh validation/projection, receipt schema, tests, and operator documentation with optional `unreadable_required_files` evidence.

## Capabilities

### New Capabilities

None.

### Modified Capabilities

- `job-retry-mechanism`: stage-scoped attempt truth becomes flat-independent and DB/file-journal projections converge before truncation.
- `production-scheduler-orchestration`: bare gateway-crossed submit ambiguity remains unknown rather than becoming proven absence.
- `runtime-evidence-and-operations`: scheduler evidence root scans admit only governed pass artifacts.
- `scheduler-registry-refresh`: removal-refusal skip causes include unreadable required files while old receipts remain valid.

## Impact

- Affected code: scheduler state projection/attempt readers, PostgreSQL candidate-state reads, candidate execution evidence/proofs, scheduler evidence filename discovery/retention, registry publisher/refresh receipt projection, JSON Schema, focused tests, and operator/governance documentation.
- Affected systems: DB-backed orchestration deployments, node-22 scheduler evidence and registry refresh outputs, and readiness validation consuming a shared scheduler evidence root.
- No database migration, retry limit change, Slurm submission/reconciliation policy change, tracker rename, registry publishability change, frontend/API behavior change, or SHUD numerical behavior change is intended.
- Issues closed by this change: #1579, #1575, #1692, #1572, and #1553. The minimal mergeable slice is the complete five-issue evidence contract: splitting publisher/schema or retry projection/DB truth would leave an internally inconsistent producer-consumer boundary.
