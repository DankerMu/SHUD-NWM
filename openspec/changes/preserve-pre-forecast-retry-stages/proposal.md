## Why

Issue #2439 is a confirmed scheduler stage-boundary defect: an ordinary forcing resume is rewritten into forecast before forcing exists, then correctly blocked by the witness guard. The user now explicitly requests fixing it and restoring the upstream-to-display chain, superseding the issue's earlier diagnosis-only production exclusion.

Issue type: bugfix
Fixture level: expanded
Upstream suggested level: absent (shared scheduler state and live recovery require expanded)
Blast radius: retry stage selection, warm-start lineage, production submission and publication.
Selected risk packs: public CLI; config; file safety; identity fields; permissions; concurrency; resource limits; compatibility; error handling; runtime compatibility; documentation.
Evidence floor: node-27 red/green regression and sibling tests; guarded node-22 deployment and actual forcing/forecast/state/copyback progression; node-27 new identity publication and API evidence.

## What Changes

- Preserve recognized pre-forecast convert/forcing resumes through strict warm-start manifest reconciliation.
- Preserve forecast-and-later manifest enforcement, forcing witness guard, alias/precedence, terminal/quarantine/successor/authorized-repair semantics.
- Correct the existing regression that encodes forcing-to-forecast skipping: preserve its valid late-stage stale-manifest scenario and add real decision-chain pre-forecast coverage.
- Under the user's explicit recovery authorization, deploy only reviewed scheduler changes to the pinned compute runtime; do not deploy unrelated master changes or rebuild the active environment.
- Verify real GFS progression and subsequent IFS gate release through node-27 published/API identity, with rollback and honest residual accounting.

## Capabilities

### New Capabilities

None.

### Modified Capabilities

- `job-retry-mechanism`: strict warm-start manifest reconciliation respects pre-forecast work.
- `runtime-evidence-and-operations`: recovery evidence binds stage progression to deployed code and published identity.

## Impact

`services/orchestrator/scheduler_candidates.py`, existing scheduler regression tests, runbook/receipt, actual node-22 code deployment and normal scheduler operations. No DB schema, role changes, forcing-witness fabrication, circuit reset, or Python/environment migration. Node-27 remains parse/ingest/publish owner; node-22 remains DB-free.
