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

### D1. Fix by moving shared helpers downward

If service code needs response/error constants or formatting helpers, they should live in `packages/common` or a service-owned module, not under `apps.api`.

### D2. Keep API wiring in the API layer

If a helper is API-specific, service modules should not import it. API routes should call service functions and adapt results to API responses at the boundary.

### D2a. Replace the readonly validation exception explicitly

`services/production_closure/readonly_db_validation.py` currently has a documented exception for API smoke probes. This change must either replace that exception with an API-owned adapter or document why the probe remains out of gate scope. It must not move FastAPI application construction or route modules into `packages/common`.

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
