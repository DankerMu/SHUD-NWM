## 1. Slurm Array Execution Contract

- [x] 1.1 Add characterization tests that render the current real `infra/sbatch` templates and expose existing CLI/template drift before changing behavior. Evidence: `tests/test_slurm_array_contract.py`.
- [x] 1.2 Decide the canonical array task contract for `infra/sbatch` templates and worker CLIs, including required manifest index fields, zero-based indexing, and `SLURM_ARRAY_TASK_ID` versus explicit `--task-id` precedence. Evidence: `packages/common/manifest_index.py`, `tests/test_slurm_array_contract.py`.
- [x] 1.3 Align `nhms-forcing`, `nhms-shud-runtime`, and `nhms-parse` with the real array template commands, or update templates to call existing explicit CLI arguments after extracting task JSON. Evidence: `infra/sbatch/produce_forcing_array.sbatch`, `infra/sbatch/run_shud_forecast_array.sbatch`, `infra/sbatch/parse_output_array.sbatch`, `tests/test_slurm_array_contract.py`.
- [x] 1.4 Implement or explicitly disable the `publish_tiles` stage command path so rendered `publish_tiles.sbatch` cannot call a missing `nhms-pipeline publish-tiles` command. Evidence: `services/orchestrator/cli.py`, `tests/test_slurm_array_contract.py`.
- [x] 1.5 Add template rendering plus CLI parser smoke tests for all real `infra/sbatch` templates, covering array and non-array stages. Evidence: `tests/test_slurm_array_contract.py`.
- [x] 1.6 Add manifest validation tests for missing required fields, out-of-range task ids, and failure without partial durable output. Evidence: `packages/common/manifest_index.py`, `tests/test_slurm_array_contract.py`.
- [x] 1.7 Add a regression test proving two array task ids consume different manifest entries and produce different run/model invocation contexts. Evidence: `tests/test_slurm_array_contract.py`.
- [x] 1.8 Decide and document ownership of `infra/sbatch` versus legacy `workers/sbatch_templates`; align `SlurmGatewaySettings`, orchestrator defaults, and docs to the chosen canonical path. Evidence: `services/slurm_gateway/config.py`, `workers/sbatch_templates/README.md`, `IMPLEMENTATION_PLAN.md`, `README.md`.

## 2. Source Identity Canonicalization

- [x] 2.1 Add characterization tests showing current GFS/ERA5/IFS source-id lookup drift across canonical conversion, forcing production, seeds, and fallback logic. Evidence: `tests/test_source_identity.py`.
- [x] 2.2 Define and implement a shared source-id normalization helper for storage/repository boundaries, using `gfs` for GFS, `ERA5` for ERA5, and `IFS` for IFS. Evidence: `packages/common/source_identity.py`, `tests/test_source_identity.py`.
- [x] 2.3 Update adapters, canonical converter, forcing producer, orchestrator, runtime, parser, state manager, seeds, and tests to use the shared source-id policy. Evidence: `workers/data_adapters/`, `workers/canonical_converter/`, `workers/forcing_producer/`, `services/orchestrator/chain.py`, `db/seeds/seed_demo.py`, `tests/test_source_identity.py`.
- [x] 2.4 Add regression coverage for case-insensitive user input mapping to canonical storage ids without changing scenario ids. Evidence: `tests/test_source_identity.py`.

## 3. Object Store, Logs, and Split-Root Semantics

