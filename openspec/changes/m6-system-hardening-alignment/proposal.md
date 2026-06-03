## Why

The current implementation passes the local unit and frontend test suites, but the review found several deployment-path mismatches that are not exercised by mock tests: real Slurm array jobs call unsupported CLI arguments, object storage roots are inconsistent across workers, control-plane retry/cancel APIs can diverge from persisted run state, and OpenAPI/docs/frontend contracts have drifted from actual responses.

This change turns those findings into a tracked hardening stage so that production readiness is judged by end-to-end executable contracts rather than isolated mock-path success.

## What Changes

- Align real Slurm `infra/sbatch` array templates with worker CLI entrypoints and add smoke tests that validate rendered commands against parser behavior.
- Standardize object-store root usage and `source_id` canonicalization across adapters, converters, forcing production, runtime, parser, state manager, API, and tests.
- Make retry/cancel control-plane operations update `ops.pipeline_job`, `hydro.hydro_run`, `met.forecast_cycle`, and audit/event records consistently.
- Reconcile OpenAPI, backend responses, generated frontend types, frontend stores, and tests for forecast, runs, jobs, monitoring, and flood-alert endpoints.
- Update delivery documentation, task state, JSON schemas, and stale duplicate path guidance so future work targets the canonical modules.

## Capabilities

### New Capabilities

- `slurm-array-execution-contract`: Real Slurm array sbatch templates and worker CLI commands must share a tested manifest-index/task-id execution contract.
- `runtime-storage-source-canonicalization`: Runtime modules must use a single object-store root policy and a single source-id storage convention.
- `pipeline-control-state-consistency`: Retry and cancel operations must transition pipeline jobs, hydro runs, forecast cycles, and events atomically or through explicit compensating states.
- `api-contract-alignment`: OpenAPI, backend response shapes, frontend generated types, and frontend stores must describe and consume the same API contract.
- `delivery-traceability-hygiene`: OpenSpec tasks, implementation docs, JSON schemas, and canonical directory guidance must reflect the implemented system and its validation evidence.

### Modified Capabilities

- None.

## Impact

- Affected backend areas: `apps/api/routes/*`, `packages/common/*`, `services/orchestrator/*`, `services/slurm_gateway/*`.
- Affected worker areas: `workers/data_adapters`, `workers/canonical_converter`, `workers/forcing_producer`, `workers/shud_runtime`, `workers/output_parser`, `workers/flood_frequency`, `infra/sbatch`.
- Affected frontend areas: `apps/frontend/src/api`, `apps/frontend/src/stores`, forecast/monitoring/flood-alert pages and tests.
- Affected contracts: `openapi/nhms.v1.yaml`, `schemas/pipeline_job.schema.json`, OpenSpec task files, README/implementation docs.
- Validation impact: requires CLI/template contract smoke tests, storage-root split tests, retry/cancel integration tests, API contract tests, frontend regenerated type checks, and full Python/frontend regression suites.
