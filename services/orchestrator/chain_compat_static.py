# ruff: noqa: E402,E501,F401,I001
from __future__ import annotations

from services.orchestrator import chain as _chain

_CHAIN_MANIFEST_COMPAT_CHAIN_MANIFEST_ALIAS_NAMES = (
    "ANALYSIS_SCENARIO_ID",
    "DEFAULT_ERA5_REANALYSIS_LATENCY_MINUTES",
    "FORCING_CAUSALITY_CAUSAL",
    "FORCING_CAUSALITY_DELAYED_REANALYSIS",
    "ManifestValidationError",
    "PRODUCTION_CONTRACT_ID",
    "PRODUCTION_CONTRACT_SCHEMA_VERSION",
    "_analysis_forcing_causality",
    "_analysis_update_ic_step_minutes",
    "_assembly_from_entry",
    "_assembly_payload_from_runtime_manifest",
    "_assembly_quality_states",
    "_cycle_residual_blockers",
    "_default_forcing_uri",
    "_directory_uri",
    "_display_contract",
    "_ensure_segment_utc",
    "_era5_reanalysis_latency_minutes",
    "_forecast_state_checkpoint_hours",
    "_has_uri_scheme",
    "_model_package_manifest_uri",
    "_model_run_stage_evidence",
    "_nested_value",
    "_output_river_contract",
    "_preserve_directory_uri",
    "_project_name_for_basin",
    "_safe_project_name",
    "_station_metadata_for_basin",
    "_tri_state",
    "build_reindexed_manifest",
    "production_stage_for",
    "serialize_manifest_index",
)
_CHAIN_MANIFEST_COMPAT_PRODUCTION_CONTRACT_ALIAS_NAMES = ("production_status_for",)
_CHAIN_MANIFEST_COMPAT_ALIAS_NAMES = (
    *_CHAIN_MANIFEST_COMPAT_CHAIN_MANIFEST_ALIAS_NAMES,
    *_CHAIN_MANIFEST_COMPAT_PRODUCTION_CONTRACT_ALIAS_NAMES,
)
_CHAIN_MANIFEST_COMPAT_ALIAS_OWNER_MISSING = tuple(
    (name for name in _CHAIN_MANIFEST_COMPAT_CHAIN_MANIFEST_ALIAS_NAMES if not hasattr(_chain.chain_manifests, name))
)
_CHAIN_MANIFEST_COMPAT_ALIAS_OWNER_MISSING += tuple(
    (
        name
        for name in _CHAIN_MANIFEST_COMPAT_PRODUCTION_CONTRACT_ALIAS_NAMES
        if not hasattr(_chain.production_contract, name)
    )
)
_CHAIN_MANIFEST_COMPAT_ALIAS_FACADE_MISSING = tuple(
    (name for name in _CHAIN_MANIFEST_COMPAT_ALIAS_NAMES if name not in _chain.__dict__)
)
if _CHAIN_MANIFEST_COMPAT_ALIAS_OWNER_MISSING:
    raise RuntimeError(
        f"chain manifest compatibility aliases missing from owner modules: {', '.join(_CHAIN_MANIFEST_COMPAT_ALIAS_OWNER_MISSING)}"
    )
if _CHAIN_MANIFEST_COMPAT_ALIAS_FACADE_MISSING:
    raise RuntimeError(
        f"chain manifest compatibility aliases missing from facade: {', '.join(_CHAIN_MANIFEST_COMPAT_ALIAS_FACADE_MISSING)}"
    )
_CHAIN_MANIFEST_COMPAT_OWNER_ALIASES = _chain.MappingProxyType(
    {
        **{name: getattr(_chain.chain_manifests, name) for name in _CHAIN_MANIFEST_COMPAT_CHAIN_MANIFEST_ALIAS_NAMES},
        **{
            name: getattr(_chain.production_contract, name)
            for name in _CHAIN_MANIFEST_COMPAT_PRODUCTION_CONTRACT_ALIAS_NAMES
        },
    }
)
_CHAIN_MANIFEST_COMPAT_FACADE_ALIASES = _chain.MappingProxyType(
    {name: _chain.__dict__[name] for name in _CHAIN_MANIFEST_COMPAT_ALIAS_NAMES}
)
for _chain_manifest_alias_name, _chain_manifest_owner_value in _CHAIN_MANIFEST_COMPAT_OWNER_ALIASES.items():
    if _CHAIN_MANIFEST_COMPAT_FACADE_ALIASES[_chain_manifest_alias_name] is not _chain_manifest_owner_value:
        raise RuntimeError(f"chain manifest direct alias drifted from owner module: {_chain_manifest_alias_name}")
