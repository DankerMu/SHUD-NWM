## MODIFIED Requirements

### Requirement: River segments are imported with geometry and topology metadata

The system SHALL import river reach records from `input_dir/gis/river.shp` as the authoritative geometry source, one row per reach matching the row count of `input_dir/<basin>.sp.riv`, with reach-level topology (downstream reach ID, length, slope, type, boundary-condition flag) and physical parameters (depth, bank slope, width, sinuosity, Manning, Cwr, KsatH, bed thickness) preserved from the river shapefile attribute table.

The system SHALL NOT use `input_dir/gis/seg.shp` as a geometry source. The system SHALL NOT read `input_dir/<basin>.sp.rivseg` for geometry, vertex, or topology purposes; that file has no coordinate columns (only `Index`, `iRiv`, `iEle`, `Length`) and is only retained as historical cross-check evidence on the segment count.

Each imported reach geometry SHALL be a single-part `MULTILINESTRING` (a `LINESTRING` wrapped via `ST_Multi` at write time) so that `core.river_segment.geom` column type `geometry(MultiLineString, 4490)` is preserved without schema change. This single-part invariant applies to every row written by the `basins-registry-import` ingestion path; no other write path may persist multi-part values into `core.river_segment.geom` (the deprecated `_backfill_output_segment_geometry` path is removed by the same change — see "Deprecated cross-gap fallback paths are removed from the codebase" below).

#### Scenario: Reach count matches SHUD `.sp.riv` evidence

- **WHEN** `gis/river.shp` and `.sp.riv` are present in a SHUD input package
- **THEN** `core.river_network_version.segment_count` equals the `.sp.riv` reach count
- **AND** `core.river_segment` row count for the basin version equals that same value
- **AND** the import fails with a structured `BASINS_REGISTRY_REACH_COUNT_MISMATCH` error if the counts diverge

#### Scenario: river.shp single-part invariant is enforced

- **WHEN** `gis/river.shp` contains any record with more than one Polyline part, or with a part whose vertex count is less than 2, or with a record count not equal to the `.sp.riv` reach count, or missing any of the required dbf fields (`Index`, `Down`, `Type`, `Slope`, `Length`, `BC`, `Depth`, `BankSlope`, `Width`, `Sinuosity`, `Manning`, `Cwr`, `KsatH`, `BedThick`)
- **THEN** the import fails with `BASINS_REGISTRY_RIVER_SHP_INVARIANT_VIOLATED` before any registry write
- **AND** the error payload includes the offending `Index` value, part count, and (where applicable) the list of missing dbf fields
- **AND** the failure is isolated to the offending basin: previously-ingested basins retain their data, and the next basin in the queue (if any) proceeds independently

#### Scenario: Reach geometry has no fabricated cross-gap straight bridges

- **WHEN** ingestion writes a `core.river_segment.geom` value derived from `gis/river.shp`
- **THEN** for every imported row the maximum edge length between consecutive vertices is less than or equal to `max(300.0 metres, 4 × median_edge_length_in_that_reach)` measured by an equirectangular metre approximation against EPSG:4490 longitude/latitude (the same metric the legacy frontend `splitPositionsAtGaps` used); the numeric thresholds `300.0` and `4×` SHALL be hardcoded inline in the import path, not imported from any module-level constant
- **AND** no ingestion-time stitching, gap-splitting, or cross-gap straight-link insertion takes place; the polyline vertices come verbatim from the shapefile in stored order

#### Scenario: Reach topology metadata is persisted from river.shp + .sp.riv

- **WHEN** `gis/river.shp` and `.sp.riv` provide reach-level attributes (Down / Type / Slope / Length / BC / Depth / BankSlope / Width / Sinuosity / Manning / Cwr / KsatH / BedThick)
- **THEN** imported `core.river_segment` rows preserve these fields under `properties_json` (or dedicated columns where they already exist), and `downstream_segment_id` is resolved from the `Down` reach index to the corresponding `<model_id>_reach_<Down:06d>` ID
- **AND** unresolved `Down` references (e.g. terminal reach with `Down=0` or `Down=-1`) are stored as `NULL` with a `terminal_reach=true` property flag

