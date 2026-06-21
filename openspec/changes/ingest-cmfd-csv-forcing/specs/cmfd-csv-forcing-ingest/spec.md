## ADDED Requirements

### Requirement: CMFD Data Source Registration

The system SHALL register a new row in `met.data_source` with `source_id='cmfd'` so that downstream `met.forcing_version.source_id` FK references resolve and the data source is discoverable in API listings.

#### Scenario: Initial migration seeds CMFD data source

- **WHEN** the new DB migration `db/migrations/000040_seed_cmfd_data_source.sql` runs on a node-27 DB instance where `met.data_source` has only `('gfs', 'IFS')`
- **THEN** an additional row exists with `source_id='cmfd'`, `source_name='CMFD 0.1° static historical forcing'`, `source_type='archive_static'`, `status='enabled'`, `native_format='csv'`, `adapter_name='cmfd_csv_adapter'`, and `config_json` exactly equal to the JSON object `{"grid_resolution_deg": 0.1, "time_step_seconds": 10800, "variables": ["PRCP","TEMP","RH","wind","Rn"], "source_citation": "Yang et al. 2010"}` (these four keys are the complete required set; any other keys are forbidden in this initial seed row)

#### Scenario: Migration is idempotent on re-run

- **WHEN** the migration runs a second time on the same DB
- **THEN** the existing `cmfd` row is unchanged and no error is raised (uses `ON CONFLICT (source_id) DO NOTHING`)

#### Scenario: API queries with source_id='cmfd' do not 422 before ingest

- **GIVEN** the `cmfd` data source row exists but no `met.forcing_version` rows with `source_id='cmfd'` exist
- **WHEN** a client calls `GET /api/v1/met/stations/{id}/series?source_id=cmfd&cycle_time=...&model_id=...`
- **THEN** the API SHALL return 404 `FORCING_VERSION_NOT_FOUND` (not 422 or 500), because `source_id='cmfd'` is a valid free-form value and the FK constraint allows future inserts

### Requirement: CMFD CSV File Parser

The system SHALL parse CMFD per-grid-cell CSV files into station-level time series records, correctly handling the CMFD-specific 2-line header format, fractional-days-from-start time encoding, and 5-variable schema.

#### Scenario: Parser reads valid CMFD CSV header

- **WHEN** the parser opens a CSV with first line `216224\t6\t19510101\t20250101\t86400` and second line `Time_interval\tPrecip_mm.d\tTemp_C\tRH_1\tWind_m.s\tRN_w.m2`
- **THEN** it SHALL extract `row_count=216224`, `n_vars=6`, `start_date=date(1951, 1, 1)`, `end_date=date(2025, 1, 1)`, `epoch_factor_sec=86400`, and `variable_columns=['Precip_mm.d', 'Temp_C', 'RH_1', 'Wind_m.s', 'RN_w.m2']`
- **AND** SHALL compute `step_seconds = round((end_date - start_date).total_seconds() / (row_count - 1))` and SHALL abort with a typed error if `step_seconds != 10800` (CMFD must be 3-hour resolution)

#### Scenario: Parser converts time_interval to valid_time

- **WHEN** the parser reads a data row with `time_interval=0.875` from a CSV whose `start_date='1951-01-01'` and `epoch_factor_sec=86400`
- **THEN** the corresponding `valid_time` SHALL be `datetime(1951, 1, 1, 21, 0, 0, tzinfo=UTC)` (1951-01-01 + 0.875 × 86400 sec = 21:00 UTC)

#### Scenario: Parser maps CMFD variable names to canonical variables

- **WHEN** the parser produces output records
- **THEN** the variable name and unit SHALL be normalized as (aligned with `openspec/specs/canonical-conversion/spec.md:46,61,66,68`): `Precip_mm.d → ('PRCP', 'mm/day')`, `Temp_C → ('TEMP', 'degC')`, `RH_1 → ('RH', '0-1')`, `Wind_m.s → ('wind', 'm/s')`, `RN_w.m2 → ('Rn', 'W/m2')`

