## ADDED Requirements

### Requirement: Object-store path resolution from station_id

The series reader SHALL derive the absolute disk path of a station's per-cycle forcing CSV from API inputs (`station_id`, `source_id`, `cycle_time`, `model_id`) by joining `OBJECT_STORE_ROOT` with `forcing/{source_normalized}/{cycle_compact}/{basin_version_id}/{model_id}/shud/{forcing_filename}`.

- `source_normalized` SHALL lowercase API-provided `source_id`
- `cycle_compact` SHALL be UTC `YYYYMMDDHH` (10 chars, zero-padded)
- `basin_version_id` SHALL come from `met.met_station.basin_version_id` joined by `station_id`
- `forcing_filename` SHALL come from `met.met_station.properties_json->>'forcing_filename'`

#### Scenario: heihe IFS path resolves to expected disk file

- **WHEN** API receives `station_id=heihe_forc_001`, `source_id=IFS`, `cycle_time=2026-06-20T12:00:00Z`, `model_id=basins_heihe_shud`
- **AND** `met_station.basin_version_id='basins_heihe_vbasins'` and `properties_json.forcing_filename='X100.75Y37.65.csv'`
- **THEN** the reader SHALL resolve the path to `${OBJECT_STORE_ROOT}/forcing/ifs/2026062012/basins_heihe_vbasins/basins_heihe_shud/shud/X100.75Y37.65.csv`

#### Scenario: gfs and GFS both resolve to lowercase disk segment

- **WHEN** API receives `source_id=gfs` (already lowercase)
- **THEN** the path segment SHALL be `gfs` (not `GFS`)
- **AND** the same call with `source_id=GFS` SHALL also resolve to disk segment `gfs` (case-normalized)
- **AND** the same call with `source_id=Ifs` (mixed case) SHALL resolve to disk segment `ifs`

#### Scenario: cycle_time with explicit timezone normalizes to UTC compact

- **WHEN** API receives `cycle_time=2026-06-20T20:00:00+08:00`
- **THEN** the reader SHALL compute `cycle_compact=2026062012` (converted to UTC then formatted)
- **AND** the same call with `cycle_time=2026-06-20T12:00:00Z` SHALL produce the same `cycle_compact`
- **AND** a naive datetime input (without tzinfo) SHALL be treated as UTC and produce the same `cycle_compact`

#### Scenario: unknown station_id returns STATION_NOT_FOUND

- **WHEN** API receives a `station_id` not present in `met.met_station`
- **THEN** the reader SHALL raise HTTP 404 with code `STATION_NOT_FOUND` and details `{station_id}` (reusing the existing error shape from `packages/common/forecast_store.py:2099-2104`)

#### Scenario: station found but forcing_filename missing returns 500

- **WHEN** API receives a valid `station_id` whose `properties_json` does NOT contain `forcing_filename`
- **THEN** the reader SHALL raise HTTP 500 with code `STATION_FORCING_FILENAME_MISSING` and details `{station_id}`

#### Scenario: missing required filter returns 422

- **WHEN** API receives a request missing any of `cycle_time` / `model_id` / `source_id`
- **THEN** the API SHALL return HTTP 422 with code `MISSING_REQUIRED_FILTER` and details `{"required_alternatives": [["forcing_version_id"], ["model_id", "source_id", "cycle_time"]]}` (reusing the exact existing error shape from `packages/common/forecast_store.py:2132-2146`, the current station_series call-site)

### Requirement: CSV parse and valid_time computation

The reader SHALL parse the per-station shud CSV with the documented two-row header (`nrow ncol start_date end_date` then `Time_Day Precip Temp RH Wind RN`) and emit (variable, valid_time, value) tuples where `valid_time = cycle_time + timedelta(seconds=int(round(Time_Day*86400)))`.

#### Scenario: valid_time of first data row equals cycle_time

- **WHEN** the reader parses a shud CSV whose first data row has `Time_Day=0`
- **AND** the cycle_time is `2026-06-20T12:00:00Z`
- **THEN** the emitted `valid_time` of that row SHALL be `2026-06-20T12:00:00Z`

