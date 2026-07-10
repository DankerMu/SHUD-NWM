## MODIFIED Requirements

### Requirement: Disk-miss returns 404 with safe expected_path

The reader SHALL return HTTP 404 `STATION_FORCING_FILE_NOT_FOUND` whenever the resolved disk path does not exist. For a station row with `active_flag=true`, the error details SHALL include an object-store-relative `expected_path` such as `forcing/<source>/<cycle>/<basin>/<model>/shud/<filename>` plus `{station_id, basin_version_id, source_id, cycle_time, model_id}` for operator troubleshooting. For a station row with `active_flag=false` (an inactive or evidence-only row), the details SHALL be desensitized to at most `{station_id}` — no `expected_path` and no `(basin_version_id, model_id, source_id, cycle_time)` tuple — per the B4 leak fix in change `direct-grid-display-cutover` (`active-model-dynamic-resolution`). Public API responses SHALL NOT expose `OBJECT_STORE_ROOT` or host absolute paths. The `STATION_NOT_FOUND` trigger is unchanged — it remains "a `station_id` not present in `met.met_station`": inactive rows are NOT filtered out of the lookup, so successful reads of existing files stay independent of `active_flag`.

#### Scenario: file missing for an active station returns 404 with expected_path

- **WHEN** the resolved path `${OBJECT_STORE_ROOT}/forcing/ifs/2026053106/basins_heihe_vbasins/basins_heihe_shud/shud/X100.75Y37.65.csv` does not exist and the station row has `active_flag=true`
- **THEN** the API SHALL return HTTP 404 with code `STATION_FORCING_FILE_NOT_FOUND` and details containing `{station_id, expected_path, basin_version_id, source_id, cycle_time, model_id}`

#### Scenario: parent cycle directory missing also returns 404

- **WHEN** the cycle directory `${OBJECT_STORE_ROOT}/forcing/ifs/2026053106/` does not exist on disk
- **THEN** the API SHALL return HTTP 404 `STATION_FORCING_FILE_NOT_FOUND` (NOT 500); for an `active_flag=true` station the details SHALL still include the full expected leaf path

#### Scenario: file missing for an inactive station returns a desensitized 404

- **WHEN** the resolved disk path does not exist and the station row has `active_flag=false`
- **THEN** the API SHALL return HTTP 404 with code `STATION_FORCING_FILE_NOT_FOUND` and details limited to at most `{station_id}`
- **AND** the response SHALL NOT disclose `expected_path` or the `(basin_version_id, model_id, source_id, cycle_time)` identity tuple for that inactive row

#### Scenario: inactive station with an existing file still returns the series

- **WHEN** the resolved disk path exists for an `active_flag=false` station (e.g. a post-cutover legacy station whose pre-cutover file is still within the retention window)
- **THEN** the read succeeds and returns the series, because the station lookup does not filter `active_flag` and desensitization applies only to the disk-miss 404 details
