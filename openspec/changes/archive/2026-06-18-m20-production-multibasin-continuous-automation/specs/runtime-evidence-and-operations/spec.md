## ADDED Requirements

### Requirement: Structured scheduler evidence

Each scheduler pass SHALL emit structured evidence suitable for production operations and release review.

The evidence SHALL distinguish execution modes from readiness claims. Deterministic, dry-run, simulated, or production-like scheduler evidence MAY support review and readiness lineage, but it SHALL NOT mark final production readiness true unless accepted live proof receipts satisfy the readiness proof contract.

#### Scenario: pass summary evidence

WHEN a scheduler pass finishes
THEN evidence includes pass id, started/finished timestamps, execution mode, live proof receipt references when applicable, sources, cycle window, model count, candidate count, submitted count, skipped reasons, failed count, partial count, and artifact locations.

#### Scenario: model-run evidence

WHEN a model candidate reaches a terminal state
THEN evidence includes forcing station count, canonical product counts, SHUD output URI, parsed row count, segment count, display product state, quality flags, Slurm job/accounting details, and residual blockers.

#### Scenario: bounded redacted evidence artifacts

WHEN scheduler or readiness evidence is written or read
THEN the payload is bounded, redacted, and stored under the configured evidence or workspace root
AND malformed, oversized, stale, mismatched, or unsafe evidence is recorded as blocked/release_blocked evidence rather than accepted success.

#### Scenario: deterministic evidence consumed by readiness validation

WHEN readiness validation consumes scheduler evidence from deterministic, dry-run, simulated, or production-like execution
THEN the resulting readiness item is non-final deterministic review evidence
AND `final_production_readiness_claimed` remains false unless every required live proof item is accepted.

#### Scenario: live scheduler receipt binding

WHEN scheduler evidence is presented as live production proof
THEN the live receipt must bind to the readiness run id, target environment, producer artifact reference, checksum or receipt id, schema, and live execution mode
AND stale, mismatched, or deterministic receipts remain release blockers.

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

#### Scenario: diagnostic qhh scripts remain non-production evidence

WHEN docs or runbooks reference `scripts/run_qhh_continuous.py` or qhh-specific cycle scripts
THEN they identify those scripts as diagnostic or reproduction evidence
AND they identify the backend scheduler/orchestrator path as the production automation surface.