del _chain_manifest_alias_name, _chain_manifest_owner_value
_CHAIN_MANIFEST_COMPAT_TOP_LEVEL_FORWARDER_NAMES = (
    "build_model_run_assembly",
    "_publish_quality_state",
)
_CHAIN_MANIFEST_COMPAT_TOP_LEVEL_OWNER_FUNCTION_NAMES = (
    "build_model_run_assembly",
    "_publish_quality_state",
)
_CHAIN_MANIFEST_COMPAT_FORECAST_METHOD_FORWARDER_NAMES = (
    "_build_cycle_stage_manifest",
    "_write_cycle_manifest_index",
    "_prepare_forecast_runtime_manifests",
    "_build_forecast_runtime_manifest",
    "_validate_forecast_runtime_manifest",
    "_reindexed_manifest_entries",
    "_build_run_manifest",
    "_write_run_manifest",
)
_CHAIN_MANIFEST_COMPAT_FORECAST_METHOD_OWNER_FUNCTION_NAMES = (
    "build_cycle_stage_manifest",
    "write_cycle_manifest_index",
    "prepare_forecast_runtime_manifests",
    "build_forecast_runtime_manifest",
    "validate_forecast_runtime_manifest",
    "reindexed_manifest_entries",
    "build_forecast_run_manifest",
    "write_run_manifest",
)
_CHAIN_MANIFEST_COMPAT_ANALYSIS_METHOD_FORWARDER_NAMES = ("_build_run_manifest",)
_CHAIN_MANIFEST_COMPAT_ANALYSIS_METHOD_OWNER_FUNCTION_NAMES = ("build_analysis_run_manifest",)
_CHAIN_MANIFEST_COMPAT_DEPENDENCY_BINDINGS = _chain.MappingProxyType(
    {
        "build_model_run_assembly": (
            ("default_forcing_uri", "_default_forcing_uri"),
            ("preserve_directory_uri", "_preserve_directory_uri"),
            ("station_metadata_for_basin", "_station_metadata_for_basin"),
            ("output_river_contract", "_output_river_contract"),
            ("display_contract", "_display_contract"),
            ("assembly_quality_states", "_assembly_quality_states"),
            ("project_name_for_basin", "_project_name_for_basin"),
            ("model_package_manifest_uri", "_model_package_manifest_uri"),
        ),
        "_publish_quality_state": (("model_run_stage_evidence", "_model_run_stage_evidence"),),
        "ForecastOrchestrator._build_cycle_stage_manifest": (
            ("model_run_stage_evidence", "_model_run_stage_evidence"),
            ("publish_quality_state", "_publish_quality_state"),
            ("cycle_residual_blockers", "_cycle_residual_blockers"),
        ),
        "ForecastOrchestrator._prepare_forecast_runtime_manifests": (
            ("assembly_payload_from_runtime_manifest", "_assembly_payload_from_runtime_manifest"),
        ),
        "ForecastOrchestrator._build_forecast_runtime_manifest": (
            ("assembly_builder", "build_model_run_assembly"),
            ("forecast_state_checkpoint_hours", "_forecast_state_checkpoint_hours"),
        ),
        "ForecastOrchestrator._reindexed_manifest_entries": (
            ("reindex_builder", "build_reindexed_manifest"),
            ("assembly_builder", "build_model_run_assembly"),
        ),
        "ForecastOrchestrator._build_run_manifest": (
            ("forecast_state_checkpoint_hours", "_forecast_state_checkpoint_hours"),
        ),
        "AnalysisOrchestrator._build_run_manifest": (
            ("analysis_forcing_causality", "_analysis_forcing_causality"),
            ("analysis_update_ic_step_minutes", "_analysis_update_ic_step_minutes"),
        ),
    }
)
_CHAIN_MANIFEST_COMPAT_OWNER_FUNCTION_NAMES = tuple(
    dict.fromkeys(
        (
            *_CHAIN_MANIFEST_COMPAT_TOP_LEVEL_OWNER_FUNCTION_NAMES,
            *_CHAIN_MANIFEST_COMPAT_FORECAST_METHOD_OWNER_FUNCTION_NAMES,
            *_CHAIN_MANIFEST_COMPAT_ANALYSIS_METHOD_OWNER_FUNCTION_NAMES,
        )
    )
)
_CHAIN_MANIFEST_COMPAT_OWNER_FUNCTION_MISSING = tuple(
    (name for name in _CHAIN_MANIFEST_COMPAT_OWNER_FUNCTION_NAMES if not hasattr(_chain.chain_manifests, name))
)
if _CHAIN_MANIFEST_COMPAT_OWNER_FUNCTION_MISSING:
    raise RuntimeError(
        f"chain manifest compatibility functions missing from owner module: {', '.join(_CHAIN_MANIFEST_COMPAT_OWNER_FUNCTION_MISSING)}"
    )
_CHAIN_RESERVATION_COMPAT_ALIAS_NAMES = (
    "ReservationResult",
    "bind_reservation",
    "reserve_candidate",
    "slurm_comment_for",
)
_CHAIN_RESERVATION_COMPAT_ALIAS_OWNER_MISSING = tuple(
    (name for name in _CHAIN_RESERVATION_COMPAT_ALIAS_NAMES if not hasattr(_chain.reservation, name))
)
_CHAIN_RESERVATION_COMPAT_ALIAS_FACADE_MISSING = tuple(
    (name for name in _CHAIN_RESERVATION_COMPAT_ALIAS_NAMES if name not in _chain.__dict__)
)
if _CHAIN_RESERVATION_COMPAT_ALIAS_OWNER_MISSING:
    raise RuntimeError(
        f"chain reservation compatibility aliases missing from owner module: {', '.join(_CHAIN_RESERVATION_COMPAT_ALIAS_OWNER_MISSING)}"
    )
if _CHAIN_RESERVATION_COMPAT_ALIAS_FACADE_MISSING:
    raise RuntimeError(
        f"chain reservation compatibility aliases missing from facade: {', '.join(_CHAIN_RESERVATION_COMPAT_ALIAS_FACADE_MISSING)}"
    )
_CHAIN_RESERVATION_COMPAT_OWNER_ALIASES = _chain.MappingProxyType(
    {name: getattr(_chain.reservation, name) for name in _CHAIN_RESERVATION_COMPAT_ALIAS_NAMES}
)
_CHAIN_RESERVATION_COMPAT_FACADE_ALIASES = _chain.MappingProxyType(
    {name: _chain.__dict__[name] for name in _CHAIN_RESERVATION_COMPAT_ALIAS_NAMES}
)
for _chain_reservation_alias_name, _chain_reservation_owner_value in _CHAIN_RESERVATION_COMPAT_OWNER_ALIASES.items():
    if _CHAIN_RESERVATION_COMPAT_FACADE_ALIASES[_chain_reservation_alias_name] is not _chain_reservation_owner_value:
        raise RuntimeError(f"chain reservation direct alias drifted from owner module: {_chain_reservation_alias_name}")
