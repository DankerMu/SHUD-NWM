## Why

`workers/model_registry/basins_geometry.py` is 1440 lines and is not in the `.large-file-guard.json` exclusions (`maxLines` 1000), so the agent-session large-file hook rejects any commit that stages it, and the repo rule forbids new exclusions (#3081). Because of that, #2907 could not delete the two caller-less helpers in it:

- `_stable_segment_id` (`basins_geometry.py:978-985`), the last producer of `_seg_`-derived segment ids that #2866 wants gone;
- `_deduplicate_pending_segment_ids` (`:986-998`), a producer of the old `_ord_…_rec_` ids.

`git grep` finds only their definitions.

## What Changes

- **Split by responsibility**, each file under 1000 lines, no new guard exclusion:
  - `workers/model_registry/basins_geometry_types.py` (new leaf, ~130 lines): `BasinsGeometryError`, the dataclasses (`RiverSegmentGeometry`, `CrosswalkRow`, `_LayerSnapshot`, `_CoordinateTransform`, `TrustedBasinsRoot`, `_GisReadBudget`), `_check_resource_limit`, `_jsonable`.
  - `workers/model_registry/basins_geometry_shapes.py` (new, ~510 lines): coordinate transforms and domain/segment geometry (`:423-702`), `parse_seg_shp_crosswalk` (`:818-879`), the shape helpers (`:880-1064`). Imports only the types module.
  - `workers/model_registry/basins_geometry.py` (stays the primary module, ~845 lines): `ParsedBasinsGeometry` (kept here so its `output_segment_count` comment stays where `openspec/specs/basins-registry-import/spec.md:283-284` pins it), constants (`MAX_BASINS_*`, suffix sets, `RIVER_SHP_REQUIRED_DBF_FIELDS`), `_SAFE_OPEN_TEST_HOOK`, `trusted_basins_root`, `parse_basins_geometry`, sidecar limits, layer snapshot, validation, SHUD header/checksum helpers, safe IO. It re-exports every public symbol that moved.
- **Delete** `_stable_segment_id` and `_deduplicate_pending_segment_ids`.
- **Docs:** `docs/governance/API_CONTRACT_RETIREMENT_INVENTORY.md` `:301` and `:305-309` (#2907 Residual / "Left after #2907") record that the helpers were deleted by #3081.

## Triage

```text
Issue type: refactor (tech-debt)
Fixture level: expanded
Upstream suggested level: absent
Blast radius: basins registry import / qhh bootstrap parse path; a moved function that reads a monkeypatched module global silently stops honouring the patch (resource-limit and safe-open tests turn vacuous or red); a missing re-export breaks importers at import time
Selected risk packs: Legacy compatibility (import surface); Test infrastructure (monkeypatched globals); Resource limits (MAX_BASINS_* enforcement); File IO / path safety (safe-open code stays put)
Evidence floor: uv run ruff check . ; uv run pytest -q on select_tests(<all PR paths>) incl. basins/registry suites ; each file < 1000 lines by wc -l ; git grep acceptance checks
```

## Impact

- **Code:** `workers/model_registry/basins_geometry.py`, new `basins_geometry_types.py`, new `basins_geometry_shapes.py`.
- **Tests:** existing tests unchanged; two new point-limit tests in `tests/test_basins_registry_import_parser.py`. The existing basins registry import parser/security/qhh suites are the oracle.
- **Docs:** `docs/governance/API_CONTRACT_RETIREMENT_INVENTORY.md`.
- **Spec:** `basins-registry-import`.

## Notes

- The other caller-less helpers in the file (`_raw_segment_key` `:999`, `_mapped_downstream_segment_id` `:1010`, `_optional_text` `:1048`, `_shud_segment_count` `:1110`) move unchanged; deleting them is not in scope, and the inventory doc must not claim the file has no caller-less helpers left.
