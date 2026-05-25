## Context

The repository already contains most of the ingredients for a constrained MVP. QHH has real live-chain evidence: calibrated `data/Basins/qhh`, 386 forcing stations seeded from `qhh.tsd.forc`, real GFS/IFS cycles, parsed `q_down` rows for 1633 SHUD output river segments, display-product publication, and reusable diagnostic scripts. M20 also established the formal backend scheduler path through `nhms-pipeline plan-production`, with Slurm preflight, pipeline persistence, retry/cancel evidence, and clear production-readiness boundaries.

The current gap is product convergence. Existing forecast-series APIs can return river `q_down`, and `/api/v1/met/stations` can return station inventory, but the real station forcing series route is not implemented despite the OpenAPI placeholder. The frontend station page still relies on fixture/unavailable contracts for station forcing curves. The monitoring page has reusable controls, but the MVP must prove it reads formal orchestrator/pipeline state and can restart controlled failures. This change defines the narrow launch slice that connects those surfaces without expanding into nationwide or final-production scope.

## Goals / Non-Goals

**Goals:**

- Ship a two-entry internal MVP for QHH/limited basins: hydrology/meteorology display and operations.
- Read real station forcing samples from `met.forcing_station_timeseries` with station/source/cycle/forcing-version provenance.
- Let the frontend discover the latest usable QHH product without manually entering `run_id`, `forcing_version_id`, `basin_version_id`, or `river_network_version_id`.
- Display real river-segment `q_down` curves and station forcing curves without synthetic fallback data.
- Show formal pipeline stage/job/log/retry state from the backend orchestrator path, including controlled failure/retry evidence.
- Produce one QHH smoke evidence set covering download, canonical, forcing, SHUD, parse, station series, forecast series, UI display, and operations controls.

**Non-Goals:**

- Nationwide all-basin product completeness.
- Water level `stage` support or language in the MVP.
- CLDAS, ERA5 near-real-time, live IdP, live alert sink, rollback proof, or final production readiness.
- New solver behavior, new forcing algorithms, or new flood frequency claims.
- Treating qhh diagnostic scripts as the production scheduler dependency.

## Decisions

### MVP Data Contract

The MVP hydrologic variable is `q_down` discharge from `hydro.river_timeseries`. The MVP meteorology variables are `PRCP`, `TEMP`, `RH`, `wind`, `Rn`, and `Press` from `met.forcing_station_timeseries`. UI copy and issue scope must use "river discharge" or "river-segment flow", not "water level", unless a later change adds stage support.

### Station Series API

Implement `GET /api/v1/met/stations/{station_id}/series` as a read API over existing forcing station time series. The API accepts explicit `forcing_version_id` when known, or resolves one from `model_id`, `source_id`, and `cycle_time` through existing forcing/run metadata. It returns grouped series by variable, point-level `valid_time`, `value`, and `quality_flag`, plus `unit`, provenance, and `truncated` metadata.

Alternative considered: add station series to the existing station list response. That would make inventory queries large and unbounded. The separate detail route keeps the list cheap and the chart request bounded.

### Latest Display Product

Add a lightweight QHH latest-product API or equivalent stable aggregation. It should select the latest usable QHH display product for a requested source and return enough identifiers for the UI to fetch stations and river forecasts. The aggregation may compose existing runs/models/forcing queries internally, but the frontend should not need to guess versions or build IDs by convention.

Alternative considered: have the frontend call `/runs`, `/models`, station list, and river segment APIs separately and infer identity. That increases race conditions and duplicates domain rules in the UI; it is acceptable as an implementation fallback only if hidden behind a frontend data adapter with the same contract.

### Frontend Shape

Expose two MVP navigation entries: `/hydro-met` and `/ops`. The implementation may reuse `/meteorology`, `/forecast`, `/segments/:segmentId`, and `/monitoring` components, but the visible MVP workflow should not require users to jump among legacy pages. `/hydro-met` loads latest product, station inventory, river segments, and selected charts. `/ops` reuses monitoring controls but removes or hides non-MVP distractions.

### Operations Boundary

Operations evidence must come from formal pipeline/orchestrator persistence and APIs. QHH scripts can remain documented as diagnostic/reproduction paths, but they must not be the scheduler dependency for the MVP operations page. Failed run restart uses existing retry APIs and must be demonstrated with a controlled failure.

### IFS Horizon

IFS 00/12 UTC cycles may be full 7-day candidates. IFS 06/18 UTC cycles can have shorter usable horizons and the UI/API must expose actual available end time instead of padding or fabricating values.

### Release Evidence

Fast tests verify contracts and deterministic fixtures. Real QHH smoke can be opt-in because it depends on database, source data, Slurm/runtime, and local assets. Evidence must label deterministic, production-like, and live execution modes and must not claim final production readiness.

## Risks / Trade-offs

- **Risk: station series queries become expensive.** Mitigation: require bounded `limit`, variable filters, time filters, and use the existing primary key plus targeted lookup indexes where query plans show need.
- **Risk: latest-product selection hides incomplete products.** Mitigation: return status, counts, valid-time range, unavailable reasons, and never select products with missing required identities as ready.
- **Risk: UI shows synthetic or misleading charts.** Mitigation: empty/unavailable responses render explicit states; no fake series or padded IFS horizon.
- **Risk: operations page passes local script evidence as formal control.** Mitigation: specs and tasks require `ops.pipeline_job`/API-backed stage/job/log/retry evidence and preserve qhh scripts only as diagnostics.
- **Risk: MVP scope expands into production readiness.** Mitigation: non-goals and release checklist separate internal MVP from final live production proof.

## Migration Plan

1. Add backend read-path support for station series and latest product without changing existing route behavior.
2. Add any needed database indexes through migrations after confirming query shape.
3. Regenerate frontend API types from OpenAPI and wire UI adapters.
4. Add `/hydro-met` and `/ops` navigation entries while preserving existing routes.
5. Bind operations UI to formal pipeline job/log/retry APIs and validate controlled failure/retry.
6. Run contract tests, frontend build/tests, and opt-in QHH smoke evidence.
7. Update docs and progress with MVP scope and remaining production-readiness boundaries.

Rollback is straightforward for read-only API/UI changes: hide the MVP nav entries and leave existing pages/routes intact. Database indexes, if added, are additive.

## Open Questions

- Whether latest-product should live under `/api/v1/mvp/qhh/latest-product` or be expressed as a more generic `/api/v1/display-products/latest` query with `basin_id=qhh`. The MVP may use the QHH-specific path if it keeps launch risk lower.
- Whether `/ops` should be a route alias over `/monitoring` or a separate simplified page. Either is acceptable if the visible MVP workflow and tests target `/ops`.
- Whether QHH smoke should run against local PostgreSQL only or a target Slurm-accessible database for the launch rehearsal. The release checklist must record which mode was used.

## Issue #204 Fixture

Fixture level: expanded
Project profile: other / SHUD-NWM backend store and time-series query
Repair intensity: high

Change surface:
- `packages/common/forecast_store.py` station-series query helper and supporting dataclasses/helpers.
- Backend tests for store/query behavior and QHH forcing readiness evidence.
- Optional additive database index migration only if the implementation proves the current primary key/query shape is insufficient.

Must preserve:
- `forecast_series(...)`, `list_met_stations(...)`, existing run/model/basin queries, and existing API routes keep their current response shapes.
- `workers/forcing_producer` remains the writer of `met.forcing_station_timeseries`; this issue must not redesign or duplicate forcing writes.
- Existing `met.forcing_station_timeseries` composite identity `(forcing_version_id, station_id, variable, valid_time)` remains the source of truth for samples.

Must add/change:
- Store-level station series query supports explicit `forcing_version_id` and `model_id + source_id + cycle_time` forcing-version resolution.
- Query response groups samples by requested variable and preserves `unit`, `native_resolution`, `quality_flag`, `source_id`, `cycle_time`, `valid_time`, and truncation metadata.
- Readiness checks/tests verify selected QHH forcing versions, expected/actual station counts near 386 where fixture data exists, six-variable coverage, units, quality flags, missing-data reasons, and index/query-plan considerations.

