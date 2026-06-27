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

- [x] 1.7 Extract scheduler config owner slice.
  - Module/Scope: move `ProductionSchedulerConfig` and its root/path/env
    normalization body from `services/orchestrator/scheduler.py` to
    `services/orchestrator/scheduler_config.py`.
  - Stable Facade: keep
    `services.orchestrator.scheduler.ProductionSchedulerConfig`
    import-compatible by aliasing the owner class and preserving the legacy
    `__module__` value.
  - Inventory/Evidence Update: refresh structural line-count inventory and
    record `scheduler_config.py` as the scheduler config owner.
  - Verification: `uv run pytest -q tests/test_production_scheduler.py -k "default_config_paths_created or plan_production_cli or blank_config_paths or allowed_cycle_hours or evidence_dir_symlink"`;
    `uv run pytest -q tests/test_entropy_audit_script.py -k "compatibility_facade or structural_file_budget or scheduler"`;
    `uv run ruff check services/orchestrator/scheduler.py services/orchestrator/scheduler_config.py`;
    `openspec validate facade-shrink-scheduler-chain --strict --no-interactive`;
    `git diff --check`.

- [x] 1.8 Extract scheduler type owner slice.
  - Module/Scope: move `SchedulerPassResult`, `RegisteredSchedulerModel`,
    `SchedulerCandidate`, and `_resource_profile_project_identity` from
    `services/orchestrator/scheduler.py` to
    `services/orchestrator/scheduler_types.py`.
  - Stable Facade: keep legacy scheduler type names importable from
    `services.orchestrator.scheduler` by aliasing owner definitions and
    preserving the legacy `__module__` value.
  - Inventory/Evidence Update: refresh structural line-count inventory and
    record `scheduler_types.py` as the scheduler type owner.
  - Verification: `uv run pytest -q tests/test_production_scheduler.py -k "coerce_registered_model or candidate_identity or production_identity_contract or resource_profile or SchedulerCandidate or RegisteredSchedulerModel"`;
    `uv run pytest -q tests/test_entropy_audit_script.py -k "compatibility_facade or structural_file_budget or scheduler"`;
    `uv run ruff check services/orchestrator/scheduler.py services/orchestrator/scheduler_types.py`;
    `openspec validate facade-shrink-scheduler-chain --strict --no-interactive`;
    `git diff --check`.

- [x] 1.9 Extract scheduler runtime-root/config helper owner slice.
  - Module/Scope: move scheduler runtime-root preflight, root blocker/check
    assembly, source normalization, config path/env normalization, allowed
    cycle-hour parsing, and workspace/evidence directory safety helpers from
    `services/orchestrator/scheduler.py` to
    `services/orchestrator/scheduler_runtime_roots.py`.
  - Stable Facade: keep legacy scheduler private helper names importable from
    `services.orchestrator.scheduler`, forwarding lazily to the owner module;
    owner composition calls back through the facade to preserve old monkeypatch
    paths.
  - Inventory/Evidence Update: refresh structural line-count inventory and
    record `scheduler_runtime_roots.py` as the runtime-root/config helper
    owner.
  - Verification: `uv run pytest -q tests/test_production_scheduler.py -k "default_config_paths_created or plan_production_cli or blank_config_paths or allowed_cycle_hours or evidence_dir_symlink or root_preflight or allowed_roots or runtime_roots or evidence_write or scheduler_runtime_config"`;
    `uv run pytest -q tests/test_entropy_audit_script.py -k "compatibility_facade or structural_file_budget or scheduler"`;
    `uv run ruff check services/orchestrator/scheduler.py services/orchestrator/scheduler_runtime_roots.py`;
    `openspec validate facade-shrink-scheduler-chain --strict --no-interactive`;
    `git diff --check`.

