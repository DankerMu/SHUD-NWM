## 1. Auth Context and Policy Matrix

- [ ] 1.1 Define backend auth context fields, dev/test token/header behavior, production/live IdP placeholder, and stable unauthorized/forbidden error codes.
- [ ] 1.2 Implement the canonical role/action matrix for `pipeline.retry_run`, `pipeline.cancel_run`, `pipeline.rerun_cycle`, `qc.override_result`, `tiles.republish`, `sources.update_config`, `models.activate`, `models.deactivate`, `models.switch_version`, `models.rollback_version`, `models.supersede`, and `users.manage`.
- [ ] 1.3 Add tests proving missing/invalid credentials do not invoke protected service actions.

## 2. Backend Enforcement and Audit

- [ ] 2.1 Add FastAPI dependencies/middleware and service-boundary policy checks for protected routes/actions.
- [ ] 2.2 Add allowed/denied/release-blocked audit records with redaction for credential-shaped payloads, URIs, logs, and lineage fields.
- [ ] 2.3 Add regression tests for viewer denied, operator allowed subset, model_admin allowed subset, sys_admin full admin actions, and no mutation on denial.

## 3. Frontend Alignment

- [ ] 3.1 Align frontend auth store/RBAC gates with backend role matrix and local dev/test override semantics.
- [ ] 3.2 Add UI tests for action visibility, backend forbidden handling, and test-only role override.

## 4. Readiness Evidence

- [ ] 4.1 Add or update production readiness validation evidence for auth mode, action decisions, live_backend_auth_executed, release blockers, and removal criteria.
- [ ] 4.2 Run OpenSpec strict validation, focused backend tests, frontend tests when changed, and update `progress.md` / validation docs.