Risk packs considered:
- Public API / CLI / script entry: not selected - #204 adds store/query helpers only; HTTP route is #205.
- Config / project setup: not selected - no new operator configuration.
- File IO / path safety / overwrite: not selected - database reads/tests only.
- Schema / columns / units / field names: selected - station series depends on exact `met.forcing_station_timeseries` and `met.forcing_version` columns, units, variable names, and timestamps.
- Geospatial / CRS / shapefile sidecars: not selected - no geometry parsing or CRS changes.
- Time series / forcing / temporal boundaries: selected - valid-time filtering, cycle-time resolution, limit/truncation, UTC normalization, and IFS/GFS forcing-version identity are central.
- Numerical stability / conservation / NaN: selected - values must be returned as stored and missing/non-finite handling must not fabricate samples.
- Solver runtime / performance / threading: not selected - no SHUD runtime changes.
- Resource limits / large input / discovery: selected - bounded limits and readiness aggregation must avoid unbounded station/time-series scans.
- Legacy compatibility / examples: selected - existing forecast/met station consumers and seed/demo tests must keep working.
- Error handling / rollback / partial outputs: selected - missing station, missing forcing version, ambiguous resolution, invalid filters, and empty valid ranges need stable errors or explicit empty results.
- Release / packaging / dependency compatibility: not selected - no package/dependency changes expected.
- Documentation / migration notes: selected - tests/evidence must record that forcing writes are existing and not rebuilt.

Required evidence:
- Store tests: explicit forcing version, model/source/cycle resolution, redundant-filter conflict, finalized-checksum gate, station membership, forcing-window filtering, variable filtering, time filtering, limit/truncated, missing station, missing forcing version, invalid variable/limit/time range, unit/native_resolution/quality_flag preservation.
- Readiness tests/evidence: selected QHH-like forcing version reports declared/effective station count, six-variable coverage, missing-data reasons, missing unit and quality flags, forcing-window filtering, and query/index outcome without requiring live QHH data in fast CI.
- Regression command: `uv run pytest -q tests/test_forecast_api.py tests/test_migrations.py` plus any new focused tests.
- Validation command: `openspec validate m21-qhh-hydro-met-ops-mvp --strict --no-interactive`.

Invariant Matrix

Governing invariant: one selected finalized forcing version identity must bind every returned station-series sample, metadata field, truncation decision, and readiness count from one stable database snapshot without mixing samples from another model, source, cycle, station, variable, time window, or concurrent same-ID producer rewrite/pending state.
Source-of-truth identity/contract: `met.forcing_version.forcing_version_id` plus `met.forcing_station_timeseries(forcing_version_id, station_id, variable, valid_time)`.
Surfaces:
- Producers: existing `workers/forcing_producer/store.py` writes `met.forcing_station_timeseries`; unchanged except tests may assert compatibility.
- Validators/preflight: store parameter validation for station id, forcing version resolution, variable names, UTC time range, and limit.
- Storage/cache/query: SQL in `packages/common/forecast_store.py`; optional additive index migration if proven required.
- Public routes/entrypoints: none in #204; #205 will expose the HTTP route.
- Frontend/downstream consumers: unchanged in #204; future #205/#208 consume the store contract.
- Failure paths/rollback/stale state: missing station, missing forcing version, not-finalized forcing version, redundant filter conflict, station absent from selected forcing version, ambiguous model/source/cycle, empty filtered range, over-limit results, missing unit/quality flag evidence.
- Evidence/audit/readiness: focused tests or deterministic readiness helper output; no final production readiness claim.
Regression rows:
- explicit valid `forcing_version_id + station_id + variables + time range` -> grouped series from only that forcing version with units, quality flags, native resolution, returned range, and truncation metadata.
- valid `model_id + source_id + cycle_time + station_id` -> same resolved forcing version as explicit query; no ad hoc ID guessing in callers.
- explicit `forcing_version_id` with conflicting supplied `model_id`, `source_id`, or `cycle_time` -> stable conflict, not silent precedence.
- `model_id + source_id + cycle_time` resolves to multiple forcing versions or inconsistent identities -> bounded stable ambiguous/unavailable failure with details, not silent arbitrary selection or unbounded candidate materialization.
- missing station, missing forcing version, not-finalized forcing version, or station absent from selected forcing version -> stable not-found/unavailable result at store boundary, not a misleading empty success.
- out-of-window samples for the selected forcing version -> excluded from station-series points, truncation, and readiness counts.
- over-limit query -> deterministic truncation per variable without unbounded row materialization.
- QHH-like readiness fixture -> effective expected station count falls back to declared `forcing_version.station_count`, six-variable coverage, missing unit, missing quality flag, missing-data reasons, and query/index outcome are reported without re-running forcing producer.
- concurrent same-ID forcing producer rewrite around a station-series/readiness read -> `PsycopgForecastStore` opens a read-only `REPEATABLE READ` transaction before selecting `met.forcing_version`, so finalized metadata and dependent `met.forcing_station_timeseries` rows come from one stable snapshot instead of mixing old finalized identity with newly replaced or pending rows.
- existing `forecast_series` and `list_met_stations` tests -> unchanged behavior.

Boundary-surface checklist:
- Shared helper roots: datetime parsing/UTC normalization, token normalization, SQL fetch helpers.
- Public entrypoints: none for #204.
- Read surfaces: `met.forcing_version`, `met.forcing_station_timeseries`, `met.met_station`, and model/station association tables used for resolution.
- Write/delete/overwrite surfaces: none, except optional additive migration.
- Producer/consumer evidence boundaries: forcing producer remains producer; readiness helper must label deterministic vs live evidence.
- Stale-state/idempotency boundaries: repeated query for same identity returns same rows and does not mutate result tables.
- Unchanged downstream consumers: forecast API tests, met station list tests, forcing producer tests where relevant.

Non-goals:
- Implementing the FastAPI station-series route, OpenAPI schemas, or frontend generated types; those belong to #205.
- Building `/hydro-met`, latest-product, `/ops`, or controlled retry UI.
- Running live QHH/Slurm/GFS/IFS smoke in fast CI.

## Issue #205 Fixture

Fixture level: expanded
Project profile: other / SHUD-NWM FastAPI, OpenAPI, and generated frontend type contract
Repair intensity: high

Change surface:
- `apps/api/routes/data_sources.py` public `GET /api/v1/met/stations/{station_id}/series` entrypoint.
- `openapi/nhms.v1.yaml` station-series parameters, response schema, and error documentation.
- `apps/frontend/src/api/types.ts` generated contract freshness.
- `tests/test_forecast_api.py`, `tests/test_api_contract.py`, and `tests/test_openapi_drift.py` route/contract/drift coverage.

Must preserve:
- #204 store invariants remain the source of truth; the route must delegate to `PsycopgForecastStore.station_series(...)` rather than re-querying `met.forcing_station_timeseries` or reshaping identity rules.
- Existing `/api/v1/met/stations`, data-source cycle routes, forecast-series routes, and generated frontend client paths keep their current behavior.
- Success responses keep the existing API success envelope shape from `_ok(request, data)`.

Must add/change:
- Implement the station-series HTTP route with validated query parameters for `forcing_version_id`, `model_id`, `source_id`, `cycle_time`, `variables`, `from`, `to`, and `limit`.
- Map store `ForecastStoreError` failures to the existing typed API error envelope without swallowing not-found/unavailable/conflict/validation details.
- Update OpenAPI so station-series documents all supported query parameters and a response schema matching the #204 store payload, including station metadata, provenance, `unit`, `native_resolution`, point-level `quality_flag`, truncation, and range metadata.
- Regenerate frontend API types from the updated OpenAPI contract and remove the route from the OpenAPI drift deferred allowlist.

Risk packs considered:
- Public API / CLI / script entry: selected - this issue promotes a documented station-series route to a real FastAPI public endpoint.
- Config / project setup: not selected - no new runtime configuration or deployment flags.
- File IO / path safety / overwrite: not selected - route performs bounded database reads only.
- Schema / columns / units / field names: selected - OpenAPI/frontend types must match the station-series payload fields, variable names, units, quality flags, and metadata.
- Geospatial / CRS / shapefile sidecars: not selected - station geometry is serialized from existing station metadata only; no CRS parsing or map tile change.
- Time series / forcing / temporal boundaries: selected - route must pass `from`, `to`, `cycle_time`, `variables`, `limit`, and forcing identity filters exactly to the store contract.
- Numerical stability / conservation / NaN: selected - route must not coerce missing data into synthetic numeric samples or hide store value conversion failures.
- Solver runtime / performance / threading: not selected - no SHUD runtime behavior.
- Resource limits / large input / discovery: selected - `limit` and variable parsing must stay bounded at the HTTP boundary and OpenAPI must not imply unbounded downloads.
- Legacy compatibility / examples: selected - existing API consumers and generated type paths must remain compatible.
- Error handling / rollback / partial outputs: selected - missing station/version, invalid parameters, ambiguous/conflicting forcing identity, not-finalized versions, and station-not-in-version errors must surface as typed API errors.
- Release / packaging / dependency compatibility: selected - generated frontend types must be refreshed with the repository's existing toolchain without adding dependencies.
- Documentation / migration notes: selected - OpenAPI drift allowlist must be tightened so the implemented route cannot silently drift again.

