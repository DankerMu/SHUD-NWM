## ADDED Requirements

### Requirement: River segment row classes and counting invariant are documented and pinned

The repository SHALL define, in `openspec/glossary.md`, the two row classes `core.river_segment` stores under one `river_network_version_id`: the SHUD input reach row (`<model_id>_reach_<iRiv:06d>`, from `gis/river.shp`, `shud_output_river` absent or `'false'`, carries hydraulic parameters and is the only `core.river_segment_crosswalk` FK target) and the SHUD output river row (`<model_id>_shud_riv_<N:06d>`, from `.sp.riv`, `shud_output_river='true'`, carries output-series identity, geometry backfilled from the matching reach row). The repository SHALL state in the same glossary entry and in `docs/runbooks/current-production-ops.md` that `core.river_network_version.segment_count` counts reach rows only, that the two classes are equal in number because `river.shp` record count is validated equal to the `.sp.riv` reach count at import, that `count(*) == 2 × segment_count` for an rnv is therefore a design fact, and that `output_segment_count` exists only in the import receipt and `model_instance.resource_profile`, not as a `core.river_network_version` column.

#### Scenario: Hygiene query compares the right class

- **WHEN** an operator follows the runbook's river-segment count check for one `river_network_version_id`
- **THEN** the query filters with `COALESCE(properties_json->>'shud_output_river','false')` before comparing against `segment_count`
- **AND** the runbook states that an unfiltered `count(*)` equal to `2 × segment_count` is the expected value, citing #1122 and #1123 as the precedents

#### Scenario: Import test pins the invariant

- **WHEN** the real-DB import test in `tests/test_basins_registry_import.py` imports a fixture package and reads the resulting rnv
- **THEN** it asserts the physical `core.river_segment` row count equals `2 × river_network_version.segment_count`
- **AND** it asserts the `shud_output_river='true'` row count equals the reach row count

#### Scenario: Code comments point at the definition

- **WHEN** a reader opens the two-row-class comment in `workers/model_registry/basins_reingest.py` or the `output_segment_count` comment in `workers/model_registry/basins_geometry.py`
- **THEN** the `basins_geometry.py` comment states that `segment_count` counts `gis/river.shp` reach records (post-PR-2), not `seg.shp`/`.sp.rivseg` display geometry
- **AND** the `basins_reingest.py` comment names the glossary terms
- **AND** no import, parser, or backfill behavior changes
