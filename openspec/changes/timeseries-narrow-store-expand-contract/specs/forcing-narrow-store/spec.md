## ADDED Requirements

### Requirement: Forcing authority tables SHALL carry integer surrogate keys and the fact table SHALL become a narrow key-based hypertable

`met.met_station.station_key` and `met.forcing_version.forcing_version_key` SHALL be `INTEGER GENERATED ALWAYS AS IDENTITY UNIQUE`; `met.forcing_variable`, `met.forcing_unit` and `met.forcing_quality_flag` enum types SHALL be derived from the union of the production writer's vocabulary, live `pg_stats` values and seed literals, with any excluded literal recorded in the migration header. The narrow `met.forcing_station_timeseries` SHALL carry exactly `forcing_version_key INTEGER NOT NULL`, `station_key INTEGER NOT NULL`, `valid_time TIMESTAMPTZ NOT NULL`, `variable_e met.forcing_variable NOT NULL`, `value DOUBLE PRECISION NOT NULL`, `unit_e met.forcing_unit NOT NULL`, `quality_flag_e met.forcing_quality_flag NOT NULL`, `native_resolution TEXT NULL`; primary key `(forcing_version_key, station_key, variable_e, valid_time)`; one secondary index `forcing_ts_version_variable_time_key_idx (forcing_version_key, variable_e, valid_time DESC)` as the successor of `forcing_station_timeseries_qhh_latest_window_idx`; compression `segmentby (forcing_version_key, station_key)`, `orderby (variable_e, valid_time)` set as the last schema DDL; one-day chunks; foreign keys on both key columns. `source_id` MUST be derived by joining `met.forcing_version` and `basin_version_id` by joining `met.met_station` through `station_key`; neither is stored per row, and coverage/fallback semantics therefore follow the station authority's basin.

#### Scenario: Authority key add is receipted before the window
- **WHEN** the forcing expand issue is prepared
- **THEN** a node-27 receipt records each authority table's row count, index footprint, the distinct values of `variable`/`unit`/`quality_flag`/`native_resolution`, and the throwaway-cluster wall time of the IDENTITY column adds, and the runbook window budget is derived from it

#### Scenario: Catalog shape after the forcing expand migration
- **WHEN** the forcing expand migration has been applied
- **THEN** the narrow table has exactly the column set and nullability above, its primary key, one secondary index, two foreign keys, no text identity column, key-based compression settings and a one-day chunk interval; the legacy table is `met.forcing_station_timeseries_legacy` with its original shape, indexes and owner

#### Scenario: QHH fallback index successor is evidence-gated
- **WHEN** the forcing rollout receipt is captured
- **THEN** it holds before (legacy, `qhh_latest_window_idx`) and after (narrow, `forcing_ts_version_variable_time_key_idx`) `EXPLAIN (ANALYZE, BUFFERS)` of the QHH latest-product fallback CTE with every coverage loss enumerated

### Requirement: Forcing versions SHALL be routed by a store column and both writers SHALL write only the narrow table

`met.forcing_version.timeseries_store` SHALL be `NOT NULL DEFAULT 'narrow'` with values `legacy` or `narrow`; the expand migration SHALL set `legacy` on every forcing version that has rows in the renamed legacy table. `workers/forcing_producer/store.py` and `packages/common/forcing_domain_handoff_apply.py` SHALL write only the narrow table through the existing compressed-chunk write guard with the same replace window, keeping `narrow` in the same transaction; a replace targeting a `legacy` forcing version MUST be refused fail-closed before any DELETE with a code distinct from compressed-chunk-blocked and guard-internal codes, and the refusal is permanent for that version.

#### Scenario: Handoff apply writes narrow
- **WHEN** a forcing domain handoff is applied after the forcing expand deployment
- **THEN** rows land only in `met.forcing_station_timeseries` with key columns, and `timeseries_store = 'narrow'` on the forcing version after commit

#### Scenario: Legacy forcing version refused
- **WHEN** a replace targets a forcing version whose store is `legacy`
- **THEN** no DELETE is issued and the caller receives the legacy-refused code

### Requirement: Forcing readers SHALL render per-store variants like the river readers

The display-coverage station leg, the QHH latest-product fallback CTE and the legacy `station_series()` helper SHALL obtain their SQL from the shared renderer with the store of the forcing version in scope; narrow variants SHALL join `met.forcing_version` for `source_id` and `met.met_station` for `basin_version_id` and MUST reference no text identity column of the fact table; queries spanning versions of both stores use the union combinator. Response payloads and coverage counts MUST be identical between stores for the same forcing version shape; `station_series()` keeps its declared legacy-surface behaviour and changes only its SQL.

#### Scenario: Coverage count equality including basin provenance
- **WHEN** the same forcing version is materialised once as legacy and once as narrow in a test database whose legacy rows carry a `basin_version_id` different from the station authority's
- **THEN** `run_display_coverage` station counts are equal and the narrow variant reports the station authority's basin

#### Scenario: QHH fallback narrow variant shape
- **WHEN** the shape oracle renders the QHH fallback with store `narrow`
- **THEN** the fact-table predicates are `forcing_version_key`, `station_key`, `variable_e` only and `LOWER(source_id)` is applied to the joined `met.forcing_version` row

### Requirement: The forcing contract SHALL mirror the river contract

The forcing contract migration SHALL refuse while `met.forcing_station_timeseries_legacy` holds any chunk, and otherwise drop it and `met.forcing_version.timeseries_store` after the routing-free code is deployed; the forcing renderer's legacy path SHALL be removed; `tests/test_forecast_api.py` index pins SHALL name the narrow primary key and successor index; the forcing marker census and the fourteen-day receipt gate apply as for river.

#### Scenario: Refused while non-empty
- **WHEN** the forcing contract migration runs with a remaining legacy chunk
- **THEN** it raises and the catalog is unchanged

#### Scenario: Ordering after the river contract
- **WHEN** the forcing expand issue is scheduled
- **THEN** it depends on the river contract issue having merged and its rollout receipt being posted
