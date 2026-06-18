## Why

Some legacy API surfaces remain because current frontend or compatibility consumers still use them. Treating those endpoints as dead code would risk breaking shared contracts; they need an explicit deprecation and consumer-migration plan before any OpenAPI or generated type contraction.

## What Changes

- Add an API contract retirement change for legacy forecast/latest-product style compatibility surfaces.
- Require consumer inventory before endpoint deprecation or removal.
- Require replacement endpoint documentation, OpenAPI/type updates, and frontend/backend tests before contraction.
- Keep `/api/v1/mvp/qhh/latest-product` compatible until consumers are migrated.

## Capabilities

### New Capabilities

- `api-contract-retirement`: Provides a governed process for deprecating legacy API contracts without breaking current consumers.

### Modified Capabilities

<!-- No existing API behavior is modified by this planning change. -->

## Impact

- API contracts: `openapi/nhms.v1.yaml`, generated frontend types, API route tests, and current consumers.
- Frontend consumers: display bootstrap and product-loading paths that still use compatibility endpoints.
- Docs: `docs/VALIDATION.md`, current runbooks, API docs, and migration notes.
- Non-goals: deleting endpoints before migration, changing database schema, or reclassifying active shared contracts as dead code.
