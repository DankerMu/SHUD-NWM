## REMOVED Requirements

### Requirement: Structured scheduler evidence

**Reason**: Replaced by "Scheduler passes emit structured evidence": the two readiness validation (`validate-readiness`) consumer scenarios of this requirement are cut (#3221), and OpenSpec refuses a MODIFIED block that drops a scenario, so the surviving text moves to a new header.
**Migration**: See the ADDED requirement "Scheduler passes emit structured evidence"; apart from those cuts its body and scenarios are unchanged.

## ADDED Requirements

### Requirement: Scheduler passes emit structured evidence

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
