## ADDED Requirements

### Requirement: node-27 write-path components SHALL authenticate as non-superuser roles provisioned idempotently

Every recurring node-27 runtime unit that writes to the production database SHALL connect as a role with `rolsuper`, `rolcreaterole`, `rolcreatedb`, `rolreplication` and `rolbypassrls` all false, holding the measured privilege set for its component (ownership of the application-schema relations plus DML grants and default privileges for the ingest-class lanes, DML grants and default privileges on `met` for the download lane); the roles, grants, default privileges and ownership SHALL be provisioned by one idempotent script whose trailing audit fails on drift; the superuser role SHALL appear in no runtime env file — in DSN or `PGUSER` form — except the documented migration-class exceptions (archive-rebuild drill, compression replay supervisor).

#### Scenario: Provision is idempotent

- **WHEN** the provision script is run twice against the same database
- **THEN** the second run completes without error and the audit output is identical

#### Scenario: Migration-added tables stay usable before re-provision

- **WHEN** a migration run as `nhms` creates a new table in an application schema and the provision script has not yet been re-run
- **THEN** `nhms_ingest_rw` can read and write that table through default privileges, only its stats-guard ANALYZE entry reports `warning`, and the audit reports the owner drift

#### Scenario: Display grants survive the ownership transfer

- **WHEN** the ownership loop has run
- **THEN** `nhms_display_ro`'s `SELECT` privilege set over the application schemas is identical to the captured pre-transfer set, each relation's transfer committed on its own so no display query waited longer than one relation's `lock_timeout`, and any relation left untransferred after the retry passes is reported by the audit and blocks the cutover

#### Scenario: Role membership regression is refused

- **WHEN** either write role is granted membership in any other role (for example `GRANT nhms TO nhms_ingest_rw` or `GRANT pg_read_server_files TO nhms_ingest_rw`) and the provision script runs in any mode
- **THEN** the flags audit raises a security-regression error naming the role and the membership, and the runner exits 3 — the audit reads `pg_auth_members`, not only the `pg_roles` flags

#### Scenario: Owner-planted rules and triggers are refused and audited

- **WHEN** either write role attempts `CREATE RULE` or `CREATE TRIGGER` on a relation it owns
- **THEN** on ordinary tables and on TimescaleDB chunks the superuser-owned event trigger raises and no rule or trigger is created, the write role cannot drop that event trigger, and a migration run as `nhms` can still create triggers; on hypertables (and the internal `_compressed_hypertable_*` relations) TimescaleDB's utility hook processes `CREATE TRIGGER` itself and the event trigger does not fire, so a planted trigger is created and propagates to the chunks — there the guarantee is detection only: the strict audit fails on any rule (other than view `_RETURN` rules) or any non-internal trigger (other than TimescaleDB's `ts_insert_blocker`) in the six application schemas, or on any `_timescaledb_internal` relation owned by either write role, that is not on the explicit allowlist of the four `met` triggers from migration 000043, on any allow-listed trigger that is not enabled, and on any column default, `CHECK` constraint (including one added `NOT VALID`, which skips the owner-side validation but is still evaluated for new rows), rule action or trigger function in those relations that references a function the write role cannot itself `EXECUTE` (the `ALTER TABLE … SET DEFAULT` / `ADD CONSTRAINT … NOT VALID` forms of the same gadget, which the event trigger does not cover because the cold-residency lane needs `ALTER TABLE`)

#### Scenario: Write roles granted to other roles are refused

- **WHEN** either write role is granted to any other role (for example `GRANT nhms_ingest_rw TO nhms_display_ro`)
- **THEN** the flags audit raises a security-regression error naming both roles and the runner exits 3

#### Scenario: Cold tablespace grant is audited

- **WHEN** the tablespace `nhms_cold` exists and `nhms_ingest_rw` does not hold `CREATE` on it
- **THEN** the trailing audit reports the missing grant (a warning outside strict mode, an error under strict audit so the runner exits 3); when the tablespace is absent the audit prints an explicit "grant skipped" line instead of staying silent

#### Scenario: Program execution is closed

- **WHEN** either write role attempts `COPY … FROM PROGRAM`
- **THEN** the server refuses with a superuser-required error

#### Scenario: Runtime env files carry no superuser

- **WHEN** the ingest, download, compression, cold-residency and retention env files on node-27 are inspected
- **THEN** none names the `nhms` user in any credential form, the templates in `infra/env/` name the same roles as the live files, and the drill and compression-replay files are the only ones still naming `nhms`, each with its documented reason

#### Scenario: Each component runs under its role

- **WHEN** ingest, compression, cold-residency and retention each execute one real run under `nhms_ingest_rw`, and download under `nhms_download_rw`
- **THEN** each completes with its normal receipt, no `permission denied`, and the ingest tick's stats guard reports both ANALYZE legs as `ok`