del _chain_reservation_alias_name, _chain_reservation_owner_value
_CHAIN_RESERVATION_COMPAT_OWNER_METHOD_FORWARDER_NAMES = ("_reserve_cycle_stage", "_bind_cycle_stage_reservation")
_CHAIN_RESERVATION_COMPAT_OWNER_METHOD_OWNER_FUNCTION_NAMES = ("reserve_candidate", "bind_reservation")
_CHAIN_RESERVATION_COMPAT_LOCAL_METHOD_NAMES = ("_reservation_already_inflight",)
_CHAIN_RESERVATION_COMPAT_METHOD_FORWARDER_NAMES = (
    *_CHAIN_RESERVATION_COMPAT_OWNER_METHOD_FORWARDER_NAMES,
    *_CHAIN_RESERVATION_COMPAT_LOCAL_METHOD_NAMES,
)
_CHAIN_RESERVATION_COMPAT_LOCAL_BINDING_NAMES = ("_cycle_stage_idempotency_key",)
_CHAIN_RESERVATION_COMPAT_STAGE_EXECUTION_DEPENDENCY_BINDINGS = (
    ("cycle_stage_idempotency_key", "_cycle_stage_idempotency_key"),
    ("slurm_comment_for", "slurm_comment_for"),
)
_CHAIN_RESERVATION_COMPAT_OWNER_FUNCTION_MISSING = tuple(
    (
        name
        for name in _CHAIN_RESERVATION_COMPAT_OWNER_METHOD_OWNER_FUNCTION_NAMES
        if not hasattr(_chain.reservation, name)
    )
)
if _CHAIN_RESERVATION_COMPAT_OWNER_FUNCTION_MISSING:
    raise RuntimeError(
        f"chain reservation compatibility functions missing from owner module: {', '.join(_CHAIN_RESERVATION_COMPAT_OWNER_FUNCTION_MISSING)}"
    )
_CHAIN_PERSISTENCE_REPOSITORY_COMPAT_ALIAS_NAMES = ("PipelineJob", "PipelineEvent", "PipelineStore")
_CHAIN_PERSISTENCE_REPOSITORY_COMPAT_ALIAS_OWNER_MISSING = tuple(
    (name for name in _CHAIN_PERSISTENCE_REPOSITORY_COMPAT_ALIAS_NAMES if not hasattr(_chain.persistence, name))
)
_CHAIN_PERSISTENCE_REPOSITORY_COMPAT_ALIAS_FACADE_MISSING = tuple(
    (name for name in _CHAIN_PERSISTENCE_REPOSITORY_COMPAT_ALIAS_NAMES if name not in _chain.__dict__)
)
if _CHAIN_PERSISTENCE_REPOSITORY_COMPAT_ALIAS_OWNER_MISSING:
    raise RuntimeError(
        f"chain persistence compatibility aliases missing from owner module: {', '.join(_CHAIN_PERSISTENCE_REPOSITORY_COMPAT_ALIAS_OWNER_MISSING)}"
    )
if _CHAIN_PERSISTENCE_REPOSITORY_COMPAT_ALIAS_FACADE_MISSING:
    raise RuntimeError(
        f"chain persistence compatibility aliases missing from facade: {', '.join(_CHAIN_PERSISTENCE_REPOSITORY_COMPAT_ALIAS_FACADE_MISSING)}"
    )
_CHAIN_PERSISTENCE_REPOSITORY_COMPAT_OWNER_ALIASES = _chain.MappingProxyType(
    {name: getattr(_chain.persistence, name) for name in _CHAIN_PERSISTENCE_REPOSITORY_COMPAT_ALIAS_NAMES}
)
_CHAIN_PERSISTENCE_REPOSITORY_COMPAT_FACADE_ALIASES = _chain.MappingProxyType(
    {name: _chain.__dict__[name] for name in _CHAIN_PERSISTENCE_REPOSITORY_COMPAT_ALIAS_NAMES}
)
_CHAIN_PERSISTENCE_REPOSITORY_COMPAT_ALIAS_DRIFT = tuple(
    (
        name
        for name in _CHAIN_PERSISTENCE_REPOSITORY_COMPAT_ALIAS_NAMES
        if _CHAIN_PERSISTENCE_REPOSITORY_COMPAT_FACADE_ALIASES[name]
        is not _CHAIN_PERSISTENCE_REPOSITORY_COMPAT_OWNER_ALIASES[name]
    )
)
if _CHAIN_PERSISTENCE_REPOSITORY_COMPAT_ALIAS_DRIFT:
    raise RuntimeError(
        f"chain persistence direct alias drifted from owner module: {', '.join(_CHAIN_PERSISTENCE_REPOSITORY_COMPAT_ALIAS_DRIFT)}"
    )