Required evidence:
- HTTP tests: valid explicit `forcing_version_id`, valid `model_id + source_id + cycle_time`, comma-separated and repeated variable filters if supported by FastAPI parsing, time filters, `limit`/truncation metadata, default MVP variables, missing station, missing forcing version, station not in forcing version, conflicting identity filters, invalid variable, invalid time range, invalid limit, and no synthetic samples for empty valid ranges.
- API contract tests: success envelope shape, station metadata/provenance fields, point-level `quality_flag`, variable-level `unit`/`native_resolution`, and stable API error envelope for at least one store error.
- OpenAPI drift tests: `GET /api/v1/met/stations/{station_id}/series` is removed from `DEFERRED_ROUTES` and static OpenAPI matches implemented FastAPI route parameters.
- Frontend type freshness: generated `apps/frontend/src/api/types.ts` includes the updated station-series operation, query parameters, and response schemas.
- Regression commands: `uv run pytest -q tests/test_forecast_api.py tests/test_api_contract.py tests/test_openapi_drift.py`, `uv run ruff check apps/api/routes/data_sources.py tests/test_forecast_api.py tests/test_api_contract.py tests/test_openapi_drift.py`, `openspec validate m21-qhh-hydro-met-ops-mvp --strict --no-interactive`, frontend type-generation/check command used by the repo, and `git diff --check`.

Invariant Matrix

Governing invariant: the public station-series route, static OpenAPI document, generated frontend types, and route tests must expose exactly the #204 store contract for one selected finalized forcing version without inventing samples, weakening identity/error semantics, or leaving route-documentation drift.
Source-of-truth identity/contract: `PsycopgForecastStore.station_series(...)` response contract plus the documented OpenAPI operation `getMetStationSeries`.
Surfaces:
- Producers: none - #205 does not write forcing data or change `workers/forcing_producer`.
- Validators/preflight: FastAPI query parsing in `apps/api/routes/data_sources.py` plus store validation for forcing identity, variables, time range, and limit.
- Storage/cache/query: #204 `packages/common/forecast_store.py` store helpers; unchanged except compatibility fixes if tests reveal a contract gap.
- Public routes/entrypoints: `GET /api/v1/met/stations/{station_id}/series`.
- Frontend/downstream consumers: generated `apps/frontend/src/api/types.ts`; UI consumption remains #208.
- Failure paths/rollback/stale state: API error envelope for store validation/not-found/conflict/unavailable errors; OpenAPI drift allowlist removal; no partial writes.
- Evidence/audit/readiness: route tests, API contract tests, OpenAPI drift tests, generated type diff, and validation commands.
Regression rows:
- explicit valid `forcing_version_id + station_id` HTTP request -> success envelope with the same store payload, grouped variables, metadata, and point quality flags.
- valid `model_id + source_id + cycle_time + station_id` HTTP request -> route delegates tuple resolution to store and returns resolved `forcing_version_id`.
- request without variable filter -> defaults to MVP variables from the store contract without synthetic series beyond empty groups for requested variables.
- comma-separated or repeated variable filter accepted by the route -> normalized variables passed to store once per requested variable; invalid variables fail with typed validation error.
- `from`, `to`, and `limit` query parameters -> passed to store and reflected in truncation/range metadata; invalid ranges/limits fail before unbounded output.
- missing station/version, not-finalized version, ambiguous tuple, conflicting redundant filters, or station absent from forcing version -> typed API error with store code/details preserved.
- static OpenAPI and generated frontend types -> include the implemented parameters and response fields; route no longer appears in deferred drift allowlist.
- existing `/api/v1/met/stations` and forecast-series tests -> unchanged behavior.

Boundary-surface checklist:
- Shared helper roots: `_ok`, `_api_error`, FastAPI `Query` parsing, OpenAPI parameter/schema definitions, frontend type generation.
- Public entrypoints: station-series route only.
- Read surfaces: route delegates to store; no direct SQL in route.
- Write/delete/overwrite surfaces: generated frontend type file only; no runtime writes.
- Producer/consumer evidence boundaries: no forcing producer changes; frontend types are contract consumers, not proof of UI readiness.
- Stale-state/idempotency boundaries: repeated HTTP reads for the same identity are read-only and preserve #204 snapshot consistency.
- Unchanged downstream consumers: existing data-source API contract tests, forecast API tests, frontend API type consumers.

Non-goals:
- Building `/hydro-met` station chart UI; #208 owns UI consumption.
- Adding latest-product selection; #206 owns product discovery.
- Adding new forcing producer writes, station readiness algorithms, or live QHH smoke.

## Issue #206 Fixture

Fixture level: expanded
Project profile: other / SHUD-NWM FastAPI, forecast store aggregation, OpenAPI, and generated frontend type contract
Repair intensity: high

Change surface:
- Backend read helper for selecting the latest usable QHH display product from `hydro.hydro_run`, `met.forcing_version`, `core.model_instance`, `core.basin_version`, `core.river_network_version`, `met.forcing_station_timeseries`, and `hydro.river_timeseries`.
- Public `GET /api/v1/mvp/qhh/latest-product` route or equivalent stable API contract.
- `openapi/nhms.v1.yaml`, generated `apps/frontend/src/api/types.ts`, and backend/API/OpenAPI drift tests.

Must preserve:
- Existing `/api/v1/runs`, station-series, forecast-series, data-source cycle, met-station, MVT/layer, and generated frontend paths keep their current response shapes.
- #204/#205 station-series store and route contracts remain the source of truth for station forcing samples; #206 may use readiness/summary checks but must not duplicate forcing series payload logic or change producer writes.
- The latest-product route is read-only and must not mutate run/model/forcing status or publish display products.

Must add/change:
- Select the newest usable QHH product for a requested `source=GFS|IFS` using formal persisted run/model/forcing/time-series identity, not run-id naming conventions or qhh diagnostic JSON files.
- Return `basin_id`, `model_id`, `basin_version_id`, `river_network_version_id`, `source_id`, `cycle_time`, `run_id`, `forcing_version_id`, `station_count`, `expected_station_count`, `segment_count`, `expected_segment_count`, `status`, valid-time/horizon metadata, and availability reasons/quality metadata.
- Reject failed, cancelled, pending, incomplete, identity-missing, not-finalized-forcing, station-forcing-incomplete, and missing-`q_down` products as ready; unavailable responses must be explicit and typed.
- Represent IFS shorter-horizon metadata from available forcing/hydro valid-time end rather than padding to seven days.

Risk packs considered:
- Public API / CLI / script entry: selected - #206 promotes a new MVP bootstrap API.
- Config / project setup: not selected - no new runtime configuration or deployment flags expected.
- File IO / path safety / overwrite: not selected - the route reads persisted database state only and must not consume qhh diagnostic files.
- Schema / columns / units / field names: selected - response fields, counts, statuses, horizon metadata, and OpenAPI/generated types are public contract.
- Geospatial / CRS / shapefile sidecars: not selected - the API returns identifiers/counts only, not geometry.
- Time series / forcing / temporal boundaries: selected - selection depends on cycle time, forcing window, hydro valid-time range, station variable coverage, and IFS horizon disclosure.
- Numerical stability / conservation / NaN: selected - counts and ranges must come from persisted rows; missing values must not be converted into ready products.
- Solver runtime / performance / threading: not selected - no SHUD execution behavior.
- Resource limits / large input / discovery: selected - latest selection must be bounded to candidate rows and use existing/latest-ready indexes where possible.
- Legacy compatibility / examples: selected - existing run/station/forecast APIs and generated consumers must remain compatible.
- Error handling / rollback / partial outputs: selected - no usable product, incomplete candidates, missing identities, and unsupported source must surface stable unavailable/validation errors.
- Release / packaging / dependency compatibility: selected - OpenAPI/frontend type generation must remain reproducible with existing tooling.
- Documentation / migration notes: selected - API must not claim nationwide, stage, UI readiness, or final production readiness.