#### Scenario: River segment map query returns reach-level geometry

- **WHEN** the imported basin version is queried through `GET /api/v1/basin-versions/{basin_version_id}/river-segments`
- **THEN** the response contains GeoJSON features with `river_segment_id`, `river_network_version_id`, `basin_version_id`, and a `MultiLineString` geometry where every feature is a single-part reach polyline
- **AND** the per-basin feature count equals the `.sp.riv` reach count
- **AND** every feature geometry satisfies the no-fabricated-bridge invariant defined above
- **AND** the OpenAPI schema name (`GeoJsonMultiLineString`) and response structure are unchanged; only the description text is updated to reflect "reach" semantics instead of "segment"

### Requirement: Imported river segment IDs follow the reach-level naming convention

The system SHALL generate `core.river_segment.river_segment_id` values of the form `<model_id>_reach_<iRiv:06d>` where `iRiv` is the reach `Index` from `gis/river.shp` / `.sp.riv`, zero-padded to 6 digits, replacing the legacy `<model_id>_seg_<segment_order>_ord_<iRiv>_rec_<iEle>` segment-level convention. This is a **BREAKING** semantic change: ID strings change from segment granularity to reach granularity. The `crosswalk_id` ↔ `(iRiv, iEle)` mapping in `core.river_segment_crosswalk.external_id` stores the un-padded `"<iRiv>:<iEle>"` form to preserve fidelity with the raw `seg.shp` attribute values.

#### Scenario: Reach IDs are stable across re-ingest

- **WHEN** the same SHUD model package is ingested twice with unchanged checksum
- **THEN** all imported `river_segment_id` values are byte-identical between the two ingest runs

#### Scenario: Reach IDs are zero-padded

- **WHEN** a reach has `Index = 1` (or any single-digit / multi-digit Index up to 999_999)
- **THEN** the generated `river_segment_id` ends with `_reach_000001` (or correspondingly zero-padded), guaranteeing lexicographic order matches numeric order

#### Scenario: Reach IDs survive `core.river_segment_crosswalk` joins

- **WHEN** a downstream consumer joins `core.river_segment.river_segment_id = core.river_segment_crosswalk.river_segment_id`
- **THEN** every imported reach row matches at least one crosswalk row (assuming `gis/seg.shp` is present)
- **AND** the join uses the existing PRIMARY KEY composite index on `(river_segment_id, river_network_version_id)` plus the existing `river_segment_crosswalk_lookup_idx (river_network_version_id, source, river_segment_id)`

## ADDED Requirements

### Requirement: Segment-to-reach crosswalk is preserved from gis/seg.shp

The system SHALL parse `input_dir/gis/seg.shp` during ingestion and write per-segment rows into the existing `core.river_segment_crosswalk` table (schema defined in `db/migrations/000004_core.sql:56-69`) using existing columns only:

- `river_network_version_id` = the importing river network version's ID
- `river_segment_id` = `<model_id>_reach_<iRiv:06d>` (the parent reach this segment belongs to)
- `source` = the literal string `'basins_seg_shp'`
- `external_id` = `"<iRiv>:<iEle>"` (un-padded, preserves seg.shp attribute values verbatim)
- `properties_json` = JSON object `{"iRiv": <int>, "iEle": <int>, "segment_order": <int>, "length_m": <float|null>}`; `segment_order` is the segment's row offset within the source shapefile; `length_m` comes from the `Length` field if present, else `null`

The crosswalk SHALL allow frontend and analytics consumers to recover the SHUD-internal segment granularity (3738 segments for qhh) on top of the reach-level (`core.river_segment`) geometry (1633 reaches for qhh). No new migration is added; no new column is added; no existing column is repurposed.

#### Scenario: Crosswalk write count matches seg.shp record count