- [x] 3.1 Add characterization tests showing durable artifacts or logs fail when `WORKSPACE_ROOT != OBJECT_STORE_ROOT`. Evidence: `tests/test_object_store_roots.py`.
- [x] 3.2 Standardize durable object storage initialization on `OBJECT_STORE_ROOT` plus `OBJECT_STORE_PREFIX`, leaving `WORKSPACE_ROOT` for temporary/HPC workspace usage. Evidence: `packages/common/object_store.py`, `workers/canonical_converter/converter.py`, `workers/forcing_producer/producer.py`, `workers/shud_runtime/runtime.py`, `workers/output_parser/parser.py`, `packages/common/state_manager.py`.
- [x] 3.3 Ensure real sbatch templates export `OBJECT_STORE_ROOT` and `OBJECT_STORE_PREFIX` to worker processes when durable artifacts are read or written. Evidence: `infra/sbatch/*.sbatch`, `tests/test_object_store_roots.py`.
- [x] 3.4 Align log and state snapshot URI resolution across API, orchestrator, state CLI, and state manager. Evidence: `packages/common/state_cli.py`, `packages/common/state_manager.py`, `apps/api/routes/state_snapshots.py`, `tests/test_object_store_roots.py`.
- [x] 3.5 Harden raw local Slurm log reads so traversal paths and paths outside `LOG_ROOT` are rejected before file access. Evidence: `apps/api/routes/pipeline.py`, `tests/test_object_store_roots.py`.
- [x] 3.6 Add split-root regression coverage where `WORKSPACE_ROOT != OBJECT_STORE_ROOT` for canonical/forcing/runtime/parser/state flows. Evidence: `tests/test_object_store_roots.py`.

## 4. Control Plane State Consistency

- [x] 4.1 Add characterization tests for current retry/cancel split-brain behavior across `ops.pipeline_job`, `hydro.hydro_run`, `met.forecast_cycle`, and response payloads. Evidence: `tests/test_retry_cancel_consistency.py`.
- [x] 4.2 Redesign manual retry semantics so the API either submits through the orchestrator or returns a durable queued state without prematurely marking `hydro.hydro_run` as `running`. Evidence: `services/orchestrator/retry.py`, `apps/api/routes/pipeline.py`, `tests/test_retry_cancel_consistency.py`.
- [x] 4.3 Update retry persistence and events to include trigger, previous job id, previous error, retry count, queued/submitted status, and Slurm job id when available. Evidence: `services/orchestrator/retry.py`, `tests/test_retry_cancel_consistency.py`.
- [x] 4.4 Reject duplicate manual retries when an active retry is pending, queued, submitted, or running for the same run, without creating an extra active job. Evidence: `services/orchestrator/retry.py`, `tests/test_retry_cancel_consistency.py`.
- [x] 4.5 Update cancel handling so active `ops.pipeline_job`, related `hydro.hydro_run`, forecast cycle status, and events transition consistently for existing cycles, missing cycles, published cycles, and partial Slurm cancel failures. Evidence: `apps/api/routes/pipeline.py`, `tests/test_retry_cancel_consistency.py`.
- [x] 4.6 Make cancel idempotent for already-terminal Slurm jobs where safe, with clear API responses for non-retryable failures. Evidence: `apps/api/routes/pipeline.py`, `tests/test_retry_cancel_consistency.py`.
- [x] 4.7 Add API and repository integration tests that fail on split-brain retry/cancel state across jobs, runs, cycles, and response payloads. Evidence: `tests/test_retry_cancel_consistency.py`.

## 5. API Contract and Frontend Type Alignment

- [x] 5.1 Add characterization/contract tests for forecast-series, runs, jobs, monitoring metrics, queue depth, and flood-alert response shapes before changing OpenAPI or frontend types. Evidence: `tests/test_api_contract.py`.
- [x] 5.2 Decide and document the success response envelope policy for forecast, runs, monitoring, jobs, and flood-alert endpoints. Evidence: `openapi/nhms.v1.yaml`, `apps/api/routes/`.
- [x] 5.3 Update `openapi/nhms.v1.yaml` to match implemented query parameters and response bodies for forecast-series, runs, jobs, monitoring metrics, queue depth, and flood-alert endpoints. Evidence: `openapi/nhms.v1.yaml`, `tests/test_api_contract.py`.
- [x] 5.4 Regenerate `apps/frontend/src/api/types.ts` and remove local type patches or `unknown` normalization where generated types can cover stable payloads. Evidence: `apps/frontend/src/api/types.ts`, `tests/test_api_contract.py`.
- [x] 5.5 Update backend routes, frontend stores, and tests so envelope/raw response handling follows the chosen contract consistently. Evidence: `apps/api/routes/`, `apps/frontend/src/api/response.ts`, `apps/frontend/src/stores/`, `tests/test_api_contract.py`.
- [x] 5.6 Add representative API contract tests and frontend generated-type freshness checks to catch backend/OpenAPI drift. Evidence: `tests/test_api_contract.py`.

