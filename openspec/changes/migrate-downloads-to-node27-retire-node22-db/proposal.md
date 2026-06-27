## Why

Production is already node-27-centric for active DB, ingest, display API, and
public frontend. The next planned change is moving GFS/IFS download to node-27,
while keeping the production scheduler on node-22. The scheduler must therefore
learn raw source-cycle readiness from shared NFS manifests produced by node-27,
not from a node-22-local download or a separate node-22 source-cycle truth.

## What Changes

- Add a node-27 data-plane download role for GFS/IFS source discovery and raw
  download, separate from display_readonly runtime config.
- Make node-27 the production owner for source-cycle DB writes and raw manifest
  persistence.
- Keep the production scheduler on node-22, but make it detect completed
  node-27 downloads through shared NFS raw manifests.
- Stage node-27 NFS raw files into node-22's compute-visible object-store before
  submitting downstream Slurm stages, because compute nodes may not read `/ghdc`.
- When NFS raw is ready and canonical products are absent, start the cycle from
  `convert` instead of submitting node-22 `download_source_cycle`.
- Keep node-22 DB retirement as a later, separately gated cleanup; this change
  does not move orchestration to node-27.

## Capabilities

### New Capabilities

- `node27-download-orchestration`: node-27 bounded source download, preflight,
  evidence, and NFS raw-manifest handoff for GFS/IFS raw source cycles.

### Modified Capabilities

- `production-topology-contract`: node-22 scheduler remains the control point
  and consumes node-27-produced NFS raw manifests before starting downstream
  compute stages.

## Impact

- `workers/data_adapters/*_adapter.py` and `workers/data_adapters/cli.py`
- New or updated node-27 download scripts/wrappers under `scripts/`
- `infra/env/node27-*.example`, node-27 cron/runbook material
- `services/orchestrator/*` scheduler/chain source-cycle readiness and restart
  boundaries, plus pre-submit raw staging
- `infra/sbatch/*.sbatch` remains the downstream compute execution substrate
- `docs/runbooks/current-production-ops.md`,
  `docs/governance/ROLE_BOUNDARY.md`, topology guardrails, and live receipts