#### Scenario: Parser skips missing values (does not write NULL)

- **GIVEN** `met.forcing_station_timeseries.value DOUBLE PRECISION NOT NULL` (schema enforced — `db/migrations/000005_met.sql:106`)
- **WHEN** the parser encounters a NaN, +Inf, -Inf, or empty cell in a data row for a given (station × variable × valid_time) tuple
- **THEN** the parser SHALL omit that single (variable, valid_time) entry from the yielded record (the other 4 variables at the same valid_time still emit normally)
- **AND** the parser SHALL increment a per-basin `skipped_missing_count` counter accessible via its API (used by the orchestrator receipt)
- **AND** the resulting `met.forcing_station_timeseries` SHALL contain NO row with `value IS NULL`, NO row with `value = 'NaN'::float8`, and NO row with `value = 'Infinity'::float8`

#### Scenario: Parser aborts when missing-value ratio exceeds 1%

- **WHEN** the parser completes a single station CSV and `skipped_missing_count_for_station / total_cells_in_station > 0.01`
- **THEN** the per-basin ingester SHALL abort the basin, ROLLBACK any partial work, and record `failed: missing_ratio_exceeded` with the offending station_id and ratio in the receipt

#### Scenario: Parser aborts on malformed header

- **WHEN** the CSV header does not match the expected 2-line CMFD format (e.g., wrong field count, non-integer start_date, missing variable line)
- **THEN** the parser SHALL raise a typed `CMFDCSVFormatError` with the file path and offending line, and SHALL NOT return partial records

#### Scenario: Parser rejects degenerate row_count

- **WHEN** the CSV header declares `row_count <= 1` (insufficient data to compute step_seconds via `(end_date - start_date) / (row_count - 1)`) OR the data section is empty
- **THEN** the parser SHALL raise a typed `CMFDCSVFormatError` with message `"insufficient data rows: row_count=<n>"` and SHALL NOT attempt to compute step_seconds

### Requirement: CMFD Station Seeding

The system SHALL populate `met.met_station` rows for the 6 basins (`weiganhe`, `xinanjiang_upstream`, `kashigeer`, `keliya`, `hetianhe`, `qinyijiang`) that currently lack station registrations, by enumerating `/home/ghdc/nwm/Basins/<basin>/forcing/X<lon>Y<lat>.csv` files. For `heihe` and `qhh`, the seeder SHALL detect existing stations and tag them as CMFD-sourced WITHOUT creating duplicates.

#### Scenario: Seeder creates new stations for a basin with no existing met_station rows

- **GIVEN** the basin `weiganhe` has 401 CSV files matching `X<lon>Y<lat>.csv` under `/home/ghdc/nwm/Basins/weiganhe/forcing/` and `met.met_station` has 0 rows for `basin_version_id` of `basins_weiganhe_shud`
- **WHEN** the station seeder runs against this basin
- **THEN** new rows SHALL be inserted into `met.met_station`, one per X<lon>Y<lat>.csv file matched, each with:
  - `station_id` matching pattern `weiganhe_cmfd_X<lon>Y<lat>` (basin-prefixed to avoid collisions)
  - `basin_version_id` = the active basin_version_id of `basins_weiganhe_shud`
  - `geom = ST_SetSRID(ST_MakePoint(<lon>, <lat>), 4490)`
  - `station_role='forcing_grid'`
  - `active_flag=true`
  - `properties_json` exactly equal to `{"forcing_filename": "<X<lon>Y<lat>.csv>", "seed": "cmfd_station_seeder", "grid_resolution_deg": 0.1, "source_id": "cmfd"}` (4 keys; **NO** `forcing_mapping_mode` key — see design.md AD-6 for why)
- **AND** the inserted row count SHALL equal the number of CSV files matching the X<lon>Y<lat>.csv pattern in the forcing dir (typically 401 for weiganhe at time of writing; the actual disk count is the authoritative figure)

#### Scenario: Seeder is idempotent for heihe / qhh existing stations