#### Scenario: 3-hour step at Time_Day=0.125

- **WHEN** the second data row has `Time_Day=0.125`
- **AND** cycle_time is `2026-06-20T12:00:00Z`
- **THEN** the emitted `valid_time` SHALL be `2026-06-20T15:00:00Z`

#### Scenario: last data row Time_Day=6.5 yields cycle + 6 days 12 hours

- **WHEN** the last data row has `Time_Day=6.5`
- **AND** cycle_time is `2026-06-20T12:00:00Z`
- **THEN** the emitted `valid_time` SHALL be `2026-06-27T00:00:00Z` (cycle + 561600 seconds)

#### Scenario: rounding handles non-exact 3h step deterministically

- **WHEN** a Time_Day value such as `0.041666` (≈ 1 hour) is encountered
- **AND** cycle_time is `2026-06-20T12:00:00Z`
- **THEN** the reader SHALL emit `valid_time=2026-06-20T13:00:00Z` (using `int(round(Time_Day*86400))=3600`, NOT `int(Time_Day*86400)=3599`)

#### Scenario: variable name mapping per units contract, with `unit` field

- **WHEN** the reader emits rows from a CSV column
- **THEN** the API `variable` field SHALL be: `Precip→PRCP`, `Temp→TEMP`, `RH→RH`, `Wind→wind`, `RN→Rn`
- **AND** the corresponding `unit` field SHALL be: `PRCP="mm/day"`, `TEMP="degC"`, `RH="0-1"`, `wind="m/s"`, `Rn="W/m^2"`
- **AND** the reader SHALL NOT emit a `Press` variable (source CSV has no Press column)

#### Scenario: N data rows produce 5 × N tuples per default request (no row-count hardcoding)

- **WHEN** the CSV header row 1 says `N\t6\t<start_date>\t<end_date>` for some row count N
- **AND** the data section has N rows
- **THEN** the reader SHALL emit exactly 5 (variable count) × N (row count) tuples for the default request (all variables)

#### Scenario: malformed CSV raises STATION_FORCING_FILE_MALFORMED

- **WHEN** the CSV is missing the header row or contains a non-numeric value where a numeric is expected
- **THEN** the reader SHALL raise HTTP 500 with code `STATION_FORCING_FILE_MALFORMED` and details `{station_id, expected_path, parse_reason}`

#### Scenario: declared nrow mismatch raises STATION_FORCING_FILE_MALFORMED

- **WHEN** the CSV header declares `nrow=53`
- **AND** the data section contains fewer or more than 53 data rows
- **THEN** the reader SHALL raise HTTP 500 with code `STATION_FORCING_FILE_MALFORMED`
- **AND** `details.parse_reason` SHALL identify the row-count mismatch

### Requirement: Series API disk-only behavior — bypasses forcing_version and forcing_station_timeseries

The `/api/v1/met/stations/{station_id}/series` route SHALL serve all series data from the per-station shud/CSV on disk and SHALL NOT query `met.forcing_version` or `met.forcing_station_timeseries` for series content. The route SHALL NOT call `PsycopgForecastStore.station_series()` or `_ensure_forcing_version_finalized()`. (The single `met.met_station` lookup for station metadata is allowed and required per AD-2; this is the only DB query.)

#### Scenario: latest cycle with checksum NULL returns 200 from disk

- **WHEN** API receives a cycle_time matching a `met.forcing_version` row whose `checksum IS NULL`
- **AND** the corresponding `shud/<forcing_filename>` file EXISTS on disk
- **THEN** the API SHALL return HTTP 200 with the parsed series (NOT 409 `FORCING_VERSION_NOT_FINALIZED`)

#### Scenario: malformed checksum 'pending' in DB does not affect disk response

- **WHEN** API receives a `cycle_time` whose `met.forcing_version.checksum='pending'`
- **AND** the corresponding shud/<forcing_filename> file exists on disk
- **THEN** the API SHALL return HTTP 200 (NOT 409 `FORCING_VERSION_NOT_FINALIZED`)

