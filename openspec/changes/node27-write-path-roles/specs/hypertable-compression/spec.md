## ADDED Requirements

### Requirement: Recurring tiering functions SHALL run as the hypertable owner role, never as superuser

`compress_chunk`, `decompress_chunk`, `drop_chunks`, chunk `ANALYZE` and `ALTER … SET TABLESPACE` on `hydro.river_timeseries` and `met.forcing_station_timeseries` SHALL be executed by a non-superuser role that owns those hypertables when invoked by a recurring runtime unit (compression, retention, cold-residency); the documented migration-class exceptions (the one-shot compression-replay supervisor, whose run plan includes one `decompress_chunk` leg alongside `pg_dump` / `migration_apply` / `pg_restore`, and the archive-rebuild drill) keep the migration role `nhms` and are recorded as such in the runtime env template and the tier runbook; ownership SHALL be transferred with explicit schema-scoped `ALTER … OWNER TO` statements (never `REASSIGN OWNED`), the role SHALL hold `CREATE` on the cold tablespace, and the provision audit SHALL assert the owner of every compression-capable hypertable.

#### Scenario: Owner role compresses and drops

- **WHEN** `nhms_ingest_rw` runs the compression and retention runners
- **THEN** `compress_chunk`, `drop_chunks` and the cold-residency `SET TABLESPACE` succeed and the stats guard's chunk `ANALYZE` refreshes `last_analyze`

#### Scenario: Non-owner writer is refused tiering

- **WHEN** `nhms_download_rw` calls `compress_chunk` on a chunk
- **THEN** the server refuses with an owner-required error while a privilege-shape INSERT into the hypertable still succeeds

#### Scenario: A new hypertable owned by the migration role is caught

- **WHEN** a migration creates a compression-capable hypertable owned by `nhms`
- **THEN** the provision audit reports the owner drift until the script is re-run
