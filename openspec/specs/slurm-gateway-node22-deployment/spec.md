# slurm-gateway-node22-deployment Specification

## Purpose
TBD - created by archiving change m24-multibasin-continuous-daemon-live. Update Purpose after archive.
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

### Requirement: Live submit and cancel receipts are produced on node-22
The deployment SHALL emit distinct live receipts for a short job's terminal lifecycle and a long
job's cancellation, with logs under the configured workspace root.

#### Scenario: Short-job terminal receipt
- **WHEN** an opt-in node-22 proof submits a short job
- **THEN** the receipt records submit (job id), poll-to-terminal status, and the log root under the
  workspace (not the system disk).

#### Scenario: Long-job cancel receipt
- **WHEN** an opt-in node-22 proof submits a long job and cancels it while active
- **THEN** the receipt records submit, cancel-while-active, and the cancelled/accounting result
- **AND** terminal-polling and cancellation are not conflated into one unfalsifiable step.

### Requirement: Stale-job reconcile uses a durable job-id source
On restart, reconcile SHALL recover job identity from durable storage, not gateway memory.

#### Scenario: Restart reconcile by candidate identity
- **WHEN** the gateway/scheduler restarts with jobs in flight
- **THEN** job ids are read from DB `pipeline_job`/pre-execution evidence (not the in-memory
  `_jobs`), and each is reconciled via `sacct` against its
  `candidate_id/run_id/model_id/basin_id/basin_version_id/river_network_version_id`
- **AND** no duplicate is resubmitted for a still-running or already-terminal candidate.