#### Scenario: forcing_version row missing entirely returns 200 from disk

- **WHEN** API receives a `cycle_time` for which `met.forcing_version` has NO matching row
- **AND** the corresponding shud/<forcing_filename> file exists on disk
- **THEN** the API SHALL return HTTP 200 (NOT 404 `FORCING_VERSION_NOT_FOUND`)

#### Scenario: cycle present in DB but rotated out of disk returns 404, NOT fallback to DB

- **WHEN** API receives an old cycle_time whose forcing files are no longer on disk (rotated out by retention)
- **AND** the same cycle_time has a finalized row in `met.forcing_version` with `checksum NOT NULL`
- **THEN** the API SHALL return HTTP 404 with code `STATION_FORCING_FILE_NOT_FOUND` (NOT fall back to DB read)

#### Scenario: verify met.forcing_version SELECT count = 0 during series request

- **WHEN** the route handler processes a series request (any combination of inputs)
- **AND** the implementation is instrumented to count SQL SELECT statements against `met.forcing_version` and `met.forcing_station_timeseries`
- **THEN** that count SHALL be exactly 0 (route makes no such query in this code path)

#### Scenario: all 4 currently-409 cases now return 200

- **WHEN** API receives cycle_time `2026-06-20T12:00:00Z` for any combination of `(station_id, source_id, model_id)` in `{(heihe_forc_001, IFS, basins_heihe_shud), (heihe_forc_001, gfs, basins_heihe_shud), (qhh_forc_001, IFS, basins_qhh_shud), (qhh_forc_001, gfs, basins_qhh_shud)}`
- **AND** the corresponding disk file exists
- **THEN** the API SHALL return HTTP 200 + non-empty series for ALL four combinations (currently they all return 409 `FORCING_VERSION_NOT_FINALIZED`)

### Requirement: 200 response preserves existing StationSeriesResponse schema

The 200 response body SHALL conform to the existing `StationSeriesResponse` schema in `openapi/nhms.v1.yaml:2873`, with station metadata sourced from `met.met_station` (AD-2) and series content sourced from disk CSV (AD-4).

#### Scenario: station metadata block populated from met.met_station

- **WHEN** the API returns 200 for a valid request
- **THEN** the response `data.station` SHALL contain `station_id`, `basin_version_id`, `station_name`, `longitude`, `latitude`, `elevation_m`, `station_role`, `active_flag`, and `properties_json` fields populated from `met.met_station`

#### Scenario: per-variable unit field matches AD-5 contract

- **WHEN** the API returns 200 with series for `variables=PRCP,TEMP,RH,wind,Rn`
- **THEN** each `data.series[]` entry SHALL have its `unit` field set as: `PRCP="mm/day"`, `TEMP="degC"`, `RH="0-1"`, `wind="m/s"`, `Rn="W/m^2"`

#### Scenario: metadata block reports returned_points and time bounds

- **WHEN** the API returns 200
- **THEN** the response `data.metadata` SHALL contain `returned_points` (total tuple count after filtering+limit), `truncated` (boolean reflecting whether `limit` was hit), `returned_from` and `returned_to` (timezone-aware UTC bounds of returned points)

#### Scenario: series ordering — variables fixed order, points chronologically ascending

- **WHEN** the API returns 200 with multiple variables and multiple time points
- **THEN** `data.series[]` SHALL be ordered `[PRCP, TEMP, RH, wind, Rn]` (filtering keeps relative order; absent variables are omitted)
- **AND** each `series.points[]` SHALL be sorted by `valid_time` ascending

### Requirement: Disk-miss returns 404 with explicit expected_path

The reader SHALL return HTTP 404 `STATION_FORCING_FILE_NOT_FOUND` whenever the resolved disk path does not exist, including the resolved path in the error details for operator troubleshooting.

#### Scenario: file missing returns 404 with expected_path

