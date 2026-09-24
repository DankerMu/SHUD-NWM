## Context

- Line numbers are taken at `c9f363b38`. FastAPI 0.136.1, Pydantic 2.13.4.
- Precedent for the pattern: `apps/api/routes/hydro_display_models.py` (the `ApiSuccessEnvelope` subclasses behind the 4 existing `response_model` routes).
- Live pre-state (public `https://test.nwm.ac.cn`, 2026-09-24 ~13:00Z, saved to `.workplans/l1/pre/*.json` and `pre-timing.txt`):
  - 21 GET route snapshots.
  - `/runs` items carry 25 keys and no `timeseries_store` (the column is absent on the live `hydro.hydro_run`).
  - `/runs?source=GFS|gfs|Gfs` produce the same body sha256.
  - Timing, p50 of 8: forecast-series (explicit `run_id`) ≈ 0.44 s; latest-product ≈ 0.25 s; `/runs?limit=50` ≈ 0.33 s. `river-segments?limit=2` ≈ 10 s already before this change (out of scope; L2).

## Decisions

### D1: #2222 HydroRun public projection (three layers)

- **Premise update.** `timeseries_store` was added by `000059:76` and dropped again by `000060:106`, the river contract. It is absent from live `hydro.hydro_run`, and the live `/runs` items carry 25 keys. The defect is structural: `h.*` passes through any future column. The synthetic `timeseries_store` / `authority_internal` rows remain the general guard.
- **Layer 1, SQL.** A module constant `HYDRO_RUN_PUBLIC_COLUMNS` (a tuple in fixed order) holds the current public `hydro_run` columns: every key in `.workplans/l1/pre/run.json` minus the three join aliases. `get_run` and `list_runs` render `h.<col>` for each, followed by the existing `river_network_version_id`, `basin_id` and `source` aliases. Neither keeps `h.*`.
- **Layer 2, serializer.** `_hydro_run_response` projects onto `HYDRO_RUN_PUBLIC_COLUMNS` plus the aliases before `_json_ready`.
- **Layer 3, runtime model.** `HydroRun` and `HydroRunPage` are Pydantic models with `extra="ignore"`, which drops unknown keys at runtime. `extra="forbid"` is NOT used, because it would return a 500 instead of dropping the key.
  - Field types follow what `_hydro_run_response` actually returns. `_json_ready` stringifies datetimes, so those fields are `str`, and each field's required/nullable status matches today's published `HydroRun`: `start_time`, `end_time`, `created_at` and `updated_at` are required, non-null, `format: date-time` strings (`types.ts:1745-1776`), with `run_type` / `status` referencing `RunType` / `RunStatus`.
  - The published schema is the existing hand `HydroRun` in `openapi_restored_schemas.py` (see D2 "publication"), with `additionalProperties: false` added.
  - The `/runs/{run_id}` 200 response becomes `allOf[SuccessEnvelope, {data: $ref HydroRun}]` instead of an open object. No frontend code consumes the detail path (the frontend uses only `components["schemas"]["HydroRun"]`), so that change is safe for TS.
- **Test fakes.** Fakes that return less than production does (e.g. `tests/test_forecast_api.py:261-277`, which returns only `{run_id, status, source}`) are completed in test code. The model is not loosened to fit them.
- **Test.** Feed synthetic rows carrying `timeseries_store` and `authority_internal` through both endpoints and the SQL-shape seam. Neither key may appear in the output; all 25 public keys stay. forecast-series and latest-product must be unaffected. `timeseries_store` is **not** made public.

### D2: #2348 runtime response models for all 35 JSON routes

- **Inventory and scope.** 47 routes in `apps/api/routes/*.py`: 4 already have a model, 8 return `Response` (tiles and PNG), and 35 are JSON routes. All 35 get an explicit `response_model` in this PR, including the internal write and control routes. The PR body carries the inventory table (route → model → published schema source → sample source). That table answers #2348 acceptance item 1; the whole inventory is one batch because the user asked for L1 to be a single PR.
  - The 4 pre-modelled routes keep their current arrangement (runtime model plus published hand schema) and join the parity test below. No other change for them.
  - Routes outside `apps/api/routes/` are out of scope and must be listed as such in the completeness test: `/api/v1/runtime/config` (`startup_wiring.py:37`), `/health`, the slurm gateway routes, and `/{full_path}`.
