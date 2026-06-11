## Context

Governance-1 established four role boundaries. Governance-4 made layer inversions visible through entropy reporting. The current P1 finding class is `apps-api-layer-inversion`: non-API service modules importing `apps.api.*` helpers.

These findings should be fixed as boundary work, not treated as dead code. The implementation must preserve behavior while moving shared logic to a lower layer or passing API-owned helpers into service code.

## Goals / Non-Goals

**Goals:**

- Remove `apps.api.*` imports from non-API service/shared modules.
- Keep API route behavior unchanged.
- Add tests proving the layer-inversion audit returns zero current findings.
- Make layer inversion eligible for a future hard gate after the baseline is clean.

**Non-Goals:**

- No immediate CI hard-gate enablement.
- No API route removal or OpenAPI contraction.
- No frontend changes.
- No broad refactor of unrelated service modules.

## Decisions

### D0. Stage inventory before fixes

Issue #417 is an inventory-only governance slice. It records the current
`apps-api-layer-inversion` baseline, owner area, and implementation split for
later fixes, but it does not remove imports, update role-boundary policy, or
enable hard gates.

### D1. Fix by moving shared helpers downward

If service code needs response/error constants or formatting helpers, they should live in `packages/common` or a service-owned module, not under `apps.api`.

### D2. Keep API wiring in the API layer

If a helper is API-specific, service modules should not import it. API routes should call service functions and adapt results to API responses at the boundary.

### D2a. Replace the readonly validation exception explicitly

`services/production_closure/readonly_db_validation.py` currently has a
documented exception for API smoke probes. This change must either replace that
exception with an API-owned adapter or document why the probe remains out of
gate scope. It must not move FastAPI application construction or route modules
into `packages/common`.

### D3. Split implementation by ownership

`services/tiles/mvt.py` and `services/production_closure/readonly_db_validation.py` should be separate implementation issues because they have different test surfaces and owners.

## Risks / Trade-offs

- **Risk: behavior drift in API error responses.** Mitigation: run existing route and tile tests and preserve public response contracts.
- **Risk: moving helpers creates circular imports.** Mitigation: place shared helpers in low-level modules with no API dependency.
- **Risk: hard-gate is enabled too early.** Mitigation: this change prepares future enforcement but keeps CI report-only.

## Migration Plan

1. Identify exact `apps.api.*` imports outside `apps/api`.
2. Move or duplicate only stable shared helpers into lower-level modules.
3. Update service imports and API route adapters.
4. Update `docs/governance/ROLE_BOUNDARY.md` if the existing readonly validation API-probe exception is changed.
5. Add/extend static tests for layer inversion.
6. Confirm entropy audit reports no `apps-api-layer-inversion` findings, or explicitly documents any remaining API-probe exception as not gate-eligible.

## Open Questions

- Whether production-closure readonly validation should own a local probe adapter or consume shared helpers from `packages/common`.

## Issue #417 Fixture

Fixture level: none
Repair intensity: low
Project profile: NHMS

Change surface:

- Documentation inventory for current `apps.api.*` imports outside `apps/api`.
- OpenSpec task evidence for the inventory slice.

Must preserve:

- No source import changes, runtime behavior changes, role-boundary policy
  changes, or CI hard-gate enablement in #417.
- `services/tiles/mvt.py` and
  `services/production_closure/readonly_db_validation.py` remain separate
  implementation targets for #418 and #419.

Risk packs considered:

- Public API / CLI / script entry: not selected - inventory does not change
  route, CLI, or script behavior.
- Config / project setup: not selected - no configuration or workflow change.
- File IO / path safety / overwrite: not selected - no runtime file IO change.
- Schema / columns / units / field names: not selected - no data contract change.
- Auth / permissions / secrets: not selected - no auth or credential surface.
- Concurrency / shared state / ordering: not selected - no state transition.
- Resource limits / large input / discovery: not selected - audit command reads
  repo files only and is used as evidence, not changed in #417.
- Legacy compatibility / examples: not selected - compatibility behavior is
  unchanged.
- Error handling / rollback / partial outputs: not selected - no runtime failure
  path changes.
- Release / packaging / dependency compatibility: not selected - no packaging
  or dependency change.
- Documentation / migration notes: selected - the PR adds the source-of-truth
  inventory used to stage #418/#419 without expanding scope silently.

Domain packs:

- Geospatial / CRS / basin geometry: not selected - inventory mentions tile
  ownership but does not change tile geometry behavior.
- Hydro-met time series / forcing windows: not selected - no forcing behavior.
- SHUD numerical runtime / conservation / NaN: not selected - no solver change.
- PostGIS / TimescaleDB domain behavior: not selected - no DB query change.
- Slurm production lifecycle / mock-vs-real parity: not selected - no Slurm
  behavior.
- External hydro-met providers / snapshot reproducibility: not selected - no
  provider behavior.
- Run manifest / QC provenance: not selected - no manifest or QC change.
- Published NHMS artifacts / display identity: not selected - no artifact or
  display contract change.

Required evidence:

- Focused search:
  `rg -n "from apps\\.api|import apps\\.api|apps\\.api\\." . -g '!apps/api/**'`
  records exact import files and lines.
