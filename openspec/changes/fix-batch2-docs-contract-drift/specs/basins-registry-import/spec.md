## MODIFIED Requirements

### Requirement: River segment row classes and counting invariant are documented and pinned

The repository SHALL define, in `openspec/glossary.md`, the two row classes `core.river_segment` stores under one `river_network_version_id`: the SHUD input reach row (`<model_id>_reach_<iRiv:06d>`, from `gis/river.shp`, `shud_output_river` absent or `'false'`, carries hydraulic parameters and is the only row class the importer points `core.river_segment_crosswalk` at — the FK itself is table-wide) and the SHUD output river row (`<model_id>_shud_riv_<N:06d>`, from `.sp.riv`, `shud_output_river='true'`, carries output-series identity, geometry backfilled from the matching reach row). The repository SHALL state in the same glossary entry and in `docs/runbooks/current-production-ops.md` that `core.river_network_version.segment_count` counts reach rows only, that the two classes are equal in number because `river.shp` record count is validated equal to the `.sp.riv` reach count at import, that `count(*) == 2 × segment_count` for an rnv is therefore a design fact, and that `output_segment_count` is not a `core.river_network_version` column: it lands in the import receipt and `model_instance.resource_profile` and is re-read from there into scheduler manifest fields.

#### Scenario: Hygiene query compares the right class

- **WHEN** an operator follows the runbook's river-segment count check for one `river_network_version_id`
- **THEN** the query filters with `COALESCE(properties_json->>'shud_output_river','false')` before comparing against `segment_count`
- **AND** the runbook states that an unfiltered `count(*)` equal to `2 × segment_count` is the expected value, citing #1122 and #1123 as the precedents

#### Scenario: Import test pins the invariant

- **WHEN** the real-DB import test `tests/test_basins_registry_import_qhh.py::test_pr2_contract_reach_rows_single_part_and_crosswalk_count` imports a fixture package and reads the resulting rnv
- **THEN** it asserts the physical `core.river_segment` row count equals `2 × river_network_version.segment_count`
- **AND** it asserts the `shud_output_river='true'` row count equals the reach row count

#### Scenario: Code comments point at the definition

- **WHEN** a reader opens the two-row-class comment in `workers/model_registry/basins_reingest.py` or the `output_segment_count` comment in `workers/model_registry/basins_geometry.py`
- **THEN** the `basins_geometry.py` comment states that `segment_count` counts `gis/river.shp` reach records (post-PR-2), not `seg.shp`/`.sp.rivseg` display geometry
- **AND** the `basins_reingest.py` comment names the glossary terms
- **AND** no import, parser, or backfill behavior changes

### Requirement: River segments are imported with geometry and topology metadata

The system SHALL import river reach records from `input_dir/gis/river.shp` as the authoritative geometry source, one row per reach matching the row count of `input_dir/<basin>.sp.riv`, with reach-level topology (downstream reach ID, length, slope, type, boundary-condition flag) and physical parameters (depth, bank slope, width, sinuosity, Manning, Cwr, KsatH, bed thickness) preserved from the river shapefile attribute table.

The system SHALL NOT use `input_dir/gis/seg.shp` as a geometry source. The system SHALL NOT read `input_dir/<basin>.sp.rivseg` for geometry, vertex, or topology purposes; that file has no coordinate columns (only `Index`, `iRiv`, `iEle`, `Length`) and is only retained as historical cross-check evidence on the segment count.

Each imported reach geometry SHALL be a single-part `MULTILINESTRING` (a `LINESTRING` wrapped via `ST_Multi` at write time) so that `core.river_segment.geom` column type `geometry(MultiLineString, 4490)` is preserved without schema change. This single-part invariant applies to every row written by the `basins-registry-import` ingestion path; no other write path may persist multi-part values into `core.river_segment.geom` (the retained output-river geometry backfill `_backfill_output_segment_geometry` does not introduce multi-part values: it copies the already single-part `geom` of the matching reach row onto the SHUD output river row through `ST_Multi`, so the invariant holds for those rows too — see "Output-river geometry backfill writes only the target network version" below).

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

#### Scenario: River segment map query returns segment-level features sliced from parent reach polyline

- **WHEN** the imported basin version is queried through `GET /api/v1/basin-versions/{basin_version_id}/river-segments`
- **THEN** the response contains GeoJSON features with `river_segment_id`, `river_network_version_id`, `basin_version_id`, and a `MultiLineString` geometry
- **AND** the per-basin feature count equals the `.sp.rivseg` segment record count (NOT the reach count); each feature corresponds to one `(iRiv, iEle)` segment recorded in `core.river_segment_crosswalk`
- **AND** every feature's `river_segment_id` follows the segment-level form `<model_id>_seg_<iRiv>_<iEle>` derived from the crosswalk row's `external_id`, preserving the frontend contract observed at `apps/frontend/src/components/map/M11MapLibreSurface.tsx` (hover/popup/coloring/promoteId/forecast paths)
- **AND** every feature's geometry is the result of `ST_LineSubstring(reach_geom, start_fraction, end_fraction)` against the parent reach's `core.river_segment.geom`, where `start_fraction` and `end_fraction` are cumulative `sp.rivseg.Length` proportions within the parent reach (computed in `segment_order` order)
- **AND** each slice is by construction a subset of the reach polyline; no slice introduces vertices not present on the reach polyline; the no-fabricated-bridge invariant on the underlying reach geometry transitively holds for every slice
- **AND** the last segment's `end_fraction` is saturated to `1.0` to compensate for floating-point accumulation drift between `sum(sp.rivseg.Length)` and the parent reach `Length`
- **AND** if `sp.rivseg` segment order disagrees with the reach polyline direction (rare; SHUD model normally guarantees flow-ordered), the API SHALL fail with `BASINS_REGISTRY_SEGMENT_ORDER_MISMATCH` rather than emitting silently-reversed slices
- **AND** the OpenAPI schema name (`GeoJsonMultiLineString`) and response structure are unchanged; description text updated to reflect Path C semantics (segment-level features sliced from reach polyline)

