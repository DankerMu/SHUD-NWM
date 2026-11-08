## ADDED Requirements

### Requirement: The basins geometry parser SHALL stay within the large-file guard without changing its import surface

`workers/model_registry/basins_geometry.py` and every module split from it SHALL each be under the large-file guard's 1000-line limit without a guard exclusion. Every public symbol of `workers.model_registry.basins_geometry`, and every name an existing importer or test uses from it, SHALL remain importable from it, except the two deleted helpers; and exception and dataclass types SHALL be single class objects regardless of the import path. Resource-limit and safe-open globals patched on the `basins_geometry` module SHALL keep governing parsing. The caller-less segment-id producers `_stable_segment_id` and `_deduplicate_pending_segment_ids` SHALL NOT exist.

#### Scenario: Importers after the split
- **WHEN** any existing importer imports `BasinsGeometryError`, `CrosswalkRow`, `ParsedBasinsGeometry`, `TrustedBasinsRoot`, `parse_basins_geometry`, `parse_seg_shp_crosswalk`, `safe_basins_file_sha256`, `trusted_basins_root`, the suffix sets or `RIVER_SHP_REQUIRED_DBF_FIELDS` from `workers.model_registry.basins_geometry`
- **THEN** the import succeeds and exception and dataclass types are identical to those used by the parser

#### Scenario: A patched feature limit
- **WHEN** a test sets `basins_geometry.MAX_BASINS_GIS_FEATURES` to 1 and parses a basin with more features
- **THEN** parsing fails with the resource-limit error, as before the split

#### Scenario: No `_seg_` id producer
- **WHEN** the repository is searched for `_stable_segment_id` or `_deduplicate_pending_segment_ids`
- **THEN** there is no match

#### Scenario: A patched point limit
- **WHEN** a test sets `basins_geometry.MAX_BASINS_GIS_POINTS` below the domain's or the river's point count and parses the basin
- **THEN** parsing fails with the resource-limit error for `points`, as before the split
