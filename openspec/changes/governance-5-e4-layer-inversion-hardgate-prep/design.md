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
