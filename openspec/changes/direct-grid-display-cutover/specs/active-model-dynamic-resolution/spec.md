## ADDED Requirements

### Requirement: No display surface pins or caches model_id across a cutover

Every display/frontend surface SHALL resolve the active `model_id` dynamically; no surface SHALL pin or cache a `model_id` across a cutover, including MVT tile cache keys. The station-series backend keeps `model_id` an explicit, required, client-supplied filter with no server-side active-model default and no server-side `model_id` cache; the dynamic resolution happens at the caller — the frontend derives the live `model_id` from the currently selected latest product at request time. A per-request explicit old `model_id` for an explicitly-requested historical cycle is permitted, because it resolves an immutable old asset and is not a pinned live default.

#### Scenario: live station-series requests carry the currently-active model resolved by the caller

- **WHEN** a live (latest-product) station-series request is issued after a cutover
- **THEN** the caller (the station popup) supplies the `model_id` resolved from the currently selected latest product at request time — after the cutover that is the newly active variant's `model_id` — and does not reuse a `model_id` captured before the cutover
- **AND** the backend endpoint keeps `model_id` a required client-supplied filter (absence returns `MISSING_REQUIRED_FILTER`) and introduces no server-side active-model default or cached `model_id` fallback.

#### Scenario: MVT tile cache key is not model-pinned

- **WHEN** the station-MVT tile cache key is derived
- **THEN** it derives from the `active_flag=true` station source identity (`_station_source_version`)
- **AND** it self-invalidates on the flip
- **AND** it is not keyed to a pinned `model_id`.

#### Scenario: frontend does not cache model_id across cutover

- **WHEN** the frontend issues display or timeseries requests
- **THEN** it does not reuse a `model_id` captured before the cutover for a live (latest-product) request.

#### Scenario: explicit historical cycle may carry its old model_id

- **WHEN** a user explicitly requests a pre-cutover cycle
- **THEN** the request resolves that cycle's old `model_id` for the immutable old asset
- **AND** this per-request historical resolution is distinct from a pinned or cached live default.

### Requirement: Station single-query endpoint does not leak inactive-row metadata

For a `met.met_station` row with `active_flag=false` (an inactive or evidence-only row), the single-station series endpoint SHALL keep the stable `STATION_FORCING_FILE_NOT_FOUND` code on a disk miss but SHALL desensitize the `StationForcingFileNotFoundError.details` to at most `{station_id}` — disclosing neither the object-store-root-relative storage key (`expected_path`) nor the `(basin_version_id, model_id, source_id, cycle_time)` identity tuple — closing the enumeration surface recorded in the readiness §2.3 registration receipt's station-by-id lookup note (`openspec/changes/archive/2026-07-10-cmfd-direct-grid-platform-readiness/evidence/db-registration-2.3.node-27.pass.log`; the source design doc's "§2.4 N1" label is an erratum for that §2.3 record). The fix SHALL be this 404-details desensitization, NOT an `active_flag` filter at the lookup: filtering inactive rows out of `PsycopgStationLookup` would return `STATION_NOT_FOUND` for post-cutover inactive M0 legacy stations whose pre-cutover files still exist within retention — breaking the `historical-cycle-display-degradation` answerability guarantee — and would contradict the deployed `object-store-station-series-read` trigger that `STATION_NOT_FOUND` means "a `station_id` not present in `met.met_station`". Successful series reads SHALL remain independent of `active_flag`, and active-station behavior SHALL be unchanged. The fix SHALL carry a negative test.

#### Scenario: inactive / evidence-only station is not enumerable

- **WHEN** an authenticated user queries the single-station series endpoint for an inactive / evidence-only `station_id` and the resolved disk file does not exist
- **THEN** the response keeps the stable `STATION_FORCING_FILE_NOT_FOUND` code but its details do not disclose the object-store-root-relative storage key (`expected_path`) for that inactive row
- **AND** they do not disclose the `(basin_version_id, model_id, source_id, cycle_time)` tuple for that inactive row (details are limited to at most `{station_id}`; row existence of the intentionally-discoverable non-secret identifiers remains observable and is out of scope of this closure).

#### Scenario: inactive post-flip legacy station still serves its pre-cutover series

- **WHEN**, after a cutover flip, a station-series request targets a now-`active_flag=false` M0 legacy station with a pre-cutover cycle and that cycle's old `model_id`, and the resolved disk file exists within the retention window
- **THEN** the request returns the series successfully, because the lookup does not filter `active_flag` and series resolution is independent of the flip
- **AND** the leak fix changes only the disk-miss 404 details for inactive rows, never successful reads.

#### Scenario: active-station 404 behavior is preserved

- **WHEN** an active station legitimately has no forcing file for a requested cycle
- **THEN** the endpoint still returns its normal `STATION_FORCING_FILE_NOT_FOUND` behavior with full troubleshooting details
- **AND** the leak fix does not over-filter or suppress the active-station response.

#### Scenario: leak fix carries a negative test

- **WHEN** the B4 fix is implemented
- **THEN** a negative test proves an inactive / evidence-only row's disk-miss response cannot be used to enumerate the object-store-root-relative storage key (`expected_path`) or the `(basin_version_id, model_id, source_id, cycle_time)` identity tuple.