- **GIVEN** `met.met_station` already has rows for `basins_heihe_shud` (seeded by `qhh_production_bootstrap.py`) with `properties_json.forcing_filename` populated AND those filename values correspond 1:1 to files in `/home/ghdc/nwm/Basins/heihe/forcing/X<lon>Y<lat>.csv` (verified at Task §0 pre-impl introspection)
- **WHEN** the station seeder runs against `heihe`
- **THEN** NO new rows SHALL be inserted (existing station_id PK already present, looked up by `properties_json->>'forcing_filename'` reverse-join)
- **AND** the seeder SHALL `UPDATE met.met_station SET properties_json = jsonb_set(jsonb_set(properties_json, '{source_id}', '"cmfd"'), '{cmfd_seeded_at}', to_jsonb(NOW()))` for those existing rows; SHALL NOT modify other properties_json keys; SHALL NOT modify other columns (`station_role`, `geom`, `elevation_m`, `basin_version_id`, `active_flag`)
- **AND** the seeder SHALL log + receipt that `heihe` had `existing_stations=<actual count>, new_stations_inserted=0, updated_with_cmfd_marker=<actual count>`

#### Scenario: Pre-impl introspection verified heihe/qhh forcing_filename match

- **GIVEN** Task §0 pre-impl introspection on node-27 ran `SELECT station_id, properties_json->>'forcing_filename' FROM met.met_station WHERE basin_version_id = <heihe bv_id> LIMIT 10` and compared against `ls /home/ghdc/nwm/Basins/heihe/forcing/X*.csv`
- **WHEN** the introspection finding is recorded in design.md Open Questions or a PR comment
- **THEN** the recorded finding SHALL state either: (a) "match confirmed: existing forcing_filename values correspond to forcing dir CSV filenames" — implementer proceeds; OR (b) "mismatch: <reason>" — design must escalate before PR-A opens (heihe/qhh idempotency scenario above is invalid until resolution)

#### Scenario: Seeder rejects CSV filenames not matching the X<lon>Y<lat>.csv pattern

- **WHEN** the seeder encounters a file like `Prcp_Correction.csv` or `forcing_log.csv` in the forcing dir
- **THEN** it SHALL skip the file and NOT raise an error, and SHALL record the file name in the per-basin receipt under `skipped_files: [...]`

#### Scenario: Seeder aborts if basin_version_id cannot be resolved

- **WHEN** the seeder attempts to seed a basin but the model_id `basins_<basin>_shud` has no active row in `core.model_instance` (or no resolvable `basin_version_id`)
- **THEN** the seeder SHALL raise a typed `CMFDStationSeederError` and SHALL NOT insert any rows for that basin; orchestrator marks this basin as `failed: no_active_model`

### Requirement: CMFD Forcing Timeseries Ingestion

The system SHALL ingest the parsed CSV records into `met.forcing_version` (one row per basin) and `met.forcing_station_timeseries` (one row per station × variable × valid_time tuple), within a single per-basin DB transaction.

#### Scenario: Single-basin ingest creates one forcing_version row

- **WHEN** the timeseries ingester runs against basin `keliya` (32 stations, CSV start_date=1951-01-01, end_date=2025-01-01)
- **THEN** exactly one new row appears in `met.forcing_version` with:
  - `forcing_version_id='forc_cmfd_19510101_basins_keliya_shud'`
  - `model_id='basins_keliya_shud'`
  - `source_id='cmfd'`
  - `cycle_time='1951-01-01T00:00:00Z'`
  - `start_time='1951-01-01T00:00:00Z'`
  - `end_time` matches CSV header end_date (UTC midnight at the last valid_time row)
  - `station_count=32`
  - `forcing_package_uri` SHALL match the literal pattern `file://<absolute path to basin forcing dir>/` with trailing slash, computed from the orchestrator's `--basins-root` argument (e.g. `file:///home/ghdc/nwm/Basins/keliya/forcing/`)
  - `checksum` set to a non-empty deterministic hash (SHA256 hex-encoded over the canonical-sorted tuples `(station_id, variable, valid_time, value)` sorted lexically by `(station_id, variable, valid_time ASC)`); `checksum != 'pending'`
  - `lineage_json` containing AT MINIMUM the keys `{"ingest_method": "cmfd_csv_direct", "csv_file_count": 32, "rows_written": <count>, "skipped_missing_count": <count>}`

