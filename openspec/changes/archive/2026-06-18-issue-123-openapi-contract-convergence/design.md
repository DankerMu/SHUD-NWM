## Context

Issue #123 is part of Epic #120 and is unblocked by #121. Current drift surfaces include `DEFERRED_ROUTES` / `INTERNAL_ROUTES` in `tests/test_openapi_drift.py`, `/api/v1/models` response shape differences, `active` query parameter semantics, `HTTPException(detail=...)` model errors, and flood timeline threshold schemas that generate `Record<string, never>`.

Fixture level: expanded
Project profile: other

Change surface:
- Public OpenAPI: `openapi/nhms.v1.yaml`
- Drift tests: `tests/test_openapi_drift.py`
- API contract/model tests: `tests/test_api_contract.py`, `tests/test_model_registration.py`
- Backend routes/errors: `apps/api/routes/models.py`, `apps/api/errors.py`, `apps/api/routes/flood_alerts.py`
- Generated frontend types: `apps/frontend/src/api/types.ts`

Must preserve:
- Existing model registry write APIs can remain internal unless issue #123 explicitly promotes them to public OpenAPI.
- Existing `active_flag` request alias for `PUT /models/{model_id}/active` remains accepted for compatibility.
- Omitted `active` on `GET /api/v1/models` keeps the current default of active models only unless implementation and OpenAPI intentionally document a different default with tests.
- Flood alert runtime behavior remains unchanged unless schema/type drift reveals a real mismatch.
- Standard error envelope remains `{request_id, status, error}`.
- Frontend generated clients must continue to type `GET /api/v1/models` query parameters correctly when `active` changes from boolean to `true|false|all`.

Must add/change:
- `/api/v1/models` OpenAPI and implementation must agree on envelope/page shape.
- `active` query contract must be explicit and tested as `true|false|all` or intentionally narrowed with matching implementation and OpenAPI.
- Model route errors must use the ApiError envelope rather than raw `HTTPException(detail=...)`.
- Flood threshold/timeline schemas must generate meaningful object types, not empty records.
- Drift allowlists must shrink or carry issue-scoped comments for remaining intentional deferrals.

## Goals / Non-Goals

**Goals:**
- Make OpenAPI a trustworthy release contract for issue #123 surfaces.
- Keep frontend generated types usable without local fallback types for key APIs.
- Add tests that fail on future drift.

**Non-Goals:**
- Implement every future route listed in the public OpenAPI if it lacks a backing store and is explicitly deferred.
- Solve frontend production data source/RBAC work from #125.
- Build the real database integration matrix from #126.

## Decisions

### 1. Prefer Contract Convergence Over Cosmetic Allowlists

Each drift entry should be handled by implementation, OpenAPI removal/deferral, or a narrow comment explaining why it remains future work. Broad allowlists are not sufficient evidence.

### 2. Model List Uses One Page Shape

The model list route should expose a single documented page/envelope shape. Tests should assert both runtime response and OpenAPI schema agree.

### 3. Error Envelope Is Shared

Model registry errors should be raised through `ApiError` or an equivalent handler path that returns the project error envelope with stable error code/message/details.

## Risk Packs Considered

- Public API / CLI / script entry: selected - public OpenAPI and API routes change.
- Config / project setup: not selected - no deployment config change expected.
- File IO / path safety / overwrite: selected - generated frontend types are checked-in artifacts.
- Schema / columns / units / field names: selected - OpenAPI schemas and response fields are central.
- Geospatial / CRS / shapefile sidecars: not selected - no geometry processing change intended.
- Time series / forcing / temporal boundaries: selected - flood timeline schema and thresholds are affected.
- Numerical stability / conservation / NaN: not selected - no numerical computation change.
- Solver runtime / performance / threading: not selected - no solver/runtime change.
- Resource limits / large input / discovery: not selected - API contract only.
- Legacy compatibility / examples: selected - `active_flag` alias and remaining deferred routes must be intentional.
- Error handling / rollback / partial outputs: selected - error envelope convergence is required.
- Release / packaging / dependency compatibility: selected - frontend type generation/build can be affected.
- Documentation / migration notes: selected - OpenAPI and allowlist comments are contract docs.

Selected risk packs:
- Public API / CLI / script entry
- File IO / path safety / overwrite
- Schema / columns / units / field names
- Time series / forcing / temporal boundaries
- Legacy compatibility / examples
- Error handling / rollback / partial outputs
- Release / packaging / dependency compatibility
- Documentation / migration notes

## Risks / Trade-offs

- Tightening model errors can change client-visible error codes -> Mitigation: keep HTTP statuses and messages stable where possible and document codes in tests.
- Regenerating frontend types can produce large diffs -> Mitigation: include only generated OpenAPI type changes tied to this issue.
- Shrinking route allowlists may expose unrelated future work -> Mitigation: keep explicit deferrals with comments and issue basis where implementation is out of scope.

## Migration Plan

1. Add/adjust characterization tests for current drift and generated type gaps.
2. Fix OpenAPI/backend contracts and regenerate frontend types.
3. Re-run route drift, API contract, backend, and frontend checks.

Rollback strategy: revert API contract changes together with generated types; do not leave OpenAPI and runtime mismatched.

## Review Focus

- Runtime response shape equals OpenAPI and generated frontend types.
- Error paths use the shared envelope.
- Remaining drift allowlist entries are justified and minimal.
- Flood threshold schemas are useful typed objects.
- Model list default and active-filter compatibility are explicitly tested.
