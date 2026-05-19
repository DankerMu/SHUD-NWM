## Why

M10 produced production-like ops/security evidence, but production readiness still needs enforceable backend authorization rather than frontend-only gates. With current demo/Basins/deterministic data, the project can implement and test an API-side auth/RBAC boundary, audit trail, and frontend role alignment without waiting for a live enterprise identity provider.

## What Changes

- Add backend authentication seam for dev/test tokens and future IdP integration, with explicit non-live mode metadata.
- Enforce RBAC for operator/model_admin/sys_admin actions at API/service boundaries, not only in the frontend.
- Record audit decisions for allowed and denied actions without leaking secrets.
- Align frontend RBAC gates and local role override behavior with backend decisions.
- Add production-like validation evidence that distinguishes `backend_route_executed`, `policy_simulated`, and `release_blocked` modes.

## Capabilities

### New Capabilities

- `backend-auth-context`
- `rbac-policy-enforcement`
- `audit-decision-recording`
- `frontend-rbac-alignment`
- `auth-readiness-evidence`

## Impact

- FastAPI dependencies/middleware, protected routes, service action checks, audit writing, frontend auth store/gates, tests, and validation docs.
- May update OpenAPI error responses for auth failures.
- Does not require a live IdP; live IdP remains an explicit release blocker until configured and proven.

## Non-Goals

- Integrating a real enterprise IdP in this change.
- Replacing production secret management.
- Treating frontend-only role gates as production authorization.
