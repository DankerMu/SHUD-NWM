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

- [x] 1.2 Extract scheduler runtime/cancellation owner slice.
  - Module/Scope: move `ProductionScheduler.run_once`, restart reconcile,
    retention, pre-execution evidence reservation, prelock evidence writing,
    scheduler evidence context construction, and cancel-requested active Slurm
    method bodies from `services/orchestrator/scheduler.py` to
    `services/orchestrator/scheduler_runtime.py` and
    `services/orchestrator/scheduler_cancellation.py`.
  - Stable Facade: keep legacy `ProductionScheduler` methods on
    `services.orchestrator.scheduler`, lazily forwarding to owner modules while
    preserving old `uuid4` and `MAX_EVIDENCE_BYTES` monkeypatch behavior.
  - Inventory/Evidence Update: refresh structural line-count inventory and
    update scheduler cancellation compatibility ownership for the new
    cancellation owner module.
  - Verification: `uv run pytest -q tests/test_production_scheduler.py -k "run_once or scheduler or cancel or reconcile or retention or evidence or slurm_gateway or slurm_preflight"`;
    `uv run pytest -q tests/test_entropy_audit_script.py -k "compatibility_facade or structural_file_budget or scheduler"`;
    `uv run ruff check services/orchestrator/scheduler.py services/orchestrator/scheduler_runtime.py services/orchestrator/scheduler_cancellation.py tests/test_production_scheduler.py`;
    `openspec validate facade-shrink-scheduler-chain --strict --no-interactive`;
    `git diff --check`.

- [x] 1.3 Extract scheduler candidate manifest owner slice.
  - Module/Scope: move scheduler candidate construction, forcing-result merge,
    canonical identity, basin manifest, manual retry attempt, and warm-start
    manifest-field helper bodies from `services/orchestrator/scheduler.py` to
    `services/orchestrator/scheduler_candidate_manifest.py`.
  - Stable Facade: keep legacy scheduler private helper names importable from
    `services.orchestrator.scheduler`, forwarding lazily to the owner module;
    keep candidate-construction aliases such as `_blocked_candidate` and
    `_candidate_with_state_evidence` anchored at the facade.
  - Inventory/Evidence Update: refresh structural line-count inventory and
    record `scheduler_candidate_manifest.py` as the candidate manifest owner.
  - Verification: `uv run pytest -q tests/test_production_scheduler.py -k "candidate_for or candidate_manifest or warm_start or forcing_result or candidate_identity or basin_manifest or duplicate_candidate_identity"`;
    `uv run ruff check services/orchestrator/scheduler.py services/orchestrator/scheduler_candidate_manifest.py`;
    `openspec validate facade-shrink-scheduler-chain --strict --no-interactive`;
    `git diff --check`.

- [x] 1.4 Extract scheduler candidate execution evidence owner slice.
  - Module/Scope: move candidate execution attempted checks, pipeline result
    write proofs, forcing ready/blocked evidence, Slurm preflight and secret
    manifest blocked evidence, resource profile evidence, candidate execution
    evidence, model-run review evidence, stage/task evidence, and resource
    metric helpers from `services/orchestrator/scheduler.py` to
    `services/orchestrator/scheduler_candidate_execution_evidence.py`.
  - Stable Facade: keep legacy scheduler private helper names importable from
    `services.orchestrator.scheduler`, forwarding lazily to the owner module
    while preserving production contract and evidence schema semantics.
  - Inventory/Evidence Update: refresh structural line-count inventory and
    record `scheduler_candidate_execution_evidence.py` as the candidate
    execution/model-run evidence owner.
  - Verification: `uv run pytest -q tests/test_production_scheduler.py -k "model_run_evidence or candidate_evidence or preflight_blocked or resource_profile or partial_cycle or forcing_ready or forcing_blocked or evidence_write"`;
    `uv run ruff check services/orchestrator/scheduler.py services/orchestrator/scheduler_candidate_execution_evidence.py`;
    `openspec validate facade-shrink-scheduler-chain --strict --no-interactive`;
    `git diff --check`.

