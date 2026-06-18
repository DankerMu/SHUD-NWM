## Why

The entropy audit reports `apps-api-layer-inversion` findings where shared/service modules import API-layer modules. These are not dead code; they are role-boundary defects that must be fixed before the repository can safely promote layer-inversion checks to future hard-gate candidates.

## What Changes

- Add a dedicated change for removing `apps.api.*` imports from non-API service/shared modules.
- Split tile helper and production-closure readonly validation fixes into small implementation issues.
- Move shared response/error/policy helpers into shared modules or inject them from the API layer.
- Prepare the layer-inversion check for future hard-gate eligibility after findings reach zero.

## Capabilities

### New Capabilities

- `layer-inversion-hardgate-prep`: Removes current `apps.api.*` layer inversions and prepares clean future enforcement.

### Modified Capabilities

<!-- No product API behavior is intended to change. -->

## Impact

- Services: `services/tiles/mvt.py`, `services/production_closure/readonly_db_validation.py`.
- API helpers: any shared error, response, or route helper currently imported from `apps.api.*`.
- Tests: role-boundary static tests, entropy audit tests, tile/API tests, production closure readonly validation tests.
- Docs: `docs/governance/ROLE_BOUNDARY.md` must be updated if this change replaces the existing documented readonly validation API-probe exception.
- Non-goals: API contract retirement, frontend route cleanup, or CI hard-gate enablement.
