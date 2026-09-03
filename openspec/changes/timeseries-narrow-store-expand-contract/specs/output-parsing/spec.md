## MODIFIED Requirements

### Requirement: TimescaleDB ingestion into river_timeseries

Parsed and converted data MUST be written to the narrow `hydro.river_timeseries` with ALL of its columns: `run_key`, `basin_version_key`, `river_network_version_key`, `river_segment_key`, `valid_time`, `lead_time_hours`, `variable_e`, `value`, `unit_e`, `quality_flag_e`. The composite primary key is `(run_key, river_segment_key, variable_e, valid_time)`. The write MUST use upsert semantics (`INSERT ... ON CONFLICT DO UPDATE`) on this composite PK to support idempotent re-parsing. `variable_e` MUST be set to `'q_down'` (not `'discharge'`) for `.rivqdown` data, per DB query pattern section 6.1. `unit_e` MUST be set to `'m3/s'`. `run_key` and `basin_version_key` MUST be looked up from the `hydro.hydro_run` record and `river_network_version_key`/`river_segment_key` from `core.river_segment`; no text identity column is written because the narrow table has none (`timeseries-narrow-store`). The `lead_time_hours` MUST be calculated as the difference between each `valid_time` and `hydro.hydro_run.cycle_time`, expressed in hours, and MUST be NULL for analysis runs.

#### Scenario: Successful ingestion of a complete forecast

- **WHEN** the parser processes a `.rivqdown` with 7 time steps and 10 segments
- **THEN** exactly 70 rows MUST be inserted into `hydro.river_timeseries`
- **THEN** each row MUST have `run_key` and `basin_version_key` from the hydro_run record, `river_network_version_key` and `river_segment_key` from the segment authority row, `variable_e = 'q_down'`, `value` in m³/s, `unit_e = 'm3/s'`, `lead_time_hours` calculated from run cycle_time, and `quality_flag_e`
- **THEN** the `valid_time` MUST be a proper `TIMESTAMPTZ` derived from the time column (SHUD output time converted to UTC using run start_time as reference)

#### Scenario: Upsert overwrites on re-parse

- **WHEN** the parser is run a second time for the same `run_id`
- **THEN** the `ON CONFLICT` clause MUST update the `value` column with the new data
- **THEN** no duplicate primary key violations MUST occur
- **THEN** the final row count for that `run_id` MUST remain 70 (not 140)

#### Scenario: Batch ingestion performance

- **WHEN** the parser ingests data for a run with 50 segments and 168 hourly time steps (8400 rows)
- **THEN** the ingestion MUST complete within a reasonable time by using batch inserts (e.g., `executemany` or `COPY`) rather than row-by-row inserts
- **THEN** the batch size MUST be configurable (default: 1000 rows per batch)
