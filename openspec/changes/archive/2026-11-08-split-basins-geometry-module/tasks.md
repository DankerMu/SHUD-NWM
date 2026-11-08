## Risk packs

- **Legacy compatibility (import surface) — selected.** Every listed importer keeps working; classes defined once. Covered by the existing importer suites and an import smoke of every listed symbol.
- **Test infrastructure (monkeypatched globals) — selected.** Patched globals stay in the primary module or are passed explicitly. Covered by the parser/security suites plus the red-check in design.md.
- **Resource limits — selected.** `MAX_BASINS_*` enforcement unchanged. Covered by the limit tests in the parser/security suites.
- **File IO / path safety — selected.** Safe-open code and `_SAFE_OPEN_TEST_HOOK` stay in `basins_geometry.py`. Covered by `tests/test_basins_registry_import_security.py`.
- **Not selected:** public API, schema, config, auth, concurrency, release, error handling (unchanged paths).

## Evidence Floor

- `uv run ruff check .`
- `uv run pytest -q` on `select_tests(<all PR paths>)`, including `tests/test_basins_registry_import_parser.py`, `tests/test_basins_registry_import_security.py`, `tests/test_basins_registry_import_qhh.py`, `tests/test_production_object_store_validation.py`
- each of the three files < 1000 lines (`wc -l`); `.large-file-guard.json` unchanged; no `MAX_BASINS_` or `_SAFE_OPEN_TEST_HOOK` reference in the two new modules
- the two `git grep` acceptance checks from design.md

## Tasks

- [x] 1.1 Create `basins_geometry_types.py` and `basins_geometry_shapes.py`; move code per proposal.md; thread `max_features` / `max_points` into the moved geometry functions; keep patched globals and safe IO in `basins_geometry.py`; re-export moved public symbols.
- [x] 1.2 Delete `_stable_segment_id` and `_deduplicate_pending_segment_ids`.
- [x] 1.3 Update `docs/governance/API_CONTRACT_RETIREMENT_INVENTORY.md` (`:301`, `:305-309`).
- [x] 1.4 Add the domain-points and river-points `MAX_BASINS_GIS_POINTS` tests; red-check the three threaded parameters (design.md), then restore.
- [x] 2.1 Ruff, selected pytest, `wc -l` line counts, grep checks recorded.