- **Where models live.** A new package `apps/api/response_models/`: one module per route module plus the envelope. Component names reuse the existing published names. No touched file may exceed 1000 lines unless it is already excluded.
- **Current serialization baseline** (what "unchanged" means). FastAPI 0.136 already treats the `-> dict[str, Any]` / `list[dict[str, Any]]` annotation as the response field. Today every one of these routes serializes through Pydantic `dump_json` on a `dict[str, Any]` field, not `jsonable_encoder`. The rule:
  - For every route and every sample, the new model field must yield the same `json.loads` value as master's `dict[str, Any]` field, both run through `fastapi.routing.serialize_response(..., dump_json=True)` with the route's exclude flags.
  - The comparison is **type-strict**: walk the tree and require the same JSON type at every path (`int` vs `float`, `str` vs `number`, `null` vs absent). Plain `==` does not count.
  - Key order may change.
  - The only planned exception is the #2222 pair, whose expected value is the filtered payload.
- **Model typing rules.** A model must accept everything the handler produces today, because a validation error is a 500.
  - Type fields by the **Python objects the handler returns**:
    - `datetime` where the handler returns `datetime` objects. `packages/common/model_registry_public.py` and `model_registry_catalog.py` return `dict(row)` with raw datetimes, which serialize as `...Z` today, so those fields are `datetime`, not `str`.
    - `str` only where the value is provably stringified already, as with `_json_ready`.
  - `int | float` wherever either can occur.
  - Use `list[Any]` for GeoJSON coordinates and forecast-series `points`. Points are `[[epoch_ms:int, float]]` pairs; a `float` element type would print `1790164800000.0` and validate every point.
  - Use `dict[str, Any]` or `extra="allow"` wherever the handler passes through open mappings or the published schema says `additionalProperties: true`.
  - Use `response_model_exclude_unset=True` wherever handlers omit optional keys. That keeps absent keys absent instead of emitting `null`.
- **Samples.**
  - Samples are the Python objects the handlers return: built with the store fakes or the real builders, not parsed from the JSON snapshots.
  - Every list field is non-empty, and every optional branch is covered.
  - Every alternative success shape a handler can return (by role, mode, or query) is enumerated in the inventory table and gets its own sample.
  - The live snapshots `pre/state_snapshots.json` (empty items), `pre/best_available.json` (`[]`) and `pre/queue_depth.json` (a display_readonly error) are NOT usable as samples.
- **Publication (OpenAPI) — decision.** The published contract does not change except where stated. The existing envelope-reshaping step (`openapi_patching.py:299-330`: pop the generated `*Response` / `ApiSuccessEnvelope`, keep `SuccessEnvelope`, publish each 200 as `allOf[SuccessEnvelope, {data: $ref}]`) is generalised to every newly modelled route.
  - **Where a hand data schema already exists** (`openapi_restored_schemas.py`, `openapi_patching_{envelopes,display_schemas,ops_schemas,pipeline}.py`), it stays the published component. The generated duplicate component is dropped, so the published component name, `required`, nullability, `format` and enum `$ref` do not churn. `tests/test_response_model_schema_parity.py` binds the two: for every route whose published data schema is a hand schema, the model's generated JSON schema must have the same property-name set and the same `required` set at every object level. Nullability and enum names must be compatible.
  - **Where no hand data schema exists** (today an open object), the generated data component is published, so the `/runs/{run_id}` style open objects become typed. The envelope layout follows the handler:
    - an enveloped route publishes `allOf[SuccessEnvelope, {data: $ref}]`;
    - a route that returns **no envelope** publishes the generated schema at the response root, exactly matching what it returns. These routes are `/api/v1/met/best-available` (a top-level list), `/api/v1/state-snapshots` (a bare `{total_count, items, limit, offset}` page) and `/api/v1/state-snapshots/{state_id}` (a bare snapshot). They are named in the inventory. forecast-series is also bare, but keeps its hand schema.
  - Prune any generated component that no published path references, so it does not leak into the yaml or TS.
  - Deviation from #2348's "用生成的 schema 替换手工补写" (replace the hand-written schemas with generated ones): the issue itself says "逐步" (gradually). This PR binds hand schemas to models by test instead of deleting them. The honest note goes in the PR body, and #2348 is closed with that recorded.
- **Error path.** `apps/api/errors.py` has no handler for `ResponseValidationError`, so a too-narrow model in production would return a bare 500 with no `request_id`. Register a handler that returns the standard error envelope (500, new code `RESPONSE_VALIDATION_ERROR`, `details: null`) and writes one redacted server log line with the route path, `request_id` and error locations and types only. The log line must never contain `exc.body` or any payload values, which could be an 888 KB response, following the forecast-api "Error responses SHALL leave a redacted server-side log line" requirement. Test it by forcing a mismatching payload.
- **Oracle and completeness.**
  - `tests/test_response_model_preservation.py` holds the per-route and per-sample type-strict comparison above.
  - Route-table completeness: every JSON `APIRoute` from `apps/api/routes/*.py` has an explicit `response_model`. Out-of-scope routes are listed with reasons.
  - Mutations, each of which must turn a named sample red:
    - remove `exclude_unset` on a named route (name the route and the sample that omits an optional key);
    - narrow a field to `float`;
    - declare a registry datetime as `str`.
