## Design

**Must preserve:**
- **Import surface.** Every symbol importers use from `workers.model_registry.basins_geometry` stays importable from it, by the same name and identity:
  - `workers/model_registry/basins_registry_import.py:17-26` (`X as X` re-exports), `basins_registry_preflight.py:16-24`, `basins_registry_rows.py:16`, `basins_registry_support.py:21`, `qhh_bootstrap_fs.py:26`, `qhh_bootstrap_contracts.py:16`, `qhh_bootstrap_sources.py:19`, `qhh_production_bootstrap.py:30-32`;
  - `services/production_closure/object_store_validation_fixture.py:10` (`RIVER_SHP_REQUIRED_DBF_FIELDS`);
  - tests: `tests/basins_registry_import_helpers.py:31`, `tests/test_basins_registry_import_qhh.py:32-36`, `tests/test_production_object_store_validation.py:31`, and the module-object imports in `tests/test_basins_registry_import_parser.py:17` and `tests/test_basins_registry_import_security.py:19`.
  - `BasinsGeometryError` and the dataclasses are the same class objects whichever module they are imported from (each defined once). `ParsedBasinsGeometry` stays defined in `basins_geometry.py`.
  - `_point_wkt` and `_shud_count_header`, which `openspec/specs/basins-registry-import/spec.md:160` lists as retained in `basins_geometry.py`, stay importable from `basins_geometry`. `_point_wkt` moves to the shapes module because shape code calls it and is re-exported; "retained" there means not removed.
- **Monkeypatched globals keep working.** These tests patch globals on the `basins_geometry` module object and expect the code under test to honour them:
  - `MAX_BASINS_GIS_SIDECAR_BYTES` (`test_basins_registry_import_parser.py:136`, `test_basins_registry_import_security.py:458`);
  - `MAX_BASINS_GIS_FEATURES` (`test_basins_registry_import_parser.py:248`);
  - `MAX_BASINS_SHUD_EVIDENCE_LINES` (`:267`, `:288`);
  - `_SAFE_OPEN_TEST_HOOK` (`test_basins_registry_import_security.py:381-524`).

  Every function that reads one of these at call time stays in `basins_geometry.py`, or receives the value as an explicit argument from a caller in `basins_geometry.py` that reads the global at call time. `_domain_multipolygon_wkt` (reads `MAX_BASINS_GIS_FEATURES` / `MAX_BASINS_GIS_POINTS` at `:556`, `:563`) and `_river_segments_from_layer` (`:615`, `:624`) take required keyword-only `max_features` / `max_points` parameters with no defaults, passed by their caller in the primary module from the module globals at call time. No code in `basins_geometry_shapes.py` or `basins_geometry_types.py` references any `MAX_BASINS_*` name or `_SAFE_OPEN_TEST_HOOK`.
- **Behaviour.** Parsing results, checksums, error codes and messages, and resource-limit enforcement are unchanged. No logic edit beyond moving code, threading the two limit parameters, and the two deletions.
- **No edits to existing tests.** One new test is added (below); any edit to an existing test is a deviation to record.

**Must add / change:**
- The two new modules and the re-exports in `basins_geometry.py` (explicit `from ... import X as X`, the repo's existing re-export style in `basins_registry_import.py`).
- Delete `_stable_segment_id` and `_deduplicate_pending_segment_ids`. The other caller-less helpers (`_raw_segment_key`, `_mapped_downstream_segment_id`, `_optional_text`, `_shud_segment_count`) move unchanged.
- Update the inventory doc paragraphs; they must not claim the file has no caller-less helpers left.

**Dependency direction:** `basins_geometry` → `basins_geometry_shapes` → `basins_geometry_types`; `basins_geometry` → `basins_geometry_types`. No cycles; the new modules never import `basins_geometry`.

**Sibling surfaces:** `scripts/select_ci_tests_rules.py:1600` (`workers/model_registry/**`) already routes the new files; no selector change expected. `.large-file-guard.json` is not edited.

**Seams under test:**
- The existing suites `tests/test_basins_registry_import_parser.py`, `tests/test_basins_registry_import_security.py`, `tests/test_basins_registry_import_qhh.py`, `tests/test_production_object_store_validation.py`, and whatever `select_tests(<PR paths>)` selects.
- Existing coverage of the threaded limits is partial: `test_basins_registry_import_parser.py:248` (`MAX_BASINS_GIS_FEATURES=1`) only trips `_river_segments_from_layer` (the domain fixture has one record and `_check_resource_limit` uses `>`, `:1426`), and nothing patches `MAX_BASINS_GIS_POINTS`.
- **New tests** in `tests/test_basins_registry_import_parser.py`, each asserting the parse fails with `details["resource"] == "points"`. The domain is parsed before the river (`basins_geometry.py:238` before `:249`).
  - **Domain points:** the default fixture's domain has 5 points (`tests/basins_registry_import_helpers.py:212-213`), so set `MAX_BASINS_GIS_POINTS = 4`. This trips `_domain_multipolygon_wkt`.
  - **River points:** `_write_registry_fixture(tmp_path, sp_segment_count=3)` gives 6 river points against a 5-point domain, so set `MAX_BASINS_GIS_POINTS = 5`. The domain passes (5 is not > 5) and the river trips `_river_segments_from_layer`.
  - Optional: `MAX_BASINS_GIS_FEATURES = 0` covers the domain's `max_features`.
- **Red-check:** for each threaded parameter that has a test, temporarily make the receiving function ignore it (use a large constant) and confirm the matching test fails:
  - `_river_segments_from_layer` `max_features`: `:248`;
  - `_domain_multipolygon_wkt` `max_points`: domain-points test;
  - `_river_segments_from_layer` `max_points`: river-points test.

  Then restore.

**Required evidence:**
- `wc -l` of the three files, each under 1000. The agent-session large-file hook (`.claude/hooks/large-file-guard/`) blocks staged files over 1000 lines, but a plain terminal commit bypasses it, so `wc -l` is the real evidence. `.large-file-guard.json` byte-identical to master.
- `git grep -nE "MAX_BASINS_|_SAFE_OPEN_TEST_HOOK" -- workers/model_registry/basins_geometry_shapes.py workers/model_registry/basins_geometry_types.py` empty.
- `git grep -nE "_stable_segment_id|_deduplicate_pending_segment_ids" -- apps packages workers services scripts tests` empty.
- `git grep -n "_seg_" -- apps packages workers services` shows only `_delete_legacy_seg_rows`, `basins_seg_shp`, `parse_seg_shp_crosswalk`, the `<model>_seg_%` pattern/comments in `basins_registry_rows.py`, and the `<model>_seg_*` docstrings at `basins_registry_import.py:318` and `:436`.
- `uv run ruff check .`; `uv run pytest -q` on `select_tests(<all PR paths>)`.

**Non-goals:** renaming public symbols; changing parse behaviour; deleting the other caller-less helpers; touching `basins_registry_*` modules beyond what imports require (none expected).

**Review focus:** monkeypatch honouring after the move; class identity across re-exports; no import cycle; nothing but moves, two parameters, two deletions and one new test.