### Requirement: Deprecated cross-gap fallback paths are removed from the codebase

The system SHALL NOT carry "defensive" cross-gap stitching, gap-splitting, or MultiLineString-rebuild logic on the ingestion, write, output-river backfill, or frontend paths once reach-level ingestion is in place. The SHUD output river family in `workers/model_registry/basins_registry_import.py` — `_ensure_output_river_segments`, `_output_river_segment_rows`, and `_backfill_output_segment_geometry` — is NOT on the removal list below: it shares no stitching path, reads no `gis/seg.shp` geometry, and remains the default-on import and bootstrap path whose write contract is the requirement "Output-river geometry backfill writes only the target network version" and `mvt-tile-contract`. The following code paths SHALL be removed in the same change, not merely deprecated, so that no caller can re-introduce them as a "safety net":

- `workers/model_registry/basins_geometry.py`: `_merge_polyline_parts`, `gap_split_multilinestring_wkt`, `gap_split_positions`, `_nearest_attachment`, `_edge_meters`, `_median_edge`, the module-level constants `RIVER_GAP_ABSOLUTE_M` / `RIVER_GAP_RELATIVE` / `_EARTH_RADIUS_M`, and the `seg.shp` branch of `_river_segments_from_layer`; the generic WKT vertex formatter `_point_wkt` and `_shud_count_header` (which still reads the `.sp.riv` / `.sp.rivseg` count headers as import evidence and network-checksum input) are not stitching code and are retained
- `packages/common/model_registry.py`: `line_or_multiline_to_wkt` and `_multilinestring_to_wkt` (write path reverts to `geometry_to_wkt(..., "LineString")` plus SQL-side `ST_Multi`)
- `scripts/backfill_river_segment_multilinestring.py`: entire file
- `tests/test_backfill_river_segment_multilinestring.py`: entire file
- `tests/test_river_segment_gap_split.py`: entire file
- `apps/frontend/src/lib/m11/gapAwareGeometry.ts`: entire file plus its `__tests__` entry and the two call sites in `apps/frontend/src/components/map/M11MapLibreSurface.tsx` (`gapAwareLineGeometry` invocations + unused imports)

#### Scenario: No reachable call site for legacy stitching code

- **WHEN** the change is fully applied
- **THEN** a repository-wide grep for any of the following tokens returns zero matches outside of `openspec/**` (spec text and archived change audit logs) and the historical header comment of the already-applied migration `db/migrations/000037_river_segment_multilinestring.sql`: `_merge_polyline_parts`, `gap_split_multilinestring`, `gap_split_positions`, `line_or_multiline_to_wkt`, `_multilinestring_to_wkt`, `gapAwareLineGeometry`, `splitPositionsAtGaps`, `backfill_river_segment_multilinestring`, `rebackfill_river_segment`
- **AND** the list does not include the live SHUD output river tokens `_backfill_output_segment_geometry`, `_ensure_output_river_segments`, `_output_river_segment_rows`, or the `_shud_riv_` row-id infix (`<model_id>_shud_riv_<N:06d>`)

#### Scenario: Output-river geometry comes from the reach row, not seg.shp

- **WHEN** `_backfill_output_segment_geometry(cursor, river_network_version_id, *, only_missing)` fills a SHUD output river row (`shud_output_river='true'`)
- **THEN** its only geometry source is the sibling SHUD input reach row in `core.river_segment` under the same `river_network_version_id` whose `iRiv` equals the output row's `shud_riv_index` (reach geometry derived from `gis/river.shp`), and it copies that row's `geom`, `length_m`, and source `Type`
- **AND** with `only_missing` it only touches output rows whose `geom` is NULL or whose `properties_json` lacks `Type`
- **AND** it increments `core.river_network_version.geometry_generation` once, only when at least one row was updated, as specified by "Output-river geometry backfill writes only the target network version" and `mvt-tile-contract`
- **AND** a future change that adds a backfill reading `gis/seg.shp` (or other seg-level display geometry) for `shud_output_river=true` rows SHALL be rejected in code review with reference to this requirement

#### Scenario: No silent re-introduction via fallback

- **WHEN** future code attempts to add a defensive "split a MultiLineString into single-line parts based on cross-gap distance" helper at any layer (ingestion, API, frontend)
- **THEN** code review SHALL reject the change with reference to this requirement; the source-level invariant on `gis/river.shp` makes such helpers unnecessary by construction
