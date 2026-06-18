## Why

The second review after M6 found that the local regression suite is green but several production-path contracts remain inconsistent: PostgreSQL enum states, real Slurm submission/logging, flood tile format, and OpenAPI/frontend contracts can fail outside the SQLite/mock test path.

This change turns those review findings into a tracked remediation stage so release readiness is gated by production-equivalent contracts, not only by mock-path behavior.

## What Changes

- Align persisted run/cycle status transitions with PostgreSQL enum values for retry and cancel paths.
- Fix the real Slurm gateway/orchestrator submission contract for normal and array jobs, including manifest propagation, object-store roots, template selection, retryable error codes, and durable log lookup.
- Reconcile flood-return-period tile delivery so backend content type, OpenAPI, frontend map source type, and tile payload format are the same contract.
- Reconcile OpenAPI, backend routes, generated frontend types, API base configuration, and contract tests for drift found in model/data-source/forecast endpoints and documented paths.
- Add PostgreSQL-oriented and route-level contract tests so future SQLite/mock-only success cannot hide production incompatibilities.

## Capabilities

### New Capabilities

- `production-state-machine-contract`: Retry and cancel operations must only write statuses accepted by production PostgreSQL enums and must be covered by production-equivalent contract tests.
- `real-slurm-gateway-contract`: Orchestrator and RealSlurmGateway must share one explicit submission, status, retry, object-store, and log contract for real Slurm jobs.
- `flood-tile-delivery-contract`: Flood return-period map tiles must have one consistent backend, OpenAPI, and frontend delivery format.
- `api-contract-convergence`: OpenAPI, backend route behavior, generated frontend types, API base configuration, and delivery documents must converge on one executable API contract.

### Modified Capabilities

- None.

## Impact

- Affected backend areas: `apps/api/routes/pipeline.py`, `apps/api/routes/flood_alerts.py`, `apps/api/routes/data_sources.py`, `apps/api/routes/models.py`, `apps/api/routes/forecast.py`.
- Affected orchestration areas: `services/orchestrator/retry.py`, `services/orchestrator/chain.py`, `services/slurm_gateway/*`, `infra/sbatch/*`.
- Affected contracts: `db/migrations/000003_enums.sql`, any follow-up enum migration, `openapi/nhms.v1.yaml`, generated `apps/frontend/src/api/types.ts`, JSON schemas if status values change.
- Affected frontend areas: `apps/frontend/src/api/client.ts`, forecast store, flood-alert map layer, generated API types, frontend tests.
- Validation impact: requires PostgreSQL/enum migration tests or equivalent real-schema checks, FastAPI route-level RealSlurmGateway contract tests, tile content-type/decode tests, OpenAPI drift checks, Python regression, frontend type/test/build verification, and issue traceability.