_CHAIN_PERSISTENCE_REPOSITORY_COMPAT_LOCAL_REPOSITORY_NAMES = (
    "OrchestratorRepository",
    "PsycopgOrchestratorRepository",
)
_CHAIN_PERSISTENCE_REPOSITORY_COMPAT_CHAIN_LOCAL_CLASSIFICATIONS = _chain.MappingProxyType(
    {"OrchestratorRepository": "chain-local-protocol", "PsycopgOrchestratorRepository": "chain-local-implementation"}
)
_CHAIN_PERSISTENCE_REPOSITORY_COMPAT_PROTOCOL_METHOD_NAMES = (
    "has_active_orchestration",
    "has_active_pipeline",
    "has_active_analysis_run",
    "load_model_context",
    "find_forcing_context",
    "ensure_forecast_cycle",
    "create_hydro_run",
    "create_hydro_run_from_basin",
    "update_hydro_run_status",
    "upsert_pipeline_job",
    "reserve_pipeline_job",
    "reclaim_pipeline_job_reservation",
    "bind_pipeline_job_reservation",
    "query_candidate_state",
    "update_pipeline_job_status",
    "get_pipeline_job",
    "query_pipeline_jobs_by_cycle",
    "query_pipeline_jobs_by_run",
    "query_pipeline_job_by_slurm_id",
    "insert_pipeline_event",
    "update_forecast_cycle_status",
    "list_stage_statuses",
)
_CHAIN_PERSISTENCE_REPOSITORY_COMPAT_IMPLEMENTATION_METHOD_NAMES = (
    "from_env",
    "has_active_orchestration",
    "has_active_pipeline",
    "has_completed_pipeline",
    "candidate_state",
    "active_slurm_jobs",
    "has_active_analysis_run",
    "list_canonical_ready_cycles",
    "list_forecast_model_ids",
    "load_model_context",
    "find_forcing_context",
    "ensure_forecast_cycle",
    "create_hydro_run",
    "create_hydro_run_from_basin",
    "update_hydro_run_status",
    "upsert_pipeline_job",
    "reserve_pipeline_job",
    "reclaim_pipeline_job_reservation",
    "bind_pipeline_job_reservation",
    "query_candidate_state",
    "update_pipeline_job_status",
    "get_pipeline_job",
    "query_pipeline_jobs_by_cycle",
    "query_pipeline_jobs_by_run",
    "query_pipeline_job_by_slurm_id",
    "insert_pipeline_event",
    "update_forecast_cycle_status",
    "list_stage_statuses",
)
_CHAIN_PERSISTENCE_REPOSITORY_COMPAT_LEGACY_IMPORT_TOKENS = _chain.MappingProxyType(
    {
        "services/orchestrator/scheduler_adapters.py": (
            "from services.orchestrator.chain import PsycopgOrchestratorRepository",
            "PsycopgOrchestratorRepository",
            "return PsycopgOrchestratorRepository.from_env()",
        )
    }
)
_CHAIN_PERSISTENCE_REPOSITORY_COMPAT_LEGACY_IMPORT_FUNCTION_TOKENS = _chain.MappingProxyType(
    {
        "services/orchestrator/scheduler_adapters.py": _chain.MappingProxyType(
            {
                "_active_repository_from_env": (
                    "from services.orchestrator.chain import PsycopgOrchestratorRepository",
                    "return PsycopgOrchestratorRepository.from_env()",
                ),
                "_orchestrator_repository_from_env": (
                    "from services.orchestrator.chain import PsycopgOrchestratorRepository",
                    "return PsycopgOrchestratorRepository.from_env()",
                ),
            }
        )
    }
)
_CHAIN_RETRY_COMPAT_ALIAS_NAMES = ("RetryConfig", "RetryService", "compute_backoff_seconds")
_CHAIN_RETRY_COMPAT_ALIAS_OWNER_MISSING = tuple(
    (name for name in _CHAIN_RETRY_COMPAT_ALIAS_NAMES if not hasattr(_chain.retry, name))
)
_CHAIN_RETRY_COMPAT_ALIAS_FACADE_MISSING = tuple(
    (name for name in _CHAIN_RETRY_COMPAT_ALIAS_NAMES if name not in _chain.__dict__)
)
if _CHAIN_RETRY_COMPAT_ALIAS_OWNER_MISSING:
    raise RuntimeError(
        f"chain retry compatibility aliases missing from owner module: {', '.join(_CHAIN_RETRY_COMPAT_ALIAS_OWNER_MISSING)}"
    )
if _CHAIN_RETRY_COMPAT_ALIAS_FACADE_MISSING:
    raise RuntimeError(
        f"chain retry compatibility aliases missing from facade: {', '.join(_CHAIN_RETRY_COMPAT_ALIAS_FACADE_MISSING)}"
    )
_CHAIN_RETRY_COMPAT_OWNER_ALIASES = _chain.MappingProxyType(
    {name: getattr(_chain.retry, name) for name in _CHAIN_RETRY_COMPAT_ALIAS_NAMES}
)
_CHAIN_RETRY_COMPAT_FACADE_ALIASES = _chain.MappingProxyType(
    {name: _chain.__dict__[name] for name in _CHAIN_RETRY_COMPAT_ALIAS_NAMES}
)
_CHAIN_RETRY_COMPAT_ALIAS_DRIFT = tuple(
    (
        name
        for name in _CHAIN_RETRY_COMPAT_ALIAS_NAMES
        if _CHAIN_RETRY_COMPAT_FACADE_ALIASES[name] is not _CHAIN_RETRY_COMPAT_OWNER_ALIASES[name]
    )
)
if _CHAIN_RETRY_COMPAT_ALIAS_DRIFT:
    raise RuntimeError(
        f"chain retry direct alias drifted from owner module: {', '.join(_CHAIN_RETRY_COMPAT_ALIAS_DRIFT)}"
    )
