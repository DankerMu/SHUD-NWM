## Why

Node-22 is now the compute/control node, while node-27 owns the active
PostgreSQL data plane, download, ingest, display API, and frontend.

Initial context before #836/#837: the historical do-not-connect PostgreSQL
listener on node-22 `:55433` was not yet archived/stopped because the production
scheduler still read `DATABASE_URL`, used `NHMS_SCHEDULER_LOCK_BACKEND=postgres`,
and recorded live evidence with `lock_type=postgres_advisory`.

The previous node-27 download migration intentionally left node-22 DB
retirement as a later gated cleanup. This change turns that gate into concrete
implementation work: make the node-22 scheduler run without any PostgreSQL
dependency, prove it with live GFS/IFS scheduler receipts, then archive and stop
the historical do-not-connect `:55433` listener.

Current status after #837: node-22 `:55433` is historical do-not-connect,
archived/stopped rollback-only state; the authoritative stop receipt is
`docs/runbooks/receipts/2026-06-29-node22-db-retirement-stop.md`.

## What Changes

- Add an explicit DB-free scheduler runtime mode that fails closed when
  PostgreSQL dependencies are still configured.
- Make file locking the only supported lock backend for the node-22 DB-free
  scheduler.
- Replace scheduler model discovery and canonical readiness DB reads with
  file/object-store manifests.
- Replace scheduler active/completed/candidate/job/event persistence with a
  file-backed production journal.
- Replace strict forecast warm-start state lookup with a file-backed state
  snapshot index.
- Add cutover, archive, rollback, and live verification receipts required
  before stopping node-22 `:55433`.

## Capabilities

### New Capabilities

- `node22-dbfree-runtime`: DB-free scheduler mode, fail-closed preflight, file
  lock evidence, and runtime env guardrails.
- `file-model-readiness`: file/object-store model registry and canonical
  product readiness sources for scheduler planning.
- `file-orchestration-journal`: file-backed active/completed/candidate/job/event
  state for scheduling, submission, retry, and permanent-failure guards.
- `file-state-snapshot-index`: DB-free warm-start state snapshot lookup for
  strict forecast successor checkpoint policy.
- `node22-db-retirement-cutover`: archive, stop, rollback, and post-stop live
  verification for the historical node-22 PostgreSQL listener.

## Impact

- `services/orchestrator/scheduler_core.py`,
  `services/orchestrator/scheduler_config.py`,
  `services/orchestrator/scheduler_lease.py`,
  `services/orchestrator/scheduler_adapters.py`
- New file-backed scheduler state modules under `services/orchestrator/`
- `packages/common/model_registry.py`, `packages/common/met_store.py`,
  `packages/common/state_manager.py` or adapter-facing wrappers
- `services/orchestrator/chain_repository.py`, retry/reconcile integration, and
  scheduler state decision tests
- `infra/env/compute.example`, node-22 runtime env/runbooks, and topology
  guardrails
- `docs/runbooks/node22-db-retirement-runbook.md` and live receipt material
