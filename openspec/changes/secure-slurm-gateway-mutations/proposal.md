## Why

The Slurm gateway currently accepts job submission, array submission, cancellation, and optional registry reset without application-layer authentication. Loopback binding reduces exposure but does not fail closed when deployment networking drifts, and the published OpenAPI contract does not disclose the missing security requirement.

## What Changes

- Register canonical RBAC actions for Slurm submit, cancel, and reset, then enforce them as route dependencies before any gateway mutation.
- Accept a dedicated node-22 service bearer credential for scheduler-to-gateway mutation calls while preserving existing non-production and live-proof auth semantics.
- Forward the service credential only on HTTP Slurm mutation requests; health and inspection routes remain unchanged.
- Publish truthful OpenAPI security requirements for all four mutation operations without embedding credentials.
- Keep disabled internal reset absent from the standalone gateway (404), keep `display_readonly` free of Slurm routes, and deploy a fail-closed loopback bind guard plus remote-unreachability receipt on node-22; a root-managed host packet-filter remains an optional stronger control.

## Capabilities

### New Capabilities

- None.

### Modified Capabilities

- `rbac-policy-enforcement`: add Slurm submit/cancel/reset actions and no-side-effect denial behavior.
- `backend-auth-context`: derive a narrow scheduler service auth context from a dedicated bearer credential.
- `api-contract-alignment`: bind all protected HTTP methods, including DELETE, to two exact security sets: the original 11 business mutations retain the existing three alternatives, while only the four Slurm mutations add the service bearer.
- `slurm-gateway-node22-deployment`: require credential configuration, entrypoint-enforced loopback-only listening, remote-unreachability proof, and an optional root-managed packet-filter receipt when such a control is actually installed.

## Impact

- Shared auth policy/context code under `packages/common`, with `apps/api/auth.py` retaining its public facade.
- Both Slurm route mounts: the full compute/dev API and `services.slurm_gateway.app`.
- The orchestrator HTTP Slurm client, node-22 env/systemd examples, production runbook, OpenAPI source/generated types, and focused auth/gateway/deployment tests.
- No database migration, Slurm payload shape change, read-route authentication change, or display API route expansion.