- **OpenAPI and types regeneration.**
  - Task 1.2 finds and records the exact dump call (module and settings) that round-trips `openapi/nhms.v1.yaml` byte for byte from `app.openapi()` on `c9f363b38`. If none does, apply minimal hand edits and keep `test_static_openapi_matches_runtime_schema` green.
  - Regenerate `types.ts` with `cd apps/frontend && pnpm generate:api`.
  - Green: `pnpm check:api-types`, `pnpm test`, `pnpm build`, `tests/test_openapi_drift.py`, `tests/test_openapi_response_conformance.py` (property 1 still finds `data` through the `SuccessEnvelope` layout).
  - Report the component diff before and after in the PR. Expected: only the formerly open objects become typed, plus `HydroRun.additionalProperties: false`.
- **Performance.**
  - In-process benchmark of `serialize_response`, master field vs new field, on **full-size** payloads: basin versions (888 KB GeoJSON, `pre/basin_versions.json` shape), river-segments, model detail (42 KB), pipeline stages (38 KB), forecast-series, latest-product, and a runs page.
  - Live node-27 after deploy: repeat `forecast-series-url.txt` plus `/basins/basins_huaiyss/versions`, `/models/{id}` and `/pipeline/stages?...` from `routes.txt`.
  - Gate: p50 regression at most 15 % or 30 ms per route, whichever is larger. A breach is reported and fixed, for example by loosening that route's heavy subtree to `list[Any]` / `dict[str, Any]`.

### D3: #2177 run-list cache key

- In `apps/api/routes/forecast.py` `list_runs`, use `source.lower() if source is not None else None` **only in the cache key**. The value passed to `store.list_runs` is unchanged, the SQL is unchanged, and the response body is unchanged (its `source` field comes from the row).
- Tests, modelled on `tests/test_hydro_display_mvt_scaling.py::test_valid_times_cache_key_collapses_spellings_and_separates_identities` and `tests/test_precip_overlay.py:1895`:
  - `GFS` / `gfs` / `Gfs` produce exactly one new `_store` entry **and** exactly one new `_hot_paths` entry, with the loader called once.
  - The bodies are identical, including the case of `source`.
  - `basin_id=Yangtze` and `basin_id=yangtze` stay distinct keys, and so do the two `status` spellings.
  - An empty page is still not cached.
  - The literal `source=None` stays distinct from an omitted `source`.
- `cycle_time` key normalisation is not done; it is recorded as a sibling in the PR.

### D4: #2216

This is governed by `openspec/changes/timeseries-narrow-store-expand-contract/fixtures/I1-1980.md` § "Issue #2216" and tasks.md 1.15, pure local renderer logic. The current registry has 13 entries, not the issue's 20. That section is the authority; this change only sequences it into the L1 PR.

## Governing invariant

- A public `/runs` payload contains only the `HydroRun` public fields. A new `hydro_run` column cannot reach it without an explicit allowlist change.
- Every JSON route in `apps/api/routes/` declares a runtime response model. Adding one does not change the type-strict parsed response. Where a hand schema is published, a test binds it to the model.
- The `/runs` cache key's `source` dimension matches the store's case-insensitive comparison. The exact-match dimensions stay exact.

## Sibling surfaces

- `_paginated_payload` / `_ok` envelope helpers in each route module.
- `apps/api/display_cache.py` key consumers (the other 4 `display_catalog_cached` call sites, unchanged).
- `apps/api/openapi_patching.py` finalizers (MVT, precip, runtime) that must keep working after hand-patch removal.
- `tests/test_api_contract*.py`, `tests/test_openapi_*`.
- The frontend `src/api/client.ts` and store consumers of the regenerated types.
- `scripts/select_ci_tests.py` rules for the new package and tests; `tests/test_select_ci_tests.py` tracked-tree guards.

## Seams under test

- Route → store fake: `TestClient`.
- `serialize_response` oracle over samples.
- Store SQL text: the fake-cursor SQL-shape seam for the `h.<col>` projection.
- node-27 real-DB pytest (the full suite, including `tests/test_forecast_api.py` real-DB legs and response conformance).
- node-27 live display receipt.

## Required evidence

See tasks.md.

## Non-goals

- Making any currently open `additionalProperties: true` object strict, other than `HydroRun`.
- Deleting the hand-written published schemas. They stay, bound by the parity test (see the D2 deviation).
- Changing error envelopes.
- Changing tile/PNG routes.
- Changing forecast-series / latest-product builders.
- L2 SQL performance work.
