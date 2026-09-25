## ADDED Requirements

### Requirement: The migration runner MUST refuse a ledger/disk mismatch before applying anything

Before applying any migration, the runner MUST compare `public.schema_migrations` with the migration files. It MUST exit non-zero, with nothing applied and every offending name listed, when:

- a ledger version has no file on disk and is not a recorded retired version;
- two files on disk share a 6-digit prefix;
- a file on disk has the name of a recorded retired version.

Each recorded retired version MUST carry a reason naming the change that retired it. The runner's output MUST NOT contain a credential.

#### Scenario: An unrecorded missing file stops the run

- **WHEN** the ledger holds a version whose file is not on disk and that version is not recorded as retired
- **THEN** the runner SHALL exit non-zero, name that version, and apply no migration

#### Scenario: The recorded retirements pass

- **WHEN** the ledger holds exactly the 7 versions retired by `b97c16e2` besides the on-disk files, including both `000031` rows
- **THEN** the check SHALL pass and pending migrations SHALL be applied

#### Scenario: A duplicate prefix or a returning retired name stops the run

- **WHEN** two files on disk share a prefix, or a file is named like a retired version
- **THEN** the runner SHALL exit non-zero and apply no migration