Required evidence:
- Store/helper tests: latest GFS selection, latest IFS selection, source normalization, newest-ready ordering, failed/cancelled/pending/incomplete rejection, missing `forcing_version_id`, missing `river_network_version_id`, not-finalized forcing, missing station variables, missing `q_down`, station/segment count metadata, valid-time/horizon metadata, and IFS shorter-horizon disclosure.
- Resource-bound tests/evidence: candidate discovery must be bounded by source/status/basin filters and either an explicit candidate limit or a single indexed/aggregated readiness query; tests must assert the SQL shape does not perform unbounded station-series or river-timeseries materialization before selecting candidates, and must record whether `hydro_run_latest_ready_run_idx`, `river_timeseries_mvt_selected_identity_valid_time_discovery_idx`, and the `met.forcing_station_timeseries` primary key support the lookup.
- API tests: success envelope for `source=GFS|IFS`, unsupported source validation, no usable product unavailable response with reasons, and no manual IDs required in response.
- Contract tests: static OpenAPI and generated frontend types include latest-product route, response schema, availability/unavailable reason schema, and horizon/count fields.
- Drift tests: runtime/static OpenAPI parameters/responses/components for latest-product match or are intentionally patched like station-series.
- Compatibility tests/evidence: existing `/api/v1/runs`, data-source cycle, met-station, station-series, forecast-series, MVT/layer, and generated-type tests remain green.
- Regression commands: `uv run pytest -q tests/test_forecast_api.py tests/test_api_contract.py tests/test_openapi_drift.py`, `uv run ruff check apps/api/routes apps/api/main.py packages/common/forecast_store.py tests/test_forecast_api.py tests/test_api_contract.py tests/test_openapi_drift.py`, `cd apps/frontend && corepack pnpm check:api-types`, `openspec validate m21-qhh-hydro-met-ops-mvp --strict --no-interactive`, and `git diff --check`.

Invariant Matrix

Governing invariant: a latest-product response marked ready must bind one persisted QHH hydro run, finalized forcing version, model river network, basin version, station forcing coverage, river `q_down` coverage, and horizon/count metadata from a single consistent read without selecting failed, incomplete, identity-mismatched, stale, or diagnostic-file-only products.
Source-of-truth identity/contract: `hydro.hydro_run.run_id` plus `hydro.hydro_run.forcing_version_id`, `core.model_instance(model_id, basin_version_id, river_network_version_id)`, `met.forcing_version(forcing_version_id, model_id, source_id, cycle_time)`, `met.forcing_station_timeseries(forcing_version_id, station_id, variable, valid_time)`, and `hydro.river_timeseries(run_id, river_network_version_id, river_segment_id, variable, valid_time)`.
Surfaces:
- Producers: existing scheduler/SHUD/parse/forcing producers write runs, forcing versions, station series, and river time series; unchanged in #206.
- Validators/preflight: source query validation, candidate status filtering, finalized forcing gate, identity consistency checks, station variable coverage checks, `q_down` coverage checks, horizon calculations.
- Storage/cache/query: `packages/common/forecast_store.py` latest-product helper and SQL over run/model/forcing/station/hydro rows.
- Public routes/entrypoints: `GET /api/v1/mvp/qhh/latest-product` or equivalent stable route.
- Frontend/downstream consumers: generated `apps/frontend/src/api/types.ts`; `/hydro-met` consumption remains #207/#208/#209.
- Failure paths/rollback/stale state: unsupported source, no usable product, identity mismatch, incomplete counts, not-finalized forcing, missing station variables, missing `q_down`, shorter horizon; no writes or rollback.
- Evidence/audit/readiness: backend tests, API contract tests, OpenAPI drift tests, generated type diff, and validation commands; no live QHH smoke claim.
Regression rows:
- latest valid GFS QHH product with finalized forcing and `q_down` rows -> ready success payload with all bootstrap IDs, counts, status, and horizon metadata.
- latest valid IFS product with valid-time end shorter than 168h -> success payload exposes actual horizon/end time and a shorter-horizon reason/flag, without fabricated padding.
- newer failed/cancelled/pending/incomplete candidate before an older ready candidate -> selects older ready product or returns explicit unavailable if none are ready, never the incomplete candidate.
- candidate missing `forcing_version_id`, `river_network_version_id`, `basin_version_id`, `model_id`, or `cycle_time` -> rejected with unavailable reason, not a partial ready response.
- candidate with forcing checksum missing/pending or source/cycle/model mismatch between run and forcing version -> rejected with stable reason.
- candidate with fewer station variables/counts than required six MVP variables -> rejected or marked unavailable with station readiness reasons.
- candidate with no `q_down` river timeseries or zero displayable segments -> rejected or unavailable with segment/hydro reasons.
- many newer unusable candidates for one source -> discovery remains bounded by indexed source/status/cycle filters and candidate/readiness limits, then returns the newest usable product or an explicit unavailable reason without scanning arbitrary unrelated basins/sources.
- unsupported `source` query -> stable validation error.
- existing `/api/v1/runs`, data-source cycle, met-station, station-series, forecast-series, MVT/layer, and generated type tests -> unchanged behavior.

Boundary-surface checklist:
- Shared helper roots: `_ok`, `_api_error`, FastAPI query parsing, `_hydro_run_response`, station readiness helpers, candidate-query/readiness aggregation helpers, OpenAPI schema patch helpers, frontend type generation.
- Public entrypoints: latest-product route only.
- Read surfaces: formal DB tables above; no qhh diagnostic JSON or local file state.
- Write/delete/overwrite surfaces: generated frontend type file only; no runtime writes.
- Producer/consumer evidence boundaries: latest-product is bootstrap metadata, not live smoke or UI readiness proof.
- Stale-state/idempotency boundaries: repeated query for same persisted state returns the same selected product and reasons without mutating state.
- Unchanged downstream consumers: existing run list/get, station-series, forecast-series, data-source, MVT/layer, and frontend type consumers.

Non-goals:
- Building `/hydro-met`, station charts, river charts, `/ops`, retry controls, or browser smoke.
- Adding or changing forcing producer writes, SHUD runtime, parse behavior, scheduler state, or live QHH/IFS smoke.
- Claiming nationwide readiness, water level `stage`, CLDAS, ERA5 near-real-time, final production readiness, or real flood-frequency readiness.

## Issue #207 Fixture

Fixture level: expanded
Project profile: other / SHUD-NWM React frontend route, navigation, API bootstrap adapter, and frontend tests
Repair intensity: medium

Change surface:
- `apps/frontend` route registration, app navigation, `/hydro-met` page shell, query-state handling, and bootstrap data adapter.
- Frontend API client usage for latest-product, station inventory, and river segment list.
- Frontend tests for route/nav, latest-product bootstrap, no-manual-ID behavior, loading/unavailable states, and existing route compatibility.

Must preserve:
- Existing `/meteorology`, `/forecast`, `/segments/:segmentId`, `/monitoring`, `/flood-alerts`, basin, overview, and system routes remain reachable and keep their current deep-link/query behavior.
- Existing meteorology fixture UI remains available at `/meteorology`; #207 must not silently repurpose it into a fake live chart page.
- Existing generated API types and response-envelope helpers remain the source of truth; #207 must not hand-roll backend response shapes when generated contracts exist.
- #208 owns station forcing charts and #209 owns river `q_down` charts; #207 may show bootstrap inventory/list summaries and explicit placeholders but must not draw fake charts.

Must add/change:
- Add `/hydro-met` route or route alias and a visible MVP navigation entry for the hydrology/meteorology workflow.
- Implement a bootstrap adapter that requests the latest QHH display product for `source=GFS|IFS`, then uses the returned `model_id`, `basin_version_id`, `river_network_version_id`, and `forcing_version_id` to request station inventory and QHH river segment candidates without user-entered IDs.
- Preserve selected source/cycle query state where supported and reflect sanitized/corrected query state in the URL.
- Render explicit loading, unavailable, incomplete-product, and partial-bootstrap states without substituting fake station/river data.
- Use river discharge / river-segment flow wording for `q_down` scope and avoid water-level/stage language.

