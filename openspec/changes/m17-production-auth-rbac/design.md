## Context

M17 follows the completed M10 production closure and M11 frontend delivery. Current monitoring UI has role gates and dev/test override semantics, but production-sensitive actions need a backend enforcement seam that can run against deterministic fixtures now and later be wired to live identity.

## Design Decisions

- Accepted role vocabulary is `viewer`, `analyst`, `operator`, `model_admin`, and `sys_admin`, matching `docs/spec/07_devops_ops_security.md`.
- Dev/test auth uses explicit test tokens or headers and marks evidence as non-live; live IdP proof is separate and release-blocking until available.
- Backend policy checks live at API/service action boundaries for rerun, cancel, QC override, model activation/deactivation, source config change, tile republish, and model asset operations.
- Denied and release-blocked actions must not mutate state.
- Audit rows record actor, role, action, target, decision, reason, previous/new state when applicable, and redacted lineage.

## Protected Action Matrix

Stable action ids:

| Action id | viewer | analyst | operator | model_admin | sys_admin |
|---|---:|---:|---:|---:|---:|
| `pipeline.retry_run` | deny | deny | allow | allow | allow |
| `pipeline.cancel_run` | deny | deny | allow | allow | allow |
| `pipeline.rerun_cycle` | deny | deny | allow | allow | allow |
| `qc.override_result` | deny | deny | allow | deny | allow |
| `tiles.republish` | deny | deny | allow | deny | allow |
| `sources.update_config` | deny | deny | deny | deny | allow |
| `models.activate` | deny | deny | deny | allow | allow |
| `models.deactivate` | deny | deny | deny | allow | allow |
| `models.switch_version` | deny | deny | deny | allow | allow |
| `models.rollback_version` | deny | deny | deny | allow | allow |
| `models.supersede` | deny | deny | deny | allow | allow |
| `users.manage` | deny | deny | deny | deny | allow |

Policy output fields: `action_id`, `decision=allow|deny|release_blocked`, `required_roles`, `matched_roles`, `actor_id`, `target_type`, `target_id`, `reason`, `reason_code`, `execution_mode=policy_simulated|backend_route_executed|live_proof|release_blocked`, and `no_mutation_expected`.

Stable errors: missing/invalid auth returns `401 AUTH_REQUIRED`; authenticated but unauthorized returns `403 RBAC_FORBIDDEN`; configured-but-unproven live dependency returns `503 RELEASE_BLOCKED`.

## Dependency Order

- Define auth context and role vocabulary before route enforcement.
- Implement the protected action matrix before M18 mutating model operation UI depends on it.
- Add policy enforcement before frontend alignment.
- Add audit evidence and validation lane after policy decisions are stable.

## Risks and Mitigations

- Risk: dev/test override is mistaken for production auth. Mitigation: every evidence artifact records `auth_mode` and `live_backend_auth_executed`.
- Risk: partial route enforcement leaves unsafe actions open. Mitigation: action matrix tests enumerate every protected action.
- Risk: audits leak credentials or local paths. Mitigation: reuse redaction helpers and add credential-shaped test cases.

## Verification

- `openspec validate m17-production-auth-rbac --strict`
- `uv run ruff check .`
- Focused backend auth/RBAC/audit tests.
- Frontend RBAC tests and build when UI gates change.
- Opt-in production readiness evidence records auth mode, `policy_simulated`, `backend_route_executed`, `live_proof`, and live proof blocker modes.

## Workflow Fixture

Fixture level: expanded
Project profile: other
Repair intensity: high

Why:
- This change creates a shared backend authorization boundary for public API routes and service actions.
- It touches auth/permissions, audit evidence, release-blocker semantics, frontend gates, and downstream readiness evidence.
- Denied paths must prove no protected mutation occurs.

Change surface:
- Backend auth context and RBAC policy helpers for FastAPI/service actions.
- Protected pipeline, QC, tile, source, model, and user-management action boundaries where present.
- Audit/evidence production for allowed, denied, and release-blocked decisions.
- Frontend auth store/RBAC gates and dev/test role override behavior.
- Validation docs, `progress.md`, and production readiness evidence.

Must preserve:
- Existing deterministic/dev workflows can still run with explicit test credentials or opt-in dev role override.
- Current readonly model asset UI remains available to `model_admin` and `sys_admin`.
- Existing monitoring retry/cancel behavior remains compatible in test/dev mode while spoofed headers stay rejected by default.
- Production-like readiness evidence must continue to mark live proof gaps as blockers, not successes.

Must add/change:
- Canonical backend auth context with stable error codes `AUTH_REQUIRED`, `RBAC_FORBIDDEN`, and `RELEASE_BLOCKED`.
- Canonical action ids and role/action matrix matching this design.
- Backend/service checks that prevent protected mutation on missing/invalid/unauthorized credentials.
- Redacted audit decisions and readiness evidence with execution mode.
- Frontend role vocabulary and gates aligned to the backend matrix.

