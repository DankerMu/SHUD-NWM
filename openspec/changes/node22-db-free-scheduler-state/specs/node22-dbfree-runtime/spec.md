## ADDED Requirements

### Requirement: Node-22 scheduler has an explicit DB-free runtime mode

The system SHALL provide an explicit node-22 scheduler runtime mode that runs
without any PostgreSQL dependency and fails closed when DB-backed settings are
still present.

#### Scenario: DB-free mode rejects DATABASE_URL

- **WHEN** the node-22 scheduler starts with
  `NHMS_SCHEDULER_STATE_BACKEND=file`
- **AND** scheduler runtime env still contains `DATABASE_URL`
- **THEN** scheduler preflight fails before acquiring a lock or submitting jobs
- **AND** the evidence records a credential-safe `database_url_forbidden`
  blocker.

#### Scenario: DB-free mode rejects mixed backends

- **WHEN** `NHMS_SCHEDULER_DB_FREE_REQUIRED=true`
- **AND** any scheduler backend selector for state, registry, canonical
  readiness, journal, state index, or lock is unset, `postgres`, `psycopg`, or
  otherwise not `file`
- **THEN** scheduler preflight fails before acquiring a lock or submitting jobs
- **AND** evidence records the specific backend field that would have used a
  PostgreSQL-backed path.

#### Scenario: DB-free mode rejects psycopg factory reachability

- **WHEN** DB-free scheduler factories are built
- **THEN** the scheduler does not call `PsycopgModelRegistryStore.from_env`,
  `PsycopgMetStore.from_env`, `PsycopgOrchestratorRepository.from_env`,
  `PsycopgStateSnapshotRepository.from_env`, DB-backed
  `_retry_service_from_env`, SQLAlchemy `PipelineStore`,
  DB-backed `ForcingProducer.from_env`, or equivalent PostgreSQL factory paths
- **AND** tests fail if those factories are reachable in DB-free mode.

#### Scenario: DB-free mode uses file lock

- **WHEN** the node-22 scheduler starts in DB-free mode
- **THEN** it uses `NHMS_SCHEDULER_LOCK_BACKEND=file`
- **AND** live scheduler evidence records `lock_type=file`, lock path,
  contention status, owner, and pass ID
- **AND** no PostgreSQL advisory lock is attempted.

#### Scenario: Concurrent file-lock passes do not both mutate

- **WHEN** two node-22 scheduler passes start against the same file lock root
- **THEN** at most one pass may acquire the mutation lock
- **AND** the other pass records lock contention without submitting jobs.

#### Scenario: Runtime root preflight remains strict

- **WHEN** DB-free mode is enabled
- **THEN** existing scheduler root preflight still validates workspace,
  object-store, runtime, temp, evidence, lock, and allowed roots
- **AND** unsafe or missing roots block before mutation.

### Requirement: DB-free runtime evidence proves absence of PostgreSQL use

The system SHALL write bounded evidence that proves the scheduler pass did not
depend on node-22 PostgreSQL.

#### Scenario: No database dependency appears in scheduler evidence

- **WHEN** a DB-free scheduler pass completes
- **THEN** evidence includes `database_url_configured=false`
- **AND** evidence includes the selected state, registry, readiness, journal,
  and state-index backend names
- **AND** evidence includes the canonical DB-free env matrix field names
- **AND** evidence does not include a PostgreSQL host, port, advisory lock, or
  psycopg-backed dependency.

#### Scenario: DB-free Slurm preflight does not require DATABASE_URL

- **WHEN** Slurm execution is enabled under DB-free scheduler mode
- **THEN** Slurm preflight does not emit
  `SLURM_PREFLIGHT_DATABASE_URL_MISSING`
- **AND** it still validates all non-DB runtime roots and submission safety
  checks before mutation.
