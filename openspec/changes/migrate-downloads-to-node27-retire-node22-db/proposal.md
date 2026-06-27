## Why

Production is already node-27-centric for active DB, ingest, display API, and
public frontend, but node-22 still runs a historical PostgreSQL instance and the
current scheduler path still uses that local DB for source-cycle/job state. The
next planned change is moving GFS/IFS download to node-27. That migration should
also remove the remaining active dependency on node-22 DB state instead of
creating a second split-brain state path.

## What Changes

- Add a node-27 data-plane download role for GFS/IFS source discovery and raw
  download, separate from display_readonly runtime config.
- Make node-27 the production owner for source-cycle DB writes and raw manifest
  persistence.
- Convert node-22 Slurm/SHUD work into DB-free artifact/receipt production.
- Route orchestration state through node-27 while keeping node-22 as the Slurm
  execution oracle.
- Retire the node-22 historical PostgreSQL `:55433` after live observation
  proves the node-27 path.

## Capabilities

### New Capabilities

- `node27-download-orchestration`: node-27 bounded source download, preflight,
  evidence, and production ownership for GFS/IFS raw source cycles.

### Modified Capabilities

- `production-topology-contract`: node-22 historical PostgreSQL retirement and
  DB-free compute boundary after node-27 owns downloads and orchestration state.

## Impact

- `workers/data_adapters/*_adapter.py` and `workers/data_adapters/cli.py`
- New or updated node-27 download scripts/wrappers under `scripts/`
- `infra/env/node27-*.example`, node-27 cron/runbook material
- `services/orchestrator/*` scheduler/chain source-cycle and Slurm submission
  boundaries
- `infra/sbatch/*.sbatch` templates and Slurm Gateway request payload contracts
- `docs/runbooks/current-production-ops.md`,
  `docs/governance/ROLE_BOUNDARY.md`, topology guardrails, and live receipts

