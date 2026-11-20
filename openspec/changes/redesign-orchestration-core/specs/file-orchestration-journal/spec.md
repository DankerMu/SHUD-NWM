## MODIFIED Requirements

### Requirement: File journal is contract-tested against DB semantics

The system SHALL include repository contract tests that verify file-backed
orchestration state behavior against the scheduler semantics the file journal
is the only implementation of.

#### Scenario: Contract fixtures cover critical repository methods

- **WHEN** repository contract tests run
- **THEN** fixtures cover active orchestration, active pipeline, completed
  pipeline, active Slurm jobs, candidate state, model/forcing context reads,
  lifecycle writes, reservation/bind, job status updates, event insertion,
  retry supersession, permanent failure guards, historical migration, and query
  helpers
- **AND** the file journal is the only orchestration repository those fixtures
  exercise; no DB-backed orchestration repository exists to compare against.

#### Scenario: Read-side contract fixtures cover scheduler planning

- **WHEN** #833 focused tests run without `DATABASE_URL`
- **THEN** they prove file-backed active/completed/candidate/active-Slurm
  decisions are visible to scheduler planning
- **AND** no DB-backed active/orchestrator repository factory exists for
  read-side construction to call.

## ADDED Requirements

### Requirement: The journal repository keeps one class surface across responsibility modules

`FileOrchestrationJournalRepository` SHALL keep these stable while its leaf
responsibilities live in separate mixin modules:

- its import path and class name;
- its public and private method names and their signatures;
- its read attribution;
- the module-level names importable from
  `services.orchestrator.file_orchestration_journal`;
- its on-disk formats.

#### Scenario: A responsibility cluster moves to a mixin

- **WHEN** a cluster of methods moves from `file_orchestration_journal.py` into
  a `file_journal_*` mixin module
- **THEN** the characterization guard's pinned sets are unchanged:
  - the method names resolvable on the class, with their signatures;
  - the attribution-wrapped public methods;
  - the private methods called by the named external modules;
  - the public module-level names importable from the facade module.
- **AND** module-level patch points are still read off
  `services.orchestrator.file_orchestration_journal` at call time, and the
  cluster-map late-lookup lint covers the new mixin module.