- [x] 1.10 Extract scheduler gateway/preflight orchestration owner slice.
  - Module/Scope: move Slurm preflight orchestration, gateway backend
    resolution, bounded gateway health probing, gateway availability checks,
    and gateway constants from `services/orchestrator/scheduler.py` to
    `services/orchestrator/scheduler_gateway.py`.
  - Stable Facade: keep legacy scheduler private gateway/preflight names
    importable and monkeypatch-compatible through
    `services.orchestrator.scheduler`, forwarding lazily to the owner module.
  - Inventory/Evidence Update: refresh structural line-count inventory and
    record `scheduler_gateway.py` as the scheduler gateway/preflight
    orchestration owner.
  - Verification: `uv run pytest -q tests/test_production_scheduler.py tests/test_cli_publish_qdown.py tests/test_run_qhh_continuous.py -k "slurm_gateway or slurm_preflight or grib_env or database_url or database_host"`;
    `uv run pytest -q tests/test_entropy_audit_script.py -k "compatibility_facade or structural_file_budget or scheduler"`;
    `uv run ruff check services/orchestrator/scheduler.py services/orchestrator/scheduler_gateway.py`;
    `openspec validate facade-shrink-scheduler-chain --strict --no-interactive`;
    `git diff --check`.

- [x] 1.11 Extract scheduler adapter/provider owner slice.
  - Module/Scope: move scheduler adapter/provider protocols, canonical
    readiness fallback/provider construction, default source adapters, forcing
    producer construction, and repository factories from
    `services/orchestrator/scheduler.py` to
    `services/orchestrator/scheduler_adapters.py`.
  - Stable Facade: keep legacy scheduler adapter/provider names importable and
    monkeypatch-compatible through `services.orchestrator.scheduler`.
  - Inventory/Evidence Update: refresh structural line-count inventory and
    record `scheduler_adapters.py` as the scheduler adapter/provider owner.
  - Verification: `uv run pytest -q tests/test_production_scheduler.py tests/test_qhh_production_bootstrap.py -k "from_env or default_adapters or active_repository_from_env or canonical_readiness_provider_from_env or forcing_producer_from_env or MetStoreCanonicalReadinessProvider or canonical_readiness"`;
    `uv run pytest -q tests/test_entropy_audit_script.py -k "compatibility_facade or structural_file_budget or scheduler"`;
    `uv run ruff check services/orchestrator/scheduler.py services/orchestrator/scheduler_adapters.py`;
    `openspec validate facade-shrink-scheduler-chain --strict --no-interactive`;
    `git diff --check`.

- [x] 1.12 Extract scheduler model-discovery method body.
  - Module/Scope: move the `ProductionScheduler._discover_models` method body
    from `services/orchestrator/scheduler.py` to
    `services/orchestrator/scheduler_models.py`, alongside active model
    pagination, coercion, duplicate detection, and filter helpers.
  - Stable Facade: keep `ProductionScheduler._discover_models` callable
    through `services.orchestrator.scheduler`, forwarding to the owner module
    while owner composition calls legacy scheduler helper names.
  - Inventory/Evidence Update: refresh structural line-count inventory and
    record the method forwarder under the scheduler model-discovery group.
  - Verification: `uv run pytest -q tests/test_production_scheduler.py -k "coerce_registered_model or discover_models or active_model or model_limit or duplicate_active_model or filter"`;
    `uv run pytest -q tests/test_entropy_audit_script.py -k "compatibility_facade or structural_file_budget or scheduler"`;
    `uv run ruff check services/orchestrator/scheduler.py services/orchestrator/scheduler_models.py`;
    `openspec validate facade-shrink-scheduler-chain --strict --no-interactive`;
    `git diff --check`.

- [x] 1.13 Extract scheduler state compatibility installer.
  - Module/Scope: move scheduler-state monkeypatch wrapper installation,
    wrapper-set drift checks, and state re-export parity checks from
    `services/orchestrator/scheduler.py` to
    `services/orchestrator/scheduler_state_compat.py`.
  - Stable Facade: keep legacy `_SCHEDULER_STATE_COMPAT_*` names and old
    scheduler private state helper monkeypatch paths available through
    `services.orchestrator.scheduler`.
  - Inventory/Evidence Update: refresh structural line-count inventory and
    record `scheduler_state_compat.py` as the state compatibility installer
    owner.
  - Verification: `uv run pytest -q tests/test_production_scheduler.py -k "candidate_state_decision_scheduler_monkeypatch or owner_module_matches_scheduler_facade or bounded_evidence_owner_module_matches_scheduler_facade"`;
    `uv run pytest -q tests/test_entropy_audit_script.py -k "compatibility_facade or structural_file_budget or scheduler"`;
    `uv run ruff check services/orchestrator/scheduler.py services/orchestrator/scheduler_state_compat.py`;
    `openspec validate facade-shrink-scheduler-chain --strict --no-interactive`;
    `git diff --check`.

