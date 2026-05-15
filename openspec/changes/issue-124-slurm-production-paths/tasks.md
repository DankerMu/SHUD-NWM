## 1. Inventory and Mapping

- [x] 1.1 Document Forecast M3, legacy Forecast, Analysis, and Hindcast stage/job_type/template/manifest contracts in tests or fixtures.
- [x] 1.2 Ensure `config/job_type_templates.yaml` and `SlurmGatewaySettings` default mappings include every production job type used by Forecast M3, Analysis, and Hindcast.
- [x] 1.3 Add mapping tests that fail when a production stage points to a missing `infra/sbatch` template or an unmapped real-gateway job type.
- [x] 1.4 Assert the Analysis production mapping explicitly. Preferred mapping is `analysis_download_source_cycle`, `analysis_convert_canonical`, `analysis_produce_forcing`, `run_shud_analysis`, `parse_analysis_output`, and `save_state_snapshot`; any alternative names must be documented in tests and remain Analysis-specific.

## 2. Analysis Production Slurm Path

- [x] 2.1 Move Analysis stages to canonical real-gateway job types/templates rather than rendered `script` payloads for real Slurm execution.
- [x] 2.2 Add/adjust `infra/sbatch` templates for Analysis download, canonical conversion, forcing, SHUD analysis runtime, parse, and state snapshot stages.
- [x] 2.3 Prove Analysis submissions do not rely on ignored `script` manifest payloads in real gateway mode.
- [x] 2.4 Add fake real-gateway coverage for Analysis submit, status polling, timeout/failure attribution, logs, and cancel where applicable.

## 3. Hindcast Production Slurm Path

- [x] 3.1 Ensure Hindcast Slurm array manifest includes model/source/year/run/forcing/object-store fields required by the production `hindcast` template.
- [x] 3.2 Prevent metadata-only Hindcast forcing from entering SHUD runtime; fail with stable `HINDCAST_FORCING_PACKAGE_UNAVAILABLE` or an equivalent documented error code when real forcing output is unavailable.
- [x] 3.3 Add/adjust tests for Hindcast real gateway submission, manifest index/template rendering, and insufficient/metadata-only forcing guards.
- [x] 3.4 Assert where the metadata-only forcing error is surfaced: raised exception, persisted `hydro.hydro_run.error_code`, and/or API response.

## 4. Legacy Template Boundary

- [x] 4.1 Update `workers/sbatch_templates/README.md` to clearly mark legacy templates as non-production or document remaining intentional test-only use.
- [x] 4.2 Remove, migrate, or explicitly quarantine code paths that still use `workers/sbatch_templates` for production execution, including `OrchestratorConfig.templates_dir` defaults and `_submit_and_wait` rendered `script` payload behavior.
- [x] 4.3 Add tests or assertions that production defaults use `infra/sbatch`.

## 5. Fake Slurm Evidence Matrix

- [x] 5.1 Cover fake `sbatch` submission for single-job and array production templates.
- [x] 5.2 Cover fake `sacct` status parsing for root jobs and array tasks.
- [x] 5.3 Cover fake `scancel` cancel behavior.
- [x] 5.4 Cover fake `sinfo`/health behavior for the real gateway.
- [x] 5.5 Cover log path/fetch behavior for real gateway jobs.

## 6. Required Evidence

- [x] 6.1 `openspec validate issue-124-slurm-production-paths --strict --no-interactive` passes.
- [x] 6.2 `uv run pytest -q tests/test_analysis_pipeline.py tests/test_real_slurm_gateway.py tests/test_job_array.py tests/test_hindcast.py` passes.
- [x] 6.3 `uv run pytest -q tests/test_orchestrator.py tests/test_e2e_m3.py tests/test_ifs_forecast_integration.py` passes or any intentionally skipped service-heavy test is justified.
- [x] 6.4 `uv run pytest -q tests/test_api.py tests/test_gateway.py` passes.
- [x] 6.5 `uv run ruff check .` passes.

## Risk Pack Evidence Mapping

- Public API / CLI / script entry: tasks 1.1, 2.3, 3.1, evidence 6.2.
- Config / project setup: tasks 1.2, 1.3, 1.4, 4.3.
- File IO / path safety / overwrite: tasks 2.2, 3.1, 5.2.
- Schema / columns / units / field names: tasks 1.1, 3.1, 3.2, 3.4.
- Time series / forcing / temporal boundaries: tasks 2.2, 3.1, 3.2.
- Solver runtime / performance / threading: tasks 2.2, 3.1.
- Resource limits / large input / discovery: tasks 2.4, 3.3, 5.1, 5.2.
- Legacy compatibility / examples: tasks 4.1, 4.2, 4.3.
- Error handling / rollback / partial outputs: tasks 2.4, 3.2, 3.3, 3.4, 5.3.
- Release / packaging / dependency compatibility: tasks 1.2, 1.3, 4.3.
- Documentation / migration notes: task 4.1.

## Non-Goals

- Frontend production data access/RBAC work from #125.
- Real database/e2e integration matrix from #126.
- OpenAPI drift already handled by #123 except where issue #124 changes API-visible behavior.