- **WHEN** the resolved path `${OBJECT_STORE_ROOT}/forcing/ifs/2026053106/basins_heihe_vbasins/basins_heihe_shud/shud/X100.75Y37.65.csv` does not exist
- **THEN** the API SHALL return HTTP 404 with code `STATION_FORCING_FILE_NOT_FOUND` and details containing `{station_id, expected_path, basin_version_id, source_id, cycle_time, model_id}`

#### Scenario: parent cycle directory missing also returns 404

- **WHEN** the cycle directory `${OBJECT_STORE_ROOT}/forcing/ifs/2026053106/` does not exist on disk
- **THEN** the API SHALL return HTTP 404 `STATION_FORCING_FILE_NOT_FOUND` (NOT 500); details SHALL still include the full expected leaf path

### Requirement: OBJECT_STORE_ROOT env required at startup, role boundary updated

The display API SHALL fail to start when `OBJECT_STORE_ROOT` env var is missing or does not resolve to an existing readable directory. The display role boundary SHALL be updated to remove `OBJECT_STORE_ROOT` from the forbidden compute-path env list, so that this env can be legitimately set on display.env.

#### Scenario: missing env var fails startup

- **WHEN** the display API process starts without `OBJECT_STORE_ROOT` env var set
- **THEN** `load_runtime_config()` SHALL raise `RuntimeModeError` with message containing `OBJECT_STORE_ROOT env var is required`; the process SHALL exit non-zero before binding the HTTP port

#### Scenario: env points to non-existent directory fails startup

- **WHEN** `OBJECT_STORE_ROOT=/no/such/path` and `/no/such/path` does not exist
- **THEN** `load_runtime_config()` SHALL raise `RuntimeModeError` with message containing `is not a readable directory`

#### Scenario: env points to existing readable directory starts cleanly

- **WHEN** `OBJECT_STORE_ROOT=/home/ghdc/nwm/object-store` and that path is a readable directory
- **THEN** the API SHALL start normally and serve requests

#### Scenario: OBJECT_STORE_ROOT no longer triggers DISPLAY_BOUNDARY_CONFIG_UNSAFE

- **WHEN** display.env contains `OBJECT_STORE_ROOT=/home/ghdc/nwm/object-store`
- **AND** `display_boundary_blockers()` (`apps/api/runtime_mode.py:176-202`) runs at startup
- **THEN** the function SHALL NOT emit a `DISPLAY_BOUNDARY_CONFIG_UNSAFE` blocker for that env key

#### Scenario: tests/test_role_boundary_static.py reflects boundary change

- **WHEN** test `tests/test_role_boundary_static.py` runs after this change
- **THEN** `DISPLAY_RUNTIME_FORBIDDEN_ENV_KEYS` (line 19-27) SHALL NOT include `OBJECT_STORE_ROOT`
- **AND** the assertion at line 89 `DISPLAY_RUNTIME_FORBIDDEN_ENV_KEYS == docker_runtime.DISPLAY_FORBIDDEN_ENV_KEYS` SHALL pass (both sides updated together)

### Requirement: Variable, time-window, and limit filtering applied in reader

The reader SHALL accept optional `variables`, `from_time`, `to_time`, and `limit` parameters and apply them after CSV parse before returning the series. `limit` truncates total tuples (not per-variable). Unknown or unsupported variables are silently dropped from the filter.

#### Scenario: variables filter restricts emitted variables

- **WHEN** API request includes `variables=PRCP,TEMP`
- **THEN** the response SHALL contain only `PRCP` and `TEMP` variables, no `RH/wind/Rn`

#### Scenario: from_time / to_time restrict emitted valid_times inclusive

- **WHEN** API request includes `from=2026-06-20T15:00:00Z` and `to=2026-06-20T18:00:00Z`
- **AND** the CSV contains data points at `12:00, 15:00, 18:00, 21:00`
- **THEN** the response SHALL contain only `15:00` and `18:00` data points (inclusive both ends)

#### Scenario: from_time > to_time returns empty series 200

- **WHEN** API request includes `from=2026-06-25T00:00:00Z` and `to=2026-06-20T00:00:00Z`
- **THEN** the API SHALL return HTTP 200 with `data.series=[]` (no error)