- [x] 1.14 Extract scheduler evidence payload/proof owner slice.
  - Module/Scope: move scheduler evidence JSON serialization, bounded payload
    fitting, retained-field compaction, execution/write proof calculation,
    Slurm status-sync proof calculation, cancellation proof calculation, and
    mutation/no-mutation proof helpers from
    `services/orchestrator/scheduler_evidence.py` to
    `services/orchestrator/scheduler_evidence_payload.py` and
    `services/orchestrator/scheduler_evidence_proofs.py`.
  - Stable Facade: keep the old `services.orchestrator.scheduler_evidence`
    API and the existing `services.orchestrator.scheduler` evidence helper
    forwarders import-compatible; payload fitting still resolves the
    `scheduler_evidence.bounded_evidence_payload` callback path lazily.
  - Inventory/Evidence Update: refresh structural line-count inventory and
    record payload/proof owner modules in scheduler compatibility coverage.
  - Verification: `uv run pytest -q tests/test_production_scheduler.py -k "evidence or mutation_proof or slurm_status_sync or cancellation or runtime_config"`;
    `uv run pytest -q tests/test_entropy_audit_script.py -k "compatibility_facade or structural_file_budget or scheduler"`;
    `uv run ruff check services/orchestrator/scheduler_evidence.py services/orchestrator/scheduler_evidence_payload.py services/orchestrator/scheduler_evidence_proofs.py`;
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

- [x] 2.12 Extract chain config/source/error owner slice.
  - Module/Scope: move source scenario mapping, orchestration error classes,
    `OrchestratorConfig`, and `_env_flag` from
    `services/orchestrator/chain.py` to
    `services/orchestrator/chain_config.py`.
  - Stable Facade: keep legacy chain import paths for config, source mapping,
    and orchestration errors by aliasing owner definitions and preserving
    legacy class `__module__` values.
  - Inventory/Evidence Update: refresh structural line-count inventory for
    `chain.py` and `chain_config.py`, with the owner under 1000 lines.
  - Verification: `uv run pytest -q tests/test_orchestrator.py tests/test_analysis_pipeline.py -k "config or already_active or PipelineAlreadyActive"`;
    `uv run pytest -q tests/test_ifs_forecast_integration.py tests/test_source_identity.py tests/test_orchestration_chain.py tests/test_production_scheduler.py -k "scenario or http_slurm_gateway_client or package or require_forecast_warm_start or SlurmClientError"`;
    `uv run pytest -q tests/test_entropy_audit_script.py -k "compatibility_facade or structural_file_budget or chain"`;
    `uv run ruff check services/orchestrator/chain.py services/orchestrator/chain_config.py`;
    `openspec validate facade-shrink-scheduler-chain --strict --no-interactive`;
    `git diff --check`.

- [x] 2.13 Extract chain manifest contract helper owner slice.
  - Module/Scope: move manifest contract, quality-state, residual-blocker,
    payload serialization, URI, project-name, basin identity, nested mapping,
    optional integer, checkpoint, gateway-time, and UTC formatting helpers from
    `services/orchestrator/chain_manifests.py` to
    `services/orchestrator/chain_manifest_contracts.py`.
  - Stable Facade: keep legacy helper imports available through
    `services.orchestrator.chain_manifests`, with `services.orchestrator.chain`
    continuing to alias and forward through `chain_manifests`.
  - Inventory/Evidence Update: refresh chain compatibility and structural
    line-count inventories for `chain_manifests.py` and
    `chain_manifest_contracts.py`, with both files under 1000 lines.
  - Verification: `uv run pytest -q tests/test_orchestration_chain.py tests/test_warm_start_chaining.py tests/test_analysis_pipeline.py -k "manifest or assembly or production_status or warm_start"`;
    `uv run pytest -q tests/test_entropy_audit_script.py -k "compatibility_facade or structural_file_budget or chain"`;
    `uv run ruff check services/orchestrator/chain_manifests.py services/orchestrator/chain_manifest_contracts.py`;
    `openspec validate facade-shrink-scheduler-chain --strict --no-interactive`;
    `git diff --check`.
