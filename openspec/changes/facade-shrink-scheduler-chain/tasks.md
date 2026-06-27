## 1. Scheduler Facade Shrink

- [x] 1.1 Extract scheduler Slurm/preflight implementation owner.
  - Module/Scope: move database-host, storage-root, template, env, SHUD,
    gateway-helper, GRIB-helper, and production Slurm env helper bodies from
    `services/orchestrator/scheduler.py` to
    `services/orchestrator/scheduler_preflight.py`.
  - Stable Facade: keep `services.orchestrator.scheduler` private names
    importable; keep `_slurm_preflight`, `_slurm_gateway_check`,
    `_default_gateway_probe`, and `_slurm_gateway_backend` monkeypatch behavior
    compatible.
  - Inventory/Evidence Update: add scheduler inventory coverage for the retained
    `scheduler-preflight-compat` alias group.
  - Verification: `uv run pytest -q tests/test_production_scheduler.py -k "slurm_gateway or slurm_preflight or grib_env or database_url or database_host"`;
    `uv run pytest -q tests/test_entropy_audit_script.py -k "compatibility_facade or structural_file_budget or scheduler"`;
    `openspec validate facade-shrink-scheduler-chain --strict --no-interactive`;
    `git diff --check`.

## 2. Chain Facade Shrink

- [x] 2.1 Extract chain source-cycle repair owner slice.
  - Module/Scope: move source-cycle repair, retry provenance,
    repaired-stage evidence, sort-key, task identity, and bounded
    candidate-state helper bodies from `services/orchestrator/chain.py` to
    `services/orchestrator/chain_source_cycle.py`.
  - Stable Facade: keep `services.orchestrator.chain` private names importable
    until caller migration is explicitly covered.
  - Inventory/Evidence Update: add chain inventory coverage for the retained
    `chain-source-cycle-repair-facade` alias group.
  - Verification: `uv run pytest -q tests/test_orchestration_chain.py -k "source_cycle or retry_provenance or candidate_state or repaired or repair"`;
    `uv run pytest -q tests/test_entropy_audit_script.py -k "compatibility_facade or structural_file_budget or chain"`;
    `openspec validate facade-shrink-scheduler-chain --strict --no-interactive`;
    `git diff --check`.

- [x] 2.2 Extract chain runtime utility owner slice.
  - Module/Scope: move cycle/job id helpers, restart/cohort checks,
    time/date-range parsing, auto-trigger identity helpers, template export
    helpers, and gateway response helpers from `services/orchestrator/chain.py`
    to `services/orchestrator/chain_runtime_utils.py`.
  - Stable Facade: keep `services.orchestrator.chain` private utility names
    importable and patchable through the legacy module.
  - Inventory/Evidence Update: add chain inventory coverage for the retained
    `chain-runtime-utility-facade` alias group.
  - Verification: `uv run pytest -q tests/test_orchestration_chain.py -k "auto_trigger or template_export or source_cycle or retry_provenance or candidate_state or repaired or repair or date_range"`;
    `uv run pytest -q tests/test_entropy_audit_script.py -k "compatibility_facade or structural_file_budget or chain"`;
    `uv run ruff check services/orchestrator/chain.py services/orchestrator/chain_source_cycle.py services/orchestrator/chain_runtime_utils.py`;
    `openspec validate facade-shrink-scheduler-chain --strict --no-interactive`;
    `git diff --check`.

- [x] 2.3 Extract chain analysis method owner slice.
  - Module/Scope: move `AnalysisOrchestrator` state lookup, context
    construction, manifest construction, stage status hooks, pipeline event
    target, and best-available helper bodies from
    `services/orchestrator/chain.py` to
    `services/orchestrator/chain_analysis.py`.
  - Stable Facade: keep the legacy `AnalysisOrchestrator` private methods on
    `services.orchestrator.chain`, forwarding through the owner module while
    preserving legacy monkeypatch behavior for analysis helper functions.
  - Inventory/Evidence Update: add chain inventory coverage for the retained
    `chain-analysis-forwarders` method group.
  - Verification: `uv run pytest -q tests/test_analysis_pipeline.py tests/test_orchestration_chain.py -k "analysis or manifest"`;
    `uv run pytest -q tests/test_entropy_audit_script.py -k "compatibility_facade or structural_file_budget or chain"`;
    `uv run ruff check services/orchestrator/chain.py services/orchestrator/chain_analysis.py`;
    `openspec validate facade-shrink-scheduler-chain --strict --no-interactive`;
    `git diff --check`.

