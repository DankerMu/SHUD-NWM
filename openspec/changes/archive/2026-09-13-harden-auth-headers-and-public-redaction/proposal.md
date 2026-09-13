## Why

Five pre-existing, small-blast-radius backend defects share two broken invariants:

1. **Inbound identity/secret headers are trusted outside `packages/common/request_auth.py`'s gated path.**
   - #2081: `apps/api/routes/forecast.py` `_require_hindcast_access_role` reads a client-asserted
     `X-User-Role` directly (no `ALLOW_DEV_ROLE_HEADER` gate, no production-mode refusal, no audit).
     Any public client passing `X-User-Role: analyst` passes it; without the header the route 403s
     while OpenAPI declares it anonymous. Decorative RBAC.
   - #2169: `request_auth._internal_live_proof_token_matches` compares the live-proof secret header
     with `==` (non constant-time); `apps/api/auth.py:143-183` holds an unreferenced dead copy.
2. **Public response bodies carry raw paths/checksums/URIs that sibling fields already render.**
   - #2038: `model_registry._model_public_projection` keeps `mesh_properties_json` (raw
     `source_path`, `resolved_source_path`, `package_checksum`, ...) on lifecycle/active responses,
     while the same body's `preflight.lineage.mesh_properties` is `[redacted]`.
   - #1975: manual-retry 503 `error.message`/`details.error_message` only pass secrets redaction, so
     sbatch absolute paths leak while sibling `runtime_root_resolution` is `[local-path]`.
   - #1976: `public_evidence._sanitize_public_path_or_uri_scalar` classifies scheme-anchored URIs only
     after the whitespace bail-out, so `"s3://nhms prod/objects"` renders `"[object-uri] prod/objects"`.

## What Changes

- **#2081 (user decision 2026-09-13: delete path)**: remove `_require_hindcast_access_role` and
  `_HINDCAST_ACCESS_ROLES`; `run_types=hindcast` is served like any other run type, matching the
  anonymous OpenAPI declaration. No production code reads `X-User-Role` outside `request_auth.py`.
- **#2169**: live-proof comparison uses ASCII-encoded bytes + `hmac.compare_digest`
  (`UnicodeEncodeError` → no match, fail closed), mirroring `service_bearer_matches`; delete the dead
  private helpers in `apps/api/auth.py`.
- **#2038**: `_model_public_projection` pops `mesh_properties_json` (mirrors `_model_asset_detail`);
  drop the field from the restored `ModelInstance` schema, regenerate `openapi/nhms.v1.yaml` and
  `apps/frontend/src/api/types.ts`, remove the conformance stub/mutation.
- **#1975**: route-level `error_message` rendering in `apps/api/routes/pipeline.py` goes through the
  shared path-aware public renderer (`services.orchestrator.public_evidence._public_message`) at the
  retry 503 and at the two GET read payload builders (`_basin_result`, `_job_payload`) that use the same
  helper on the same field. Persisted rows/events keep raw text.
- **#1976**: scheme-anchored URI (`^[A-Za-z][A-Za-z0-9+.-]*://`) classification moves ahead of the
  whitespace bail-out; bare `s3:`/`published:` prefixes and mid-prose `://` stay behind it.

Non-goals: `request_auth`/`auth_policy` semantics, nginx config, real IdP, live-proof channel design,
token `.strip()` asymmetry, `POST /mesh-versions` input schema, file-lane journal renderer,
`scheduler_file_providers` copy (already the target shape), persisted DB/journal text.

## Capabilities

### New Capabilities

(none)

### Modified Capabilities

- `forecast-api`: hindcast series are served without a client-asserted role gate.
- `backend-auth-context`: live-proof secret compared in constant time, fail closed on non-ASCII.
- `model-operation-audit`: lifecycle/active response model projections never carry `mesh_properties_json`.
- `job-retry-mechanism`: public `error_message` renders local paths; spaced scheme-anchored URIs classify whole.

## Impact

- Code: `apps/api/routes/forecast.py`, `packages/common/request_auth.py`, `apps/api/auth.py`,
  `packages/common/model_registry.py`, `apps/api/openapi_restored_schemas.py`,
  `openapi/nhms.v1.yaml`, `apps/frontend/src/api/types.ts`, `apps/api/routes/pipeline.py`,
  `services/orchestrator/public_evidence.py`.
- Tests: `tests/test_forecast_api.py`, `tests/test_monitoring_api.py`, `tests/test_model_registration.py`,
  `tests/test_openapi_response_conformance.py`, `tests/test_retry.py`,
  `tests/test_file_orchestration_journal.py`.
- API: response shape narrows (`data.model.mesh_properties_json` removed — zero frontend consumers);
  hindcast requests without a role header change from 403 to 200.
