## Why

Current production is physically node-27-centric for active data mutation, while
node-22 is compute/Slurm-only. The original handoff had three friction points:
forcing-domain data still had a node-22 DB mirror path, node-27 ingest was not
formalized as its own data-plane role, and current docs/guards could still drift
back toward old node-22-writer assumptions. After #837, that mirror path is
archived/stopped rollback-only, compatibility-only, sunset-bound, and requires
explicit DSN plus the archived-rollback allow flag.

## What Changes

- Define the object-store forcing-domain handoff as the canonical contract for
  node-22 compute outputs consumed by node-27 ingest.
- Harden the archived node-22 rollback forcing mirror so it is explicit,
  allow-flagged, audited, and never falls back to display runtime configuration.
- Formalize node-27 ingest as a bounded data-plane writer role that is separate
  from the node-27 display API's `display_readonly` runtime.
- Add topology guardrails so docs, scripts, and verification routes keep the
  current split: node-22 produces compute artifacts, node-27 writes active DB
  state, and display reads from node-27 DB/object-store.

## Capabilities

### New Capabilities

- `forcing-domain-handoff`: Canonical object-store contract and archived
  rollback guardrails for forcing metadata, station series, and interpolation
  weights.
- `node27-ingest-boundary`: Runtime and operational boundary for the node-27
  data-plane ingest worker.
- `production-topology-contract`: Current production topology facts, oracle
  routing, and static drift checks for data/compute/display responsibilities.

### Modified Capabilities

- None.

## Impact

- `scripts/node27_mirror_forcing.py` and `scripts/node27_autopipeline.py`
- Node-27 ingest wrapper/env templates under `scripts/` and `infra/env/`
- Object-store forcing package readers/importers under `packages/` or
  `workers/` depending on the final module boundary
- Runtime/docs guardrails under `apps/api/runtime_mode.py`,
  `scripts/governance/`, `docs/runbooks/`, and `docs/governance/`
- Node-27 live receipts for qhh/heihe ingest and display readiness