Risk packs considered:
- Public API / CLI / script entry: selected - #207 adds a public frontend route and visible navigation entry.
- Config / project setup: not selected - no new build tool, env var, or deployment flag expected.
- File IO / path safety / overwrite: not selected - frontend route performs network reads only and writes no files at runtime.
- Schema / columns / units / field names: selected - the bootstrap adapter consumes generated API response fields for latest product, stations, river segments, source/cycle, and version IDs.
- Geospatial / CRS / shapefile sidecars: selected - the page may render or list station/river geographic candidates, but it must treat backend geometry/coordinates as display data and not reinterpret CRS.
- Time series / forcing / temporal boundaries: selected - source/cycle/latest-product horizon metadata must be preserved for downstream chart issues and shorter IFS horizons must be visible as metadata, not padded.
- Numerical stability / conservation / NaN: not selected - #207 does not render numeric station/river series values.
- Solver runtime / performance / threading: not selected - no SHUD runtime behavior.
- Resource limits / large input / discovery: selected - station and river candidate loading must be bounded/paginated or explicitly capped, and unavailable/partial states must not trigger unbounded client fetch loops.
- Legacy compatibility / examples: selected - existing frontend routes, nav tests, meteorology page, forecast page, monitoring RBAC, and generated type consumers must remain compatible.
- Error handling / rollback / partial outputs: selected - latest-product unavailable, station inventory failure, river segment failure, and partial bootstrap must render typed states without fake data.
- Release / packaging / dependency compatibility: selected - frontend tests/build must pass without adding dependencies unless justified.
- Documentation / migration notes: selected - UI labels and test evidence must keep #207 within route/bootstrap scope and not claim station/river chart completion.

Required evidence:
- Route/nav tests: `/hydro-met` is reachable, visible in navigation, and existing `/meteorology`, `/forecast`, `/segments/:segmentId`, and `/monitoring` routes remain available.
- Bootstrap adapter tests: selected source defaults to `GFS` or preserved `source=IFS`; latest-product is requested; returned IDs are used for station inventory and river segment list calls; no user-entered IDs are required.
- Query-state tests: supported `source` and `cycle` query values are preserved or normalized; unsupported source/cycle values render validation/unavailable state without backend calls that mix products.
- Loading/incomplete tests: initial bootstrap renders a loading state, and latest-product unavailable or identity-incomplete responses render explicit incomplete-product state without unsafe station/river follow-up calls.
- Unavailable/partial tests: no usable latest product, station inventory failure, river segment failure, empty station list, and empty river list render explicit states and do not draw charts or fixture curves.
- Scope/wording tests: MVP-facing labels use hydrology/meteorology, station inventory, river segment flow/discharge, and bootstrap wording; no water-level/stage wording for `q_down`.
- Compatibility evidence: existing frontend route/store tests remain green; frontend type check and build/test commands pass.
- Regression commands: `cd apps/frontend && corepack pnpm test`, `cd apps/frontend && corepack pnpm build`, `cd apps/frontend && corepack pnpm check:api-types`, `openspec validate m21-qhh-hydro-met-ops-mvp --strict --no-interactive`, and `git diff --check`.

Invariant Matrix

Governing invariant: `/hydro-met` bootstrap must derive every displayed QHH source/cycle/version/station/river candidate identity from the latest-product API response and subsequent API-backed inventory/list calls, without requiring manual IDs, mixing products across source/cycle, breaking existing routes, or rendering fake chart data.
Source-of-truth identity/contract: generated API types for `GET /api/v1/mvp/qhh/latest-product`, `GET /api/v1/met/stations`, and `GET /api/v1/basin-versions/{basin_version_id}/river-segments`, plus URL query state for `source` and optional `cycle`.
Surfaces:
- Producers: #206 latest-product API, #205 station inventory/series APIs, existing river segment API; unchanged in #207.
- Validators/preflight: frontend source/cycle query parser, response-envelope guards, bootstrap adapter identity checks, and empty/unavailable state builders.
- Storage/cache/query: frontend request/cache state only; no persistent browser storage required.
- Public routes/entrypoints: `/hydro-met` route/nav entry; existing routes remain unchanged.
- Frontend/downstream consumers: #208 station chart issue and #209 river chart issue consume the bootstrap adapter/page shell; existing meteorology/forecast/segment/monitoring routes remain sibling consumers.
- Failure paths/rollback/stale state: latest-product unavailable, stale URL source/cycle, station request failure, river request failure, partial results, empty results, component unmount/reload; no rollback.
- Evidence/audit/readiness: frontend unit/component tests, route tests, optional browser smoke in #214; #207 does not claim live smoke.
Regression rows:
- `/hydro-met` with no query -> requests latest QHH product for default source, uses returned IDs for inventory/list calls, and renders source/cycle/version summaries plus station/river candidate counts.
- `/hydro-met?source=IFS` -> requests IFS latest product and preserves source query state without falling back to GFS unless the latest-product API says unavailable.
- `/hydro-met` latest-product unavailable -> renders explicit unavailable state with reason and no station/river fake data.
- `/hydro-met` latest-product response is incomplete or lacks bootstrap-safe IDs -> renders explicit incomplete-product state, skips unsafe station/river calls, and does not synthesize data.
- Latest-product success + station inventory failure -> renders product summary and station unavailable state while still showing river list result if available.
- Latest-product success + river list failure -> renders product summary and river unavailable state while still showing station inventory result if available.
- Empty station or river result -> renders empty state and does not synthesize rows.
- Unsupported source or malformed cycle query -> corrected URL or validation state, no mixed-source/cycle bootstrap.
- Existing `/meteorology`, `/forecast`, `/segments/:segmentId`, `/monitoring` deep links -> remain routable and tests still pass.
- Navigation visible links -> include hydro-met MVP entry while respecting existing RBAC for monitoring/system links.

Non-goals:
- Rendering six-variable station forcing charts; #208 owns station chart UI and station series calls on selection.
- Rendering river `q_down` forecast-series curves or IFS line padding labels; #209 owns river chart UI.
- Adding ops route, log modal, retry controls, or RBAC changes.
- Adding backend endpoints, changing latest-product semantics, or modifying station/river producer writes.
- Claiming live QHH/IFS smoke or browser smoke completion.

Issue ownership note:
- `hydro-met-mvp-ui` is the full M21 capability spec. For #207 acceptance, only the Hydro-met MVP entry, latest-product bootstrap, route/nav compatibility, query-state, loading/unavailable/incomplete-product states, and no-fake-data shell apply. Station chart scenarios are #208, river chart scenarios are #209, and browser smoke is #214.

## Issue #208 Fixture

Fixture level: expanded
Project profile: other / SHUD-NWM React frontend station selection, station-series API consumption, chart rendering, and tests
Repair intensity: medium

Change surface:
- `apps/frontend` `/hydro-met` station list/marker/detail selection behavior, station-series data adapter, station forcing chart components, and route/component tests.
- Frontend API client usage for `GET /api/v1/met/stations/{station_id}/series`.
- Reuse of #207 latest-product bootstrap identity, station inventory, query state, no-fake-data shell, coordinate compatibility, and message redaction helpers.

Must preserve:
- #207 `/hydro-met` route/nav/bootstrap behavior, source/cycle query normalization, latest-product unavailable/incomplete states, partial station/river candidate loading, runtime station coordinate fallback, and redacted UI error messages.
- Existing `/meteorology`, `/forecast`, `/segments/:segmentId`, `/monitoring`, basin, overview, flood-alert, and system routes remain reachable and keep current deep-link/query behavior.
- Station series response fields come from generated API types and response-envelope helpers; #208 must not hand-roll backend payloads or change OpenAPI/generated types.
- #209 owns river `q_down` forecast-series charts and IFS river-horizon chart labeling; #208 may keep the river area as candidate list/placeholder and must not add forecast-series calls.
- #211/#212 own `/ops`, log, retry, and RBAC controls; #208 must not change those surfaces.

Must add/change:
- Add visible station markers derived from real station inventory coordinates, plus station list selection and station search/filtering; stations without usable coordinates must remain searchable/selectable from the list without rendering fake marker positions.
- On selected station change, call `GET /api/v1/met/stations/{station_id}/series` with `forcing_version_id` from latest-product and variables `PRCP`, `TEMP`, `RH`, `wind`, `Rn`, and `Press`; source/cycle/model metadata should be displayed from the response/product, not guessed by convention.
- Render six variable chart panels from real station-series points with unit, valid-time range, source, cycle, forcing version, point `quality_flag`, variable-level truncation metadata, and explicit empty/unavailable/error states.
- Preserve no-synthetic-data behavior: missing variables, empty points, API errors, invalid station selection, or station/forcing mismatch must render explicit states and must not silently switch station, source, cycle, forcing version, or draw fake curves.
- Keep station discovery and station-series samples bounded by existing API limits and avoid unbounded client fetch loops.

