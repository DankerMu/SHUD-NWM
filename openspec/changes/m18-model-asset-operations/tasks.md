## 1. Preflight and Backend Operations

- [x] 1.1 Define lifecycle endpoints/actions and canonical states `inactive`, `active`, `deprecated`, `superseded` for activate, deactivate, version switch, rollback, supersede, and deprecate.
- [x] 1.2 Implement active uniqueness for `(basin_id, basin_version_id)`, atomic previous-active superseding, and stable concurrent activation behavior.
- [x] 1.3 Implement preflight checks for basin/river/mesh/package lineage, object URI prefix, checksum reread, copied-root evidence, active conflicts, missing active model risk, and downstream impact summary.
- [x] 1.4 Add idempotent activation/deactivation/version-switch behavior and rollback requiring prior active-state evidence.

## 2. Audit and Safety

- [x] 2.1 Record audit/evidence for allowed, blocked, repeated, and rollback operations.
- [x] 2.2 Add redaction tests for local source paths, object URIs, checksums, request reasons, and audit payloads.
- [x] 2.3 Add no-mutation tests for RBAC/preflight-denied operations.

## 3. UI Controls

- [x] 3.1 Add model asset operation controls to the M14 model asset page, gated by M17 backend/frontend roles.
- [x] 3.2 Show preflight summary, confirmation state, blocked reasons, successful audit reference, and stale state refresh.
- [x] 3.3 Add frontend tests for authorized controls, unauthorized hidden/disabled state, backend forbidden handling, preflight blocker, and success refresh.

## 4. Validation

- [x] 4.1 Run OpenSpec strict validation, backend model operation tests, API type freshness if OpenAPI changes, and frontend tests/build if UI changes.
- [x] 4.2 Add production-like validation drill for bad activation, rollback, blocked deactivation, and idempotent repeat using deterministic Basins/model data.
- [x] 4.3 Update `progress.md` and validation docs with supported readonly/mutating model operation scope.

## Evidence Mapping for Workflow Fixture

- [x] Public API / CLI / script entry: tests cover each lifecycle operation through the public API/service boundary with allowed, missing-auth, forbidden-role, and preflight-blocked paths where applicable.
- [x] Config / project setup: deterministic validation drill runs without live object-store mutation, live credentials, or production delete/upload privileges.
- [x] File IO / path safety / overwrite: preflight/audit tests cover local source path, object URI with sensitive components, invalid prefix, checksum reread failure, copied-root symlink/unsafe source, and redacted output.
- [x] Schema / columns / units / field names: tests assert stable lifecycle state, preflight, audit, rollback, blocker, request id, actor/roles/action id, previous/new state, and downstream impact fields.
- [x] Resource limits / large input / discovery: preflight checks are scoped to the candidate model/package/downstream surfaces and tests cover bounded downstream impact output.
- [x] Legacy compatibility / examples: readonly model asset list/detail, Basins import inactive default, active model lookup consumers, and existing registry tests continue to pass.
- [x] Error handling / rollback / partial outputs: tests prove RBAC denied, preflight blocked, active conflict, missing active risk, missing/stale rollback history, and repeated operations do not produce partial or contradictory state.
- [x] Release / packaging / dependency compatibility: production-like drill records deterministic capability and live object-store/delete/upload non-goals truthfully.
- [x] Documentation / migration notes: `docs/VALIDATION.md`, `docs/spec/07_devops_ops_security.md`, `progress.md`, or relevant module docs describe supported mutating operations, RBAC roles, deterministic drill, rollback limits, and object-store non-goals.

## Required Verification Commands

- [x] `openspec validate m18-model-asset-operations --strict --no-interactive`
- [x] `uv run pytest -q tests/test_model_registration.py tests/test_model_activation_audit_integration.py`
- [x] `uv run pytest -q tests/test_production_ops_validation.py tests/test_production_object_store_validation.py` if readiness drill or production closure evidence changes.
- [x] `uv run pytest -q tests/test_api_contract.py tests/test_auth_policy_matrix.py` if API/RBAC contracts change.
- [x] `uv run ruff check .`
- [x] `cd apps/frontend && corepack pnpm test` if UI controls or frontend stores change.
- [x] `cd apps/frontend && corepack pnpm build` if UI controls or frontend stores change.
