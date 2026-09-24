## Risk packs

- **Public API / CLI / script entry: selected.** Every JSON route gains runtime response validation, and `/runs` drops unknown columns. Covered by the oracle (2.3), the conformance tests and the live receipt (5.2).
- **Schema / columns / units / field names: selected.**
  - HydroRun allowlist; numeric/null/absent preservation (2.3).
  - OpenAPI and TS regeneration (2.4).
- **Legacy compatibility / examples: selected.** Parsed-JSON equality for every route except #2222 (2.3). The frontend builds against the regenerated types (2.4).
- **Release / operational: selected.** Deploy display on node-27 after merge, take the before/after receipt (5.2); roll back to the previous SHA on failure.
- **Error handling: selected.** The new `ResponseValidationError` handler returns the standard envelope and logs one redacted line (2.3).
- **Resource limits / large input: selected.** Serialisation micro-benchmark (2.5) and live timing gate (5.2).
- **Not selected:**
  - Concurrency / shared state (#2177 changes only a pure key function).
  - Auth / permissions.
  - File IO.
  - Config.
  - Documentation / migration notes (OpenAPI is regenerated; no migration).

## 1. Baselines

- [x] 1.1 Live pre-state recorded in `.workplans/l1/`: 21 route snapshots, the `/runs` key set, the 3-spelling sha256, and the p50 timings. The live `hydro_run` has no `timeseries_store` column.
- [ ] 1.2 Record the exact dump call and settings that round-trip `openapi/nhms.v1.yaml` byte for byte from `app.openapi()` on `c9f363b38` (2.4 prerequisite).

## 2. Implementation

- [ ] 2.1 #2222:
  - `HYDRO_RUN_PUBLIC_COLUMNS`; explicit projection in `get_run` / `list_runs`.
  - Serializer allowlist.
  - `HydroRun` / `HydroRunPage` models on both routes.
  - Detail schema references `HydroRun`.
  - Tests with `timeseries_store` + `authority_internal` rows.
- [ ] 2.2 #2177: lowercase `source` in the cache key only, plus the tests listed in D3.
- [ ] 2.3 #2348:
  - `apps/api/response_models/` package covering all 35 JSON routes; envelope reshaping generalised.
  - Type-strict `tests/test_response_model_preservation.py` oracle over handler-object samples (every alternative success shape) and the route-table completeness check.
  - `tests/test_response_model_schema_parity.py`.
  - `ResponseValidationError` envelope handler and its test.
  - Mutation red-proofs (named samples).
- [ ] 2.4 `openapi/nhms.v1.yaml` and `apps/frontend/src/api/types.ts` regenerated. `test_openapi_drift`, `test_openapi_response_conformance` and `pnpm check:api-types` / `test` / `build` green.
- [ ] 2.5 Full-size `serialize_response` benchmark, master field vs new field: basin versions, river-segments, model detail, pipeline stages, forecast-series, latest-product, runs page. Numbers go in the PR body.
- [ ] 2.6 #2216: narrow-store tasks.md 1.15, per `fixtures/I1-1980.md` § Issue #2216.
- [ ] 2.7 Selector and CI routing for the new package and tests; tracked-tree guards green.

## 3. Verification

- [ ] 3.1 Local: `uv run ruff check .`; targeted pytest; `openspec validate forecast-api-response-contract --strict --no-interactive` and `timeseries-narrow-store-expand-contract`; the frontend commands.
- [ ] 3.2 node-27 full pytest on the frozen SHA (disposable DB). The failure set must equal master's.

## 4. Review / CI

- [ ] 4.1 Review rounds recorded with fix_gate. CI green, including SQL Migration Dry Run when routed.

## 5. After merge

- [ ] 5.1 node-27 `git pull --ff-only` and display restart per `docs/runbooks/node-27-bringup-checklist.md` C1: `/health`, runtime config `display_readonly`, `/slurm/health` 404.
- [ ] 5.2 Live receipt:
  - Re-snapshot the 21 routes. Compare against `.workplans/l1/pre/` recursively, checking both the key set and the JSON value type at every path; values may drift with new cycles. `queue_depth` (a display error), `best_available` (`[]`) and `state_snapshots` (empty) give no structural evidence and are reported as such.
  - The 3 source spellings produce identical bodies.
  - The p50 timing gate from D2.
  - Recorded in the post-merge archive PR.

## Evidence Floor

1. `get_run` / `list_runs` SQL has no `h.*`. Rows carrying `timeseries_store` / `authority_internal` leave both endpoints without those keys, and every current public field is kept.
2. The type-strict oracle shows master-field equals new-field for every modelled route and sample (except the #2222 filter). The completeness check shows all 35 JSON routes modelled. The parity test binds every published hand schema. Each mutation leg turns its named sample red. A forced validation failure returns the standard error envelope.
3. The static OpenAPI and TS types are regenerated. The drift test, response conformance and the frontend check/test/build are all green. `/runs/{run_id}` references `HydroRun`, not an open object.
4. #2177: three spellings produce one new `_store` entry and one new `_hot_paths` entry, with one loader call and identical bodies. `basin_id` / `status` stay exact, and the empty-page and literal-`None` semantics are kept.
5. #2216: the narrow-store 1.15 Evidence Floor.
6. The micro-benchmark and the live p50 within the D2 gate (or the deviation reported).
7. The node-27 full pytest failure set equals master's. The live receipt (5.2) shows parsed key-set and value-type equality for every route that has structural evidence.