Risk packs considered:
- Public API / CLI / script entry: selected - #208 expands the visible `/hydro-met` route with station selection and chart behavior.
- Config / project setup: not selected - no new build tool, environment variable, or deployment flag expected.
- File IO / path safety / overwrite: not selected - frontend route performs network reads only and writes no files at runtime.
- Schema / columns / units / field names: selected - station charts consume generated station-series fields, units, variables, provenance IDs, `quality_flag`, and truncation metadata.
- Geospatial / CRS / shapefile sidecars: selected - station markers/list positions use backend-provided station coordinates as display data only; no CRS reinterpretation or tile/schema change.
- Time series / forcing / temporal boundaries: selected - selected station/source/cycle/forcing version, valid-time range, requested variables, truncation, and point ordering are central.
- Numerical stability / conservation / NaN: selected - chart data must not fabricate values or hide non-finite/missing samples; unsupported points must render unavailable/empty states.
- Solver runtime / performance / threading: not selected - no SHUD runtime behavior.
- Resource limits / large input / discovery: selected - station inventory display and station-series point rendering must stay capped and not request unbounded points.
- Legacy compatibility / examples: selected - existing route tests, meteorology fixture UI, forecast pages, and generated API type consumers must keep working.
- Error handling / rollback / partial outputs: selected - station-series API failures, missing station/forcing version, empty variables, partial variables, truncation, and selection changes must render stable states without stale charts.
- Release / packaging / dependency compatibility: selected - frontend tests/build must pass without adding dependencies unless strongly justified by existing chart stack reuse.
- Documentation / migration notes: selected - UI/test evidence must keep #208 within station forcing chart scope and not claim river/ops/browser-smoke completion.

Required evidence:
- Data-adapter tests: selected station id + latest-product forcing version + six variables -> one bounded station-series request using the generated path and response envelope; no manual run/source/forcing IDs are entered by the user.
- Marker/list/search tests: real-inventory stations with usable coordinates render visible markers; stations without coordinates remain in the list but do not get fake marker positions; search filters by station id/name, shows an explicit no-results state, and never fabricates stations.
- Selection tests: station row/marker/search-result selection updates selected station, station metadata, and six-variable chart area; selecting another station clears stale series/loading/error state and requests the new station.
- Chart tests: real points render through the chart option/data model with units, source, cycle, forcing version, valid-time range, and one panel per MVP variable.
- Quality/truncation tests: non-ok `quality_flag`, empty variable points, missing unit, and `truncated=true` render explicit indicators near the affected variable chart.
- Error/unavailable tests: station-series HTTP error, missing/empty station series, selected station absent from inventory, and latest-product unavailable/incomplete states do not draw fake charts and do not silently switch station.
- Resource and compatibility tests: station-series `limit` stays bounded, no forecast-series calls are made, existing `/hydro-met` bootstrap tests and sibling route tests remain green.
- Regression commands: `cd apps/frontend && corepack pnpm test`, `cd apps/frontend && corepack pnpm build`, `cd apps/frontend && corepack pnpm check:api-types`, `openspec validate m21-qhh-hydro-met-ops-mvp --strict --no-interactive`, and `git diff --check`.

Invariant Matrix

Governing invariant: the selected `/hydro-met` station chart must bind one user-selected station, one latest-product forcing version, one source/cycle, and the six MVP forcing variables from the station-series API response without mixing stale station/source/cycle data or synthesizing chart points.
Source-of-truth identity/contract: generated API types for `GET /api/v1/met/stations/{station_id}/series`, #207 latest-product bootstrap result, selected `station_id`, `forcing_version_id`, `source_id`, `cycle_time`, and `series[].variable`.
Surfaces:
- Producers: #205 station-series API and #206 latest-product API; unchanged in #208.
- Validators/preflight: frontend selected-station state, bootstrap readiness, station id membership checks, station-series response-envelope guards, and no-data state builders.
- Storage/cache/query: in-memory React request state only; no persistent browser storage required.
- Public routes/entrypoints: `/hydro-met` station selection and chart area; existing route/nav entry remains unchanged.
- Frontend/downstream consumers: #209 river chart issue and #214 browser smoke consume the same route shell; existing meteorology and segment detail pages remain sibling consumers.
- Failure paths/rollback/stale state: station-series loading/error/empty/truncated states, selected station changes while a request is in flight, latest-product source/cycle changes, component unmount/reload, and redacted UI errors.
- Evidence/audit/readiness: frontend adapter/component tests; full MVP browser smoke and live QHH evidence remain #214.
Regression rows:
- default ready `/hydro-met` + first station selected -> station-series request uses that `station_id`, latest-product `forcing_version_id`, six MVP variables, bounded limit, and renders six chart panels from returned points.
- selected station changes before a previous station-series request resolves -> stale result is ignored and the UI reflects only the currently selected station.
- station search filters visible inventory -> list and markers reflect only matching real stations where marker coordinates exist; empty search renders an explicit no-results state and does not clear a valid selected station unless the user selects a different result.
- station-series response contains missing/empty variable, non-ok `quality_flag`, missing unit, or `truncated=true` -> affected variable renders explicit quality/truncation/unavailable state, not a fake line.
- station-series route returns not-found/unavailable/error or redacted unsafe message -> station chart area shows stable sanitized error, product/river candidate shell remains usable, and no stale chart remains visible.
- latest-product unavailable/incomplete/cycle mismatch -> station chart calls are skipped, matching #207 bootstrap safety behavior.
- station inventory has runtime `longitude`/`latitude`, GeoJSON `geom`, or missing coordinates -> selection/list/search rendering remains stable; only coordinate-backed stations render markers, and series calls still use station identity, not coordinate-derived IDs.
- `/hydro-met` river placeholder/list -> no forecast-series calls in #208.
- existing `/meteorology`, `/forecast`, `/segments/:segmentId`, and `/monitoring` tests -> unchanged behavior.

Non-goals:
- Rendering river `q_down` forecast-series curves, river segment selection chart updates, or IFS river shorter-horizon chart labels; #209 owns them.
- Adding `/ops`, log modal, retry controls, RBAC changes, controlled failure evidence, or scheduler persistence; #210-#213 own them.
- Modifying backend station-series route, latest-product semantics, OpenAPI, generated API types, database schema, forcing producer writes, or SHUD runtime.
- Full MVP browser smoke or live QHH/GFS/IFS evidence; #214 owns release smoke. #208 may add focused component or existing-test browser-like coverage, but must not claim final smoke readiness.

Issue ownership note:
- `hydro-met-mvp-ui` is the full M21 capability spec. For #208 acceptance, only station inventory markers/list, station selection, station-series API consumption, six-variable forcing chart rendering, QC/truncation/unavailable states, and no-fake-data behavior apply. River chart scenarios are #209, ops scenarios are #211/#212, controlled retry evidence is #213, and full browser/live smoke is #214.

## Issue #209 Fixture

Fixture level: expanded
Project profile: other / SHUD-NWM React frontend river segment selection, forecast-series API consumption, chart rendering, and tests
Repair intensity: medium

Change surface:
- `apps/frontend` `/hydro-met` river segment list/map-like selection behavior, forecast-series data adapter, `q_down` river discharge chart components, IFS shorter-horizon labeling, and route/component tests.
- Frontend API client usage for the existing generated river forecast-series route `GET /api/v1/basin-versions/{basin_version_id}/river-segments/{segment_id}/forecast-series`.
- Reuse of #207 latest-product bootstrap identity, river segment candidates, query state, no-fake-data shell, and #208 station chart surfaces.

Must preserve:
- #207 `/hydro-met` route/nav/bootstrap behavior, source/cycle query normalization, latest-product unavailable/incomplete states, partial station/river candidate loading, and bounded/redacted status messages.
- #208 station inventory markers/list/search/selection, station-series six-variable charts, station-series bounded render/error handling, and station chart tests.
- Existing `/meteorology`, `/forecast`, `/segments/:segmentId`, `/monitoring`, basin, overview, flood-alert, and system routes remain reachable and keep current deep-link/query behavior.
- `q_down` is the only MVP river chart variable. UI copy must use river discharge, river flow, or river-segment flow wording; it must not call `q_down` water level or stage.
- #211/#212 own `/ops`, log, retry, and RBAC controls; #209 must not change those surfaces.

Must add/change:
- Add river segment selection from the existing QHH river candidate list and any map/list affordance available in the `/hydro-met` shell. Selection must bind to the selected candidate `river_segment_id` from latest-product `basin_version_id` and `river_network_version_id`.
- On selected river segment change, call forecast-series with latest-product `basin_version_id`, `river_network_version_id`, selected `river_segment_id`, selected source/scenario, and variable `q_down`.
- Render a real `q_down` discharge chart with unit metadata, source/scenario, cycle/valid-time range where available, explicit loading/error/empty/unavailable states, and no synthetic points.
- Preserve GFS/IFS source selection through the existing `/hydro-met` source query state. IFS products whose available horizon is shorter than expected must display actual available horizon/end metadata and must not pad the line.
- Keep forecast-series requests and rendered points bounded; malformed/oversized responses must fail closed with explicit state instead of unbounded ECharts options or misleading lines.