_CHAIN_RETRY_COMPAT_CONSTRUCTOR_PARAM_NAMES = ("retry_service",)
_CHAIN_RETRY_COMPAT_INSTANCE_CONFIG_NAMES = ("retry_config",)
_CHAIN_RETRY_COMPAT_LOCAL_METHOD_NAMES = (
    "_schedule_cycle_stage_retry",
    "_retry_job_for_stage_result",
    "_retry_partial_array_stage",
    "_release_retry_store_transaction",
)
_CHAIN_RETRY_COMPAT_LOCAL_FACTORY_NAMES = ("_retry_service_from_env",)
_CHAIN_RETRY_COMPAT_CHAIN_LOCAL_CLASSIFICATIONS = _chain.MappingProxyType(
    {
        **{name: "chain-local-bridge" for name in _CHAIN_RETRY_COMPAT_LOCAL_METHOD_NAMES},
        **{name: "chain-local-factory" for name in _CHAIN_RETRY_COMPAT_LOCAL_FACTORY_NAMES},
    }
)
_CHAIN_STAGE_CATALOG_COMPAT_REEXPORT_NAMES = ("ANALYSIS_STAGES", "LEGACY_FORECAST_STAGES", "M3_STAGES", "STAGES")
_CHAIN_TYPE_COMPAT_REEXPORT_NAMES = (
    "AnalysisRunContext",
    "ArrayAggregation",
    "ArrayTaskResult",
    "CycleOrchestrationContext",
    "DisplayLogPublication",
    "DisplayLogPublicationAttempt",
    "ForcingContext",
    "ForecastRunContext",
    "InitialStateSelection",
    "ModelContext",
    "ModelRunAssembly",
    "OrchestratorError",
    "PipelineResult",
    "StageDefinition",
    "StageRunResult",
    "TerminalJobObservation",
)
_CHAIN_STAGE_CATALOG_TYPE_COMPAT_REEXPORT_NAMES = (
    *_CHAIN_STAGE_CATALOG_COMPAT_REEXPORT_NAMES,
    *_CHAIN_TYPE_COMPAT_REEXPORT_NAMES,
)
_CHAIN_STAGE_CATALOG_TYPE_COMPAT_REEXPORT_MISSING = tuple(
    (name for name in _CHAIN_STAGE_CATALOG_COMPAT_REEXPORT_NAMES if not hasattr(_chain._chain_stages_module, name))
)
_CHAIN_STAGE_CATALOG_TYPE_COMPAT_REEXPORT_MISSING += tuple(
    (name for name in _CHAIN_TYPE_COMPAT_REEXPORT_NAMES if not hasattr(_chain._chain_types_module, name))
)
_CHAIN_STAGE_CATALOG_TYPE_COMPAT_FACADE_MISSING = tuple(
    (name for name in _CHAIN_STAGE_CATALOG_TYPE_COMPAT_REEXPORT_NAMES if name not in _chain.__dict__)
)
if _CHAIN_STAGE_CATALOG_TYPE_COMPAT_REEXPORT_MISSING:
    raise RuntimeError(
        f"chain stage catalog/type compatibility names missing from owner modules: {', '.join(_CHAIN_STAGE_CATALOG_TYPE_COMPAT_REEXPORT_MISSING)}"
    )
if _CHAIN_STAGE_CATALOG_TYPE_COMPAT_FACADE_MISSING:
    raise RuntimeError(
        f"chain stage catalog/type compatibility names missing from facade: {', '.join(_CHAIN_STAGE_CATALOG_TYPE_COMPAT_FACADE_MISSING)}"
    )
if set(getattr(_chain._chain_stages_module, "__all__", ())) != set(_CHAIN_STAGE_CATALOG_COMPAT_REEXPORT_NAMES):
    raise RuntimeError("chain stage catalog compatibility names drifted from owner __all__")
if set(getattr(_chain._chain_types_module, "__all__", ())) != set(_CHAIN_TYPE_COMPAT_REEXPORT_NAMES):
    raise RuntimeError("chain type compatibility names drifted from owner __all__")
_CHAIN_STAGE_CATALOG_TYPE_COMPAT_OWNER_REEXPORTS = _chain.MappingProxyType(
    {
        **{name: getattr(_chain._chain_stages_module, name) for name in _CHAIN_STAGE_CATALOG_COMPAT_REEXPORT_NAMES},
        **{name: getattr(_chain._chain_types_module, name) for name in _CHAIN_TYPE_COMPAT_REEXPORT_NAMES},
    }
)
_CHAIN_STAGE_CATALOG_TYPE_COMPAT_FACADE_REEXPORTS = _chain.MappingProxyType(
    {name: _chain.__dict__[name] for name in _CHAIN_STAGE_CATALOG_TYPE_COMPAT_REEXPORT_NAMES}
)
for (
    _chain_stage_type_direct_name,
    _chain_stage_type_owner_value,
) in _CHAIN_STAGE_CATALOG_TYPE_COMPAT_OWNER_REEXPORTS.items():
    if (
        _CHAIN_STAGE_CATALOG_TYPE_COMPAT_FACADE_REEXPORTS[_chain_stage_type_direct_name]
        is not _chain_stage_type_owner_value
    ):
        raise RuntimeError(
            f"chain stage catalog/type direct re-export drifted from owner module: {_chain_stage_type_direct_name}"
        )
del _chain_stage_type_direct_name, _chain_stage_type_owner_value
_CHAIN_STAGE_CATALOG_TYPE_COMPAT_EXPORTS = tuple(
    (
        _CHAIN_STAGE_CATALOG_TYPE_COMPAT_FACADE_REEXPORTS[name]
        for name in _CHAIN_STAGE_CATALOG_TYPE_COMPAT_REEXPORT_NAMES
    )
)
_CHAIN_STAGE_EXECUTION_COMPAT_FORWARDER_NAMES = (
    "_submit_and_wait_cycle_stage",
    "_run_local_publish_stage",
    "_resume_cycle_stage",
    "_poll_cycle_stage_until_terminal",
    "_record_cycle_stage_poll_timeout",
    "_submit_array_stage",
    "_slurm_submission_manifest",
)
_CHAIN_STAGE_EXECUTION_COMPAT_OWNER_FUNCTION_NAMES = (
    "submit_and_wait_cycle_stage",
    "run_local_publish_stage",
    "resume_cycle_stage",
    "poll_cycle_stage_until_terminal",
    "record_cycle_stage_poll_timeout",
    "submit_array_stage",
    "slurm_submission_manifest",
)
_CHAIN_STAGE_EXECUTION_COMPAT_DEPENDENCY_FIELDS = (
    "terminal_job_statuses",
    "pipeline_job_id",
    "published_artifact_root_configured",
    "cycle_stage_idempotency_key",
    "slurm_comment_for",
    "cycle_payload_model_id",
    "cycle_pipeline_job_model_id",
    "coerce_mapping",
    "coerce_array_task_id",
    "status_from_gateway_job",
    "parse_gateway_time",
    "utcnow",
    "format_time",
    "safe_pipeline_event_details",
    "submission_runtime_root_contract",
    "aggregation_error_code",
    "aggregation_error_message",
    "slurm_accounting_from_payload",
    "resource_metrics_from_payload",
    "stage_task_result_evidence",
    "stage_status_message",
    "make_slurm_client_error",
    "tile_publisher_cls",
    "publish_error_cls",
    "failure_payload",
    "redact_payload",
)
_CHAIN_STAGE_EXECUTION_COMPAT_OWNER_MISSING = tuple(
    (
        name
        for name in _CHAIN_STAGE_EXECUTION_COMPAT_OWNER_FUNCTION_NAMES
        if not hasattr(_chain.chain_stage_execution, name)
    )
)
if _CHAIN_STAGE_EXECUTION_COMPAT_OWNER_MISSING:
    raise RuntimeError(
        f"chain stage execution compatibility names missing from owner module: {', '.join(_CHAIN_STAGE_EXECUTION_COMPAT_OWNER_MISSING)}"
    )
