## Context

Fixture level: **expanded**. Repair intensity: **high**. Project profile: **NHMS**. Upstream suggested level and minimal mergeable slice: absent (legacy hand-written issue).

The same router is mounted by the full compute/dev API and the bounded standalone gateway, while reusable service code is forbidden from importing `apps.api`. The production scheduler reaches the standalone gateway over loopback. Existing API auth recognizes dev/test and live-proof identities, but there is no production scheduler service credential. Internal reset is conditionally registered by the standalone app and must remain absent when disabled.

## Goals / Non-Goals

**Goals:**
- Deny every Slurm mutation before gateway side effects unless one canonical policy decision allows it.
- Preserve legitimate node-22 scheduler submit/cancel calls through a least-privilege service identity.
- Keep implementation, static OpenAPI, deployment examples, runbook, and live receipts on one security contract.

**Non-Goals:**
- Authenticating Slurm health, list/status, array-result, or log reads.
- Adding a production IdP, changing Slurm payloads, changing display routes, or enabling internal reset in production.
- Replacing host firewall administration or exposing the gateway beyond loopback.

## Decisions

1. **Enforce at the shared router.** Each mutation route gets a pre-handler dependency; neither app-only middleware nor deployment-only controls are sufficient because the router has two mounts. Authorization happens before request-body validation where FastAPI permits, and always before gateway construction/calls.
2. **Keep dependency direction downward.** Reusable request-auth construction lives in `packages/common`; `apps.api.auth` remains the API facade. `services.slurm_gateway` must not import `apps.api`.
3. **Use canonical actions.** `slurm.submit_job` allows `operator`, `model_admin`, and `sys_admin`; `slurm.cancel_job` allows the same roles; `slurm.reset_registry` allows only `sys_admin`. Single and array submit share the submit action.
4. **Use a route-scoped service bearer.** `SLURM_GATEWAY_SERVICE_TOKEN` authenticates a fixed scheduler actor with `operator` role only at Slurm mutation dependencies. It is an ASCII opaque token of at least 16 characters with no whitespace and is never trimmed; non-ASCII configured values are unusable configuration, non-ASCII request headers are ordinary mismatches, and comparison of usable values is constant-time. Missing, empty, short/invalid, or mismatched credentials fail closed without escaping as 500. Existing dev/test and live-proof identities remain available under their existing environment gates, so an authorized `sys_admin` can exercise enabled reset without granting reset to the scheduler token.
5. **Do not leak or over-forward the token.** `HttpSlurmGatewayClient` reads/injects the token only for POST/DELETE Slurm mutation calls. It is absent from health/read requests, exceptions, response details, reprs, logs, evidence, and OpenAPI values.
6. **Publish two exact conditional security sets.** OpenAPI adds a named Slurm service bearer scheme only to the two submit operations, cancel, and reset; the original 11 business mutations retain their existing three identity alternatives and reject the scheduler token. Runtime/static schema tests compare documented and enforced operations across every HTTP method, including DELETE, and prove generated artifacts are secret-free.
7. **Preserve reset registration semantics.** When `allow_internal_reset=false`, the standalone app does not register the route and returns 404 before auth. When enabled, reset is still sys-admin protected.
8. **Deploy defense in depth without inventing root access.** Live node-22 uses the measured `127.0.0.1:8090`, and the module entrypoint rejects every non-loopback bind before uvicorn starts. Gateway and scheduler consume one owner-mode-0600 untracked credential source. Local `ss`, deliberate non-loopback-start rejection, and a remote negative probe prove the mandatory boundary; a root-managed host packet-filter/ACL is optional stronger defense and is recorded only when actually installed.

## Risks / Trade-offs

- **Credential rollout can temporarily stop scheduling** → stop the scheduler timer, configure both consumers, restart/verify gateway, then resume the timer; never add an anonymous compatibility bypass.
- **Shared auth extraction can regress existing API behavior** → retain facade names and run existing auth/OpenAPI suites plus Slurm-specific matrix tests.
- **A token in process environment is visible to same-user processes** → owner-only env files, dedicated service account boundary where available, no serialization/logging, and mandatory entrypoint-enforced loopback binding.
- **The node-22 service user cannot install root packet-filter rules** → make non-loopback binding mechanically impossible in the checked-in entrypoint and prove remote refusal; record a host packet-filter only if an operator independently installs one.

## Migration Plan

1. Stop `nhms-compute-scheduler.timer`; install code and owner-only shared credential into the gateway and scheduler env sources.
2. Deploy the fail-closed loopback bind guard, restart the standalone gateway on measured port 8090, and prove health, anonymous/invalid denial, valid-auth pre-validation passage, reset 404, loopback-only listening, deliberate non-loopback-start rejection, and remote refusal.
3. Resume the scheduler timer only after authenticated pre-validation and the scheduler's read-only gateway preflight both succeed. Rollout snapshots the prior secret and both user-systemd drop-ins before any overwrite. Rollback stops the timer, restores each prior config file exactly (or removes a rollout-created file that was previously absent), reloads/restarts the gateway, and deliberately leaves the timer stopped until the restored code/config independently passes the complete authenticated rollout gate; it never resumes from anonymous health alone.

## Open Questions

- None; read-only node-22 inspection established the live port, unit/env ownership, and lack of noninteractive root access before deployment.