- [x] 1.5 Extract scheduler active model discovery owner slice.
  - Module/Scope: move active model registry pagination, registered model
    coercion, duplicate active-model exclusion, output-segment count coercion,
    and discovery filter expression helper bodies from
    `services/orchestrator/scheduler.py` to
    `services/orchestrator/scheduler_models.py`.
  - Stable Facade: keep legacy scheduler private helper names importable from
    `services.orchestrator.scheduler`, forwarding lazily to the owner module.
  - Inventory/Evidence Update: refresh structural line-count inventory and
    record `scheduler_models.py` as the active model discovery/coercion owner.
  - Verification: `uv run pytest -q tests/test_production_scheduler.py -k "coerce_registered_model or discover_models or active_model or model_limit or duplicate_active_model or filter"`;
    `uv run pytest -q tests/test_entropy_audit_script.py -k "compatibility_facade or structural_file_budget or scheduler"`;
    `uv run ruff check services/orchestrator/scheduler.py services/orchestrator/scheduler_models.py`;
    `openspec validate facade-shrink-scheduler-chain --strict --no-interactive`;
    `git diff --check`.

- [x] 1.6 Extract scheduler candidate quality/output owner slice.
  - Module/Scope: move candidate artifact references, resource summaries,
    forcing/output/display/frequency quality states, residual blockers, output
    river manifest helpers, output URI/key helpers, station metadata helpers,
    nested bool coercion, and terminal/unavailable status classification from
    `services/orchestrator/scheduler.py` to
    `services/orchestrator/scheduler_candidate_quality.py`.
  - Stable Facade: keep legacy scheduler private helper names importable from
    `services.orchestrator.scheduler`, forwarding lazily to the owner module.
  - Inventory/Evidence Update: refresh structural line-count inventory and
    record `scheduler_candidate_quality.py` as the candidate quality/output
    owner.
  - Verification: `uv run pytest -q tests/test_production_scheduler.py -k "candidate_quality or output_evidence or resource_summary or residual_blockers or output_river or model_run_evidence or candidate_evidence"`;
    `uv run pytest -q tests/test_entropy_audit_script.py -k "compatibility_facade or structural_file_budget or scheduler"`;
    `uv run ruff check services/orchestrator/scheduler.py services/orchestrator/scheduler_candidate_quality.py`;
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

- [x] 2.7 Extract chain workspace/log and HTTP Slurm client owner slices.
  - Module/Scope: move workspace path safety, workspace read/write, published
    log URI/path construction, gateway log persistence, local stage log writing,
    and log-publication helper bodies from `services/orchestrator/chain.py` to
    `services/orchestrator/chain_workspace.py`; move
    `HttpSlurmGatewayClient` request behavior to
    `services/orchestrator/chain_slurm_client.py`.
  - Stable Facade: keep legacy `ForecastOrchestrator` private workspace/log
    methods, top-level `_workspace_relative_parts`, and
    `services.orchestrator.chain.HttpSlurmGatewayClient` import path compatible
    through thin forwarders/subclassing.
  - Inventory/Evidence Update: refresh structural line-count inventory for
    `chain.py`, `chain_workspace.py`, and `chain_slurm_client.py`, each owner
    module kept below 1000 lines.
  - Verification: `uv run pytest -q tests/test_orchestration_chain.py -k "workspace_log_compat or http_slurm_gateway_client or pipeline_logs or workspace or published_log"`;
    `uv run pytest -q tests/test_pipeline_logs_artifacts.py`;
    `uv run pytest -q tests/test_entropy_audit_script.py -k "compatibility_facade or structural_file_budget or chain"`;
    `uv run ruff check services/orchestrator/chain.py services/orchestrator/chain_workspace.py services/orchestrator/chain_slurm_client.py tests/test_orchestration_chain.py`;
    `openspec validate facade-shrink-scheduler-chain --strict --no-interactive`;
    `git diff --check`.

- [x] 2.8 Extract chain forecast cycle normalization owner slice.
  - Module/Scope: move cycle basin normalization, cohort warm-start application,
    cycle basin identity validation, cycle pipeline-job lookup, existing-stage
    job selection, download raw-manifest retry check, and stage job predicate
    helper bodies from `services/orchestrator/chain.py` to
    `services/orchestrator/chain_forecast_cycle.py`.
  - Stable Facade: keep legacy `ForecastOrchestrator` private method and
    static helper names importable/patchable through
    `services.orchestrator.chain`, forwarding to the owner module.
  - Inventory/Evidence Update: refresh structural line-count inventory for
    `chain.py` and `chain_forecast_cycle.py`, with the new owner under 1000
    lines.
  - Verification: `uv run pytest -q tests/test_warm_start.py -k "warm_start"`;
    `uv run pytest -q tests/test_warm_start_chaining.py -k "warm_start"`;
    `uv run pytest -q tests/test_orchestration_chain.py -k "find_existing_stage_job or duplicate_candidate_identity or cycle_basin or normalize_cycle or candidate_identity or existing_stage"`;
    `uv run ruff check services/orchestrator/chain.py services/orchestrator/chain_forecast_cycle.py`;
    `openspec validate facade-shrink-scheduler-chain --strict --no-interactive`;
    `git diff --check`.

