# slurm-gateway-node22-deployment Specification

## Purpose

Define the standalone Slurm gateway deployed on node-22: the service itself, stale-job reconcile from a durable job-id source, defence in depth for Slurm mutations, and rollout or rollback that refuses while a scheduler pass is running.

## Requirements

### Requirement: A standalone Slurm gateway service is deployed on node-22
A standalone HTTP Slurm gateway (app + systemd unit + listen URL) SHALL be deployed on node-22,
because the generic chain submits only via the gateway and no such service is proven there today
(`NHMS_SERVICE_ROLE=slurm_gateway` is currently reserved/fail-fast). Its submission contract is
m20 `slurm-array-runner-integration` and m23 `real-shud-slurm-execution`; m24 adds deployment and
live receipts only.

#### Scenario: Gateway app and unit listen at the configured URL
- **WHEN** the gateway is deployed with `SLURM_GATEWAY_URL=http://127.0.0.1:8081`
- **THEN** a standalone gateway app/systemd unit listens at that URL and serves
  `/api/v1/slurm/health`
- **AND** the scheduler preflight HTTP-probes the configured URL (not only an in-process
  `create_gateway().health()`).

#### Scenario: Gateway exposes only Slurm routes
- **WHEN** the deployed gateway's route inventory is checked
- **THEN** it contains only `/health` and `/api/v1/slurm/*`, with no forecast/model/pipeline/static/
  frontend business routes, and the systemd `ExecStart` points to a dedicated gateway entrypoint
  rather than the full business API (`apps.api.main:create_app`)
- **AND** a gateway exposing business routes fails the deployment receipt.

#### Scenario: Health receipt probes all four binaries
- **WHEN** the health endpoint is queried
- **THEN** it reports resolved/executable probe results for `sbatch`, `squeue`, `sacct`, and
  `scancel` (not only `sinfo --version`)
- **AND** an unreachable gateway or any missing binary is a pre-mutation blocker before download,
  SHUD, or publish work.

#### Scenario: Mock-vs-real parity gates live use
- **WHEN** the same stage manifest is submitted via mock and real backends
- **THEN** both yield the same submit→poll→terminal lifecycle and the same `infra/sbatch` template
  selection, and live use is gated on parity.

### Requirement: Stale-job reconcile uses a durable job-id source
On restart, reconcile SHALL recover job identity from durable storage, not gateway memory.

#### Scenario: Restart reconcile by candidate identity
- **WHEN** the gateway/scheduler restarts with jobs in flight
- **THEN** job ids are read from DB `pipeline_job`/pre-execution evidence (not the in-memory
  `_jobs`), and each is reconciled via `sacct` against its
  `candidate_id/run_id/model_id/basin_id/basin_version_id/river_network_version_id`
- **AND** no duplicate is resubmitted for a still-running or already-terminal candidate.

### Requirement: Node-22 Slurm mutation defense in depth
The production Slurm gateway SHALL combine application-layer service authentication with an entrypoint-enforced loopback-only bind. The service SHALL reject every non-loopback bind before uvicorn starts; a root-managed host packet-filter or ACL is an optional stronger control, not a claim made without an installed live mechanism.

#### Scenario: Standalone entrypoint starts with a deterministic HTTP protocol implementation
- **WHEN** the checked-in module entrypoint (`python -m services.slurm_gateway`) starts the standalone gateway
- **THEN** it passes `http="h11"` explicitly to programmatic `uvicorn.run`
- **AND** the resolved host/port (from `SLURM_GATEWAY_URL` or `--url` plus any `--host`/`--port` override) are forwarded unchanged
- **AND** the loopback-only bind guard still runs before uvicorn is imported or started
- **AND** no fallback, try/except, or dependency change is introduced: an optional native httptools runtime breakage in the node-22 maintained active Python environment must not turn the gateway into an unbound exit-1 process, and this pin is not an authorization to rebuild that environment.

#### Scenario: Gateway and scheduler share an owner-only credential
- **WHEN** the node-22 gateway and DB-free scheduler are deployed
- **THEN** both consume the same `SLURM_GATEWAY_SERVICE_TOKEN` from one untracked owner-mode-0600 environment source
- **AND** the checked-in unit/examples contain only variable names or placeholders, never the credential value.