#### Scenario: Single-basin ingest writes all non-missing timeseries rows

- **GIVEN** the keliya CSV files contain 216224 records each across 5 variables (PRCP, TEMP, RH, wind, Rn) for 32 stations
- **WHEN** the timeseries ingester completes successfully
- **THEN** `met.forcing_station_timeseries` SHALL contain `(32 × 5 × 216224) − skipped_missing_count` rows where `forcing_version_id='forc_cmfd_19510101_basins_keliya_shud'` (the exact count is `32 × 5 × 216224 = 34,595,840` ONLY when the parser-emitted `skipped_missing_count == 0`; per "Parser skips missing values" scenario NaN/Inf/empty cells are omitted from the insert set, not written as NULL or with `quality_flag='missing'`)
- **AND** every written row SHALL have `quality_flag='ok'`, `value IS NOT NULL`, `value` is a finite double (not NaN, not Infinity), `unit` matching the variable convention from the parser (see "Parser maps CMFD variable names" scenario), and `valid_time` in UTC
- **AND** the per-basin receipt SHALL surface `skipped_missing_count` so operators can audit the gap between theoretical max (`32 × 5 × 216224`) and actual rows written

#### Scenario: Single-basin ingest is atomic (all-or-nothing)

- **GIVEN** the timeseries ingester is mid-flight on basin `weiganhe` and a downstream INSERT raises an error (e.g., FK violation or disk full)
- **WHEN** the per-basin transaction handler catches the exception
- **THEN** the transaction SHALL be rolled back, leaving `met.forcing_version` WITHOUT a `weiganhe` CMFD row AND `met.forcing_station_timeseries` WITHOUT any weiganhe CMFD timeseries rows (no partial state)
- **AND** the orchestrator SHALL mark this basin as `failed` in the receipt with the underlying error message

#### Scenario: Re-ingest with identical CSV is idempotent

- **GIVEN** `weiganhe` has been ingested once (rows present in both tables for `forcing_version_id='forc_cmfd_19510101_basins_weiganhe_shud'`)
- **WHEN** the ingester is invoked a second time with the same basin and the CSV files on disk are unchanged (computed checksum = prior DB checksum)
- **THEN** in a single transaction, the ingester SHALL invoke `replace_forcing_timeseries` (DELETE old + INSERT new) BEFORE invoking `upsert_forcing_version` (UPDATE station_count + checksum + lineage_json); this ORDER avoids a transient snapshot where `station_count` mismatches actual timeseries station count
- **AND** post-commit row counts SHALL match a fresh ingest (no double-insert, no leftover stale rows; the resulting `forcing_version.checksum` equals the prior value bit-for-bit)

#### Scenario: Re-ingest with changed CSV is rejected without --force

- **GIVEN** `weiganhe` has been ingested once (DB has `forcing_version` with checksum X)
- **AND** an operator has modified the CMFD CSV files on disk (e.g. corrected data, appended years), so the freshly-computed checksum is Y ≠ X
- **WHEN** the ingester runs WITHOUT the `--force` flag
- **THEN** the ingester SHALL raise a typed `CMFD_FORCING_VERSION_CHECKSUM_CONFLICT` error containing both checksums, abort the transaction, and SHALL NOT modify any DB row

#### Scenario: Re-ingest with changed CSV proceeds with --force

- **GIVEN** the same conditions as the prior scenario
- **WHEN** the ingester runs WITH the `--force` flag
- **THEN** the ingester SHALL proceed, write the new timeseries + forcing_version, AND SHALL record the prior checksum under `lineage_json.previous_checksum` so the overwrite is auditable

#### Scenario: API series query returns ingested data

