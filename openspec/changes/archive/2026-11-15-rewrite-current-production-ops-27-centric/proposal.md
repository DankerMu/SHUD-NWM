## Why

`docs/runbooks/current-production-ops.md` still contains historical 2026-06-14
instructions that describe node-22 as the production orchestrator and database
writer. Live verification on 2026-06-22 confirms the current deployment is
node-27-centric: node-27 runs the active DB, cron-driven ingest, display API, and
frontend/public reverse proxy, while node-22 is compute/Slurm-only.

## What Changes

- Rewrite the runbook sections for nodes/services, startup confirmation, Slurm
  Gateway, API/display service, and artifact locations.
- Remove the top stale warning after the body no longer repeats the stale
  node-22-writer commands.
- Record the verified 2026-06-22 evidence path: node-27 `node27_autopipe` cron,
  active DB on `127.0.0.1:55432`, display API on `127.0.0.1:8080`, public
  `https://test.nwm.ac.cn`, node-22 Slurm Gateway, and shared NFS
  `/ghdc/data/nwm` ↔ `/home/ghdc/nwm`.
- Preserve role-contract cross-references to `ROLE_BOUNDARY.md` and
  `two-node-deployment-overview.md` without treating their design-intent text as
  current physical topology.

## Capabilities

### New Capabilities

- None.

### Modified Capabilities

- `production-ops-readiness`: current-production on-call runbook must match
  verified node-27-centric production topology and must not retain misleading
  node-22 writer instructions.

## Impact

- `docs/runbooks/current-production-ops.md`
- `openspec/specs/production-ops-readiness/spec.md`
- Live verification evidence from node-27 and node-22 SSH probes
