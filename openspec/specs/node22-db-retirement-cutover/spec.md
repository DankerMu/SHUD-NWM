# node22-db-retirement-cutover Specification

## Purpose
TBD - created by archiving change node22-db-free-scheduler-state. Update Purpose after archive.

## Requirements

### Requirement: Node-22 historical PostgreSQL is stopped only after DB-free proof

The system SHALL stop the historical do-not-connect PostgreSQL `:55433` (of
the retired node-22 host; stopped on 2026-06-29, #837) only after the DB-free
scheduler path is live-proven and archived rollback evidence exists.

#### Scenario: Stop gate checks all DB-free evidence

- **WHEN** an operator attempts to retire the database of the retired node-22 host
- **THEN** the latest live receipts show scheduler env has no `DATABASE_URL`
- **AND** `NHMS_SCHEDULER_LOCK_BACKEND=file`
- **AND** DB-free scheduler evidence has `lock_type=file`
- **AND** DB-free scheduler evidence has no DB dependency blocker and no
  `download_source_cycle` submission
- **AND** at least one GFS and one IFS live cycle reached `convert-or-later`
  without scheduler PostgreSQL.

#### Scenario: Pre-stop listener attribution is captured

- **WHEN** the historical do-not-connect `:55433` of the retired node-22 host is
  still listening before retirement and before archived/stopped rollback-only
  state is reached
- **THEN** the retirement receipt records `ss -ltnp`, PID, process owner,
  service or unit metadata when available, command line, and active
  client/session attribution
- **AND** the evidence proves `:55433` is only the historical do-not-connect
  PostgreSQL rollback target, not a scheduler-owned runtime dependency.

#### Scenario: Archive and rollback evidence exists before stop

- **WHEN** the historical do-not-connect `:55433` of the retired node-22 host is
  stopped into archived/stopped rollback-only state
- **THEN** a dump/archive path, checksum, service/unit metadata, env backup,
  process owner notes, and rollback commands are recorded
- **AND** the receipt identifies whether stopping required the PostgreSQL owner
  account or an administrator rather than `frd_muziyao`.

#### Scenario: Cutover preparation is reversible

- **WHEN** the scheduler is switched to DB-free mode (done on
  the retired node-22 host, 2026-06-28)
- **THEN** the scheduler timer is frozen or bounded during migration
- **AND** scheduler env/unit backups, imported journal checksums, replay
  evidence, and rollback commands are recorded before the timer is re-enabled.

#### Scenario: Post-stop verification passes

- **WHEN** the historical PostgreSQL of the retired node-22 host has been stopped
- **THEN** `ss -ltnp | grep 55433` is empty
- **AND** a bounded no-DB scheduler pass succeeds
- **AND** compute API and Slurm gateway remain healthy.

### Requirement: Guardrails prevent reintroducing active node-22 DB assumptions

The system SHALL include static and live guardrails that prevent active
scheduler documentation or runtime templates from depending on the historical
PostgreSQL of the retired node-22 host after retirement.

#### Scenario: Active env templates reject node-22 DB

- **WHEN** topology guardrails scan active env templates, scheduler runbooks, or
  production verification instructions
- **THEN** references to `:55433` or `10.0.2.100:55433` fail unless clearly
  marked as historical archive, rollback, or compatibility context
- **AND** scheduler examples of the compute role do not include active `DATABASE_URL`.