#### Scenario: from_time before CSV window returns 200 with all rows from CSV start

- **WHEN** the CSV window is `2026-06-20T12:00Z..2026-06-27T00:00Z` and request `from=2020-01-01T00:00Z, to=2030-01-01T00:00Z`
- **THEN** the API SHALL return all 5 × N tuples (200 OK; no error for out-of-window from/to)

#### Scenario: limit truncates total tuples across all variables, preserves variable+time order

- **WHEN** API request includes `limit=10`
- **AND** the unfiltered output would have 265 tuples (5 vars × 53 rows)
- **THEN** the response SHALL contain exactly 10 tuples; sort order SHALL be variable order `[PRCP, TEMP, RH, wind, Rn]` first, then `valid_time` ascending within each variable
- **AND** `data.metadata.truncated` SHALL be `true`

#### Scenario: Press variable in request is silently dropped, response omits Press

- **WHEN** API request includes `variables=Press`
- **THEN** the response SHALL succeed (200) with `data.series=[]` (no Press key in series, no error)

#### Scenario: PRCP + Press in request returns only PRCP

- **WHEN** API request includes `variables=PRCP,Press`
- **THEN** the response SHALL contain only `PRCP` series (Press silently dropped; no `Press` key in `data.series[]`)

#### Scenario: unknown variable name silently dropped

- **WHEN** API request includes `variables=PRCP,UnknownVariable`
- **THEN** the response SHALL contain only `PRCP` series (UnknownVariable silently dropped; no error)

### Requirement: Series reader is side-effect free

The reader SHALL be pure-read: no writes to disk, no DB mutations, no filesystem modifications under `OBJECT_STORE_ROOT`.

#### Scenario: N consecutive identical requests produce identical responses

- **WHEN** the API receives N identical series requests
- **THEN** all N responses SHALL be byte-identical (modulo `request_id`)
- **AND** the mtime of all files under `OBJECT_STORE_ROOT/forcing/...` SHALL be unchanged after N requests

#### Scenario: reader does not write anywhere

- **WHEN** any series request is processed
- **THEN** no `open(...,"w")` / `mkdir` / write syscall SHALL be issued by reader code on any path under `OBJECT_STORE_ROOT`

### Requirement: Old DB-only finalize-gate errors no longer emitted on series path

After this change, `/api/v1/met/stations/{station_id}/series` SHALL never return HTTP errors with codes `FORCING_VERSION_NOT_FINALIZED` or `FORCING_VERSION_NOT_FOUND` (those codes remain valid on other endpoints that still consume `met.forcing_version`).

#### Scenario: any DB state for forcing_version no longer surfaces 409/404 on series path

- **WHEN** API receives a series request and `met.forcing_version` is in any state (row missing, row with NULL checksum, row with 'pending', row finalized)
- **THEN** the API SHALL NOT return code `FORCING_VERSION_NOT_FINALIZED` or `FORCING_VERSION_NOT_FOUND` for this path; only the disk-path codes (`STATION_FORCING_FILE_NOT_FOUND`, etc.) apply

### Requirement: forcing_version_id query param silently ignored

The existing `forcing_version_id` query param (`apps/api/routes/data_sources.py:113`) SHALL be silently accepted but unused by the new disk-only path. OpenAPI documentation MUST mark this param deprecated.

#### Scenario: forcing_version_id passed alongside cycle_time is ignored, request succeeds

- **WHEN** API request includes both `forcing_version_id=forc_ifs_2026062012_basins_heihe_shud` and `cycle_time=2026-06-20T12:00:00Z`
- **THEN** the response SHALL be identical to the same request without `forcing_version_id` (no 422, no 200 with different body)

#### Scenario: forcing_version_id alone without cycle_time returns 422

- **WHEN** API request includes ONLY `forcing_version_id=...` without `cycle_time`/`model_id`/`source_id`
- **THEN** the API SHALL return HTTP 422 `MISSING_REQUIRED_FILTER` listing the missing fields (because the new path requires cycle_time/model_id/source_id; forcing_version_id alone is no longer enough)