if set(getattr(_chain.chain_stage_execution, "__all__", ())) != {
    "StageExecutionDependencies",
    *_CHAIN_STAGE_EXECUTION_COMPAT_OWNER_FUNCTION_NAMES,
}:
    raise RuntimeError("chain stage execution compatibility names drifted from owner __all__")
if (
    tuple(_chain.chain_stage_execution.StageExecutionDependencies.__dataclass_fields__)
    != _CHAIN_STAGE_EXECUTION_COMPAT_DEPENDENCY_FIELDS
):
    raise RuntimeError("chain stage execution dependency fields drifted from compatibility fixture")
_CHAIN_STAGE_EXECUTION_COMPAT_FORWARDERS = _chain.MappingProxyType(
    {
        facade_name: getattr(_chain.chain_stage_execution, owner_name)
        for facade_name, owner_name in zip(
            _CHAIN_STAGE_EXECUTION_COMPAT_FORWARDER_NAMES,
            _CHAIN_STAGE_EXECUTION_COMPAT_OWNER_FUNCTION_NAMES,
            strict=True,
        )
    }
)
_CHAIN_TILE_PUBLISHER_COMPAT_ALIAS_NAMES = ("TilePublisher", "PublishError")
_CHAIN_TILE_PUBLISHER_COMPAT_FAILURE_FUNCTION_NAMES = ("failure_payload",)
_CHAIN_TILE_PUBLISHER_COMPAT_STAGE_EXECUTION_DEPENDENCY_FIELDS = (
    "tile_publisher_cls",
    "publish_error_cls",
    "failure_payload",
)
_CHAIN_TILE_PUBLISHER_COMPAT_STAGE_EXECUTION_DEPENDENCY_BINDINGS = (
    ("tile_publisher_cls", "TilePublisher"),
    ("publish_error_cls", "PublishError"),
    ("failure_payload", "failure_payload"),
)
_CHAIN_TILE_PUBLISHER_COMPAT_ALIAS_OWNER_MISSING = tuple(
    (name for name in _CHAIN_TILE_PUBLISHER_COMPAT_ALIAS_NAMES if not hasattr(_chain._tile_publisher_module, name))
)
_CHAIN_TILE_PUBLISHER_COMPAT_ALIAS_FACADE_MISSING = tuple(
    (name for name in _CHAIN_TILE_PUBLISHER_COMPAT_ALIAS_NAMES if name not in _chain.__dict__)
)
_CHAIN_TILE_PUBLISHER_COMPAT_FUNCTION_OWNER_MISSING = tuple(
    (
        name
        for name in _CHAIN_TILE_PUBLISHER_COMPAT_FAILURE_FUNCTION_NAMES
        if not hasattr(_chain._tile_publisher_publisher_module, name)
    )
)
_CHAIN_TILE_PUBLISHER_COMPAT_FUNCTION_FACADE_MISSING = tuple(
    (name for name in _CHAIN_TILE_PUBLISHER_COMPAT_FAILURE_FUNCTION_NAMES if name not in _chain.__dict__)
)
if _CHAIN_TILE_PUBLISHER_COMPAT_ALIAS_OWNER_MISSING:
    raise RuntimeError(
        f"chain tile-publisher compatibility aliases missing from owner module: {', '.join(_CHAIN_TILE_PUBLISHER_COMPAT_ALIAS_OWNER_MISSING)}"
    )
if _CHAIN_TILE_PUBLISHER_COMPAT_ALIAS_FACADE_MISSING:
    raise RuntimeError(
        f"chain tile-publisher compatibility aliases missing from facade: {', '.join(_CHAIN_TILE_PUBLISHER_COMPAT_ALIAS_FACADE_MISSING)}"
    )
if _CHAIN_TILE_PUBLISHER_COMPAT_FUNCTION_OWNER_MISSING:
    raise RuntimeError(
        f"chain tile-publisher compatibility functions missing from owner module: {', '.join(_CHAIN_TILE_PUBLISHER_COMPAT_FUNCTION_OWNER_MISSING)}"
    )
if _CHAIN_TILE_PUBLISHER_COMPAT_FUNCTION_FACADE_MISSING:
    raise RuntimeError(
        f"chain tile-publisher compatibility functions missing from facade: {', '.join(_CHAIN_TILE_PUBLISHER_COMPAT_FUNCTION_FACADE_MISSING)}"
    )