- **WHEN** a SHUD input package contains `gis/seg.shp` with N records
- **THEN** the import writes exactly N rows into `core.river_segment_crosswalk` for that basin version's `river_network_version_id` filtered by `source='basins_seg_shp'`
- **AND** each crosswalk row carries `(river_network_version_id, river_segment_id, source='basins_seg_shp', external_id, properties_json)` with `river_segment_id` resolved from the segment's `iRiv` to the matching `<model_id>_reach_<iRiv:06d>` reach ID and `external_id` formatted as `"<iRiv>:<iEle>"`
- **AND** the import fails with `BASINS_REGISTRY_CROSSWALK_REACH_MISSING` if any segment's `iRiv` does not match an imported reach

#### Scenario: Crosswalk is idempotent under re-ingest

- **WHEN** the same SHUD model package is ingested twice
- **THEN** the second ingest does not duplicate crosswalk rows: the existing `UNIQUE (river_network_version_id, river_segment_id, source)` constraint plus the `create_crosswalk_entries` upsert path (`ON CONFLICT ... DO UPDATE`) keeps row count equal to the seg.shp record count
- **AND** `external_id` and `properties_json` values are overwritten to the freshly-parsed values

#### Scenario: Crosswalk has indexed lookups for hover/click queries

- **WHEN** a basin version's crosswalk rows are queried by `(river_network_version_id, source='basins_seg_shp', external_id)` or by `(river_network_version_id, river_segment_id)`
- **THEN** the query plan uses the existing `river_segment_crosswalk_lookup_idx` index (no sequential scan over the full crosswalk table)
- **AND** the query returns results within frontend interaction latency budgets (< 50 ms p95 on cached data)

### Requirement: Required input files are validated for presence

The system SHALL validate the presence of `gis/river.shp` (with its `.dbf`, `.shx` sidecars) and `gis/seg.shp` (with its sidecars) at the start of ingestion. Missing files SHALL cause structured failure before any registry write.

#### Scenario: river.shp is missing

- **WHEN** `gis/river.shp` (or any of its `.dbf` / `.shx` sidecars) is not present in the SHUD input package
- **THEN** the import fails with `BASINS_REGISTRY_RIVER_SHP_MISSING` before any registry write
- **AND** the error payload includes the basin name and the missing file path
- **AND** the import does NOT silently fall back to `gis/seg.shp` (the legacy fallback is removed)

#### Scenario: seg.shp is missing

- **WHEN** `gis/seg.shp` (or any of its sidecars) is not present in the SHUD input package
- **THEN** the import fails with `BASINS_REGISTRY_SEG_SHP_MISSING` before writing the crosswalk rows
- **AND** the error payload includes the basin name and missing file path
- **AND** no partial crosswalk rows are written

#### Scenario: Per-basin ingest is transactional

- **WHEN** any failure occurs while ingesting a single basin's river segments or crosswalk rows
- **THEN** all writes performed for that basin's `river_segment` and `river_segment_crosswalk` rows are rolled back in a single transaction
- **AND** previously-ingested basins are unaffected
- **AND** the next basin in the queue (if any) proceeds independently from a clean state

#### Scenario: river_segment is written before river_segment_crosswalk within the same transaction

- **WHEN** ingestion writes both `core.river_segment` (with new `<model_id>_reach_<iRiv:06d>` IDs) and `core.river_segment_crosswalk` rows for the same basin
- **THEN** within the single per-basin transaction, all `core.river_segment` insert/upsert statements complete before any `core.river_segment_crosswalk` insert/upsert statement runs
- **AND** the FOREIGN KEY constraint on `core.river_segment_crosswalk (river_segment_id, river_network_version_id)` referencing `core.river_segment (river_segment_id, river_network_version_id)` ([db/migrations/000004_core.sql:64-65](../../../db/migrations/000004_core.sql:64)) is satisfied at every statement boundary

#### Scenario: Legacy seg-level rows are removed before reach-level rows are written