Risk packs considered:
- Public API / CLI / script entry: selected - #209 expands visible `/hydro-met` behavior with river segment selection and charting.
- Config / project setup: not selected - no new build tool, environment variable, or deployment flag expected.
- File IO / path safety / overwrite: not selected - frontend route performs network reads only and writes no files at runtime.
- Schema / columns / units / field names: selected - river charts consume forecast-series fields, `q_down`, units, source/scenario labels, river network IDs, and valid times.
- Geospatial / CRS / shapefile sidecars: selected - river candidate features may include geometry for display/selection, but #209 must not reinterpret CRS or fabricate geometry.
- Time series / forcing / temporal boundaries: selected - selected source/cycle/product identity, forecast valid-time range, IFS shorter horizons, and no-padding behavior are central.
- Numerical stability / conservation / NaN: selected - chart data must not fabricate values or hide non-finite/missing samples.
- Solver runtime / performance / threading: not selected - no SHUD runtime behavior.
- Resource limits / large input / discovery: selected - river candidate display, forecast-series request parameters, response validation, and rendered chart data must stay bounded.
- Legacy compatibility / examples: selected - station charts, existing route tests, segment detail chart consumers, forecast pages, and generated API type consumers must keep working.
- Error handling / rollback / partial outputs: selected - forecast-series API failures, empty q_down series, malformed responses, selection changes, source changes, and stale responses must render stable states without stale/fake charts.
- Release / packaging / dependency compatibility: selected - frontend tests/build must pass without adding dependencies unless strongly justified by existing chart stack reuse.
- Documentation / migration notes: selected - UI/test evidence must keep #209 within river `q_down` scope and not claim ops/browser/live smoke completion.

Required evidence:
- Data-adapter tests: selected river segment id + latest-product basin/rivnet/source/cycle + variable `q_down` -> one bounded forecast-series request using the generated path and response envelope; no user-entered IDs are required.
- River list/selection tests: real QHH river candidates render as selectable rows/features; selecting a river updates selected river metadata and the chart request; empty river list renders explicit state and no fake segment.
- Chart tests: real `q_down` points render through the chart option/data model with unit metadata, source/scenario label, valid-time range, and selected river segment identity.
- IFS horizon tests: an IFS product or forecast response shorter than seven days displays actual available end/horizon metadata and does not pad synthetic values.
- Error/unavailable tests: forecast-series HTTP error, missing/empty `q_down`, malformed/oversized points, selected river absent from candidates, and latest-product unavailable/incomplete states do not draw fake charts and do not silently switch river/source/cycle.
- Wording tests: MVP-facing labels on `/hydro-met` use river discharge/flow terminology for `q_down` and do not label it as water level or stage.
- Resource and compatibility tests: forecast-series request variables are fixed to `q_down`, rendered points are bounded, station-series behavior from #208 remains green, and existing `/forecast`/`/segments/:segmentId` consumers remain compatible.
- Regression commands: `cd apps/frontend && corepack pnpm test`, `cd apps/frontend && corepack pnpm build`, `cd apps/frontend && corepack pnpm check:api-types`, `openspec validate m21-qhh-hydro-met-ops-mvp --strict --no-interactive`, and `git diff --check`.

Invariant Matrix

Governing invariant: the selected `/hydro-met` river chart must bind one user-selected river segment, one latest-product basin version, one river network version, one selected source/scenario/cycle, and the `q_down` forecast-series response without mixing stale segment/source/cycle data, padding IFS horizons, or synthesizing chart points.
Source-of-truth identity/contract: generated API types for forecast-series, #207 latest-product bootstrap result, selected `river_segment_id`, `basin_version_id`, `river_network_version_id`, selected source/scenario, `cycle_time`, and response variable `q_down`.
Surfaces:
- Producers: existing forecast-series API and #206 latest-product API; unchanged in #209.
- Validators/preflight: frontend selected-river state, bootstrap readiness, river id membership checks, forecast-series response-envelope guards, `q_down` variable checks, time/value bounds, and no-data state builders.
- Storage/cache/query: in-memory React request state only; no persistent browser storage required.
- Public routes/entrypoints: `/hydro-met` river candidate selection and river chart area; existing route/nav entry remains unchanged.
- Frontend/downstream consumers: #214 browser smoke consumes this route; existing forecast and segment detail pages remain sibling consumers.
- Failure paths/rollback/stale state: forecast-series loading/error/empty/truncated states, selected river changes while a request is in flight, source/cycle/product changes, component unmount/reload, shorter IFS horizon disclosure, and bounded UI errors.
- Evidence/audit/readiness: frontend adapter/component tests; full MVP browser smoke and live QHH evidence remain #214.
Regression rows:
- default ready `/hydro-met` + first river selected -> forecast-series request uses selected `river_segment_id`, latest-product `basin_version_id`, `river_network_version_id`, selected source/scenario, variable `q_down`, and renders a bounded real discharge chart.
- selected river changes before a previous forecast-series request resolves -> stale result is ignored and the UI reflects only the currently selected river.
- source changes to IFS -> latest-product/bootstrap reloads and forecast-series request uses IFS scenario/source identity without falling back to GFS unless the product is explicitly unavailable.
- IFS product/series valid-time end is shorter than expected -> chart shows shorter-horizon/end metadata and contains only returned points; no padded synthetic values.
- forecast-series response contains missing/empty `q_down`, non-finite values, malformed points, mismatched variable, or oversized point arrays -> explicit unavailable/error/capped state, not a fake line or unbounded ECharts payload.
- selected river segment no longer exists in current candidate list -> explicit unavailable state, no forecast-series request for absent river, and no stale chart.
- latest-product unavailable/incomplete/cycle mismatch -> river chart calls are skipped, matching #207 bootstrap safety behavior.
- station chart panels from #208 -> unchanged and still render/respond to station selection.
- existing `/forecast`, `/segments/:segmentId`, `/meteorology`, and `/monitoring` tests -> unchanged behavior.

Non-goals:
- Rendering station forcing charts or changing station-series behavior; #208 owns them and #209 must preserve them.
- Adding `/ops`, log modal, retry controls, RBAC changes, controlled failure evidence, or scheduler persistence; #210-#213 own them.
- Modifying backend forecast-series route, latest-product semantics, OpenAPI, generated API types, database schema, SHUD runtime, or parser output.
- Full MVP browser smoke or live QHH/GFS/IFS evidence; #214 owns release smoke. #209 may add focused component/browser-like tests but must not claim final smoke readiness.

Issue ownership note:
- `hydro-met-mvp-ui` is the full M21 capability spec. For #209 acceptance, only river candidate selection, forecast-series API consumption, real `q_down` discharge chart rendering, IFS shorter-horizon labeling, no-water-level/stage wording, and no-synthetic-data behavior apply. Station chart scenarios are #208, ops scenarios are #211/#212, controlled retry evidence is #213, and full browser/live smoke is #214.

## Issue #210 Fixture

Fixture level: expanded
Project profile: other / SHUD-NWM backend orchestrator persistence, monitoring API, log route, and retry state
Repair intensity: high

Change surface:
- `services/orchestrator/persistence.py`, `services/orchestrator/retry.py`, and formal scheduler/orchestrator code paths that create or expose `ops.pipeline_job` / `ops.pipeline_event` evidence.
- `apps/api/routes/pipeline.py` monitoring read APIs for status, stages, jobs, logs, retry, and OpenAPI/type drift only when the existing contract lacks required fields.
- Backend monitoring/orchestrator tests, deterministic QHH-like stage fixtures, and documentation only where needed to keep the formal scheduler vs qhh diagnostic boundary clear.

Must preserve:
- Existing monitoring endpoints, success/error envelope shapes, RBAC gates for retry/cancel, log path containment behavior, and safe bounded log reads.
- Existing M20 production scheduler semantics: canonical stage order is `download`, `convert`, `forcing`, `forecast`, `parse`, `frequency`, `publish`; UI may label `forecast` as SHUD execution, but persisted stage identity must remain canonical.
- QHH diagnostic scripts and `.nhms-runs/qhh-continuous` JSON remain reproduction/debug evidence only and must not become the production operations control source.
- Existing `/ops` or `/monitoring` frontend behavior is not changed by #210 except generated API types if the backend contract changes.

