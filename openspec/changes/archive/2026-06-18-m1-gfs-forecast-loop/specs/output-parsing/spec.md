# Output Parsing

Capability: `output-parsing`
Status: draft
Parent: m1-gfs-forecast-loop

## ADDED Requirements

### Requirement: File parsing of .rivqdown format

The parser MUST read `.rivqdown` files produced by SHUD. The file format is a CSV/DAT text file where the first column is a time index and subsequent columns correspond to river segments in order. The parser MUST extract all data columns and associate each column with the correct `river_segment_id` based on the segment ordering defined in the `river_network_version`.

#### Scenario: Parse a well-formed .rivqdown file

- **WHEN** the parser reads a `.rivqdown` file from `runs/{run_id}/output/` containing 7 data rows and 10 river segment columns
- **THEN** the parser MUST extract 70 (time, segment, value) tuples
- **THEN** column index 1 MUST map to the first `river_segment_id` in the river_network_version's segment ordering
- **THEN** column index N MUST map to the Nth segment in order

#### Scenario: Parse .rivqdown with time column in SHUD epoch format

- **WHEN** the `.rivqdown` file uses SHUD's time format (minutes since simulation start or absolute timestamps)
- **THEN** the parser MUST convert each time value to a UTC `valid_time` (`TIMESTAMPTZ`)
- **THEN** the conversion MUST use the run's `start_time` from `hydro.hydro_run` as the epoch reference if times are relative

#### Scenario: Reject malformed .rivqdown file

- **WHEN** the `.rivqdown` file contains non-numeric values in data columns or has inconsistent column counts across rows
- **THEN** the parser MUST raise an error specifying the row number and nature of the malformation
- **THEN** the run status MUST NOT transition to `parsed`

### Requirement: Unit conversion from cubic meters per day to cubic meters per second

All flow values in `.rivqdown` are in m cubed per day (m³/d). The parser MUST convert every value to m cubed per second (m³/s) by dividing by 86400. The converted values MUST be stored in `hydro.river_timeseries`.

#### Scenario: Convert flow values correctly

- **WHEN** the parser reads a `.rivqdown` value of `86400.0` (m³/d)
- **THEN** the converted value written to `hydro.river_timeseries` MUST be `1.0` (m³/s)

#### Scenario: Handle zero and small values

- **WHEN** the parser reads a `.rivqdown` value of `0.0`
- **THEN** the converted value MUST be `0.0` (no division error)
- **THEN** values smaller than `1e-10` m³/d MUST be stored as-is after conversion (no rounding to zero)

#### Scenario: Conversion preserves precision

- **WHEN** the parser reads a value of `12345.6789` (m³/d)
- **THEN** the converted value MUST be `12345.6789 / 86400 = 0.14288980...` stored with at least 6 significant digits of precision (DOUBLE PRECISION column)

### Requirement: Column-segment consistency validation

Before ingestion, the parser MUST verify that the number of data columns in `.rivqdown` (excluding the time column) equals the number of `river_segment` records associated with the run's `river_network_version_id` in `core.river_segment`.

#### Scenario: Column count matches river segment count

- **WHEN** the `.rivqdown` file has 10 data columns and the `river_network_version` has 10 river segments in `core.river_segment`
- **THEN** validation MUST pass and parsing proceeds

#### Scenario: Column count mismatch detected

- **WHEN** the `.rivqdown` file has 12 data columns but the `river_network_version` has only 10 river segments
- **THEN** the parser MUST raise an error: "Column count mismatch: file has 12 columns, river_network_version has 10 segments"
- **THEN** no data MUST be written to `hydro.river_timeseries`
- **THEN** the run status MUST be set to `failed` with `error_code` and `error_message` recording the mismatch detail

#### Scenario: Segment ordering is loaded from database

- **WHEN** the parser validates column-segment consistency
- **THEN** the segment ordering MUST be loaded from `core.river_segment` filtered by `river_network_version_id`, ordered by `segment_order`
- **THEN** this ordering MUST be used to map column indices to `river_segment_id` values

### Requirement: TimescaleDB ingestion into river_timeseries