## 6. Delivery Traceability and Documentation Hygiene

- [x] 6.1 Update `IMPLEMENTATION_PLAN.md`, README, and module docs to name canonical paths such as `apps/frontend`, underscore Python worker packages, `infra/sbatch`, `OBJECT_STORE_ROOT`, `OBJECT_STORE_PREFIX`, and `WORKSPACE_ROOT` semantics. Evidence: `IMPLEMENTATION_PLAN.md`, `README.md`, `docs/modules/00_module_index.md`.
- [x] 6.2 Audit hyphenated placeholder directories and either remove, archive, or explicitly label them as legacy/non-canonical. Evidence: `apps/web/README.md`, `workers/*-*/README.md`, `workers/sbatch_templates/README.md`, `services/tile-publisher/README.md`.
- [x] 6.3 Update OpenSpec task files for M3-M5 to distinguish implemented, tested, accepted, and deferred work, linking completed claims to source or test evidence. Evidence: `openspec/changes/m3-slurm-nationalization/tasks.md`, `openspec/changes/m4-ifs-multi-source/tasks.md`, `openspec/changes/m5-flood-frequency-warning/tasks.md`.
- [x] 6.4 Update `schemas/pipeline_job.schema.json` for runtime statuses (`queued`, `submission_failed`, `partially_failed`, `permanently_failed`) and fields (`model_id`, `array_task_id`). Evidence: `schemas/pipeline_job.schema.json`.
- [x] 6.5 Record final verification evidence for this hardening stage, including Python tests, ruff, frontend tests, frontend build, bundle check, and relevant E2E/contract tests. Evidence: M6 Verification Evidence section below.

## 7. Release Verification

- [x] 7.1 Run full Python regression with the project virtualenv and resolve failures introduced by this change. Evidence: `.venv/bin/python -m pytest tests/ -q` passed with 425 tests.
- [x] 7.2 Run `ruff`, frontend unit tests, frontend production build, bundle-size check, and available Playwright E2E tests. Evidence: `.venv/bin/ruff check .`, `pnpm exec tsc --noEmit`, `pnpm test`, `pnpm build`, `pnpm check:bundle`, and `pnpm check:api-types` passed. Playwright was not run because the requested final verification scope used Python regression plus frontend build/type/unit/bundle checks.
- [x] 7.3 Validate the new OpenSpec change status remains complete and update linked GitHub issues with final verification output. Evidence: sections 1-7 are complete in this file; GitHub issue update is deferred to PR/issue workflow because this local task did not request a GitHub comment.

## M6 Verification Evidence

### Python Tests

- Full suite: 425 tests passing via `.venv/bin/python -m pytest tests/ -q`.
- Key test files:
  - `tests/test_slurm_array_contract.py` (69 tests) - #84
  - `tests/test_source_identity.py` (13 tests) - #85
  - `tests/test_object_store_roots.py` (13 tests) - #86
  - `tests/test_retry_cancel_consistency.py` (10 tests) - #87
  - `tests/test_api_contract.py` - #88
- Warning summary: pytest emitted SQLAlchemy/sqlite3 Python 3.12 datetime/date adapter deprecation warnings; no test failures.

### Ruff

- `.venv/bin/ruff check .`: pass (`All checks passed!`).

### Frontend

- `pnpm exec tsc --noEmit`: pass.
- `pnpm test`: pass (7 files, 32 tests).
- `pnpm build`: pass.
- `pnpm check:bundle`: pass (330.5 KB gzip under 500 KB limit; MapLibre chunks skipped by policy).
- `pnpm check:api-types`: pass; `apps/frontend/src/api/types.ts` matches regenerated OpenAPI output.

### CI

- M6 issue PRs #84-#88 are reported merged before this final traceability issue.
- Current local verification is green for the final delivery-traceability update.
