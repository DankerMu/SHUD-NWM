## Why

Node-22 was manually repaired after issue #874, but the automatic scheduler still
entered a high-CPU live pass with no Slurm submission or journal progress. The
compute node cannot be called business-ready until the scheduler can run current
code in DB-free mode, preserve retry identity, reconcile Slurm arrays safely, and
make bounded progress under concurrent submission.

## What Changes

- Validate restart/resume candidates before downstream forecast retry so a
  missing `forcing_package_uri` tree is reported as a stable artifact recovery
  blocker instead of generic `NODE_FAILURE`.
- Preserve DB-free scheduler/runtime flags in manual and automatic retry Slurm
  manifests, including forecast retries on node-22.
- Reconcile Slurm array tasks using durable manifest/task identity, not only
  generic job names such as `nhms_forecast` or `nhms_forcing`.
- Treat completed forecast-cycle plus terminal stage/copyback evidence as a
  terminal candidate when stale `hydro_run.status=created` rows remain.
- Bound scheduler live-pass progress/evidence work so a pass cannot burn CPU
  indefinitely while holding the production scheduler lock.
- Restart node-22 services from the merged latest code and prove concurrent
  business operation with live Slurm/file-journal evidence.

## Capabilities

### New Capabilities

None.

### Modified Capabilities

- `production-scheduler-orchestration`: scheduler live passes must be bounded,
  restartable, and business-runnable on node-22 under concurrent submission.
- `job-retry-mechanism`: retry submissions must preserve DB-free runtime
  contracts and stable recovery classifiers.
- `production-slurm-workload`: Slurm reconcile must bind terminal array task
  evidence to submitted manifest/task identity.
- `multibasin-state-idempotency`: stale candidate state must not cause completed
  cycle/basin chains to rerun, and missing upstream forcing artifacts must block
  downstream resume explicitly.

## Impact

- Affected code: scheduler candidate/state/retry/reconcile/runtime guard modules
  under `services/orchestrator`, Slurm submission manifest handling, focused
  tests, and operational docs/runbooks where needed.
- Affected systems: node-22 compute scheduler systemd service/timer, file-backed
  orchestration journal, Slurm array submission/reconcile, SHUD runtime
  DB-free mode.
- No display API, frontend, node-27 database, schema migration, or historical
  node-22 PostgreSQL rollback listener changes are intended.
