## Why

Batch L1 of the 10-batch serial run: the forecast API response contract (master `c9f363b38`).

- **#2222**: `PsycopgForecastStore.get_run` / `list_runs` (`packages/common/forecast_store.py:1410-1500`) `SELECT h.*`, and `_hydro_run_response` (`:4547`) serialises the row verbatim. Any future `hydro.hydro_run` column (the I7 `timeseries_store` router, any authority column) flows into `GET /api/v1/runs{,/{run_id}}` unreviewed. The list OpenAPI names `HydroRunPage`, while the detail is `additionalProperties: true`. The `timeseries_store` column that #2222 anticipated was added by `000059` and dropped again by the `000060` contract. Live `hydro.hydro_run` does not have it (checked 2026-09-24), so nothing leaks today, but the `h.*` pass-through remains a structural hole.
- **#2348**: of 47 routes in `apps/api/routes/*.py` (the issue said 46), only 4 declare `response_model`. 8 return `Response` (tiles/PNG). The other **35 JSON routes** are typed `dict[str, Any]`, so their shape is not enforced at runtime, and their 200 schemas are hand-patched in `apps/api/openapi_patching.py` / `openapi_restored_schemas.py`.
- **#2177**: the `/api/v1/runs` display-cache key (`apps/api/routes/forecast.py:~114`) is case-sensitive in `source`, but the store compares with `LOWER()`. `GFS` / `gfs` / `Gfs` each take their own cache slot.
- **#2216** (narrow-store I1): an unqualified output `*` over the river fact table (`SELECT * FROM hydro.river_timeseries [rt]`) makes the guarded text-identity helper return an empty set, and narrow render returns the SQL. This fixture is an addendum in the active `timeseries-narrow-store-expand-contract` change (task 1.15 plus a `fixtures/I1-1980.md` section), following the #2115 pattern. This change only references it.

## What Changes

- **#2222**:
  - One public `HydroRun` field allowlist in the store; `get_run` / `list_runs` project explicit `h.<col>` plus the existing join aliases.
  - The serializer projects through the same allowlist.
  - Both routes declare a Pydantic `HydroRun` / `HydroRunPage` `response_model` whose unknown keys are dropped.
  - The OpenAPI detail response references the same `HydroRun`.
- **#2348**:
  - All 35 JSON routes (including those in the #2222 pair) declare a Pydantic `response_model` in a new `apps/api/response_models/` package.
  - Where a hand-written schema is published today, it stays the published contract and a parity test binds it to the model. #2348's "逐步替换" (gradual replacement) is followed with a binding test instead of deletion; this deviation is recorded. Routes with no hand-written schema publish the generated one.
  - A `ResponseValidationError` handler returns the standard error envelope.
  - `openapi/nhms.v1.yaml` and `apps/frontend/src/api/types.ts` are regenerated in the same PR.
  - Parsed JSON response bodies must not change, except for the #2222 filtering.
- **#2177**: the `source` component of the `/runs` cache key is lowercased. The SQL, the response bodies, and the `basin_id` / `status` key dimensions are unchanged.
- **#2216**: a scanner-owned unqualified output-star classifier is added to `packages/common/river_ts_render.py`, per the narrow-store addendum.

## Impact

- Affected specs: `forecast-api` (ADDED: HydroRun public projection), `api-contract-convergence` (ADDED: JSON routes declare runtime response models), `display-catalog-cache-trust` (ADDED: run-list cache key source case), and `timeseries-narrow-store` (in the active narrow-store change, task 1.15).
- Affected code:
  - `apps/api/routes/{forecast,models,pipeline,data_sources,best_available,state_snapshots}.py`
  - new `apps/api/response_models/`
  - `apps/api/openapi_patching.py`, `apps/api/openapi_restored_schemas.py`
  - `packages/common/forecast_store.py` (runs SQL/serializer only)
  - `packages/common/river_ts_render.py`
  - `openapi/nhms.v1.yaml`, `apps/frontend/src/api/types.ts`
  - tests
- Out of scope:
  - forecast-series / latest-product **payload content**: gaining a `response_model` changes validation, not bytes.
  - read-path SQL performance (L2: #2424 found that forecast-series `issue_time=latest` takes ~9 s warm and can hit a 30 s timeout).
  - a `list_runs` free-text domain check.
  - normalising the `cycle_time` key sibling (named in #2177; not done).
  - a frontend runtime validator (#2129 B).
  - tile/PNG routes.

## Triage

```text
Issue type: contract hardening + bugfix
Fixture level: expanded
Upstream suggested level: absent (expanded: public API response shape on every JSON route, generated client types, display cache key)
Blast radius: model narrower than the handler -> fields silently dropped or 500 ResponseValidationError on production display routes; wrong numeric/null typing -> JSON changes break the frontend; validation cost on large payloads -> display latency regression
Selected risk packs: Public API / CLI / script entry; Schema / columns / units / field names; Legacy compatibility; Error handling (ResponseValidationError envelope); Release / operational (display deploy); Resource limits / large input (validation cost)
Evidence floor: see tasks.md
```
