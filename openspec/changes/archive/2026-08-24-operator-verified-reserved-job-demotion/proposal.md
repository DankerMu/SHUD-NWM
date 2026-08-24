## Why

On node-22, Slurm accounting does not store job comments. The fail-closed #1116 gate therefore correctly keeps ambiguous reserved-unbound cohort masters in `reserved`, but a dead submission can then wedge its cycle forever because no guarded operator surface can prove the manual absence decision and open the existing reclaim path.

## What Changes

- Add a file-journal-only `nhms-pipeline demote-reserved-job` operator command with explicit confirmation, exact attempt/anchor compare-and-swap inputs, and required operator/evidence metadata.
- Add one typed, atomic journal transition from the narrowly-defined `comment_accounting_unproven` held shape to `reservation_lost` with a distinct `operator_verified_absence` decision and an audit event.
- Allow only that decision, alongside the existing automatic `absence_retry_permitted` decision, through the reclaim predicate and the forecast-cycle verified-retry shortcut.
- Preserve the #1116 automatic fail-closed path, generic transition whitelist, manual-retry status set, identity-release non-reclaimability, and PostgreSQL repository behavior.
- Replace the runbook's unsupported hand-edit dead end with the guarded command and a production-safe node-22 procedure: use a naturally occurring held row when one exists, otherwise stop after a read-only census rather than manufacturing an incident.

## Capabilities

### New Capabilities

- None.

### Modified Capabilities

- `production-scheduler-orchestration`: add a guarded operator recovery contract for manually verified-dead comment-unobservable reservations.
- `job-retry-mechanism`: extend the existing file-journal reclaim door with a distinct audited operator decision while preserving all non-reclaimable shapes.

## Impact

- Runtime: `services/orchestrator/accepted_submit_identity.py`, `file_orchestration_journal.py`, `chain_forecast_orchestrator_cycle.py`, and `cli.py`.
- Tests: file-journal CAS/event/reclaim coverage, cycle retry-shortcut coverage, both CLI entrypoints, and selector importer ownership if a new CLI test suite is introduced.
- Operations: `docs/runbooks/failed-basin-retry.md`, a read-only node-22 held-row census, and a conditional live receipt on the first naturally occurring safe target. Absence of such a target does not authorize or require production fault injection.
- No database migration, PostgreSQL reclaim change, Slurm configuration change, HTTP retry expansion, automatic comment-less absence heuristic, or production fault injection for release evidence.