#### Scenario: Gateway is not remotely reachable
- **WHEN** the node-22 deployment receipt is captured at the configured live gateway port (currently measured as 8090)
- **THEN** `ss -ltnp` shows the gateway bound only to a loopback address such as `127.0.0.1:8090`
- **AND** starting the checked-in entrypoint with `0.0.0.0`, `::`, a hostname, or any non-loopback IP fails before uvicorn opens a socket
- **AND** a probe from another host cannot connect to node-22 at the configured gateway port
- **AND** any packet-filter/ACL receipt is included only if an operator-managed rule is actually installed.

#### Scenario: Live auth boundary is proven without creating a job
- **WHEN** local node-22 probes send an invalid submission body with no token, a wrong token, and the configured token
- **THEN** missing/wrong credentials return `401` before body validation while the configured token passes auth and reaches the existing validation error
- **AND** the receipt proves no `sbatch`, `scancel`, or reset side effect was created by these probes.

#### Scenario: Disabled reset remains absent
- **WHEN** the standalone production gateway starts with `SLURM_GATEWAY_ALLOW_INTERNAL_RESET` unset or false
- **THEN** `POST /api/v1/slurm/internal/reset` returns 404 because the route is not registered
- **AND** authentication changes do not turn the disabled route into 401, 403, or a registered operation.

#### Scenario: Scheduler remains operational after credential rollout
- **WHEN** the DB-free scheduler resumes after gateway auth deployment
- **THEN** its HTTP Slurm client attaches the service bearer to submit/cancel calls and gateway preflight remains healthy
- **AND** health and read-only gateway requests do not carry the service token.

### Requirement: Gateway rollout and rollback refuse while a scheduler pass is running

The documented gateway rollout and rollback procedures SHALL decide whether a scheduler pass is running from the state printed by `systemctl --user is-active nhms-compute-scheduler.service`, SHALL stop before any probe start, backup, overwrite or restore when that state is `active`, `activating`, `deactivating` or `reloading` or cannot be read, and SHALL NOT wait, retry, stop or kill the service.

#### Scenario: A pass is running
- **WHEN** the scheduler service is `activating` at step 0 of the rollout or the rollback
- **THEN** the step prints that a pass is still running and how to re-run, and exits 1 before any backup or restore

#### Scenario: The state cannot be read
- **WHEN** `is-active` prints nothing or an unknown word
- **THEN** the step names what was printed and exits 1

#### Scenario: No pass is running
- **WHEN** the scheduler service is `inactive`
- **THEN** the step continues to the fence probes

#### Scenario: The last pass failed
- **WHEN** the scheduler service is `failed`
- **THEN** the step prints that the fence probe requires `inactive` and how to reset the unit, and continues without resetting it

### Requirement: A scheduler configured for the slurm backend submits only through a gateway on the slurm backend
The backend name SHALL have two values, `mock` and `slurm`. When the scheduler is configured for the `slurm` backend, its gateway probe SHALL treat a health body that names any other backend as unhealthy and not submit-capable, SHALL record the reported backend in the gateway check, and the pass SHALL carry the `SLURM_GATEWAY_UNAVAILABLE` blocker with a reason that names the reported backend. A health body without a backend name SHALL be judged on its health and binaries alone. A scheduler configured with a backend name other than `mock` or `slurm` SHALL NOT probe over HTTP and SHALL get an unhealthy gateway check. The in-process probe used with the `mock` backend in development and tests SHALL be unchanged.

#### Scenario: Remote gateway on the mock backend
- **GIVEN** a scheduler configured for the `slurm` backend with a gateway URL and a service token
- **WHEN** the gateway's health body reports `healthy` true, all four binaries executable and `backend` `mock`
- **THEN** the gateway check is unhealthy and not submit-capable, records backend `mock`, and the pass carries `SLURM_GATEWAY_UNAVAILABLE` with a reason naming `mock`

#### Scenario: Remote gateway on the slurm backend
- **GIVEN** the same scheduler
- **WHEN** the health body reports `healthy` true, all four binaries executable and `backend` `slurm`
- **THEN** the gateway check is healthy and submit-capable, as before

#### Scenario: Health body without a backend name
- **GIVEN** the same scheduler
- **WHEN** the health body is healthy with all four binaries executable and carries no `backend` key
- **THEN** the gateway check is healthy and submit-capable, as before

#### Scenario: Unknown backend name on the scheduler
- **GIVEN** a scheduler whose environment sets the backend to `real`
- **WHEN** the gateway check runs
- **THEN** no HTTP request is made and the gateway check is unhealthy with the `SLURM_GATEWAY_UNAVAILABLE` blocker

#### Scenario: Development default
- **GIVEN** a scheduler with no backend configured
- **WHEN** the gateway check runs
- **THEN** it uses the in-process mock gateway and is healthy, as before
