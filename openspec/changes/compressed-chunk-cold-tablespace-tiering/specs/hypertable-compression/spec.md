# Proposed survivor delta

Target contract only; canonical implementation is unchanged by this revision.

## MODIFIED Requirements

### Requirement: Recurring tiering functions SHALL run as the hypertable owner role, never as superuser

Recurring compression, decompression, retention and chunk ANALYZE SHALL use the
non-superuser hypertable owner role for `hydro.river_timeseries` and
`met.forcing_station_timeseries` when invoked by a recurring
runtime unit (compression, retention); the documented migration-class exceptions
(the one-shot compression-replay supervisor, whose run plan includes one
`decompress_chunk` leg alongside `pg_dump` / `migration_apply` / `pg_restore`,
and the archive-rebuild drill) keep the migration role `nhms` and are recorded
as such in the runtime env template and the tier runbook; ownership SHALL be
transferred with explicit schema-scoped `ALTER … OWNER TO` statements (never
`REASSIGN OWNED`), and the provision audit SHALL assert the owner of every
compression-capable hypertable. Retired cold-residency movement and `CREATE` on
its tablespace SHALL NOT be recurring maintenance requirements.

#### Scenario: Owner role compresses and drops

- **WHEN** `nhms_ingest_rw` runs the compression and retention runners
- **THEN** `compress_chunk` and `drop_chunks` succeed and the stats guard's
  chunk `ANALYZE` refreshes `last_analyze`

#### Scenario: Non-owner writer is refused tiering

- **WHEN** `nhms_download_rw` calls `compress_chunk` on a chunk
- **THEN** the server refuses with an owner-required error while a
  privilege-shape INSERT into the hypertable still succeeds

#### Scenario: A new hypertable owned by the migration role is caught

- **WHEN** a migration creates a compression-capable hypertable owned by `nhms`
- **THEN** the provision audit reports the owner drift until the script is
  re-run

## ADDED Requirements

### Requirement: Normal compression SHALL launch safely without cold configuration

The compression unit/wrapper/preflight/budget owner SHALL use only compression
configuration and a single compression launch leg. Statement and cleanup budgets
SHALL fit the finite wrapper budget, which SHALL fit the finite systemd budget.
Cold env loading, paired-lane fields/defaults/mirrors/arguments and
compatibility aliases SHALL be removed from all receipt/config consumers.
Held-descriptor mode-0600/no-symlink inert parsing, import-origin validation,
argv execution, bounded termination/cleanup, secret-safe refusal and the fixed
lifecycle mutex before lane-local/database locks SHALL survive. Ordinary
compression selection, retention windows and discovery/lag behavior SHALL remain
unchanged.

#### Scenario: Cold env is absent

- **WHEN** the real compression wrapper/CLI launches with safe compression
  configuration and no cold env file
- **THEN** ordinary compression runs without a cold launcher or paired budget
  dependency and its receipt reflects only surviving behavior

#### Scenario: Configuration or locking is unsafe

- **WHEN** the compression env is malformed, symlinked, wrong-mode or untrusted
  in origin, or the lifecycle mutex cannot be acquired
- **THEN** launch refuses without shell-evaluating config, disclosing secrets or
  performing unowned maintenance

#### Scenario: Execution exceeds its bounded budget

- **WHEN** a compression child exceeds the configured finite timeout
- **THEN** bounded termination and cleanup finish within the wrapper/systemd
  relationship without starting a cold leg or leaving the lifecycle mutex owned
