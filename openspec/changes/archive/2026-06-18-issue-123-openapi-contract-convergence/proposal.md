## Why

Issue #123 tracks production contract drift between `openapi/nhms.v1.yaml`, FastAPI routes, generated frontend types, and runtime response/error shapes. The drift breaks client generation confidence and hides API regressions behind allowlists.

## What Changes

- Converge model list/detail/active contracts with implemented FastAPI response shapes and query semantics.
- Reduce OpenAPI drift allowlists by either implementing documented routes, documenting implemented routes, or explicitly deferring future-only routes with comments.
- Align model registry errors with the project `ApiError` envelope.
- Fix flood alert timeline/threshold schemas so generated frontend types represent real objects rather than `Record<string, never>`.
- Add regression tests for route drift, model response envelope/page shape, active query values, error envelope, and generated type/schema alignment.

## Capabilities

### New Capabilities

- `api-contract-convergence`: Public OpenAPI, FastAPI behavior, generated frontend types, and tests stay aligned for model and flood alert contracts.

### Modified Capabilities

- `api-openspec-traceability-contract`: Extends prior API traceability requirements to cover issue #123 route drift allowlists, model envelopes, active query semantics, error envelopes, and flood threshold object schemas.

## Impact

- Affects `openapi/nhms.v1.yaml`, `tests/test_openapi_drift.py`, model/flood API routes, frontend generated API types, and API contract tests.
- Requires backend and frontend verification because generated types are an explicit acceptance criterion.