- **WHEN** a basin that previously had `core.river_segment` rows using the legacy `<model>_seg_<segment_order>_ord_<iRiv>_rec_<iEle>` ID format is re-ingested under the new contract
- **THEN** the same transaction first runs `DELETE FROM core.river_segment_crosswalk WHERE river_segment_id LIKE '<old_model_id>_seg_%'` (to clear FK-dependent crosswalk rows) followed by `DELETE FROM core.river_segment WHERE river_segment_id LIKE '<old_model_id>_seg_%'`
- **AND** then inserts the new `_reach_<iRiv:06d>` rows and matching crosswalk rows
- **AND** no FK orphans exist at transaction commit; no legacy `_seg_*` IDs remain in either table for the basin's `river_network_version_id`

### Requirement: Deprecated cross-gap fallback paths are removed from the codebase

The system SHALL NOT carry "defensive" cross-gap stitching, gap-splitting, or MultiLineString-rebuild logic on the ingestion, write, output-river backfill, or frontend paths once reach-level ingestion is in place. The following code paths SHALL be removed in the same change, not merely deprecated, so that no caller can re-introduce them as a "safety net":

- `workers/model_registry/basins_geometry.py`: `_merge_polyline_parts`, `gap_split_multilinestring_wkt`, `gap_split_positions`, `_nearest_attachment`, `_point_wkt`, `_edge_meters`, `_median_edge`, the module-level constants `RIVER_GAP_ABSOLUTE_M` / `RIVER_GAP_RELATIVE` / `_EARTH_RADIUS_M`, the `seg.shp` branch of `_river_segments_from_layer`, and the `_shud_count_header(sp_rivseg, ...)` cross-check (replaced by the crosswalk count check against `gis/seg.shp` itself)
- `workers/model_registry/basins_registry_import.py`: `_backfill_output_segment_geometry` and `_ensure_output_river_segments` (whichever helpers exist that share the deprecated stitching path); `qhh_production_bootstrap.py` callers updated to the new ingestion entry point
- `packages/common/model_registry.py`: `line_or_multiline_to_wkt` and `_multilinestring_to_wkt` (write path reverts to `geometry_to_wkt(..., "LineString")` plus SQL-side `ST_Multi`)
- `scripts/backfill_river_segment_multilinestring.py`: entire file
- `tests/test_backfill_river_segment_multilinestring.py`: entire file
- `tests/test_river_segment_gap_split.py`: entire file
- `apps/frontend/src/lib/m11/gapAwareGeometry.ts`: entire file plus its `__tests__` entry and the two call sites in `apps/frontend/src/components/map/M11MapLibreSurface.tsx` (`gapAwareLineGeometry` invocations + unused imports)

#### Scenario: No reachable call site for legacy stitching code

- **WHEN** the change is fully applied
- **THEN** a repository-wide grep for any of the following tokens returns zero matches outside of the change's own audit log / changelog files: `_merge_polyline_parts`, `gap_split_multilinestring`, `gap_split_positions`, `line_or_multiline_to_wkt`, `_multilinestring_to_wkt`, `gapAwareLineGeometry`, `splitPositionsAtGaps`, `backfill_river_segment_multilinestring`, `_backfill_output_segment_geometry`, `_ensure_output_river_segments`, `_output_river_segment_rows`, `_shud_riv_`, `rebackfill_river_segment`

#### Scenario: Output-river backfill is not re-introduced

- **WHEN** a future change attempts to add a separate backfill path that reads `gis/seg.shp` and writes `core.river_segment.geom` for `shud_output_river=true` rows (mirroring the deleted `_backfill_output_segment_geometry`)
- **THEN** code review SHALL reject the change with reference to this requirement; reach geometry SHALL come from `gis/river.shp` via the single ingestion path

#### Scenario: No silent re-introduction via fallback

