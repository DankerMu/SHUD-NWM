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

#### Scenario: Program execution is closed

- **WHEN** either write role attempts `COPY … FROM PROGRAM`
- **THEN** the server refuses with a superuser-required error

#### Scenario: Runtime env files carry no superuser

- **WHEN** the ingest, download, compression, cold-residency and retention env files on node-27 are inspected
- **THEN** none names the `nhms` user in any credential form, the templates in `infra/env/` name the same roles as the live files, and the drill and compression-replay files are the only ones still naming `nhms`, each with its documented reason

#### Scenario: Each component runs under its role

- **WHEN** ingest, compression, cold-residency and retention each execute one real run under `nhms_ingest_rw`, and download under `nhms_download_rw`
- **THEN** each completes with its normal receipt, no `permission denied`, and the ingest tick's stats guard reports both ANALYZE legs as `ok`