Must add/change:
- QHH formal scheduler/orchestrator execution writes or exposes source/cycle-scoped persisted stage/job evidence for all canonical MVP stages.
- Monitoring APIs return QHH stage/job records from formal persistence with run id, status, Slurm job id where available, submitted/started/finished timestamps, duration, retry count, and bounded/redacted `log_uri`.
- Jobs/status filtering by `source + cycle_time` must not mix sibling cycles or sources; missing cycles fail explicitly.
- Retry accepts the failed states required by the ops MVP contract, creates retry metadata/events, preserves authorization evidence, and reports submission failures without marking unproven work successful.

Risk packs considered:
- Public API / CLI / script entry: selected - #210 hardens public monitoring and retry API behavior used by `/ops`.
- Config / project setup: selected - log roots, Slurm retry settings, and formal scheduler identity affect backend behavior, but no new production config should be required.
- File IO / path safety / overwrite: selected - job log reads use server-side `log_uri` and must remain bounded, contained, no-follow, and redacted.
- Schema / columns / units / field names: selected - `PipelineJob`, `PipelineEvent`, OpenAPI, and frontend types must expose exact stage/status/job fields.
- Geospatial / CRS / shapefile sidecars: not selected - no geometry, CRS, map, or shapefile behavior.
- Time series / forcing / temporal boundaries: selected - source/cycle filtering and UTC cycle identity must be stable across status, stages, and jobs.
- Numerical stability / conservation / NaN: not selected - no numerical forecast values or solver math.
- Solver runtime / performance / threading: not selected - #210 observes/submits jobs but does not change SHUD runtime.
- Resource limits / large input / discovery: selected - jobs pagination, bounded logs, stage aggregation, and source/cycle queries must avoid unbounded scans or reads.
- Legacy compatibility / examples: selected - existing monitoring tests, runbook semantics, and frontend generated consumers must keep working.
- Error handling / rollback / partial outputs: selected - failed, partially failed, submission failed, cancelled, retry conflict, missing log, and missing cycle states need stable API behavior.
- Release / packaging / dependency compatibility: selected - OpenAPI/frontend types must be refreshed if the backend contract changes without adding dependencies.
- Documentation / migration notes: selected - issue evidence must keep formal scheduler and qhh diagnostic-script boundaries explicit.

Required evidence:
- Backend monitoring tests: source/cycle-scoped stage status over all seven canonical stages, no mixed-cycle jobs, job payload fields, failed/partial status aggregation, timestamps/duration, Slurm job id, retry count, bounded/redacted log URI, missing cycle, and pagination/filter behavior.
- Retry tests: manual retry for `failed`, `submission_failed`, `partially_failed`, and `permanently_failed` source states, retry metadata/event creation, active retry conflict, unauthorized retry no mutation, submission failure response, and no synthetic success.
- Log route tests: contained relative log, tail limit, traversal rejection, symlink swap rejection, missing log, redacted details, and no client-side filesystem assumptions.
- Contract tests: OpenAPI/frontend type drift check if any ops fields or status enum values change.
- Regression commands: `uv run pytest -q tests/test_monitoring_api.py tests/test_openapi_drift.py tests/test_api_contract.py`, targeted orchestrator tests changed by the implementation, `uv run ruff check apps/api/routes/pipeline.py services/orchestrator tests/test_monitoring_api.py`, `openspec validate m21-qhh-hydro-met-ops-mvp --strict --no-interactive`, and `git diff --check`.

Invariant Matrix

Governing invariant: every operations API response and retry/log action for a selected QHH source/cycle/run must be derived from formal persisted orchestrator state bound to that same identity, with bounded server-side log access and retry evidence, never from qhh diagnostic JSON or mixed sibling-cycle records.
Source-of-truth identity/contract: `ops.pipeline_job(job_id, run_id, cycle_id, stage, status, slurm_job_id, timestamps, retry_count, log_uri)`, `ops.pipeline_event`, `met.forecast_cycle(source, cycle_time, cycle_id)`, and retry policy evidence.
Surfaces:
- Producers: formal scheduler/orchestrator job creation and retry service writes to `PipelineStore`; qhh diagnostic scripts are explicitly out of scope as production producers.
- Validators/preflight: `source` normalization, `cycle_time` parsing, safe run id validation, RBAC policy decision checks, retry source-status checks, and log URI containment.
- Storage/cache/query: `PipelineStore`, `ops.pipeline_job`, `ops.pipeline_event`, `met.forecast_cycle`, and `hydro.hydro_run` status transitions touched by retry/cancel.
- Public routes/entrypoints: `GET /api/v1/pipeline/status`, `GET /api/v1/pipeline/stages`, `GET /api/v1/jobs`, `GET /api/v1/jobs/{job_id}/logs`, `POST /api/v1/runs/{run_id}/retry`, and OpenAPI schemas if changed.
- Frontend/downstream consumers: generated `apps/frontend/src/api/types.ts` and existing monitoring store/components; `/ops` UI implementation remains #211/#212.
- Failure paths/rollback/stale state: missing cycle, invalid source/cycle, mixed source/cycle filters, failed/partial/submission/permanent states, active retry conflict, retry submission failure, cancelled jobs, missing or unsafe logs, and unauthorized retry/cancel.
- Evidence/audit/readiness: backend tests, pipeline events, retry metadata, log route evidence, runbook notes, and PR evidence; controlled live failure evidence remains #213.
Regression rows:
- QHH-like persisted jobs for all canonical stages in one `cycle_id` -> `/pipeline/stages` returns ordered stage summaries with status, progress, and only jobs from that cycle.
- jobs from another source or cycle with similar run ids -> `/pipeline/status`, `/pipeline/stages`, and `/jobs?source=&cycle_time=` exclude them instead of mixing evidence.
- job payload with Slurm id, timestamps, retry count, and `log_uri` -> `/jobs` exposes required fields and deterministic `duration_seconds` without leaking unbounded error/log text.
- relative contained log under configured root -> `/jobs/{job_id}/logs` returns at most the bounded tail and redacted `log_uri`.
- traversal, symlink swap, missing file, or unsafe log URI -> stable API error and no filesystem content leak.
- failed, submission failed, partially failed, or permanently failed run with no active retry -> authorized retry creates persisted retry job/event with incremented retry metadata and returned execution status.
- active retry already pending/submitted/running -> retry returns conflict and does not create duplicate side effects.
- unauthorized retry/cancel -> RBAC error and no job/status/event mutation.
- retry gateway submission failure -> API reports failure with persisted submission-failed metadata and does not claim submitted/running success.
- OpenAPI/frontend types for ops fields/statuses -> either unchanged and proven sufficient, or updated with drift checks.
- qhh diagnostic `.nhms-runs/qhh-continuous` JSON present/absent -> monitoring API behavior is unchanged because it reads formal persistence only.

Boundary-surface checklist:
- Shared helper roots: source/cycle normalization, status classification, stage aggregation, duration calculation, `_job_payload`, `_stage_summaries`, log URI path binding, retry status selection.
- Public entrypoints: pipeline status/stages/jobs/logs/retry/cancel routes.
- Read surfaces: `ops.pipeline_job`, `ops.pipeline_event`, `met.forecast_cycle`, `hydro.hydro_run`, configured log root.
- Write/delete/overwrite surfaces: retry job/event creation and hydro/cycle status updates where retry/cancel already writes; no delete/overwrite behavior added.
- Staging/publish/rollback surfaces: retry/cancel status transitions only; no product publish/rollback implementation.
- Producer/consumer evidence boundaries: formal scheduler/orchestrator persistence is authoritative; qhh diagnostic JSON is non-authoritative.
- Stale-state/idempotency boundaries: repeated reads are read-only, retry conflict prevents duplicate active retries, and failed submission records remain auditable.
- Unchanged downstream consumers: existing monitoring frontend store/components, OpenAPI drift tests, qhh runbooks, M20 scheduler tests.

Non-goals:
- Building or simplifying `/ops`; #211/#212 own frontend route, log modal, retry buttons, and RBAC UI behavior.
- Running live controlled failure/retry evidence; #213 owns live or controlled execution evidence.
- Rewriting QHH diagnostic scripts or making `.nhms-runs/qhh-continuous` a production dependency.
- Changing SHUD runtime, station-series, latest-product, `/hydro-met`, river charts, or live smoke docs.

Review focus:
- Prove all ops MVP backend data comes from formal persistence and is scoped by source/cycle/run identity.
- Prove log reads remain bounded, contained, and server-side.
- Prove retry metadata and failed-state handling cover the status set named by the ops MVP spec.
- Prove existing monitoring/OpenAPI/frontend type consumers remain compatible.
