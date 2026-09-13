# migration-runner-lock-safety Specification

## Purpose
Keep the migration runner from stalling a live database behind the PostgreSQL FIFO lock queue: bounded lock waits by default with explicit precedence, and actionable, redacted lock-holder diagnostics without ledger writes on failure (#2277).

## Requirements
### Requirement: The migration runner MUST bound lock waits by default on every session it opens

`packages.common.migrate.main()` MUST configure its session before the first statement (including `ensure_schema_migrations_table`). `lock_timeout` MUST resolve by precedence: an explicit `NHMS_MIGRATE_LOCK_TIMEOUT` value is applied verbatim; otherwise a non-zero session value already in effect (from `PGOPTIONS`, role or database settings) is kept; otherwise `5s` is applied. `statement_timeout` MUST be changed only when `NHMS_MIGRATE_STATEMENT_TIMEOUT` is set, so long legitimate migrations are never killed by default and a `PGOPTIONS` value keeps working. An invalid value MUST fail before any migration statement with exit code 1 and a message naming the variable. The runner MUST print the effective `lock_timeout` and `statement_timeout`, each with its `pg_settings.source`, once before applying migrations. `apply_migration`, `ensure_schema_migrations_table`, `migration_has_been_applied` and `record_migration` MUST keep their signatures and MUST NOT change the session settings of connections supplied by other callers.

#### Scenario: Default guard applied when nothing is configured
- **WHEN** `main()` runs with no `NHMS_MIGRATE_LOCK_TIMEOUT`, no `NHMS_MIGRATE_STATEMENT_TIMEOUT` and a session whose `lock_timeout` is `0`
- **THEN** the session `lock_timeout` is `5s` before the first statement, `statement_timeout` is not changed, and both effective values are printed

#### Scenario: Operator PGOPTIONS value is respected
- **WHEN** the session already reports `lock_timeout = 30s` and `statement_timeout = 120s` from `PGOPTIONS` and neither environment variable is set
- **THEN** the runner keeps `30s` and `120s` and prints them

#### Scenario: Explicit environment value wins
- **WHEN** `NHMS_MIGRATE_LOCK_TIMEOUT=200ms` and `NHMS_MIGRATE_STATEMENT_TIMEOUT=10min` are set
- **THEN** the session reports `200ms` and `10min` before the first statement

#### Scenario: Invalid value fails closed before migrating
- **WHEN** `NHMS_MIGRATE_LOCK_TIMEOUT=five-seconds`
- **THEN** no migration statement or ledger row is executed, the output names `NHMS_MIGRATE_LOCK_TIMEOUT`, and the process exits 1

#### Scenario: Library callers keep their own session
- **WHEN** a test helper calls `apply_migration(connection, file)` on its own connection
- **THEN** that connection's `lock_timeout`/`statement_timeout` are unchanged by the call

### Requirement: A lock-timeout failure MUST be actionable and MUST NOT record the migration

When a migration statement fails with SQLSTATE `55P03` (`lock_not_available`), the runner MUST NOT insert the file into `public.schema_migrations`, MUST print the failing filename, the PostgreSQL error, the effective `lock_timeout`, and a bounded list (at most 20 rows) of other sessions in this database that have an open transaction (`xact_start IS NOT NULL AND pid <> pg_backend_pid() AND datname = current_database()`, so `idle in transaction (aborted)` and `fastpath function call` holders are included) ordered by `xact_start` (pid, usename, application_name, state, wait_event_type, transaction age, query head), then exit 1. The query head MUST be produced by redacting the full query text first and truncating to 120 characters afterwards. Every printed error text and query head MUST pass through `packages.common.redaction.redact_database_dsn` with the runner's `DATABASE_URL`, and SQL password literals (`PASSWORD '<literal>'`, case-insensitive) MUST be replaced by a redaction marker; the DSN MUST never be printed. A failure of this diagnostic query MUST NOT replace the original error or exit code. The output MUST state that statements of the failing file executed before the failure are already committed (autocommit) and that rerunning skips ledger-recorded files only. The runner MUST NOT retry and MUST NOT cancel or terminate other sessions. For SQLSTATE `57014` (`query_canceled`) the output MUST name the effective `statement_timeout`.

#### Scenario: Blocked ALTER prints the lock holder and exits 1
- **GIVEN** session A holds `ACCESS EXCLUSIVE` on a scratch table in an open transaction
- **WHEN** the runner applies a pending file containing `ALTER TABLE` on that table with `lock_timeout=200ms`
- **THEN** the process exits 1 within seconds, the output contains session A's pid and `application_name`, and the file is absent from `public.schema_migrations`

#### Scenario: Diagnostic query failure keeps the original error
- **WHEN** the lock-holder query itself raises
- **THEN** the original `55P03` error text is still printed, a one-line "diagnostic unavailable" note is printed, and the exit code is 1

#### Scenario: Aborted-transaction holder is listed
- **WHEN** the lock-timeout diagnostic sees a session in state `idle in transaction (aborted)` with a non-null `xact_start`
- **THEN** that session's pid is printed

#### Scenario: Secrets never reach the migration log
- **GIVEN** `DATABASE_URL=postgresql://nhms:s3cretpw@db/nhms` and a holder whose query is `ALTER ROLE x PASSWORD 'secret'`
- **WHEN** the lock-timeout diagnostic prints
- **THEN** stdout contains neither `s3cretpw` nor `secret`

#### Scenario: Password literal spanning the truncation boundary is not leaked
- **GIVEN** a holder query whose `PASSWORD 'longsecretvalue'` literal starts before and ends after character 120
- **WHEN** the lock-timeout diagnostic prints
- **THEN** stdout contains no substring of `longsecretvalue` longer than 3 characters

#### Scenario: Guarded runner applies the full history on an empty database
- **GIVEN** an empty isolated database, the connecting role is the database owner with superuser (as `nhms` on node-27 and CI), and the cluster role `nhms_ingest_rw` was created beforehand by only the `DO $roles$` role-creation block of `db/roles/node27_write_roles.sql` (as `apply_migrations_from_zero` does) (runbook §9.6, required by 000059's `OWNER TO`)
- **WHEN** `python -m packages.common.migrate` runs with no timeout environment variables
- **THEN** it exits 0, `public.schema_migrations` holds one row per `db/migrations/*.sql` file, and no index has `pg_index.indisvalid = false`