- **GIVEN** the keliya basin ingest has committed successfully
- **WHEN** a client calls `GET /api/v1/met/stations/keliya_cmfd_X82.45Y36.25/series?source_id=cmfd&cycle_time=1951-01-01T00:00:00Z&model_id=basins_keliya_shud&variables=PRCP,TEMP&limit=10`
- **THEN** the API SHALL return HTTP 200 with `series` containing 2 entries (`variable='PRCP'` and `variable='TEMP'`), each with up to 10 `points` ordered by `valid_time`, and `forcing_version_id='forc_cmfd_19510101_basins_keliya_shud'`

#### Scenario: API series query does not see CMFD timeseries for non-CMFD source_id

- **GIVEN** the keliya basin ingest has committed CMFD rows
- **WHEN** a client calls the same endpoint but with `source_id=gfs` (or any non-cmfd value)
- **THEN** the API SHALL return 404 `FORCING_VERSION_NOT_FOUND` because no `gfs` forcing_version exists for keliya
- **AND** no CMFD timeseries rows SHALL be returned (source_id isolation preserved at the forcing_version level)

### Requirement: Batch Orchestration and Receipt

The system SHALL provide a batch orchestration script that ingests all 8 eligible basins from `/home/ghdc/nwm/Basins/`, skips the 2 basins without forcing directories, and emits a structured aggregate receipt.

#### Scenario: Orchestrator skips basins without forcing directory

- **WHEN** the orchestrator runs against `/home/ghdc/nwm/Basins/`
- **THEN** it SHALL detect that `tailanhe` and `zhaochen` have no `forcing/` subdirectory
- **AND** SHALL skip them (do NOT call station_seeder or timeseries_ingester for them)
- **AND** SHALL record them in the receipt under `skipped_basins` with `reason='no_forcing_dir'`

#### Scenario: Orchestrator skips basins with empty forcing directory

- **GIVEN** a basin whose `forcing/` directory exists but contains zero files matching `X<lon>Y<lat>.csv` pattern (e.g. only `Prcp_Correction.csv` or `README.md`)
- **WHEN** the orchestrator processes this basin
- **THEN** it SHALL NOT call station_seeder (would insert 0 rows; misleading)
- **AND** SHALL NOT call timeseries_ingester (would attempt to write forcing_version with station_count=0; meaningless)
- **AND** SHALL record the basin in the receipt under `skipped_basins` with `reason='no_eligible_csv_files'`

#### Scenario: Orchestrator aborts on replica DB

- **WHEN** the orchestrator starts and queries `SELECT pg_is_in_recovery()` against the configured DATABASE_URL
- **AND** the result is `true` (the DB is in recovery / standby / replica mode)
- **THEN** the orchestrator SHALL abort with exit code != 0 BEFORE collecting baseline metrics or invoking any worker
- **AND** SHALL emit a receipt with `aborted_reason='not_primary_db'` and a hint that node-27 is the project's primary DB

#### Scenario: Orchestrator rejects --basin with nonexistent name

- **WHEN** the orchestrator is invoked with `--basin <name>` and `<name>` does not match any directory under `--basins-root`
- **THEN** the orchestrator SHALL abort with exit code != 0 BEFORE any DB access, emitting `CMFD_BASIN_NOT_FOUND: <name>`
- **AND** if at least one valid basin is provided alongside the invalid one (e.g., `--basin keliya --basin keliyaa`), the orchestrator SHALL still abort fail-fast (no partial ingest of valid basins)

#### Scenario: Orchestrator halts on mid-run disk pressure

- **GIVEN** the pre-ingest disk capacity check passed at start
- **WHEN** the orchestrator completes a basin commit and re-queries PG data dir free-bytes (between basins)
- **AND** the free-bytes has dropped below 10% of total OR below the pre-ingest threshold
- **THEN** the orchestrator SHALL halt before the next basin, record `aborted_reason='disk_capacity_mid_run'` in the receipt, and emit the per-basin rollback SQL block for any successfully-committed basins so operator can selectively undo

#### Scenario: Orchestrator processes 8 eligible basins serially

