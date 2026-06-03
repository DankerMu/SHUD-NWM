## Context

The M0-M5 implementation now contains a broad functional surface: data adapters, canonical conversion, forcing production, SHUD runtime, output parsing, Slurm gateway/orchestrator, monitoring APIs, flood warning APIs, and a React frontend. Local verification is strong on unit-level and mock paths, but the delivery review found that several production contracts have drifted:

- Real Slurm sbatch templates call CLI parameters that the workers do not accept.
- Mock Slurm tests do not execute rendered scripts, so real cluster failures can be invisible.
- `WORKSPACE_ROOT`, `OBJECT_STORE_ROOT`, `LOG_ROOT`, and object URI handling are inconsistent across modules.
- GFS storage identity is split between `gfs` and `GFS`.
- Manual retry and cancel operations can update `ops.pipeline_job` without matching `hydro.hydro_run` and `met.forecast_cycle` transitions.
- OpenAPI, docs, backend success envelope behavior, generated frontend types, and frontend stores describe different API shapes.
- OpenSpec tasks and implementation docs contain stale paths and checkbox states.

This hardening change is a cross-cutting stabilization stage. It should be implemented before treating the project as production-ready.

## Goals / Non-Goals

**Goals:**

- Make the real Slurm execution path testable without requiring a live Slurm cluster.
- Make storage location and source identity deterministic across all workers and APIs.
- Make retry/cancel state transitions consistent, idempotent, and auditable.
- Make OpenAPI the single source of truth for frontend API types and backend response contracts.
- Make delivery documentation and OpenSpec task state reliable for future implementation planning.

**Non-Goals:**

- Do not introduce new hydrological algorithms, new data sources, or new frontend features.
- Do not redesign database schemas unrelated to the identified contract gaps.
- Do not replace Slurm with another scheduler.
- Do not implement full production authentication beyond documenting and preserving current role-header behavior unless an existing endpoint contract requires it.

## Decisions

### 1. Treat `infra/sbatch` as the canonical real Slurm template set

Use `infra/sbatch` for real Slurm templates because `SlurmGatewaySettings.template_dir` defaults there and M3 array orchestration depends on those files. Legacy `workers/sbatch_templates` may remain for older single-run flows only if explicitly documented and covered by tests.

Alternatives considered:

- Move real templates into `workers/sbatch_templates`: rejected because current settings and M3 docs already point to `infra/sbatch`.
- Keep both paths equivalent: rejected because duplicate templates caused drift.

### 2. Define one manifest-index task contract for array workers

Array templates and worker CLIs must agree on `--manifest-index` and `--task-id`, or templates must extract the task JSON and call existing explicit parameters. The preferred direction is to add first-class manifest-index support to array-capable CLIs so the same contract can be smoke-tested without shell-specific parsing.

The manifest index entry must include at minimum `task_id`, `model_id`, `basin_version_id`, `river_network_version_id`, `run_id`, `source_id`, `cycle_time`, `workspace_dir`, and the stage-specific fields required by the invoked worker.

### 3. Separate temporary workspace from object storage

`WORKSPACE_ROOT` is the local/HPC execution workspace. `OBJECT_STORE_ROOT` plus `OBJECT_STORE_PREFIX` is the durable object store abstraction. Workers that create or consume durable raw/canonical/forcing/runs/states/tiles objects must use `OBJECT_STORE_ROOT`. APIs that read object URIs must resolve through the same object-store abstraction unless the endpoint is explicitly reading Slurm-native raw logs.

### 4. Canonicalize source IDs at storage boundaries

The system must define one storage identity mapping for data sources. This change uses:

- `gfs` for GFS storage and canonical product records.
- `ERA5` for ERA5 storage and canonical product records.
- `IFS` for IFS storage and canonical product records.

User-facing inputs may be case-insensitive, but repositories and object keys must receive normalized storage IDs. Scenario IDs remain semantic values such as `forecast_gfs_deterministic` and `forecast_ifs_deterministic`.

### 5. Control-plane writes must be state-machine aware

Retry and cancel APIs must not update only one table. A control action must either:

- update all affected records in one transaction, or
- write an explicit queued/compensating state that a background orchestrator can consume.

Manual retry must not report a run as `running` until a real Slurm job is submitted or an orchestrator has accepted the work. Cancel must update jobs, hydro run, forecast cycle, and events consistently, and repeated cancel calls for already-terminal Slurm jobs should be idempotent when safe.

### 6. Pick one API success response contract and regenerate types

The project must choose either unified envelope responses or documented raw responses per endpoint. This change prefers the existing monitoring `_ok` envelope pattern as the target for new and operational endpoints, but the implementation decision can retain raw forecast responses only if OpenAPI and frontend generated types are updated to match. The important contract is that backend, OpenAPI, frontend generated types, tests, and docs must agree.

### 7. Treat documentation cleanup as delivery hygiene, not optional polish

Stale OpenSpec task states and duplicate path references directly affect issue generation and future agent work. This change requires documentation to name canonical directories and to link completed task claims to tests or source evidence.

## Risks / Trade-offs

- Real Slurm behavior can differ between clusters -> mitigate with CLI/template smoke tests plus subprocess-mocked RealSlurmGateway tests; reserve live-cluster validation for staging.
- Envelope unification can break frontend assumptions -> mitigate by changing OpenAPI, regenerated types, frontend stores, and tests in the same issue.
- Source ID normalization can affect existing seeded data -> mitigate with migration or seed update tests that verify old and new demo flows.
- Storage-root changes can expose hidden test assumptions -> mitigate by adding tests where `WORKSPACE_ROOT != OBJECT_STORE_ROOT`.
- Retry/cancel transactional updates can become too broad -> mitigate by keeping transaction scopes to control-plane metadata and writing object operations outside DB transactions where appropriate.

## Migration Plan

1. Add characterization tests for the current broken contracts so failures are visible.
2. Align Slurm templates and worker CLI parsing; verify rendered commands without a live cluster.
3. Normalize source IDs and object-store root usage; update seeds and fixtures.
4. Fix retry/cancel state transitions and API responses; add integration tests.
5. Reconcile OpenAPI and frontend types; regenerate `apps/frontend/src/api/types.ts`.
6. Update JSON schemas, OpenSpec task state, README, and implementation plan path references.
7. Run full verification: Python tests, ruff, frontend unit tests, frontend build, bundle check, and any available E2E tests.

Rollback is mostly code-level. If envelope unification is too disruptive, revert that sub-change and instead update OpenAPI/docs to explicitly document raw responses for the affected endpoints.

## Open Questions

- Should all successful API responses use `{request_id, status, data}`, or should forecast/time-series endpoints remain raw for backwards compatibility?
- Should legacy `workers/sbatch_templates` be removed, deprecated in docs, or maintained as single-run local templates?
- Should `source_id` records already seeded as `GFS` be migrated to `gfs`, or should query normalization support both during a transition period?