_CHAIN_TILE_PUBLISHER_COMPAT_OWNER_ALIASES = _chain.MappingProxyType(
    {name: getattr(_chain._tile_publisher_module, name) for name in _CHAIN_TILE_PUBLISHER_COMPAT_ALIAS_NAMES}
)
_CHAIN_TILE_PUBLISHER_COMPAT_FACADE_ALIASES = _chain.MappingProxyType(
    {name: _chain.__dict__[name] for name in _CHAIN_TILE_PUBLISHER_COMPAT_ALIAS_NAMES}
)
_CHAIN_TILE_PUBLISHER_COMPAT_OWNER_FUNCTIONS = _chain.MappingProxyType(
    {
        name: getattr(_chain._tile_publisher_publisher_module, name)
        for name in _CHAIN_TILE_PUBLISHER_COMPAT_FAILURE_FUNCTION_NAMES
    }
)
_CHAIN_TILE_PUBLISHER_COMPAT_FACADE_FUNCTIONS = _chain.MappingProxyType(
    {name: _chain.__dict__[name] for name in _CHAIN_TILE_PUBLISHER_COMPAT_FAILURE_FUNCTION_NAMES}
)
_CHAIN_TILE_PUBLISHER_COMPAT_ALIAS_DRIFT = tuple(
    (
        name
        for name in _CHAIN_TILE_PUBLISHER_COMPAT_ALIAS_NAMES
        if _CHAIN_TILE_PUBLISHER_COMPAT_OWNER_ALIASES[name] is not _CHAIN_TILE_PUBLISHER_COMPAT_FACADE_ALIASES[name]
    )
)
_CHAIN_TILE_PUBLISHER_COMPAT_FUNCTION_DRIFT = tuple(
    (
        name
        for name in _CHAIN_TILE_PUBLISHER_COMPAT_FAILURE_FUNCTION_NAMES
        if _CHAIN_TILE_PUBLISHER_COMPAT_OWNER_FUNCTIONS[name] is not _CHAIN_TILE_PUBLISHER_COMPAT_FACADE_FUNCTIONS[name]
    )
)
if _CHAIN_TILE_PUBLISHER_COMPAT_ALIAS_DRIFT:
    raise RuntimeError(
        f"chain tile-publisher direct alias drifted from owner module: {', '.join(_CHAIN_TILE_PUBLISHER_COMPAT_ALIAS_DRIFT)}"
    )
if _CHAIN_TILE_PUBLISHER_COMPAT_FUNCTION_DRIFT:
    raise RuntimeError(
        f"chain tile-publisher failure function drifted from owner module: {', '.join(_CHAIN_TILE_PUBLISHER_COMPAT_FUNCTION_DRIFT)}"
    )
_CHAIN_ARRAY_ACCOUNTING_COMPAT_TOP_LEVEL_FORWARDER_NAMES = (
    "_basin_key",
    "_basin_identifier",
    "_basin_original_task_id",
    "_record_array_task_outcomes",
    "_candidate_outcomes",
    "_safe_candidate_outcome_payload",
    "parse_sacct_array_results",
    "_coerce_array_aggregation",
    "_aggregation_from_task_results",
    "_aggregation_error_code",
    "_aggregation_error_message",
    "_sacct_extra_fields",
    "_slurm_accounting_from_payload",
    "_resource_metrics_from_payload",
    "_stage_task_result_evidence",
    "_array_task_log_uri",
    "_array_task_status",
    "_parse_slurm_exit_code",
)
_CHAIN_ARRAY_ACCOUNTING_COMPAT_TOP_LEVEL_OWNER_FUNCTION_NAMES = (
    "basin_key",
    "basin_identifier",
    "basin_original_task_id",
    "record_array_task_outcomes",
    "candidate_outcomes",
    "safe_candidate_outcome_payload",
    "parse_sacct_array_results",
    "coerce_array_aggregation",
    "aggregation_from_task_results",
    "aggregation_error_code",
    "aggregation_error_message",
    "sacct_extra_fields",
    "slurm_accounting_from_payload",
    "resource_metrics_from_payload",
    "stage_task_result_evidence",
    "array_task_log_uri",
    "array_task_status",
    "parse_slurm_exit_code",
)
_CHAIN_ARRAY_ACCOUNTING_COMPAT_METHOD_FORWARDER_NAMES = (
    "_aggregate_array_stage",
    "_require_complete_array_accounting",
    "_record_cycle_stage_status_override",
    "_record_cycle_stage_accounting_event",
    "_record_cycle_stage_accounting_gap",
    "_apply_array_progress",
)
_CHAIN_ARRAY_ACCOUNTING_COMPAT_METHOD_OWNER_FUNCTION_NAMES = (
    "aggregate_array_stage",
    "require_complete_array_accounting",
    "record_cycle_stage_status_override",
    "record_cycle_stage_accounting_event",
    "record_cycle_stage_accounting_gap",
    "apply_array_progress",
)
_CHAIN_ARRAY_ACCOUNTING_COMPAT_LOCAL_BINDING_NAMES = ("_array_accounting_dependencies", "_context_array_log_uri")
_CHAIN_ARRAY_ACCOUNTING_COMPAT_DEPENDENCY_FIELDS = (
    "coerce_mapping",
    "safe_candidate_outcome_payload",
    "safe_pipeline_event_details",
    "record_array_task_outcomes",
    "stage_task_result_evidence",
    "parse_sacct_array_results",
    "coerce_array_aggregation",
    "aggregation_from_task_results",
    "aggregation_error_code",
    "aggregation_error_message",
    "sacct_extra_fields",
    "slurm_accounting_from_payload",
    "resource_metrics_from_payload",
    "production_status_for",
    "context_array_log_uri",
    "array_task_status",
    "parse_slurm_exit_code",
    "basin_key",
    "basin_original_task_id",
    "status_from_gateway_job",
    "parse_gateway_time",
    "utcnow",
    "build_reindexed_manifest",
)
_CHAIN_ARRAY_ACCOUNTING_COMPAT_DEPENDENCY_BINDINGS = (
    ("coerce_mapping", "_coerce_mapping"),
    ("safe_candidate_outcome_payload", "_safe_candidate_outcome_payload"),
    ("safe_pipeline_event_details", "_safe_pipeline_event_details"),
    ("record_array_task_outcomes", "_record_array_task_outcomes"),
    ("stage_task_result_evidence", "_stage_task_result_evidence"),
    ("parse_sacct_array_results", "parse_sacct_array_results"),
    ("coerce_array_aggregation", "_coerce_array_aggregation"),
    ("aggregation_from_task_results", "_aggregation_from_task_results"),
    ("aggregation_error_code", "_aggregation_error_code"),
    ("aggregation_error_message", "_aggregation_error_message"),
    ("sacct_extra_fields", "_sacct_extra_fields"),
    ("slurm_accounting_from_payload", "_slurm_accounting_from_payload"),
    ("resource_metrics_from_payload", "_resource_metrics_from_payload"),
    ("production_status_for", "production_status_for"),
    ("context_array_log_uri", "_context_array_log_uri"),
    ("array_task_status", "_array_task_status"),
    ("parse_slurm_exit_code", "_parse_slurm_exit_code"),
    ("basin_key", "_basin_key"),
    ("basin_original_task_id", "_basin_original_task_id"),
    ("status_from_gateway_job", "_status_from_gateway_job"),
    ("parse_gateway_time", "_parse_gateway_time"),
    ("utcnow", "_utcnow"),
    ("build_reindexed_manifest", "build_reindexed_manifest"),
)
_CHAIN_ARRAY_ACCOUNTING_COMPAT_OWNER_FUNCTION_NAMES = tuple(
    dict.fromkeys(
        (
            *_CHAIN_ARRAY_ACCOUNTING_COMPAT_TOP_LEVEL_OWNER_FUNCTION_NAMES,
            *_CHAIN_ARRAY_ACCOUNTING_COMPAT_METHOD_OWNER_FUNCTION_NAMES,
            "context_array_log_uri",
        )
    )
)
_CHAIN_ARRAY_ACCOUNTING_COMPAT_OWNER_MISSING = tuple(
    (
        name
        for name in _CHAIN_ARRAY_ACCOUNTING_COMPAT_OWNER_FUNCTION_NAMES
        if not hasattr(_chain.chain_array_accounting, name)
    )
)
if _CHAIN_ARRAY_ACCOUNTING_COMPAT_OWNER_MISSING:
    raise RuntimeError(
        f"chain array accounting compatibility names missing from owner module: {', '.join(_CHAIN_ARRAY_ACCOUNTING_COMPAT_OWNER_MISSING)}"
    )