- **WHEN** the orchestrator finds 8 basins with `forcing/` directories present
- **THEN** it SHALL process basins in order from smallest CSV row_count to largest (keliya → ... → heihe), calling station_seeder then timeseries_ingester for each
- **AND** SHALL NOT process basins in parallel (one basin at a time, serial)
- **AND** SHALL continue to the next basin even if one fails (when invoked with `--continue-on-error` flag); without that flag it SHALL halt on first failure

#### Scenario: Orchestrator emits structured receipt

- **WHEN** the orchestrator completes (success or partial failure)
- **THEN** it SHALL write a JSON receipt to the path provided by `--output` with schema:
  - Top-level: `schema_version='cmfd.ingest_aggregate.v1'`, `started_at`, `finished_at`, `basins_root`, `total_basins_discovered`, `total_basins_processed`, `total_basins_failed`, `total_basins_skipped`
  - `per_basin: [{basin, status, station_seeder: {existing_stations, new_stations_inserted, updated_with_cmfd_marker, skipped_files}, timeseries_ingester: {forcing_version_id, rows_written, elapsed_seconds, checksum}}, ...]`
  - `skipped_basins: [{basin, reason}, ...]`
  - `failed_basins: [{basin, stage: 'seeder'|'ingester', error_message}, ...]`
  - `pre_ingest_baseline: {met_station_row_count, forcing_version_row_count, hypertable_size_pretty}`
  - `post_ingest_metrics: {met_station_row_count, forcing_version_row_count, hypertable_size_pretty}`
  - `rollback_sql_per_basin: {<basin>: ['<DELETE statement>', ...]}` (operator can copy-paste to undo per-basin ingest)

#### Scenario: Orchestrator respects --basin filter for partial runs

- **WHEN** the orchestrator is invoked with `--basin keliya --basin hetianhe`
- **THEN** it SHALL process ONLY those 2 basins (and skip even `tailanhe`/`zhaochen` without recording them in `skipped_basins`)
- **AND** the receipt SHALL reflect `total_basins_processed=2` and the per-basin entries match

#### Scenario: Orchestrator captures pre-ingest baseline before first basin

- **WHEN** the orchestrator starts
- **THEN** it SHALL query `met.met_station`, `met.forcing_version`, and `met.forcing_station_timeseries` row counts plus `hypertable_size('met.forcing_station_timeseries')` and record these as `pre_ingest_baseline` BEFORE invoking the first basin
- **AND** if the baseline query fails the orchestrator SHALL abort without modifying any data

### Requirement: Scope and Operational Limits

The change SHALL explicitly enforce documented out-of-scope boundaries and operational guardrails so that future code or operators do not silently expand ingest behavior.

#### Scenario: Press variable is not synthesized

- **WHEN** the timeseries_ingester writes records to `met.forcing_station_timeseries` for any CMFD basin
- **THEN** no row SHALL have `variable='Press'` (CMFD CSVs lack this variable)
- **AND** the API `GET /met/stations/{id}/series?source_id=cmfd&variables=Press` SHALL return an empty `series` entry (or omit `Press` from the response) without raising an error
- **AND** a post-ingest test SHALL assert `SELECT COUNT(*) FROM met.forcing_station_timeseries WHERE forcing_version_id LIKE 'forc_cmfd_%' AND variable='Press' == 0`

#### Scenario: Prcp_Correction is not applied in this change

- **WHEN** the ingester processes a basin where `forcing/Prcp_Correction.csv` exists
- **THEN** the file SHALL be left untouched on disk, no correction multipliers SHALL be applied to `Precip_mm.d` values written to `met.forcing_station_timeseries`, and the per-basin receipt entry SHALL include both `prcp_correction_applied: false` AND `prcp_correction_csv_present: true`
- **AND** a smoke test on a basin with Prcp_Correction.csv present SHALL assert the receipt JSON contains the exact field `prcp_correction_applied: false`

#### Scenario: canonical_met_product / interp_weight / forcing_version_component are not populated

