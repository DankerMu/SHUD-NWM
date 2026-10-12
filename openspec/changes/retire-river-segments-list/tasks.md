# Tasks

## 1. Retire the list endpoint (#2866)

- [ ] 1.1 Backend: delete the list route in `apps/api/routes/models.py`, the store list entry + Path C
      slice branch in `packages/common/model_registry_river_segments.py` (and re-exports in
      `model_registry.py` / `model_registry_public.py` / `model_registry_contracts.py`) **only where no
      caller other than the route remains**; delete the response models / OpenAPI patches that only the
      list used. Any non-route caller found (scripts, services, tests of import correctness) is reported
      and kept working — do not delete a helper that something else still calls.
- [ ] 1.2 Frontend: delete `fetchHydroMetRiverSegments`, its call, the `riverSegments` field and now-unused
      types / tests; regenerate `apps/frontend/src/api/types.ts`; `pnpm check:api-types` passes.
- [ ] 1.3 Tests: a contract test that the list path is absent from the OpenAPI document and is not
      served, and that detail still answers for a reach id and an output-river id (existing tests may
      already pin the latter — name them). Tests that pinned the list shape are deleted or re-pointed,
      each listed in the PR with its disposition. Tests that used the list only as a convenient way to
      observe import results are re-pointed at the store / SQL, not deleted.
- [ ] 1.4 Docs: retirement record in `docs/governance/API_CONTRACT_RETIREMENT_INVENTORY.md`;
      `docs/runbooks/display-mobile-evidence.md` workaround replaced; `docs/spec/04_api_design.md` and
      other LIVING docs that advertise the list updated; `services/production_closure/*` fixture
      dictionaries that name the list URL updated or reported.

## Correction (2026-10-12, during implementation)

Task 1.1's store deletion and the re-pointing of the two basins-registry import DB files are **withdrawn
from this change** — see proposal "Scope correction". Consequences for the text below: the store list
entry, its helpers, the `RIVER_SEGMENT_COLLECTION_*` constants, the river cases of
`tests/test_list_search_contract.py` and the store-level list tests of `tests/test_model_registration.py`
stay; the two Evidence-floor zero-greps are replaced by: no route, OpenAPI path / component, response
model, generated type or frontend source names the list (`git grep -nE "river-segments[\"'?]" -- apps
openapi` leaves only the detail match in `riverClickLane.test.ts`), and `git grep "_seg_" -- apps` is
empty. Follow-up issue: oracle transition + store removal.

## Named surfaces (from fixture review; not exhaustive, but each must be dispositioned in the PR)

**OpenAPI is hand-maintained.** `apps/frontend/src/api/types.ts` is generated from
`openapi/nhms.v1.yaml` (`apps/frontend/package.json:22-23`), and
`tests/test_openapi_drift.py::test_static_openapi_matches_runtime_schema` requires the YAML to equal
`app.openapi()`. So: edit the runtime patches, hand-edit the YAML to match, then regenerate types.

- Delete: the two component injections and the list patch in `apps/api/openapi_patching.py` (~:384-391;
  `_set_operation_response_schema`, defined in `apps/api/openapi_patching_envelopes.py`, returns silently for a missing path, so leaving them would keep two
  orphan components green); the two builders in `apps/api/openapi_restored_schemas.py` (~:185-250);
  the list response models in `apps/api/response_models/models.py` (~:112-137 — `RiverSegmentProperties`
  included, it is referenced only by `RiverSegmentFeature`; `GeoJsonGeometry` at ~:22 is shared and stays —
  and :316-317); the store's
  list entry and its private helpers (`_list_river_segment_rnv_ids`, `_has_segment_crosswalk_for_rnv`,
  `_list_river_segments_segment_slice`, `_reach_row_to_legacy_feature`); the `RIVER_SEGMENT_COLLECTION_*`
  constants if nothing else uses them.
- Keep: `GeoJsonLineString` / `GeoJsonMultiLineString` / `RiverSegment` (detail's `geom` still references
  them — fix the docstring that names Path C as their source), `RiverSegmentGeoJsonBudgetError` and
  `RIVER_SEGMENT_DETAIL_MAX_SERIALIZED_BYTES` (detail still uses them).
- Route count pin: `tests/test_response_model_schema_parity.py:237-238` asserts `len(ROUTES) == 39`; it
  becomes 38, and the test's name (`..._35_plus_4_...`) is renamed to stay true (the file does not contain the string `river-segments`, so a keyword grep misses it).
- Backend tests to disposition: `tests/test_api_contract_resources.py` (~:454-489),
  `tests/test_openapi_response_conformance.py`, `tests/test_response_model_preservation.py`,
  `tests/test_model_registration.py` (list cases), `tests/test_list_search_contract.py` (delete only the
  river cases; the FILE stays — met-station cases live there and `scripts/select_ci_tests.py` names it),
  `tests/api_contract_helpers.py`, `tests/test_basins_registry_import_db.py` and
  `tests/test_basins_registry_import_qhh.py` (re-point at SQL), `tests/test_real_database_integration.py`
  (~:242). Frontend: `apps/frontend/src/pages/hydroMet/__tests__/bootstrap.test.ts`.
- New contract test: `GET` on the list path answers `404` and not an `ok`/`data` envelope (do not assert
  the body text — the SPA fallback in `apps/api/startup_wiring.py:84-87` answers it); the path and the two
  components are absent from `app.openapi()`.
- Living docs to update: `docs/spec/04_api_design.md`, `docs/modules/13_api_backend_design.md`,
  `docs/modules/13_api_backend_spec.md`, `docs/spec/06_frontend_gis_design.md`,
  `docs/appendices/E_api_openapi_draft.md`, `docs/runbooks/qhh-backend-smoke.md` (~:218),
  `docs/runbooks/display-mobile-evidence.md`, `docs/governance/API_CONTRACT_RETIREMENT_INVENTORY.md`.
  `services/production_closure/scale_validation.py:97` names a list URL with a `bbox` parameter the route
  never had: remove or replace that synthetic entry with a real endpoint, do not invent another URL.
- Not touched: `workers/model_registry/basins_geometry.py` `_stable_segment_id` (pre-existing dead `_seg_`
  generator — report only); historical receipts and `docs/review/**`.

## 约定

- **Fixture level:** compact. **Risk packs:** api-contract, frontend type sync.
- **Must preserve:** detail endpoint and forecast-series behaviour for `_reach_` and `_shud_riv_` ids;
  import and `core.river_segment_crosswalk` writing; MVT; every other `/basin-versions/{bv}/...` route;
  no migration, no schema change.
- **Non-goals:** see proposal "Out of scope".
- **Evidence floor:** `uv run ruff check .`; touched pytest files green; the new contract test red
  against the pre-change tree; `pnpm tsc` / `pnpm test` / `pnpm check:api-types` green;
  `openspec validate retire-river-segments-list --strict`; `git grep "_seg_"` in `apps/ packages/`
  shows no remaining producer of the derived id; `git grep -nE "list_river_segments|river-segments[\"'?]" -- apps packages tests services`
  leaves only the new contract test and `apps/frontend/src/__tests__/riverClickLane.test.ts` (its
  `includes('river-segments')` matches the DETAIL request and stays untouched). Must-run even though not edited: `tests/test_openapi_drift.py`,
  `tests/test_response_model_schema_parity.py`. node-27 real-DB pytest for the touched integration
  files (`tests/test_real_database_integration.py`, the two basins-registry import DB files) from the
  verify checkout. No production deploy.