if (
    tuple(_chain.chain_array_accounting.ArrayAccountingDependencies.__dataclass_fields__)
    != _CHAIN_ARRAY_ACCOUNTING_COMPAT_DEPENDENCY_FIELDS
):
    raise RuntimeError("chain array accounting dependency fields drifted from compatibility fixture")
if (
    tuple((field for field, _ in _CHAIN_ARRAY_ACCOUNTING_COMPAT_DEPENDENCY_BINDINGS))
    != _CHAIN_ARRAY_ACCOUNTING_COMPAT_DEPENDENCY_FIELDS
):
    raise RuntimeError("chain array accounting dependency bindings drifted from field fixture")
_CHAIN_WORKSPACE_LOG_COMPAT_METHOD_FORWARDER_NAMES = (
    "_log_uri_for_pipeline_job",
    "_display_log_publication_for_stage",
    "_display_log_publication_for_pipeline_job",
    "_try_publish_log_for_advertise",
    "_log_persistence_error",
    "_raise_publish_error_after_durable_update",
    "_persist_gateway_logs",
    "_write_local_stage_log",
    "_log_uri_for_stage",
    "_published_log_path",
    "_workspace_path",
    "_safe_workspace_write_bytes",
    "_safe_workspace_read_bytes",
)
_CHAIN_WORKSPACE_LOG_COMPAT_METHOD_OWNER_FUNCTION_NAMES = (
    "log_uri_for_pipeline_job",
    "display_log_publication_for_stage",
    "display_log_publication_for_pipeline_job",
    "try_publish_log_for_advertise",
    "log_persistence_error",
    "raise_publish_error_after_durable_update",
    "persist_gateway_logs",
    "write_local_stage_log",
    "log_uri_for_stage",
    "published_log_path",
    "workspace_path",
    "safe_workspace_write_bytes",
    "safe_workspace_read_bytes",
)
_CHAIN_WORKSPACE_LOG_COMPAT_TOP_LEVEL_FORWARDER_NAMES = ("_workspace_relative_parts",)
_CHAIN_WORKSPACE_LOG_COMPAT_TOP_LEVEL_OWNER_FUNCTION_NAMES = ("workspace_relative_parts",)
_CHAIN_WORKSPACE_LOG_COMPAT_OWNER_FUNCTION_NAMES = (
    *_CHAIN_WORKSPACE_LOG_COMPAT_METHOD_OWNER_FUNCTION_NAMES,
    *_CHAIN_WORKSPACE_LOG_COMPAT_TOP_LEVEL_OWNER_FUNCTION_NAMES,
)
_CHAIN_WORKSPACE_LOG_COMPAT_OWNER_MISSING = tuple(
    (name for name in _CHAIN_WORKSPACE_LOG_COMPAT_OWNER_FUNCTION_NAMES if not hasattr(_chain.chain_workspace, name))
)
if _CHAIN_WORKSPACE_LOG_COMPAT_OWNER_MISSING:
    raise RuntimeError(
        f"chain workspace/log compatibility names missing from owner module: {', '.join(_CHAIN_WORKSPACE_LOG_COMPAT_OWNER_MISSING)}"
    )
if set(getattr(_chain.chain_workspace, "__all__", ())) != set(_CHAIN_WORKSPACE_LOG_COMPAT_OWNER_FUNCTION_NAMES):
    raise RuntimeError("chain workspace/log compatibility names drifted from owner __all__")

CHAIN_COMPAT_STATIC_EXPORTS = {name: value for name, value in globals().items() if name.startswith("_CHAIN_")}