- **WHEN** the timeseries_ingester completes a basin
- **THEN** `met.canonical_met_product` SHALL NOT contain any rows with `source_id='cmfd'` (the CMFD path bypasses canonical conversion); a post-ingest test SHALL assert `SELECT COUNT(*) FROM met.canonical_met_product WHERE source_id='cmfd' == 0`
- **AND** `met.interp_weight` SHALL NOT contain any rows with `source_id='cmfd'` (1:1 station↔grid CMFD does not require IDW weights); a post-ingest test SHALL assert `SELECT COUNT(*) FROM met.interp_weight WHERE source_id='cmfd' == 0`
- **AND** `met.forcing_version_component` SHALL NOT contain rows tying any CMFD `forcing_version_id` to any canonical product; a post-ingest test SHALL assert `SELECT COUNT(*) FROM met.forcing_version_component WHERE forcing_version_id LIKE 'forc_cmfd_%' == 0`

#### Scenario: Disk capacity pre-check abort

- **WHEN** the orchestrator starts and the pre-ingest baseline query reports `hypertable_size + estimated_new_rows × avg_row_bytes > 0.7 × pg_data_dir_free_bytes`
- **THEN** the orchestrator SHALL refuse to proceed and SHALL emit a receipt with `aborted_reason='disk_capacity_pre_check_failed'`, requiring operator intervention
- **AND** the disk capacity threshold (70%) and the estimation formula SHALL be documented in the runbook with the source of `avg_row_bytes` calibration

### Requirement: Receipt-First Verification on Node-27

The change SHALL produce verifiable evidence of correctness on node-27 (the only oracle for real-DB ingest), not on local mock or CI.

#### Scenario: Real-DB integration test runs on node-27

- **WHEN** the implementer completes the worker code
- **THEN** a pytest in `tests/test_cmfd_csv_ingest_timeseries_ingester_real_db.py` (flat layout, consistent with `tests/test_forcing_producer.py`) SHALL run against the node-27 primary DB (gated by `real-db-integration` marker or `NWM_REAL_DB_URL` env), exercising at minimum: parser → station_seeder → timeseries_ingester → forcing_version row → timeseries row count → API series query round-trip, for a small basin (keliya) with `--limit` to avoid full ingest cost
- **AND** the test SHALL clean up (DELETE) its own rows after assertion, preserving DB state for other tests

#### Scenario: Production batch ingest receipt is committed to runbook

- **WHEN** the batch orchestrator completes the 8-basin ingest on node-27 for the first time
- **THEN** the operator SHALL commit the resulting receipt JSON file under `docs/runbooks/receipts/cmfd-ingest-<UTC date>.md` (markdown wrapper around the JSON with operator notes), including basin-by-basin timings, final row counts, and the rollback SQL block

#### Scenario: API curl e2e verification on node-27

- **GIVEN** the frontend `/meteorology` UI is OUT OF SCOPE (frontend `HydroMetSource` hardcoded to `'GFS' | 'IFS'` per proposal.md OUT OF SCOPE section)
- **WHEN** the production batch ingest is complete and uvicorn is serving from the post-ingest DB
- **THEN** the operator SHALL run `curl` against `GET /api/v1/met/stations/<station_id>/series?source_id=cmfd&cycle_time=<basin start_date>&model_id=basins_<basin>_shud&variables=PRCP,TEMP&limit=10` for AT LEAST one station per ingested basin (e.g. one heihe + one weiganhe + one keliya call) and confirm:
  - HTTP 200 status
  - `series` field non-empty
  - At least 2 entries in `series` (PRCP, TEMP)
  - Each entry's `points` array non-empty (up to 10 points) with `valid_time` in expected range
  - `forcing_version_id` matches expected `forc_cmfd_<YYYYMMDD>_basins_<basin>_shud`
- **AND** the curl outputs (or pretty-printed JSON snippets) SHALL be appended to the runbook receipt (`docs/runbooks/receipts/cmfd-ingest-<UTC date>.md`) under a "Live API verification" section
- **AND** a separate follow-up issue SHALL be created (or referenced) tracking frontend UI exposure work (extend `HydroMetSource`, add bootstrap branch, UI source selector); the issue link SHALL be cited in this change's runbook