- [x] 2.9 Extract chain forecast trigger owner slice.
  - Module/Scope: move legacy forecast trigger, canonical-ready trigger,
    staged forecast creation, stage status query, ready-cycle iteration, stale
    canonical demotion, auto-trigger canonical readiness validation, ready-cycle
    listing, model listing, and completed-forecast checks from
    `services/orchestrator/chain.py` to
    `services/orchestrator/chain_forecast_trigger.py`.
  - Stable Facade: keep public `ForecastOrchestrator.trigger_*` /
    `stage_statuses` methods plus legacy private trigger helpers on
    `services.orchestrator.chain`, forwarding through the owner module while
    preserving old monkeypatch paths for auto-trigger identity helpers.
  - Inventory/Evidence Update: refresh structural line-count inventory for
    `chain.py` and `chain_forecast_trigger.py`, with the owner under 1000 lines.
  - Verification: `uv run pytest -q tests/test_orchestration_chain.py -k "trigger_ready_forecasts or canonical_readiness or stale_canonical or trigger_forecast or stage_statuses or has_completed_forecast"`;
    `uv run pytest -q tests/test_entropy_audit_script.py -k "compatibility_facade or structural_file_budget or chain"`;
    `uv run ruff check services/orchestrator/chain.py services/orchestrator/chain_forecast_trigger.py`;
    `openspec validate facade-shrink-scheduler-chain --strict --no-interactive`;
    `git diff --check`.

- [x] 2.10 Extract chain forecast control owner slice.
  - Module/Scope: move cycle orchestration entrypoint, Slurm status sync, and
    active cycle cancellation bodies from `services/orchestrator/chain.py` to
    `services/orchestrator/chain_forecast_control.py`.
  - Stable Facade: keep public `ForecastOrchestrator.orchestrate_cycle`,
    `sync_cycle_statuses`, and `cancel_active_cycle_jobs` methods on
    `services.orchestrator.chain`, forwarding through the owner module while
    preserving old monkeypatch paths for private chain helper functions.
  - Inventory/Evidence Update: refresh structural line-count inventory for
    `chain.py` and `chain_forecast_control.py`, with the owner under 1000
    lines.
  - Verification: `uv run pytest -q tests/test_orchestration_chain.py -k "orchestrate_cycle or sync_cycle_statuses or cancel_active_cycle_jobs or cancel or status"`;
    `uv run pytest -q tests/test_entropy_audit_script.py -k "compatibility_facade or structural_file_budget or chain"`;
    `uv run ruff check services/orchestrator/chain.py services/orchestrator/chain_forecast_control.py`;
    `openspec validate facade-shrink-scheduler-chain --strict --no-interactive`;
    `git diff --check`.

- [x] 2.11 Extract chain analysis orchestrator class owner slice.
  - Module/Scope: move the legacy `AnalysisOrchestrator` class wrapper from
    `services/orchestrator/chain.py` to
    `services/orchestrator/chain_analysis_orchestrator.py`, keeping analysis
    method bodies delegated to `services/orchestrator/chain_analysis.py`.
  - Stable Facade: keep `services.orchestrator.chain.AnalysisOrchestrator`
    import-compatible by aliasing the owner class and preserving the legacy
    `__module__` value.
  - Inventory/Evidence Update: refresh structural line-count inventory for
    `chain.py` and `chain_analysis_orchestrator.py`, with the owner under 1000
    lines.
  - Verification: `uv run pytest -q tests/test_analysis_pipeline.py tests/test_orchestration_chain.py -k "analysis or manifest"`;
    `uv run pytest -q tests/test_entropy_audit_script.py -k "compatibility_facade or structural_file_budget or chain"`;
    `uv run ruff check services/orchestrator/chain.py services/orchestrator/chain_analysis_orchestrator.py`;
    `openspec validate facade-shrink-scheduler-chain --strict --no-interactive`;
    `git diff --check`.
