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

## Evidence Mapping for Workflow Fixture

- [ ] Public API / CLI / script entry: backend tests cover at least one protected route/service allowed path, missing credential path, forbidden role path, and release-blocked live proof path where configured.
- [ ] Config / project setup: tests or validation artifacts prove dev/test auth uses explicit credentials or an explicit opt-in override, and fast CI does not require live IdP credentials.
- [ ] File IO / path safety / overwrite: audit/readiness tests include credential-shaped values, local paths, object URI/userinfo/query/fragment, checksum-like strings, and lineage-like payloads and assert redaction.
- [ ] Schema / columns / units / field names: tests assert stable fields for auth context, policy output, audit rows, readiness evidence, blocker id, execution mode, and reason code.
- [ ] Legacy compatibility / examples: focused tests keep existing monitoring retry/cancel dev-role behavior and readonly model asset gates working.
- [ ] Error handling / rollback / partial outputs: tests prove denied and release-blocked actions do not call protected service mutation hooks and record no-mutation expectations.
- [ ] Release / packaging / dependency compatibility: production readiness evidence marks live IdP as a release blocker with removal criteria instead of passing it or failing deterministic checks.
- [ ] Live proof truthfulness: readiness tests assert `execution_mode=live_proof` is emitted only for opt-in live IdP proof and is distinct from `policy_simulated`, `backend_route_executed`, and `release_blocked`.
- [ ] Documentation / migration notes: `docs/VALIDATION.md`, relevant runbook/spec docs, or `progress.md` explain deterministic/backend/live auth modes and live proof gaps.

## Required Verification Commands

- [ ] `openspec validate m17-production-auth-rbac --strict --no-interactive`
- [ ] `uv run pytest -q tests/test_monitoring_api.py tests/test_retry.py tests/test_production_ops_validation.py`
- [ ] `uv run pytest -q tests/test_model_registration.py tests/test_model_activation_audit_integration.py` if model auth/audit boundaries are touched.
- [ ] `uv run ruff check .`
- [ ] `cd apps/frontend && corepack pnpm test`
- [ ] `cd apps/frontend && corepack pnpm build`
