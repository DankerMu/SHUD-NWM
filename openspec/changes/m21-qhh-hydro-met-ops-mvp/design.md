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

Governing invariant: one selected forcing version identity must bind every returned station-series sample, metadata field, truncation decision, and readiness count without mixing samples from another model, source, cycle, station, variable, or time window.
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
