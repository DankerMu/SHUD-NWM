## MODIFIED Requirements

### Requirement: Scheduler facade keeps stable entrypoints
The scheduler facade SHALL keep `ProductionScheduler`,
`ProductionSchedulerConfig`, the scheduler CLI/default factory paths, and the
`services.orchestrator.scheduler` import and monkeypatch surfaces stable while
implementation families live in owner modules. Owner modules SHALL keep reading
facade names at call time, so that a patch on the facade is observed. The
facade SHALL import each name it exposes explicitly. It SHALL NOT install
names programmatically (`globals().update`, `setdefault` loops, or merged
export dicts), and it SHALL NOT carry `_*_COMPAT_*` self-check tables.

#### Scenario: legacy scheduler monkeypatch path is used
- **WHEN** a test monkeypatches a helper through `services.orchestrator.scheduler`
- **THEN** the owner module observes the patched behavior, because it reads the
  name off the facade at call time
- **AND** a static name-resolution test fails before merge if any name that an
  owner reads through the facade, or that a test patches on it, does not
  resolve on the facade

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
- **THEN** the behavior lands in the matching owner module, and the facade
  gains at most an explicit import line for it

#### Scenario: cancellation/status proof remains local glue
- **WHEN** cancellation orchestration code still lives in `scheduler.py`
- **THEN** tests cover equivalent cancellation, status-sync, mutation-proof,
  and lease-lost behavior

### Requirement: Scheduler extraction is parity tested
Every scheduler extraction PR SHALL include focused tests for the owner module
it moves behaviour into, and SHALL keep the facade name-resolution test green.
No compatibility inventory is maintained.

#### Scenario: scheduler owner family is extracted
- **WHEN** an owner family is moved or narrowed
- **THEN** focused scheduler tests pass for the moved behavior and the facade
  name-resolution test passes

#### Scenario: scheduler inventory is updated with the extraction
- **WHEN** scheduler behavior changes ownership
- **THEN** no compatibility inventory is edited; the owner module, the explicit
  facade import, and its focused tests are the record, and
  `docs/governance/SCHEDULER_COMPATIBILITY_INVENTORY.md` does not exist
