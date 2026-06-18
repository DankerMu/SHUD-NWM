## Why

The current API and ops UI still behave like a single-node control surface: the FastAPI app mounts Slurm routes unconditionally, retry/cancel can call the local gateway, job logs are local-path oriented, and latest-product discovery can pass E2E with historical data. The two-node deployment needs a first-class contract where node 22 produces and controls work, while node 27 can only read database state and published artifacts, then run as a Dockerized display service without physical Slurm or workspace access.

## What Changes

- Add an explicit `NHMS_SERVICE_ROLE` runtime contract for `dev_monolith`, `compute_control`, `display_readonly`, and `slurm_gateway`.
- Make Slurm router exposure and control-plane dependencies role-aware so `display_readonly` cannot expose `/api/v1/slurm/*`.
- Make `display_readonly` retry/cancel and adjacent queue-depth behavior fail closed or degrade read-only without constructing a Slurm gateway dependency.
- Add a published artifact log contract covering both 22-side log publication and 27-side log reads so `/api/v1/jobs/{job_id}/logs` consumes allowlisted `published://`, publish-root `file://`, and `s3://` logs instead of 22 private workspace paths.
- Extend QHH latest-product and ops identity filters so cross-plane E2E can lock on `run_id`, `source`, `cycle_time`, and `model_id`.
- Convert `/ops` display behavior to read-only diagnostics in `display_readonly`: no real retry/cancel controls, diagnostic copy, 22 recovery guidance, and refreshed read-only state after 22 acts.
- Add a runtime config endpoint so the frontend gets display role and capability flags from the backend service, not from hardcoded build assumptions.
- Add two-node Docker runtime assets after safety boundaries exist: one app image, role-specific compose/env/systemd files, Docker HostConfig/security tests, Docker disk preflight, and evidence paths under the repo or `/scratch/frd_muziyao`.
- Add readonly DB permission-denied probes and Docker E2E evidence gates so 27 is proven with read-only credentials and without Slurm/Munge/workspace mounts.

## Capabilities

### New Capabilities

- `runtime-service-role-boundary`: defines service roles, production fail-fast behavior, and role-gated route exposure.
- `display-control-mutation-guard`: defines display-mode fail-closed behavior for retry/cancel and control-plane mutation attempts.
- `published-artifact-log-reader`: defines safe published artifact URI support for job logs and private path rejection.
- `qhh-latest-product-identity`: defines strict latest-product identity filters for cross-plane E2E.
- `display-readonly-ops-ui`: defines `/ops` read-only diagnostics, diagnostic copy, and manual 22 recovery guidance.
- `two-node-docker-runtime`: defines one-image, multi-role Docker/Compose/systemd runtime assets and physical capability separation.
- `readonly-db-and-e2e-evidence`: defines readonly DB validation, Docker security checks, and two-node evidence gates.

### Modified Capabilities

None.

## Impact

- Backend runtime: new role helper/config, `apps/api/main.py`, `services/slurm_gateway/config.py`, and tests that assert route exposure by role.
- Backend control API: `apps/api/routes/pipeline.py`, retry/cancel error handling, display-mode queue-depth behavior, pipeline/job log route, OpenAPI patches or static OpenAPI, and monitoring API tests.
- Backend data access: `packages/common/forecast_store.py` latest-product queries and API route parameters in `apps/api/routes/forecast.py`.
- Artifact access: new `services/artifacts/*` module plus compute-side log publication/URI normalization for `published://`, publish-root `file://`, and allowlisted `s3://` log reads with redaction/path-safety tests.
- Frontend: runtime config client, `/ops` monitoring components, `JobsTable`, diagnostic-copy components, `/hydro-met` strict latest-product query parameters, generated API types, and focused tests.
- Infrastructure: `infra/docker/Dockerfile.app`, `infra/docker/entrypoint.sh`, `infra/compose.compute.yml`, `infra/compose.display.yml`, `infra/env/*.example`, `infra/systemd/*`, and `infra/README.two-node-docker.md`.
- Verification: focused backend/frontend tests, Docker build/compose config checks, Docker display security checks, readonly DB smoke, and two-node E2E evidence written under `artifacts/` or `/scratch/frd_muziyao`.

## Non-Goals

- Kubernetes or a multi-node orchestrator.
- A 27-to-22 RPC/HTTP control channel.
- Automatic `operation_request` execution queue.
- Real retry/cancel buttons on 27 in MVP.
- Containerizing Slurm/Munge as a required first-phase deliverable; Slurm Gateway may remain a 22 host systemd service.
- Reusing `infra/docker-compose.dev.yml` as a production two-node compose file.
- Claiming final production readiness without live target-environment receipts.