- [x] 2.4 Extract chain PostgreSQL repository owner slice.
  - Module/Scope: move `PsycopgOrchestratorRepository` method bodies from
    `services/orchestrator/chain.py` to
    `services/orchestrator/chain_repository.py`, with bounded candidate-state
    assembly in `services/orchestrator/chain_repository_state.py`.
  - Stable Facade: keep `services.orchestrator.chain.PsycopgOrchestratorRepository`
    importable with legacy `__module__` identity for existing scheduler/tests.
  - Inventory/Evidence Update: refresh structural line-count inventory and
    record the repository body owner while retaining the legacy chain facade.
  - Verification: `uv run pytest -q tests/test_orchestration_chain.py -k "persistence_repository_compat or candidate_state"`;
    `uv run pytest -q tests/test_gateway_reconcile.py -k "reserve_pipeline_job_sql_absorbs_all_unique_conflicts"`;
    `uv run pytest -q tests/test_entropy_audit_script.py -k "compatibility_facade or structural_file_budget or chain"`;
    `uv run ruff check services/orchestrator/chain.py services/orchestrator/chain_repository.py services/orchestrator/chain_repository_state.py`;
    `openspec validate facade-shrink-scheduler-chain --strict --no-interactive`;
    `git diff --check`.

- [x] 2.5 Extract forecast execution/submission/template owner slices.
  - Module/Scope: move `ForecastOrchestrator` run-chain, retry, submit/poll,
    poll-timeout, terminal-status, submission-failure, duplicate-skip, stage
    manifest, and template-render method bodies from
    `services/orchestrator/chain.py` to
    `services/orchestrator/chain_forecast_execution.py`,
    `services/orchestrator/chain_forecast_submission.py`, and
    `services/orchestrator/chain_forecast_templates.py`.
  - Stable Facade: keep legacy `ForecastOrchestrator` private methods on
    `services.orchestrator.chain`, lazily forwarding to owner modules so
    existing imports and monkeypatch paths remain anchored at the facade.
  - Inventory/Evidence Update: refresh structural line-count inventory for the
    new forecast owner modules, each kept below 1000 lines.
  - Verification: `uv run pytest -q tests/test_orchestration_chain.py -k "stage_execution or submit_and_wait or poll_until_terminal or partial_array or template_export or submission_failed or duplicate_submission or pipeline_logs or retry"`;
    `uv run pytest -q tests/test_pipeline_logs_artifacts.py`;
    `uv run pytest -q tests/test_entropy_audit_script.py -k "compatibility_facade or structural_file_budget or chain"`;
    `uv run ruff check services/orchestrator/chain.py services/orchestrator/chain_forecast_execution.py services/orchestrator/chain_forecast_submission.py services/orchestrator/chain_forecast_templates.py`;
    `openspec validate facade-shrink-scheduler-chain --strict --no-interactive`;
    `git diff --check`.

- [x] 2.6 Extract forecast state/run-context owner slice.
  - Module/Scope: move `ForecastOrchestrator` run-context, forecast manifest,
    warm-start selection, strict/prefilled state validation, exact-state lookup,
    and state QC method bodies from `services/orchestrator/chain.py` to
    `services/orchestrator/chain_forecast_state.py`.
  - Stable Facade: keep legacy forecast state private methods on
    `services.orchestrator.chain`, lazily forwarding to the owner module while
    preserving manifest helper identity and warm-start error semantics.
  - Inventory/Evidence Update: refresh structural line-count inventory for
    `chain_forecast_state.py`, kept below 1000 lines.
  - Verification: `uv run pytest -q tests/test_warm_start_chaining.py tests/test_warm_start.py`;
    `uv run pytest -q tests/test_orchestration_chain.py -k "chain_manifest_legacy_methods_delegate or warm_start or initial_state or prefilled or strict_forecast_state or build_run_context or manifest"`;
    `uv run pytest -q tests/test_entropy_audit_script.py -k "compatibility_facade or structural_file_budget or chain"`;
    `uv run ruff check services/orchestrator/chain.py services/orchestrator/chain_forecast_state.py`;
    `openspec validate facade-shrink-scheduler-chain --strict --no-interactive`;
    `git diff --check`.
