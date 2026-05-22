## ADDED Requirements

### Requirement: Structured scheduler evidence

Each scheduler pass SHALL emit structured evidence suitable for production operations and release review.

#### Scenario: pass summary evidence

WHEN a scheduler pass finishes
THEN evidence includes pass id, started/finished timestamps, execution mode, live proof receipt references when applicable, sources, cycle window, model count, candidate count, submitted count, skipped reasons, failed count, partial count, and artifact locations.

#### Scenario: model-run evidence

WHEN a model candidate reaches a terminal state
THEN evidence includes forcing station count, canonical product counts, SHUD output URI, parsed row count, segment count, display product state, quality flags, Slurm job/accounting details, and residual blockers.

### Requirement: Operations controls and validation

The production automation SHALL expose or provide operator controls for dry-run planning, retry, cancellation, and fast validation without requiring full live multi-cycle reruns.

#### Scenario: dry-run planning

WHEN an operator runs dry-run mode
THEN the scheduler reports selected candidates and skip/block reasons
AND it does not download data, submit Slurm jobs, run SHUD, or mutate hydro/met result tables.

#### Scenario: fast regression lane

WHEN CI or PR validation runs
THEN it uses deterministic fixtures and focused tests for discovery, idempotency, Slurm preflight/export, array partial states, and evidence formatting
AND full live GFS/IFS/SHUD multi-cycle execution remains opt-in
AND final production readiness remains false unless accepted live receipts satisfy the readiness proof contract.

#### Scenario: dry-run no mutation

WHEN dry-run mode is executed
THEN tests prove it does not download data, submit Slurm jobs, run SHUD, or mutate hydro/met result tables.
