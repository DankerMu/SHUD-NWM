## ADDED Requirements

### Requirement: Scheduler facade keeps stable entrypoints
The scheduler facade SHALL keep `ProductionScheduler`,
`ProductionSchedulerConfig`, scheduler CLI/default factory paths, and legacy
`services.orchestrator.scheduler` import and monkeypatch surfaces stable while
implementation families move to owner modules.

#### Scenario: legacy scheduler monkeypatch path is used
- **WHEN** a test or downstream caller monkeypatches an inventoried helper
  through `services.orchestrator.scheduler`
- **THEN** the moved owner module observes the patched behavior or an
  inventory-backed compatibility test fails before merge

#### Scenario: scheduler public entrypoint is constructed
- **WHEN** callers construct `ProductionScheduler` through the existing facade
- **THEN** scheduler state, lease, discovery, candidate construction, evidence,
  execution, and cancellation/status behavior remain equivalent to the
  pre-extraction behavior

### Requirement: Scheduler owner modules own complete behavior families
Scheduler extraction SHALL move implementation by complete owner families:
state, lease, discovery, candidate construction, execution/cohort handling,
evidence write/proof assembly, and cancellation/status proof.

#### Scenario: new scheduler behavior is added
- **WHEN** a PR adds scheduler state, lease, discovery, candidate, execution,
  evidence, or proof behavior
- **THEN** the behavior lands in the matching owner module, or the scheduler
  compatibility inventory is updated with a documented retention reason

#### Scenario: cancellation/status proof remains local glue
- **WHEN** cancellation orchestration code still lives in `scheduler.py`
- **THEN** the inventory identifies it as local glue and tests cover equivalent
  cancellation, status-sync, mutation-proof, and lease-lost behavior

### Requirement: Scheduler extraction is parity tested
Every scheduler extraction PR SHALL include focused tests for the owner module
and compatibility tests for the facade surface it preserves. Each PR SHALL
update `docs/governance/SCHEDULER_COMPATIBILITY_INVENTORY.md` in the same PR
when it moves behavior, changes a facade wrapper, or documents retained local
glue.

#### Scenario: scheduler owner family is extracted
- **WHEN** an owner family is moved or narrowed
- **THEN** focused scheduler tests pass for the moved behavior and the
  compatibility facade guard reports no untracked scheduler facade growth

#### Scenario: scheduler inventory is updated with the extraction
- **WHEN** scheduler state, lease, discovery, candidate, execution, evidence, or
  cancellation/status behavior changes ownership or retention classification
- **THEN** the scheduler compatibility inventory records the owner module,
  retained facade surface, removal condition if any, and focused verification
  command in the same PR
