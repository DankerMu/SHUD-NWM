## REMOVED Requirements

### Requirement: Structured scheduler evidence

**Reason**: Replaced by "Scheduler passes emit structured evidence": the two readiness validation (`validate-readiness`) consumer scenarios of this requirement are cut (#3221), and OpenSpec refuses a MODIFIED block that drops a scenario, so the surviving text moves to a new header.
**Migration**: See the ADDED requirement "Scheduler passes emit structured evidence"; apart from those cuts its body and scenarios are unchanged.

### Requirement: Scheduler evidence root discovery admits only governed pass artifacts

**Reason**: Replaced by "Scheduler pass-evidence retention admits only governed pass artifacts": readiness root discovery (`readiness_scheduler_evidence`) is deleted with the readiness lane (#3221), which removes three of the four scenarios, and OpenSpec refuses a MODIFIED block that drops a scenario, so the surviving text moves to a new header.
**Migration**: See the ADDED requirement; the filename predicate (`scheduler_evidence.is_scheduler_pass_evidence_filename`) and retention behaviour are unchanged.

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

### Requirement: Scheduler pass-evidence retention admits only governed pass artifacts

Scheduler pass-evidence retention SHALL classify file names through one shared pass filename contract. A pass artifact SHALL have the governed `scheduler_` prefix and an accepted pass JSON suffix; non-pass state such as `no-progress-tracker.json`, temporary files, and unrelated top-level JSON SHALL NOT enter pass-retention deletion selection. Runtime pass writers SHALL continue to emit names accepted by this predicate.

#### Scenario: Retention classifies only governed names as pass artifacts

- **WHEN** a set of governed pass, pre-execution pass, tracker, temporary, and unrelated JSON names is presented to pass-evidence retention
- **THEN** only the governed pass and pre-execution pass names are classified as scheduler pass artifacts
- **AND** the tracker, temporary, and unrelated files never enter deletion selection
