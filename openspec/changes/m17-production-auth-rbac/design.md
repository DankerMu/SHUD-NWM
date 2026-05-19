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

Policy output fields: `action_id`, `decision=allow|deny|release_blocked`, `required_roles`, `matched_roles`, `actor_id`, `target_type`, `target_id`, `reason_code`, and `no_mutation_expected`.

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
- Opt-in production readiness evidence records auth mode and live proof blocker.