Selected risk packs:
- Public API / CLI / script entry: route and service-boundary authorization is the main contract.
- Config / project setup: dev/test auth mode and live IdP placeholder must be explicit.
- File IO / path safety / overwrite: audit/evidence artifacts must not leak local paths, URIs, credentials, or lineage.
- Schema / columns / units / field names: auth context, policy output, audit row, and readiness evidence fields are contract-bearing.
- Legacy compatibility / examples: existing dev/test retry/cancel and model asset readonly flows must remain compatible.
- Error handling / rollback / partial outputs: denied and release-blocked paths must not mutate protected state.
- Release / packaging / dependency compatibility: live IdP remains a release blocker and must not be required for fast CI.
- Documentation / migration notes: docs must explain deterministic versus live proof semantics.

Risk packs considered:
- Public API / CLI / script entry: selected - public FastAPI/service action enforcement.
- Config / project setup: selected - dev/test token/header and live IdP blocker configuration.
- File IO / path safety / overwrite: selected - redacted audit/readiness evidence may contain path/URI-shaped inputs.
- Schema / columns / units / field names: selected - stable auth/audit/readiness contracts.
- Geospatial / CRS / shapefile sidecars: not selected - M17 does not parse or transform geospatial artifacts.
- Time series / forcing / temporal boundaries: not selected - no forcing/time-series contract changes.
- Numerical stability / conservation / NaN: not selected - no solver or numeric computation changes.
- Solver runtime / performance / threading: not selected - no SHUD runtime or threading changes.
- Resource limits / large input / discovery: not selected - no broad filesystem discovery or large-input ingestion is added.
- Legacy compatibility / examples: selected - existing dev/test operator flows and frontend gates must keep working.
- Error handling / rollback / partial outputs: selected - protected denials must be stable and no-mutation.
- Release / packaging / dependency compatibility: selected - live IdP proof is release-blocked, not a fast-CI dependency.
- Documentation / migration notes: selected - production readiness interpretation changes.

Invariant Matrix:
- Governing invariant: every protected action decision is derived from the same canonical auth context and action matrix before mutation, and every emitted audit/readiness artifact accurately records that decision without leaking sensitive inputs or claiming unproven live auth.
- Source-of-truth identity/contract: `actor_id`, `roles[]`, `auth_mode`, canonical `action_id`, target resource identity, policy decision, and `execution_mode`.
- Producers: backend auth dependency/policy helper, protected API/service callers, frontend auth store/dev override, readiness validation producer.
- Validators/preflight: policy matrix evaluator, FastAPI dependencies, service-boundary guards, redaction helper, readiness evidence schema checks.
- Storage/cache/query: audit/evidence JSON files and any in-memory test stores used by protected operations.
- Public routes/entrypoints: pipeline retry/cancel/rerun, QC override, tile republish, source config update, model operation stubs/hooks, user-management placeholder where present.
- Frontend/downstream consumers: RBAC gates, monitoring job action controls, model asset navigation/page gates, production readiness summary readers.
- Failure paths/rollback/stale state: missing/invalid credential, authenticated unauthorized role, release-blocked live mode, denied operation no-mutation assertions, stale frontend role/backend forbidden handling.
- Evidence/audit/readiness: audit rows, auth/RBAC validation artifacts, release blocker artifacts, docs and `progress.md`.
- Regression rows:
  - protected route/service + valid test actor with allowed role/action -> mutation may execute and an allowed audit/readiness decision is recorded.
  - protected route/service + missing/invalid credential -> `401 AUTH_REQUIRED`, no mutation, denied audit/readiness decision where applicable.
  - protected route/service + authenticated role outside matrix -> `403 RBAC_FORBIDDEN`, no mutation, denied audit/readiness decision.
  - live auth requested without accepted live IdP proof -> `503 RELEASE_BLOCKED`, no mutation, `execution_mode=release_blocked` evidence with removal criteria.
  - accepted opt-in live IdP proof -> `execution_mode=live_proof` evidence with provider metadata, role mapping result, protected action checks, and redacted credentials.
  - audit/readiness payload with credential/path/URI/lineage-shaped input -> redacted artifact with stable schema and no sensitive raw value.
  - unchanged frontend/model asset/monitoring consumer -> role visibility and dev/test override remain compatible with backend decisions.

Boundary-surface checklist:
- Shared helper roots: canonical auth context, RBAC matrix evaluator, redaction/evidence helpers.
- Public entrypoints: FastAPI protected routes and service functions for listed action ids.
- Read surfaces: frontend role store, readiness summary readers, audit/evidence readers in tests.
- Write/delete/overwrite surfaces: protected service mutations and audit/evidence writes.
- Staging/publish/rollback surfaces: release-blocked live proof and no-mutation denial paths; no production publish/delete workflow is added.
- Producer/consumer evidence boundaries: backend policy output to audit/readiness files and frontend gate to backend forbidden handling.
- Stale-state/idempotency boundaries: denied/release-blocked repeated calls remain no-mutation; frontend stale role receives backend forbidden response.
- Unchanged downstream consumers: existing monitoring retry/cancel tests, model asset readonly page, production ops validation.

Non-goals:
- Real enterprise IdP integration.
- Implementing M18 model lifecycle mutations.
- Treating frontend gates as sufficient production authorization.
- Proving live backend auth in fast CI.

Review focus:
- Canonical action ids and role matrix are implemented once and reused consistently.
- Denied/release-blocked protected actions cannot mutate service state.
- Audit/readiness evidence is schema-stable, redacted, and does not conflate simulated/backend/live modes.
- Frontend role visibility matches backend authorization semantics.
- Fast CI never depends on live IdP credentials.
