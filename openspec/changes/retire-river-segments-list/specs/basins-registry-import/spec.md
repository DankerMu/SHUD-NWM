## ADDED Requirements

### Requirement: River reaches are imported with geometry and topology metadata

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

#### Scenario: The river segment list endpoint is retired

- **WHEN** a client requests `GET /api/v1/basin-versions/{basin_version_id}/river-segments`
- **THEN** no route serves it: the response status is `404` and the body is not the API's `ok` / `data` envelope; the path is absent from the published OpenAPI document (`openapi/nhms.v1.yaml` and the runtime schema alike) and from the generated frontend types, and so are the `RiverSegmentFeature` and `RiverSegmentFeatureCollection` components that only the list used
- **AND** no API response exposes the derived segment-level id form `<model_id>_seg_<iRiv>_<iEle>`; the only river segment ids the API accepts or returns are `core.river_segment.river_segment_id` values (reach rows `<model_id>_reach_<iRiv:06d>` and SHUD output rows)
- **AND** `GET /api/v1/basin-versions/{basin_version_id}/river-segments/{segment_id}` and `.../river-segments/{segment_id}/forecast-series` are unchanged: both still answer 200 for a reach id and for a SHUD output-river id of a renderable segment
- **AND** `core.river_segment_crosswalk` rows are still written by import and still joinable; retiring the list endpoint removes a reader, not the data

## REMOVED Requirements

### Requirement: River segments are imported with geometry and topology metadata

**Reason**: Its scenario "River segment map query returns segment-level features sliced from parent reach polyline" required the list endpoint `GET /api/v1/basin-versions/{basin_version_id}/river-segments` and its derived `<model_id>_seg_<iRiv>_<iEle>` id, which #2866 retires (owner ruling 2026-10-12: no external API users; the id resolves through no other endpoint). A MODIFIED block cannot drop a scenario, so the requirement is removed and re-added under a new name.

**Migration**: Every other statement and scenario of this requirement is carried verbatim by "River reaches are imported with geometry and topology metadata". Clients use `core.river_segment.river_segment_id` values (reach or SHUD output-river ids) with the detail and forecast-series endpoints; the map uses the MVT layer.