- **WHEN** future code attempts to add a defensive "split a MultiLineString into single-line parts based on cross-gap distance" helper at any layer (ingestion, API, frontend)
- **THEN** code review SHALL reject the change with reference to this requirement; the source-level invariant on `gis/river.shp` makes such helpers unnecessary by construction

### Requirement: All basin packages are re-ingested under the reach-source contract

The system SHALL re-ingest all 10 currently-tracked SHUD basin packages (`qhh`, `heihe`, `hetianhe`, `kashigeer`, `keliya`, `qinyijiang`, `tailanhe`, `weiganhe`, `xinanjiang_upstream`, `zhaochen`) under the new reach-source contract before the change is considered deployed. Each basin's re-ingest SHALL produce a structured receipt and SHALL pass the no-fabricated-bridge invariant. Per-basin failures do not block other basins (see "Per-basin ingest is transactional" scenario).

#### Scenario: Per-basin re-ingest produces a structured receipt

- **WHEN** a basin is re-ingested under the new contract
- **THEN** the ingest emits a JSON receipt recording: `basin_id`, `old_model_id`, `new_model_id`, `river_shp_record_count`, `sp_riv_reach_count`, `imported_reach_count`, `crosswalk_row_count`, `seg_shp_record_count`, `geom_null_count`, `max_edge_meters_observed`, `multi_part_violation_count` (must be 0), `tile_cache_purged_count`
- **AND** the receipt is appended to a basin-import audit log location agreed in tasks.md

#### Scenario: Map tile cache is purged per basin after re-ingest

- **WHEN** any basin's reach geometry rows are rewritten
- **THEN** the corresponding `map.tile_cache` rows are deleted with a per-basin `DELETE FROM map.tile_cache WHERE basin_version_id IN (<old basin_version_id>, <new basin_version_id>)` (NOT a full table `TRUNCATE`)
- **AND** the deletion is recorded in the receipt as `tile_cache_purged_count`
- **AND** unchanged basins' tile cache rows remain intact

#### Scenario: Live frontend verification covers at least 3 basins

- **WHEN** the change is being verified for production deployment
- **THEN** at minimum `qhh` (the original assault case), `heihe`, and one additional basin chosen from the remaining 8 are loaded in the node-27 display frontend
- **AND** a browser screenshot or live receipt is produced confirming no visible cross-gap straight bridges in the river layer
- **AND** each screenshot's metadata (zoom level, centre lng/lat) is recorded alongside the file
- **AND** segment-level hover/popup interactions still resolve to a non-empty payload via the crosswalk table

## REMOVED Requirements

### Requirement: River segment geometry may come from seg.shp as a fallback to river.shp

**Reason**: `gis/seg.shp` is the SHUD segment-to-mesh-element index table, not a polyline source for reach display. Its multi-part record structure (qhh: 330/3738 records, storage-order ≠ flow-order, parts up to 1721 m apart) forces ingestion to fabricate cross-gap "bridge" straight lines that show up in the frontend as visible jumps. The empirical fix (`gap_split_multilinestring_wkt` + MultiLineString column + frontend `splitPositionsAtGaps`) treats the symptom; using `gis/river.shp` (which is by construction single-part, flow-ordered, and one-to-one with `.sp.riv` reaches) eliminates the bridge at the source. Keeping `seg.shp` as a fallback would let the same defect re-appear silently in any basin where `river.shp` parsing fails.

**Migration**: All ingestion paths SHALL use `gis/river.shp` as the authoritative geometry source per the new "River segments are imported with geometry and topology metadata" requirement above. If `gis/river.shp` is malformed (multi-part records, reach count mismatch with `.sp.riv`, missing dbf fields), ingestion SHALL fail with `BASINS_REGISTRY_RIVER_SHP_INVARIANT_VIOLATED` rather than silently fall back to `seg.shp`. SHUD package producers are responsible for delivering well-formed `gis/river.shp`; the project does not provide an in-process repair path. The deleted helpers (`_merge_polyline_parts`, `gap_split_*`, `_backfill_output_segment_geometry`) are not retained as private utilities.
