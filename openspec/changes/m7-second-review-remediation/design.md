## Context

M6 closed several hardening gaps and all local Python/frontend regressions pass, but the second review found production-path mismatches that are easy to miss in SQLite and mock Slurm tests. The highest-risk gaps are:

- Control APIs write `pending` and `cancelled` into enum-backed PostgreSQL tables where those values are not both accepted.
- Real Slurm and orchestrator disagree on nested manifest propagation, template ownership, retryable error codes, and array log lookup.
- Flood-return-period tile endpoints are named `.pbf` and documented as vector tiles while returning JSON.
- OpenAPI, backend route behavior, generated frontend types, and API base configuration disagree on response envelopes, request bodies, path prefixing, and implemented endpoints.

This remediation stage is a release-blocking follow-up to M6, not the roadmap M7 CLDAS stage.

## Goals / Non-Goals

**Goals:**

- Make retry/cancel state transitions valid against production PostgreSQL schema and explicit in OpenAPI/schema contracts.
- Make the real Slurm execution path testable through the FastAPI route boundary, including manifest nesting, object store roots, status/error mapping, and log retrieval.
- Make flood tile delivery either true MVT/PBF or explicitly GeoJSON, with backend, frontend, OpenAPI, and docs all matching the chosen format.
- Make OpenAPI a trustworthy source for generated frontend types and API consumers.
- Add regression tests that fail when SQLite/mock-only success diverges from production behavior.

**Non-Goals:**

- Do not add new data sources such as CLDAS.
- Do not redesign the SHUD model execution algorithm or flood-frequency statistics.
- Do not implement a full map tile cache service unless required to satisfy the flood tile format contract.
- Do not archive previous OpenSpec changes; this change records follow-up remediation tasks and issue traceability.

## Decisions

### 1. Status Changes Must Follow PostgreSQL Enum Contracts

Retry and cancel logic will use one of two explicit approaches per table:

- Extend enum values through a forward-only migration when the state is a real domain state.
- Map control-plane-only states to existing enum values when they are queue implementation details.

The implementation must not rely on `TEXT` columns in SQLite tests as proof of production validity. Tests must assert the allowed values in `db/migrations/000003_enums.sql` plus any new migration.

Alternative considered: keep API-only states in `ops.pipeline_job.status` and never touch `hydro_run`/`forecast_cycle`. This avoids enum migration but leaves run/cycle state stale during user-visible retry/cancel operations, so it is insufficient unless paired with explicit derived-state API behavior.

### 2. Real Slurm Submission Uses Structured Requests, Not Implicit Nested Dicts

The array submit route will use an explicit request contract with `job_type`, `cycle_id`, `stage_name`, `tasks`, and `manifest`. RealSlurmGateway must merge nested `manifest` before rendering templates, with top-level fields taking precedence. Legacy single-job paths must either:

- Submit by `job_type` and manifest only, using configured templates; or
- Support a constrained `script` mode with validation and documented ownership.

The preferred path is to standardize on gateway-owned templates for production safety because `docs/spec/05_slurm_hpc_design.md` requires fixed command templates rather than arbitrary shell.

### 3. Slurm Error Codes Are Stable Control-Plane Signals

Raw Slurm states must be preserved in job metadata, but retry uses stable error codes. Examples:

- `TIMEOUT` -> `SLURM_TIMEOUT`
- `NODE_FAIL` and `PREEMPTED` -> `NODE_FAILURE`
- `OUT_OF_MEMORY` -> `OUT_OF_MEMORY`
- unknown terminal failures -> `SLURM_JOB_FAILED` with raw state in details

Poll timeout must be persisted as a failed pipeline job with `SLURM_JOB_TIMEOUT`, then run through the same retry decision path as observed Slurm terminal states.

### 4. Logs Need Durable Lookup Metadata

RealSlurmGateway cannot depend on in-memory `_jobs` for log location after restart. It must persist or reconstruct enough metadata to locate logs for master and array jobs. For array jobs, log lookup must aggregate `%A_%a.out` and `%A_%a.err` per task and expose task identifiers.

Orchestrator manifest keys for durable storage are lower-case (`object_store_root`, `object_store_prefix`); sbatch templates export those values as upper-case worker environment variables (`OBJECT_STORE_ROOT`, `OBJECT_STORE_PREFIX`).

### 5. Flood Tile Format Must Be Chosen Explicitly

For release readiness, the implementation must choose one format:

- **MVT/PBF path**: use `.pbf`, `application/x-protobuf`, MapLibre vector source, source-layer `flood_return_period`, tile bbox clipping, and tests that validate decodable vector bytes.
- **GeoJSON path**: use JSON content type, frontend GeoJSON source, OpenAPI JSON schema, and docs that no longer call it `.pbf`.

Because the product is national-scale map browsing, MVT/PBF is preferred unless implementation constraints require a short-term GeoJSON fallback.

The tile module docs must also reconcile table and property naming: existing docs mention `map.tile_asset`, while migrations define `map.tile_cache`; flood tile features must document segment id, displayed value, unit, quality flag, return period, and warning level consistently.

### 6. API Contract Convergence Needs Executable Drift Checks

OpenAPI must use exactly one prefix strategy and match implemented route behavior. Generated frontend types must be regenerated from that OpenAPI and checked in CI. Route contract tests must cover representative endpoints that previously drifted: `forecast-series`, `data-sources`, `models/{id}/active`, tile endpoints, monitoring jobs, and flood-alert APIs.

## Risks / Trade-offs

- **Enum migration risk** -> Mitigate with forward-only migrations and explicit compatibility handling for existing rows.
- **Changing envelope policy can break consumers** -> Mitigate by documenting raw versus enveloped endpoints or adding compatibility wrappers before removing old behavior.
- **MVT implementation may require PostGIS-specific tests** -> Mitigate with a focused integration test using PostGIS where available and a unit-level content-type/SQL-shape test otherwise.
- **Persisting Slurm metadata adds schema surface** -> Mitigate by reusing `ops.pipeline_job` fields where possible and adding only minimal fields for log lookup.
- **Issue volume can become too large** -> Mitigate by grouping into 5 delivery-oriented issues tied to this single OpenSpec change.

## Migration Plan

1. Add characterization tests for enum, Slurm route, flood tile format, and OpenAPI drift.
2. Fix enum/status behavior first because downstream retry/cancel tests depend on it.
3. Fix real Slurm submission, status/error mapping, and log lookup next.
4. Fix flood tile format and frontend map source together in one atomic contract change.
5. Fix OpenAPI/frontend types and API base configuration last, then regenerate types.
6. Run full Python regression, ruff, frontend typecheck, frontend tests, frontend build, and OpenSpec status.

Rollback strategy: revert route and worker behavior while keeping forward enum migrations harmless. If an enum value was added but later unused, leave it in place and prevent new writes rather than attempting PostgreSQL enum removal.

## Open Questions

- Should `hydro.run_status` gain `pending`, or should retry map `hydro_run` back to `created`/`staged` while `ops.pipeline_job.status` remains `pending`?
- Should `met.cycle_status` gain `cancelled`, or should cancellation be represented through a terminal failed/superseded status plus event metadata?
- Is the release target willing to implement MVT now, or should a GeoJSON fallback be accepted with explicit performance caveats?