- Entropy evidence:
  `PYTHONDONTWRITEBYTECODE=1 uv run --no-sync python scripts/governance/audit_repo_entropy.py --format json`
  records current `apps-api-layer-inversion` findings.
- OpenSpec validation:
  `openspec validate governance-5-e4-layer-inversion-hardgate-prep --strict --no-interactive`.

Non-goals:

- No import fixes; those belong to #418 and #419.
- No role-boundary documentation edits except inventory evidence.
- No hard-gate or workflow behavior changes.

## Issue #418 Fixture

Fixture level: expanded
Repair intensity: medium
Project profile: NHMS

Change surface:

- `services/tiles/mvt.py` tile helper validation, budget, and property errors.
- `apps/api/routes/flood_alerts.py` API route adapter boundary for MVT tile
  routes and layer metadata routes that call tile helpers.
- Focused tile/API tests that assert existing public HTTP status, error code,
  details, and response headers remain stable.

Must preserve:

- Public tile route behavior for identifier validation, XYZ validation, encoded
  and raw payload byte budgets, feature/coordinate budgets, property validation,
  cache hit/miss/bypass response headers, and MVT media type.
- `services/tiles/mvt.py` stays reusable below the API layer and no longer
  imports `apps.api.*`.
- #419 readonly validation imports remain out of scope and may still be present
  after #418.

Must add/change:

- Replace service-to-API `ApiError` construction in `services/tiles/mvt.py`
  with a lower-layer tile/domain exception or equivalent shared helper.
- Adapt `apps/api/routes/flood_alerts.py` so tile/domain exceptions become the
  same `ApiError` response shape currently observed by API callers.

Risk packs considered:

- Public API / CLI / script entry: selected - public `/api/v1/tiles/**` and
  `/api/v1/layers/**` error contracts must not drift.
- Config / project setup: not selected - no env, workflow, or deployment config
  change.
- File IO / path safety / overwrite: not selected - this PR does not change
  object-store or filesystem IO; existing DB tile-cache reads/writes stay on the
  same code paths.
- Schema / columns / units / field names: selected - MVT error `details`,
  response headers, and tile metadata fields are API-visible contract fields.
- Auth / permissions / secrets: not selected - tile routes in scope do not change
  auth or credential handling.
- Concurrency / shared state / ordering: not selected - no scheduler, lock, or
  shared state transition change.
- Resource limits / large input / discovery: selected - tile byte,
  feature-count, coordinate-count, and identifier/XYZ bounds are explicit safety
  limits.
- Legacy compatibility / examples: selected - existing API tests and frontend
  route consumers expect stable tile error JSON and MVT responses.
- Error handling / rollback / partial outputs: selected - service exceptions
  must map to stable API errors without bypassing FastAPI error handlers.
- Release / packaging / dependency compatibility: not selected - no dependency
  or packaging change.
- Documentation / migration notes: selected - OpenSpec tasks record #418
  completion while leaving #419/#420 open.

Domain packs:

- Geospatial / CRS / basin geometry: selected - tile XYZ and MVT envelope
  behavior must remain unchanged even though SQL/geometry generation is not
  intentionally modified.
- Hydro-met time series / forcing windows: not selected - no forcing or forecast
  time-window logic changes.
- SHUD numerical runtime / conservation / NaN: not selected - no solver or
  numerical runtime change.
- PostGIS / TimescaleDB domain behavior: not selected - no SQL or DB schema
  semantics change is intended.
- Slurm production lifecycle / mock-vs-real parity: not selected - no Slurm
  surface.
- External hydro-met providers / snapshot reproducibility: not selected - no
  provider surface.
- Run manifest / QC provenance: not selected - no run manifest or QC evidence
  change.
- Published NHMS artifacts / display identity: selected - published display tile
  identity, cache keys, headers, and response bodies must remain compatible.

Required evidence:

- Focused import proof:
  `rg -n "from apps\\.api|import apps\\.api|apps\\.api\\." services/tiles/mvt.py`
  returns no matches.
- Focused tile/API tests:
  `uv run --no-sync pytest -q tests/test_flood_alerts_api.py`.
- Audit tests:
  `uv run --no-sync pytest -q tests/test_entropy_audit_script.py`.
- Entropy audit:
  `PYTHONDONTWRITEBYTECODE=1 uv run --no-sync python scripts/governance/audit_repo_entropy.py --format json`
  shows no `services/tiles/mvt.py` `apps-api-layer-inversion` finding; #419
  readonly validation findings may remain.
- Static quality:
  `uv run --no-sync ruff check services/tiles/mvt.py apps/api/routes/flood_alerts.py tests/test_flood_alerts_api.py`.
- OpenSpec validation:
  `openspec validate governance-5-e4-layer-inversion-hardgate-prep --strict --no-interactive`.

Non-goals:

- No readonly validation boundary fix; #419 owns
  `services/production_closure/readonly_db_validation.py`.
- No CI hard-gate enablement; #420 owns enforcement prep after #418/#419.
- No frontend changes and no API endpoint retirement.

Review focus:

- Confirm no `apps.api.*` import remains in `services/tiles/mvt.py`.
- Confirm tile/domain exceptions cannot leak as unhandled 500 errors at public
  API routes.
- Confirm HTTP status, error code, details, cache headers, and MVT response
  behavior remain covered by tests.
