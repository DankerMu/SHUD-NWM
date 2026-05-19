## 1. Preflight and Backend Operations

- [ ] 1.1 Define lifecycle endpoints/actions and canonical states `inactive`, `active`, `deprecated`, `superseded` for activate, deactivate, version switch, rollback, supersede, and deprecate.
- [ ] 1.2 Implement active uniqueness for `(basin_id, basin_version_id)`, atomic previous-active superseding, and stable concurrent activation behavior.
- [ ] 1.3 Implement preflight checks for basin/river/mesh/package lineage, object URI prefix, checksum reread, copied-root evidence, active conflicts, missing active model risk, and downstream impact summary.
- [ ] 1.4 Add idempotent activation/deactivation/version-switch behavior and rollback requiring prior active-state evidence.

## 2. Audit and Safety

- [ ] 2.1 Record audit/evidence for allowed, blocked, repeated, and rollback operations.
- [ ] 2.2 Add redaction tests for local source paths, object URIs, checksums, request reasons, and audit payloads.
- [ ] 2.3 Add no-mutation tests for RBAC/preflight-denied operations.

## 3. UI Controls

- [ ] 3.1 Add model asset operation controls to the M14 model asset page, gated by M17 backend/frontend roles.
- [ ] 3.2 Show preflight summary, confirmation state, blocked reasons, successful audit reference, and stale state refresh.
- [ ] 3.3 Add frontend tests for authorized controls, unauthorized hidden/disabled state, backend forbidden handling, preflight blocker, and success refresh.

## 4. Validation

- [ ] 4.1 Run OpenSpec strict validation, backend model operation tests, API type freshness if OpenAPI changes, and frontend tests/build if UI changes.
- [ ] 4.2 Add production-like validation drill for bad activation, rollback, blocked deactivation, and idempotent repeat using deterministic Basins/model data.
- [ ] 4.3 Update `progress.md` and validation docs with supported readonly/mutating model operation scope.
