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