Parsed and converted data MUST be written to `hydro.river_timeseries` with ALL required columns: `run_id`, `basin_version_id`, `river_network_version_id`, `river_segment_id`, `valid_time`, `lead_time_hours`, `variable`, `value`, `unit`, `quality_flag`. The composite primary key is `(run_id, river_network_version_id, river_segment_id, variable, valid_time)`. The write MUST use upsert semantics (`INSERT ... ON CONFLICT DO UPDATE`) on this composite PK to support idempotent re-parsing. The `variable` column MUST be set to `'q_down'` (not `'discharge'`) for `.rivqdown` data, per DB query pattern section 6.1. The `unit` column MUST be set to `'m3/s'`. The `basin_version_id` MUST be looked up from the `hydro.hydro_run` record. The `lead_time_hours` MUST be calculated as the difference between each `valid_time` and `hydro.hydro_run.cycle_time`, expressed in hours.

#### Scenario: Successful ingestion of a complete forecast

- **WHEN** the parser processes a `.rivqdown` with 7 time steps and 10 segments
- **THEN** exactly 70 rows MUST be inserted into `hydro.river_timeseries`
- **THEN** each row MUST have `run_id` from the current run, `basin_version_id` from the hydro_run record, `river_network_version_id` from the model instance, `river_segment_id`, `variable = 'q_down'`, `value` in m³/s, `unit = 'm3/s'`, `lead_time_hours` calculated from run cycle_time, and `quality_flag`
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

### Requirement: QC validation

The parser MUST perform quality control checks on the converted flow values before or during ingestion. QC checks MUST include: (1) non-negative flow — all discharge values MUST be >= 0; (2) range check — no value SHALL exceed a configurable upper bound (default: 100000 m³/s). QC results MUST be written to `ops.qc_result`.

#### Scenario: All values pass QC

- **WHEN** all converted discharge values are non-negative and below the upper bound
- **THEN** a single `ops.qc_result` row MUST be inserted with `passed = true`
- **THEN** `checks_json` MUST contain a JSON object listing each check name and its result (e.g., `{"non_negative": {"passed": true, "count": 70}, "range_check": {"passed": true, "max_value": 42.5}}`)
- **THEN** `target_type` MUST be `'hydro_run'` and `target_id` MUST be the `run_id`

#### Scenario: Negative flow values detected

- **WHEN** the parser finds any converted value < 0
- **THEN** a `ops.qc_result` row MUST be inserted with `passed = false`
- **THEN** `checks_json` MUST list the failing segment IDs, time steps, and values
- **THEN** the data MUST still be ingested (QC is advisory, not blocking) but the run status MUST be flagged with a QC warning

#### Scenario: Extreme outlier detected

- **WHEN** a converted discharge value exceeds the configured upper bound (default: 100000 m³/s)
- **THEN** the QC result MUST record the outlier with `passed = false`
- **THEN** `checks_json` MUST include `"range_check": {"passed": false, "outliers": [{"segment_id": "...", "valid_time": "...", "value": ...}]}`

### Requirement: Idempotent re-parsing

The CLI `nhms-parse shud-output --run-id <run_id>` MUST be safely re-executable. Running the parser multiple times for the same `run_id` MUST produce the same final state in `hydro.river_timeseries` and `ops.qc_result`. No duplicate rows SHALL be created. The Slurm job `parse_output_array` MUST also be idempotent.

#### Scenario: Re-parse produces identical results

- **WHEN** a user runs `nhms-parse shud-output --run-id RUN001` twice in succession
- **THEN** the row count in `hydro.river_timeseries` for `run_id = 'RUN001'` MUST be identical after both runs
- **THEN** all `value` entries MUST be identical
- **THEN** the second `ops.qc_result` entry MUST overwrite or append without creating inconsistency

#### Scenario: Re-parse after source file update

- **WHEN** the `.rivqdown` file in object storage is replaced with a corrected version and the parser is re-run
- **THEN** the upsert MUST overwrite all previous values with the corrected data
- **THEN** a new `ops.qc_result` record MUST be created reflecting the updated data
- **THEN** the `hydro.hydro_run.status` transition MUST be re-evaluated based on the new QC results

#### Scenario: Concurrent parse attempts do not corrupt data

- **WHEN** two instances of the parser run simultaneously for the same `run_id` (e.g., Slurm retry)
- **THEN** the upsert semantics MUST ensure no primary key violations
- **THEN** the final data MUST be consistent (last writer wins on conflict)
