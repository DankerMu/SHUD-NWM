# node22-dbfree-runtime Specification

## Purpose
The compute-role scheduler runs only in the DB-free mode: it has no PostgreSQL dependency, refuses leftover DB-backed settings, and writes bounded evidence that a pass used no database.

## Requirements

### Requirement: Node-22 scheduler has an explicit DB-free runtime mode

The system SHALL run the node-22 scheduler only in the DB-free runtime mode:
there is no DB-backed scheduler mode to select. The scheduler SHALL run without
any PostgreSQL dependency and SHALL fail closed when DB-backed settings are
still present. `NHMS_SCHEDULER_DB_FREE_REQUIRED` SHALL no longer select the
scheduler's mode. It SHALL remain the job-env contract that the Slurm gateway
exports to workers, and the scheduler SHALL write
`scheduler_db_free_required=true` into every run manifest regardless of its own
process env.

#### Scenario: DB-free mode is the only scheduler mode

- **WHEN** the scheduler is built from env with `NHMS_SCHEDULER_DB_FREE_REQUIRED`
  unset, `false`, or `true`
- **AND** the state, registry, canonical readiness, journal, state index, and
  lock backend selectors are all set to `file`
- **THEN** it uses the file journal, the file-journal retry service, the NFS
  raw-manifest discovery adapters, and the file lock in every case
- **AND** no code path exists that would build `PsycopgOrchestratorRepository`,
  a SQL `RetryService`, SQLAlchemy `PipelineStore`, a PostgreSQL scheduler
  lease, or the upstream-probing GFS/IFS discovery adapters for the scheduler.

#### Scenario: DB-free mode rejects DATABASE_URL

- **WHEN** the node-22 scheduler starts with
  `NHMS_SCHEDULER_STATE_BACKEND=file`
- **AND** scheduler runtime env still contains `DATABASE_URL`
- **THEN** scheduler preflight fails before acquiring a lock or submitting jobs
- **AND** the evidence records a credential-safe `database_url_forbidden`
  blocker.

#### Scenario: DB-free mode rejects mixed backends

- **WHEN** any scheduler backend selector for state, registry, canonical
  readiness, journal, state index, or lock is unset, blank, `postgres`,
  `psycopg`, or otherwise not `file`
- **THEN** scheduler preflight fails before acquiring a lock or submitting jobs
- **AND** evidence records the specific backend field that would have used a
  PostgreSQL-backed path.

#### Scenario: Worker job env carries the DB-free contract without the scheduler env

- **WHEN** the scheduler runs with `NHMS_SCHEDULER_DB_FREE_REQUIRED` absent from
  its process env and submits a stage job
- **THEN** the run manifest records `scheduler_db_free_required=true`
- **AND** the Slurm job env built by the gateway carries the DB-free worker
  switches (`NHMS_CANONICAL_DB_FREE`, `NHMS_FORCING_DB_FREE`, file registry
  backend), so workers never fall back to a database path.

#### Scenario: DB-free mode rejects psycopg factory reachability

- **WHEN** DB-free scheduler factories are built
- **THEN** the scheduler does not call `PsycopgModelRegistryStore.from_env`,
  `PsycopgMetStore.from_env`, `PsycopgStateSnapshotRepository.from_env`,
  SQLAlchemy `PipelineStore`, DB-backed `ForcingProducer.from_env`, or
  equivalent PostgreSQL factory paths
- **AND** tests fail if those factories are reachable from the scheduler.

#### Scenario: DB-free mode uses file lock

- **WHEN** the node-22 scheduler starts
- **THEN** it uses `NHMS_SCHEDULER_LOCK_BACKEND=file`
- **AND** live scheduler evidence records `lock_type=file`, lock path,
  contention status, owner, and pass ID
- **AND** no PostgreSQL advisory lock is attempted.

#### Scenario: Concurrent file-lock passes do not both mutate

- **WHEN** two node-22 scheduler passes start against the same file lock root
- **THEN** at most one pass may acquire the mutation lock
- **AND** the other pass records lock contention without submitting jobs.

#### Scenario: Runtime root preflight remains strict

- **WHEN** the scheduler starts
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
