from __future__ import annotations

import importlib
import os
import re
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from types import MappingProxyType
from typing import Any, Mapping, Protocol, Sequence

import services.orchestrator.time_consistency as _time_consistency_module
import services.tile_publisher as _tile_publisher_module
import services.tile_publisher.publisher as _tile_publisher_publisher_module
import workers.canonical_converter.converter as _canonical_converter_module
import workers.data_adapters.base as _data_adapters_base_module
from packages.common.best_available import BestAvailableManager  # noqa: F401
from packages.common.object_store import LocalObjectStore
from packages.common.redaction import redact_payload
from packages.common.safe_fs import (
    SafeFilesystemError,
    atomic_write_bytes_no_follow,
    ensure_directory_no_follow,
    read_bytes_no_follow,
)
from packages.common.source_identity import normalize_source_id
from packages.common.state_lineage import (
    LINEAGE_MAX_LEAD_EXCEEDED,
    LINEAGE_PACKAGE_VERSION_MISMATCH,
    LINEAGE_SOURCE_MISMATCH,
)
from packages.common.state_manager import StateManager, StateSnapshot
from services.artifacts import ArtifactLogError, published_log_relative_path, published_log_uri
from services.orchestrator import (
    chain_analysis,  # noqa: F401
    chain_array_accounting,
    chain_forecast_control,
    chain_forecast_cycle,
    chain_forecast_trigger,
    chain_manifests,
    chain_runtime_utils,
    chain_slurm_client,
    chain_source_cycle,
    chain_stage_execution,
    chain_workspace,
    persistence,
    production_contract,
    reservation,
    retry,
)
from services.orchestrator import (
    chain_stages as _chain_stages_module,
)
from services.orchestrator import (
    chain_types as _chain_types_module,
)
from services.orchestrator.chain_stages import (
    ANALYSIS_STAGES,  # noqa: F401
    LEGACY_FORECAST_STAGES,  # noqa: F401
    STAGES,
)
from services.orchestrator.chain_stages import (
    M3_STAGES as M3_STAGES,
)
from services.orchestrator.chain_types import (
    AnalysisRunContext,
    ArrayAggregation,
    ArrayTaskResult,
    CycleOrchestrationContext,
    DisplayLogPublication,
    DisplayLogPublicationAttempt,
    ForcingContext,
    ForecastRunContext,
    InitialStateSelection,
    ModelContext,
    ModelRunAssembly,
    OrchestratorError,
    PipelineResult,
    StageDefinition,
    StageRunResult,
    TerminalJobObservation,
)
from services.orchestrator.persistence import PipelineEvent as PipelineEvent
from services.orchestrator.persistence import PipelineJob, PipelineStore
from services.orchestrator.reservation import (
    ReservationResult,
    bind_reservation,
    reserve_candidate,
    slurm_comment_for,
)
from services.orchestrator.retry import RetryConfig, RetryService, compute_backoff_seconds
from services.orchestrator.time_consistency import check_three_way_time_consistency
from services.slurm_gateway.config import SlurmGatewaySettings
from services.tile_publisher import PublishError, TilePublisher
from services.tile_publisher.publisher import failure_payload
from workers.canonical_converter.converter import (
    evaluate_canonical_readiness,  # noqa: F401
    expected_converter_version,  # noqa: F401
)
from workers.data_adapters.base import cycle_id_for, format_cycle_time, parse_cycle_time  # noqa: F401

ANALYSIS_SCENARIO_ID = chain_manifests.ANALYSIS_SCENARIO_ID
DEFAULT_ERA5_REANALYSIS_LATENCY_MINUTES = chain_manifests.DEFAULT_ERA5_REANALYSIS_LATENCY_MINUTES
FORCING_CAUSALITY_CAUSAL = chain_manifests.FORCING_CAUSALITY_CAUSAL
FORCING_CAUSALITY_DELAYED_REANALYSIS = chain_manifests.FORCING_CAUSALITY_DELAYED_REANALYSIS
ManifestValidationError = chain_manifests.ManifestValidationError
PRODUCTION_CONTRACT_ID = chain_manifests.PRODUCTION_CONTRACT_ID
PRODUCTION_CONTRACT_SCHEMA_VERSION = chain_manifests.PRODUCTION_CONTRACT_SCHEMA_VERSION
_analysis_forcing_causality = chain_manifests._analysis_forcing_causality
_analysis_update_ic_step_minutes = chain_manifests._analysis_update_ic_step_minutes
_assembly_from_entry = chain_manifests._assembly_from_entry
_assembly_payload_from_runtime_manifest = chain_manifests._assembly_payload_from_runtime_manifest
_assembly_quality_states = chain_manifests._assembly_quality_states
_cycle_residual_blockers = chain_manifests._cycle_residual_blockers
_default_forcing_uri = chain_manifests._default_forcing_uri
_directory_uri = chain_manifests._directory_uri
_display_contract = chain_manifests._display_contract
_ensure_segment_utc = chain_manifests._ensure_segment_utc
_era5_reanalysis_latency_minutes = chain_manifests._era5_reanalysis_latency_minutes
_frequency_contract = chain_manifests._frequency_contract
_forecast_state_checkpoint_hours = chain_manifests._forecast_state_checkpoint_hours
_has_uri_scheme = chain_manifests._has_uri_scheme
_model_package_manifest_uri = chain_manifests._model_package_manifest_uri
_model_run_stage_evidence = chain_manifests._model_run_stage_evidence
_nested_value = chain_manifests._nested_value
_output_river_contract = chain_manifests._output_river_contract
_preserve_directory_uri = chain_manifests._preserve_directory_uri
_project_name_for_basin = chain_manifests._project_name_for_basin
_safe_project_name = chain_manifests._safe_project_name
_station_metadata_for_basin = chain_manifests._station_metadata_for_basin
_tri_state = chain_manifests._tri_state
build_reindexed_manifest = chain_manifests.build_reindexed_manifest
production_stage_for = chain_manifests.production_stage_for
production_status_for = production_contract.production_status_for
serialize_manifest_index = chain_manifests.serialize_manifest_index

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
    "_frequency_contract",
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
    name for name in _CHAIN_MANIFEST_COMPAT_CHAIN_MANIFEST_ALIAS_NAMES if not hasattr(chain_manifests, name)
)
_CHAIN_MANIFEST_COMPAT_ALIAS_OWNER_MISSING += tuple(
    name for name in _CHAIN_MANIFEST_COMPAT_PRODUCTION_CONTRACT_ALIAS_NAMES if not hasattr(production_contract, name)
)
_CHAIN_MANIFEST_COMPAT_ALIAS_FACADE_MISSING = tuple(
    name for name in _CHAIN_MANIFEST_COMPAT_ALIAS_NAMES if name not in globals()
)
if _CHAIN_MANIFEST_COMPAT_ALIAS_OWNER_MISSING:
    raise RuntimeError(
        "chain manifest compatibility aliases missing from owner modules: "
        f"{', '.join(_CHAIN_MANIFEST_COMPAT_ALIAS_OWNER_MISSING)}"
    )
if _CHAIN_MANIFEST_COMPAT_ALIAS_FACADE_MISSING:
    raise RuntimeError(
        "chain manifest compatibility aliases missing from facade: "
        f"{', '.join(_CHAIN_MANIFEST_COMPAT_ALIAS_FACADE_MISSING)}"
    )
_CHAIN_MANIFEST_COMPAT_OWNER_ALIASES = MappingProxyType(
    {
        **{name: getattr(chain_manifests, name) for name in _CHAIN_MANIFEST_COMPAT_CHAIN_MANIFEST_ALIAS_NAMES},
        **{name: getattr(production_contract, name) for name in _CHAIN_MANIFEST_COMPAT_PRODUCTION_CONTRACT_ALIAS_NAMES},
    }
)
_CHAIN_MANIFEST_COMPAT_FACADE_ALIASES = MappingProxyType(
    {name: globals()[name] for name in _CHAIN_MANIFEST_COMPAT_ALIAS_NAMES}
)
for _chain_manifest_alias_name, _chain_manifest_owner_value in _CHAIN_MANIFEST_COMPAT_OWNER_ALIASES.items():
    if _CHAIN_MANIFEST_COMPAT_FACADE_ALIASES[_chain_manifest_alias_name] is not _chain_manifest_owner_value:
        raise RuntimeError(f"chain manifest direct alias drifted from owner module: {_chain_manifest_alias_name}")
del _chain_manifest_alias_name, _chain_manifest_owner_value

_CHAIN_MANIFEST_COMPAT_TOP_LEVEL_FORWARDER_NAMES = (
    "build_model_run_assembly",
    "_frequency_quality_state",
    "_publish_quality_state",
)
_CHAIN_MANIFEST_COMPAT_TOP_LEVEL_OWNER_FUNCTION_NAMES = (
    "build_model_run_assembly",
    "_frequency_quality_state",
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
_CHAIN_MANIFEST_COMPAT_DEPENDENCY_BINDINGS = MappingProxyType(
    {
        "build_model_run_assembly": (
            ("default_forcing_uri", "_default_forcing_uri"),
            ("preserve_directory_uri", "_preserve_directory_uri"),
            ("station_metadata_for_basin", "_station_metadata_for_basin"),
            ("output_river_contract", "_output_river_contract"),
            ("frequency_contract", "_frequency_contract"),
            ("display_contract", "_display_contract"),
            ("assembly_quality_states", "_assembly_quality_states"),
            ("project_name_for_basin", "_project_name_for_basin"),
            ("model_package_manifest_uri", "_model_package_manifest_uri"),
        ),
        "_frequency_quality_state": (("model_run_stage_evidence", "_model_run_stage_evidence"),),
        "_publish_quality_state": (("model_run_stage_evidence", "_model_run_stage_evidence"),),
        "ForecastOrchestrator._build_cycle_stage_manifest": (
            ("model_run_stage_evidence", "_model_run_stage_evidence"),
            ("frequency_quality_state", "_frequency_quality_state"),
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
    name for name in _CHAIN_MANIFEST_COMPAT_OWNER_FUNCTION_NAMES if not hasattr(chain_manifests, name)
)
if _CHAIN_MANIFEST_COMPAT_OWNER_FUNCTION_MISSING:
    raise RuntimeError(
        "chain manifest compatibility functions missing from owner module: "
        f"{', '.join(_CHAIN_MANIFEST_COMPAT_OWNER_FUNCTION_MISSING)}"
    )

_CHAIN_RESERVATION_COMPAT_ALIAS_NAMES = (
    "ReservationResult",
    "bind_reservation",
    "reserve_candidate",
    "slurm_comment_for",
)
_CHAIN_RESERVATION_COMPAT_ALIAS_OWNER_MISSING = tuple(
    name for name in _CHAIN_RESERVATION_COMPAT_ALIAS_NAMES if not hasattr(reservation, name)
)
_CHAIN_RESERVATION_COMPAT_ALIAS_FACADE_MISSING = tuple(
    name for name in _CHAIN_RESERVATION_COMPAT_ALIAS_NAMES if name not in globals()
)
if _CHAIN_RESERVATION_COMPAT_ALIAS_OWNER_MISSING:
    raise RuntimeError(
        "chain reservation compatibility aliases missing from owner module: "
        f"{', '.join(_CHAIN_RESERVATION_COMPAT_ALIAS_OWNER_MISSING)}"
    )
if _CHAIN_RESERVATION_COMPAT_ALIAS_FACADE_MISSING:
    raise RuntimeError(
        "chain reservation compatibility aliases missing from facade: "
        f"{', '.join(_CHAIN_RESERVATION_COMPAT_ALIAS_FACADE_MISSING)}"
    )
_CHAIN_RESERVATION_COMPAT_OWNER_ALIASES = MappingProxyType(
    {name: getattr(reservation, name) for name in _CHAIN_RESERVATION_COMPAT_ALIAS_NAMES}
)
_CHAIN_RESERVATION_COMPAT_FACADE_ALIASES = MappingProxyType(
    {name: globals()[name] for name in _CHAIN_RESERVATION_COMPAT_ALIAS_NAMES}
)
for _chain_reservation_alias_name, _chain_reservation_owner_value in _CHAIN_RESERVATION_COMPAT_OWNER_ALIASES.items():
    if _CHAIN_RESERVATION_COMPAT_FACADE_ALIASES[_chain_reservation_alias_name] is not (_chain_reservation_owner_value):
        raise RuntimeError(f"chain reservation direct alias drifted from owner module: {_chain_reservation_alias_name}")
del _chain_reservation_alias_name, _chain_reservation_owner_value

_CHAIN_RESERVATION_COMPAT_OWNER_METHOD_FORWARDER_NAMES = (
    "_reserve_cycle_stage",
    "_bind_cycle_stage_reservation",
)
_CHAIN_RESERVATION_COMPAT_OWNER_METHOD_OWNER_FUNCTION_NAMES = (
    "reserve_candidate",
    "bind_reservation",
)
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
    name for name in _CHAIN_RESERVATION_COMPAT_OWNER_METHOD_OWNER_FUNCTION_NAMES if not hasattr(reservation, name)
)
if _CHAIN_RESERVATION_COMPAT_OWNER_FUNCTION_MISSING:
    raise RuntimeError(
        "chain reservation compatibility functions missing from owner module: "
        f"{', '.join(_CHAIN_RESERVATION_COMPAT_OWNER_FUNCTION_MISSING)}"
    )

_CHAIN_PERSISTENCE_REPOSITORY_COMPAT_ALIAS_NAMES = (
    "PipelineJob",
    "PipelineEvent",
    "PipelineStore",
)
_CHAIN_PERSISTENCE_REPOSITORY_COMPAT_ALIAS_OWNER_MISSING = tuple(
    name for name in _CHAIN_PERSISTENCE_REPOSITORY_COMPAT_ALIAS_NAMES if not hasattr(persistence, name)
)
_CHAIN_PERSISTENCE_REPOSITORY_COMPAT_ALIAS_FACADE_MISSING = tuple(
    name for name in _CHAIN_PERSISTENCE_REPOSITORY_COMPAT_ALIAS_NAMES if name not in globals()
)
if _CHAIN_PERSISTENCE_REPOSITORY_COMPAT_ALIAS_OWNER_MISSING:
    raise RuntimeError(
        "chain persistence compatibility aliases missing from owner module: "
        f"{', '.join(_CHAIN_PERSISTENCE_REPOSITORY_COMPAT_ALIAS_OWNER_MISSING)}"
    )
if _CHAIN_PERSISTENCE_REPOSITORY_COMPAT_ALIAS_FACADE_MISSING:
    raise RuntimeError(
        "chain persistence compatibility aliases missing from facade: "
        f"{', '.join(_CHAIN_PERSISTENCE_REPOSITORY_COMPAT_ALIAS_FACADE_MISSING)}"
    )
_CHAIN_PERSISTENCE_REPOSITORY_COMPAT_OWNER_ALIASES = MappingProxyType(
    {name: getattr(persistence, name) for name in _CHAIN_PERSISTENCE_REPOSITORY_COMPAT_ALIAS_NAMES}
)
_CHAIN_PERSISTENCE_REPOSITORY_COMPAT_FACADE_ALIASES = MappingProxyType(
    {name: globals()[name] for name in _CHAIN_PERSISTENCE_REPOSITORY_COMPAT_ALIAS_NAMES}
)
_CHAIN_PERSISTENCE_REPOSITORY_COMPAT_ALIAS_DRIFT = tuple(
    name
    for name in _CHAIN_PERSISTENCE_REPOSITORY_COMPAT_ALIAS_NAMES
    if _CHAIN_PERSISTENCE_REPOSITORY_COMPAT_FACADE_ALIASES[name]
    is not _CHAIN_PERSISTENCE_REPOSITORY_COMPAT_OWNER_ALIASES[name]
)
if _CHAIN_PERSISTENCE_REPOSITORY_COMPAT_ALIAS_DRIFT:
    raise RuntimeError(
        "chain persistence direct alias drifted from owner module: "
        f"{', '.join(_CHAIN_PERSISTENCE_REPOSITORY_COMPAT_ALIAS_DRIFT)}"
    )

_CHAIN_PERSISTENCE_REPOSITORY_COMPAT_LOCAL_REPOSITORY_NAMES = (
    "OrchestratorRepository",
    "PsycopgOrchestratorRepository",
)
_CHAIN_PERSISTENCE_REPOSITORY_COMPAT_CHAIN_LOCAL_CLASSIFICATIONS = MappingProxyType(
    {
        "OrchestratorRepository": "chain-local-protocol",
        "PsycopgOrchestratorRepository": "chain-local-implementation",
    }
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
_CHAIN_PERSISTENCE_REPOSITORY_COMPAT_LEGACY_IMPORT_TOKENS = MappingProxyType(
    {
        "services/orchestrator/scheduler.py": (
            "from services.orchestrator.chain import PsycopgOrchestratorRepository",
            "PsycopgOrchestratorRepository",
            "return PsycopgOrchestratorRepository.from_env()",
        )
    }
)
_CHAIN_PERSISTENCE_REPOSITORY_COMPAT_LEGACY_IMPORT_FUNCTION_TOKENS = MappingProxyType(
    {
        "services/orchestrator/scheduler.py": MappingProxyType(
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

_CHAIN_RETRY_COMPAT_ALIAS_NAMES = (
    "RetryConfig",
    "RetryService",
    "compute_backoff_seconds",
)
_CHAIN_RETRY_COMPAT_ALIAS_OWNER_MISSING = tuple(
    name for name in _CHAIN_RETRY_COMPAT_ALIAS_NAMES if not hasattr(retry, name)
)
_CHAIN_RETRY_COMPAT_ALIAS_FACADE_MISSING = tuple(
    name for name in _CHAIN_RETRY_COMPAT_ALIAS_NAMES if name not in globals()
)
if _CHAIN_RETRY_COMPAT_ALIAS_OWNER_MISSING:
    raise RuntimeError(
        "chain retry compatibility aliases missing from owner module: "
        f"{', '.join(_CHAIN_RETRY_COMPAT_ALIAS_OWNER_MISSING)}"
    )
if _CHAIN_RETRY_COMPAT_ALIAS_FACADE_MISSING:
    raise RuntimeError(
        f"chain retry compatibility aliases missing from facade: {', '.join(_CHAIN_RETRY_COMPAT_ALIAS_FACADE_MISSING)}"
    )
_CHAIN_RETRY_COMPAT_OWNER_ALIASES = MappingProxyType(
    {name: getattr(retry, name) for name in _CHAIN_RETRY_COMPAT_ALIAS_NAMES}
)
_CHAIN_RETRY_COMPAT_FACADE_ALIASES = MappingProxyType(
    {name: globals()[name] for name in _CHAIN_RETRY_COMPAT_ALIAS_NAMES}
)
_CHAIN_RETRY_COMPAT_ALIAS_DRIFT = tuple(
    name
    for name in _CHAIN_RETRY_COMPAT_ALIAS_NAMES
    if _CHAIN_RETRY_COMPAT_FACADE_ALIASES[name] is not _CHAIN_RETRY_COMPAT_OWNER_ALIASES[name]
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
_CHAIN_RETRY_COMPAT_CHAIN_LOCAL_CLASSIFICATIONS = MappingProxyType(
    {
        **{name: "chain-local-bridge" for name in _CHAIN_RETRY_COMPAT_LOCAL_METHOD_NAMES},
        **{name: "chain-local-factory" for name in _CHAIN_RETRY_COMPAT_LOCAL_FACTORY_NAMES},
    }
)

_CHAIN_STAGE_CATALOG_COMPAT_REEXPORT_NAMES = (
    "ANALYSIS_STAGES",
    "LEGACY_FORECAST_STAGES",
    "M3_STAGES",
    "STAGES",
)
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
    name for name in _CHAIN_STAGE_CATALOG_COMPAT_REEXPORT_NAMES if not hasattr(_chain_stages_module, name)
)
_CHAIN_STAGE_CATALOG_TYPE_COMPAT_REEXPORT_MISSING += tuple(
    name for name in _CHAIN_TYPE_COMPAT_REEXPORT_NAMES if not hasattr(_chain_types_module, name)
)
_CHAIN_STAGE_CATALOG_TYPE_COMPAT_FACADE_MISSING = tuple(
    name for name in _CHAIN_STAGE_CATALOG_TYPE_COMPAT_REEXPORT_NAMES if name not in globals()
)
if _CHAIN_STAGE_CATALOG_TYPE_COMPAT_REEXPORT_MISSING:
    raise RuntimeError(
        "chain stage catalog/type compatibility names missing from owner modules: "
        f"{', '.join(_CHAIN_STAGE_CATALOG_TYPE_COMPAT_REEXPORT_MISSING)}"
    )
if _CHAIN_STAGE_CATALOG_TYPE_COMPAT_FACADE_MISSING:
    raise RuntimeError(
        "chain stage catalog/type compatibility names missing from facade: "
        f"{', '.join(_CHAIN_STAGE_CATALOG_TYPE_COMPAT_FACADE_MISSING)}"
    )
if set(getattr(_chain_stages_module, "__all__", ())) != set(_CHAIN_STAGE_CATALOG_COMPAT_REEXPORT_NAMES):
    raise RuntimeError("chain stage catalog compatibility names drifted from owner __all__")
if set(getattr(_chain_types_module, "__all__", ())) != set(_CHAIN_TYPE_COMPAT_REEXPORT_NAMES):
    raise RuntimeError("chain type compatibility names drifted from owner __all__")
_CHAIN_STAGE_CATALOG_TYPE_COMPAT_OWNER_REEXPORTS = MappingProxyType(
    {
        **{name: getattr(_chain_stages_module, name) for name in _CHAIN_STAGE_CATALOG_COMPAT_REEXPORT_NAMES},
        **{name: getattr(_chain_types_module, name) for name in _CHAIN_TYPE_COMPAT_REEXPORT_NAMES},
    }
)
_CHAIN_STAGE_CATALOG_TYPE_COMPAT_FACADE_REEXPORTS = MappingProxyType(
    {name: globals()[name] for name in _CHAIN_STAGE_CATALOG_TYPE_COMPAT_REEXPORT_NAMES}
)
for (
    _chain_stage_type_direct_name,
    _chain_stage_type_owner_value,
) in _CHAIN_STAGE_CATALOG_TYPE_COMPAT_OWNER_REEXPORTS.items():
    if _CHAIN_STAGE_CATALOG_TYPE_COMPAT_FACADE_REEXPORTS[_chain_stage_type_direct_name] is not (
        _chain_stage_type_owner_value
    ):
        raise RuntimeError(
            f"chain stage catalog/type direct re-export drifted from owner module: {_chain_stage_type_direct_name}"
        )
del _chain_stage_type_direct_name, _chain_stage_type_owner_value
_CHAIN_STAGE_CATALOG_TYPE_COMPAT_EXPORTS = tuple(
    _CHAIN_STAGE_CATALOG_TYPE_COMPAT_FACADE_REEXPORTS[name] for name in _CHAIN_STAGE_CATALOG_TYPE_COMPAT_REEXPORT_NAMES
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
    name for name in _CHAIN_STAGE_EXECUTION_COMPAT_OWNER_FUNCTION_NAMES if not hasattr(chain_stage_execution, name)
)
if _CHAIN_STAGE_EXECUTION_COMPAT_OWNER_MISSING:
    raise RuntimeError(
        "chain stage execution compatibility names missing from owner module: "
        f"{', '.join(_CHAIN_STAGE_EXECUTION_COMPAT_OWNER_MISSING)}"
    )
if set(getattr(chain_stage_execution, "__all__", ())) != {
    "StageExecutionDependencies",
    *_CHAIN_STAGE_EXECUTION_COMPAT_OWNER_FUNCTION_NAMES,
}:
    raise RuntimeError("chain stage execution compatibility names drifted from owner __all__")
if (
    tuple(chain_stage_execution.StageExecutionDependencies.__dataclass_fields__)
    != _CHAIN_STAGE_EXECUTION_COMPAT_DEPENDENCY_FIELDS
):
    raise RuntimeError("chain stage execution dependency fields drifted from compatibility fixture")
_CHAIN_STAGE_EXECUTION_COMPAT_FORWARDERS = MappingProxyType(
    {
        facade_name: getattr(chain_stage_execution, owner_name)
        for facade_name, owner_name in zip(
            _CHAIN_STAGE_EXECUTION_COMPAT_FORWARDER_NAMES,
            _CHAIN_STAGE_EXECUTION_COMPAT_OWNER_FUNCTION_NAMES,
            strict=True,
        )
    }
)

_CHAIN_TILE_PUBLISHER_COMPAT_ALIAS_NAMES = (
    "TilePublisher",
    "PublishError",
)
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
    name for name in _CHAIN_TILE_PUBLISHER_COMPAT_ALIAS_NAMES if not hasattr(_tile_publisher_module, name)
)
_CHAIN_TILE_PUBLISHER_COMPAT_ALIAS_FACADE_MISSING = tuple(
    name for name in _CHAIN_TILE_PUBLISHER_COMPAT_ALIAS_NAMES if name not in globals()
)
_CHAIN_TILE_PUBLISHER_COMPAT_FUNCTION_OWNER_MISSING = tuple(
    name
    for name in _CHAIN_TILE_PUBLISHER_COMPAT_FAILURE_FUNCTION_NAMES
    if not hasattr(_tile_publisher_publisher_module, name)
)
_CHAIN_TILE_PUBLISHER_COMPAT_FUNCTION_FACADE_MISSING = tuple(
    name for name in _CHAIN_TILE_PUBLISHER_COMPAT_FAILURE_FUNCTION_NAMES if name not in globals()
)
if _CHAIN_TILE_PUBLISHER_COMPAT_ALIAS_OWNER_MISSING:
    raise RuntimeError(
        "chain tile-publisher compatibility aliases missing from owner module: "
        f"{', '.join(_CHAIN_TILE_PUBLISHER_COMPAT_ALIAS_OWNER_MISSING)}"
    )
if _CHAIN_TILE_PUBLISHER_COMPAT_ALIAS_FACADE_MISSING:
    raise RuntimeError(
        "chain tile-publisher compatibility aliases missing from facade: "
        f"{', '.join(_CHAIN_TILE_PUBLISHER_COMPAT_ALIAS_FACADE_MISSING)}"
    )
if _CHAIN_TILE_PUBLISHER_COMPAT_FUNCTION_OWNER_MISSING:
    raise RuntimeError(
        "chain tile-publisher compatibility functions missing from owner module: "
        f"{', '.join(_CHAIN_TILE_PUBLISHER_COMPAT_FUNCTION_OWNER_MISSING)}"
    )
if _CHAIN_TILE_PUBLISHER_COMPAT_FUNCTION_FACADE_MISSING:
    raise RuntimeError(
        "chain tile-publisher compatibility functions missing from facade: "
        f"{', '.join(_CHAIN_TILE_PUBLISHER_COMPAT_FUNCTION_FACADE_MISSING)}"
    )
_CHAIN_TILE_PUBLISHER_COMPAT_OWNER_ALIASES = MappingProxyType(
    {name: getattr(_tile_publisher_module, name) for name in _CHAIN_TILE_PUBLISHER_COMPAT_ALIAS_NAMES}
)
_CHAIN_TILE_PUBLISHER_COMPAT_FACADE_ALIASES = MappingProxyType(
    {name: globals()[name] for name in _CHAIN_TILE_PUBLISHER_COMPAT_ALIAS_NAMES}
)
_CHAIN_TILE_PUBLISHER_COMPAT_OWNER_FUNCTIONS = MappingProxyType(
    {
        name: getattr(_tile_publisher_publisher_module, name)
        for name in _CHAIN_TILE_PUBLISHER_COMPAT_FAILURE_FUNCTION_NAMES
    }
)
_CHAIN_TILE_PUBLISHER_COMPAT_FACADE_FUNCTIONS = MappingProxyType(
    {name: globals()[name] for name in _CHAIN_TILE_PUBLISHER_COMPAT_FAILURE_FUNCTION_NAMES}
)
_CHAIN_TILE_PUBLISHER_COMPAT_ALIAS_DRIFT = tuple(
    name
    for name in _CHAIN_TILE_PUBLISHER_COMPAT_ALIAS_NAMES
    if _CHAIN_TILE_PUBLISHER_COMPAT_OWNER_ALIASES[name] is not _CHAIN_TILE_PUBLISHER_COMPAT_FACADE_ALIASES[name]
)
_CHAIN_TILE_PUBLISHER_COMPAT_FUNCTION_DRIFT = tuple(
    name
    for name in _CHAIN_TILE_PUBLISHER_COMPAT_FAILURE_FUNCTION_NAMES
    if _CHAIN_TILE_PUBLISHER_COMPAT_OWNER_FUNCTIONS[name] is not _CHAIN_TILE_PUBLISHER_COMPAT_FACADE_FUNCTIONS[name]
)
if _CHAIN_TILE_PUBLISHER_COMPAT_ALIAS_DRIFT:
    raise RuntimeError(
        "chain tile-publisher direct alias drifted from owner module: "
        f"{', '.join(_CHAIN_TILE_PUBLISHER_COMPAT_ALIAS_DRIFT)}"
    )
if _CHAIN_TILE_PUBLISHER_COMPAT_FUNCTION_DRIFT:
    raise RuntimeError(
        "chain tile-publisher failure function drifted from owner module: "
        f"{', '.join(_CHAIN_TILE_PUBLISHER_COMPAT_FUNCTION_DRIFT)}"
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
_CHAIN_ARRAY_ACCOUNTING_COMPAT_LOCAL_BINDING_NAMES = (
    "_array_accounting_dependencies",
    "_context_array_log_uri",
)
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
    name for name in _CHAIN_ARRAY_ACCOUNTING_COMPAT_OWNER_FUNCTION_NAMES if not hasattr(chain_array_accounting, name)
)
if _CHAIN_ARRAY_ACCOUNTING_COMPAT_OWNER_MISSING:
    raise RuntimeError(
        "chain array accounting compatibility names missing from owner module: "
        f"{', '.join(_CHAIN_ARRAY_ACCOUNTING_COMPAT_OWNER_MISSING)}"
    )
if (
    tuple(chain_array_accounting.ArrayAccountingDependencies.__dataclass_fields__)
    != _CHAIN_ARRAY_ACCOUNTING_COMPAT_DEPENDENCY_FIELDS
):
    raise RuntimeError("chain array accounting dependency fields drifted from compatibility fixture")
if tuple(field for field, _ in _CHAIN_ARRAY_ACCOUNTING_COMPAT_DEPENDENCY_BINDINGS) != (
    _CHAIN_ARRAY_ACCOUNTING_COMPAT_DEPENDENCY_FIELDS
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
    name for name in _CHAIN_WORKSPACE_LOG_COMPAT_OWNER_FUNCTION_NAMES if not hasattr(chain_workspace, name)
)
if _CHAIN_WORKSPACE_LOG_COMPAT_OWNER_MISSING:
    raise RuntimeError(
        "chain workspace/log compatibility names missing from owner module: "
        f"{', '.join(_CHAIN_WORKSPACE_LOG_COMPAT_OWNER_MISSING)}"
    )
if set(getattr(chain_workspace, "__all__", ())) != set(_CHAIN_WORKSPACE_LOG_COMPAT_OWNER_FUNCTION_NAMES):
    raise RuntimeError("chain workspace/log compatibility names drifted from owner __all__")


def build_model_run_assembly(
    basin: Mapping[str, Any],
    *,
    source_id: str,
    cycle_id: str,
    cycle_time: datetime,
    scenario_id: str,
    workspace_root: Path,
    object_store: LocalObjectStore,
    default_forecast_horizon_hours: int,
) -> ModelRunAssembly:
    return chain_manifests.build_model_run_assembly(
        basin,
        source_id=source_id,
        cycle_id=cycle_id,
        cycle_time=cycle_time,
        scenario_id=scenario_id,
        workspace_root=workspace_root,
        object_store=object_store,
        default_forecast_horizon_hours=default_forecast_horizon_hours,
        default_forcing_uri=_default_forcing_uri,
        preserve_directory_uri=_preserve_directory_uri,
        station_metadata_for_basin=_station_metadata_for_basin,
        output_river_contract=_output_river_contract,
        frequency_contract=_frequency_contract,
        display_contract=_display_contract,
        assembly_quality_states=_assembly_quality_states,
        project_name_for_basin=_project_name_for_basin,
        model_package_manifest_uri=_model_package_manifest_uri,
    )


def _frequency_quality_state(entry: Mapping[str, Any], *, cycle_id: str) -> dict[str, Any]:
    return chain_manifests._frequency_quality_state(
        entry,
        cycle_id=cycle_id,
        model_run_stage_evidence=_model_run_stage_evidence,
    )


def _publish_quality_state(entry: Mapping[str, Any], *, cycle_id: str) -> dict[str, Any]:
    return chain_manifests._publish_quality_state(
        entry,
        cycle_id=cycle_id,
        model_run_stage_evidence=_model_run_stage_evidence,
    )


_CHAIN_MANIFEST_COMPAT_TOP_LEVEL_FACADE_MISSING = tuple(
    name for name in _CHAIN_MANIFEST_COMPAT_TOP_LEVEL_FORWARDER_NAMES if not callable(globals().get(name))
)
if _CHAIN_MANIFEST_COMPAT_TOP_LEVEL_FACADE_MISSING:
    raise RuntimeError(
        "chain manifest compatibility top-level forwarders missing from facade: "
        f"{', '.join(_CHAIN_MANIFEST_COMPAT_TOP_LEVEL_FACADE_MISSING)}"
    )
_CHAIN_MANIFEST_COMPAT_TOP_LEVEL_FORWARDERS = MappingProxyType(
    {
        facade_name: getattr(chain_manifests, owner_name)
        for facade_name, owner_name in zip(
            _CHAIN_MANIFEST_COMPAT_TOP_LEVEL_FORWARDER_NAMES,
            _CHAIN_MANIFEST_COMPAT_TOP_LEVEL_OWNER_FUNCTION_NAMES,
            strict=True,
        )
    }
)

_SAFE_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.\-]*$")
_SAFE_AREA_RE = re.compile(r"^[\d,.\-\s]+$")

TERMINAL_JOB_STATUSES = {
    "succeeded",
    "partially_failed",
    "failed",
    "cancelled",
    "submission_failed",
    "permanently_failed",
}
ACTIVE_HYDRO_STATUSES = {"created", "staged", "submitted", "running"}
COMPLETED_HYDRO_STATUSES = {"succeeded", "parsed", "frequency_done", "published", "complete"}
TERMINAL_PIPELINE_SUCCESS_STATUSES = {"succeeded", "complete", "published"}
FAILED_PIPELINE_STATUSES = {"failed", "submission_failed", "partially_failed", "permanently_failed"}
RAW_MANIFEST_READY_CYCLE_STATUSES = {"raw_complete", "canonical_ready", "forcing_ready", "complete", "published"}
ANALYSIS_SOURCE_ID = "ERA5"
# ERA5 reanalysis is published with a multi-day production delay; the analysis
# segment is therefore built from *delayed reanalysis*, never a real-time causal
# nowcast. We record a conservative default latency (5 days, ERA5T-style "initial
# release" lag) so the causality marker is honest about how far the reanalysis
# trails real time. Overridable via ERA5_REANALYSIS_LATENCY_MINUTES.
# TODO(M24): source the exact per-cycle latency from the ERA5 download metadata
# (publish_time - segment_end) once it is recorded; until then this is the floor.
DEFAULT_CANDIDATE_STATE_JOB_LIMIT = 100
DEFAULT_CANDIDATE_STATE_EVENT_LIMIT = 100
MAX_CANDIDATE_STATE_TASK_RESULTS = 16


def scenario_for_source(source_id: str) -> str:
    try:
        normalized_source_id = normalize_source_id(source_id)
    except ValueError:
        normalized_source_id = source_id
    if normalized_source_id == "gfs":
        return "forecast_gfs_deterministic"
    if normalized_source_id == "IFS":
        return "forecast_ifs_deterministic"
    return f"forecast_{source_id.lower()}_deterministic"


class PipelineAlreadyActiveError(OrchestratorError):
    def __init__(self, source_id: str, cycle_time: datetime, model_id: str) -> None:
        super().__init__(
            "PIPELINE_ALREADY_ACTIVE",
            f"An active pipeline already exists for {source_id} {format_cycle_time(cycle_time)} {model_id}.",
            {"source_id": source_id, "cycle_time": _format_time(cycle_time), "model_id": model_id},
        )


class AnalysisPipelineAlreadyActiveError(OrchestratorError):
    def __init__(self, model_id: str, start_time: datetime, end_time: datetime) -> None:
        super().__init__(
            "ANALYSIS_PIPELINE_ALREADY_ACTIVE",
            f"An active analysis pipeline already overlaps {model_id} "
            f"{_format_time(start_time)}..{_format_time(end_time)}.",
            {"model_id": model_id, "start_time": _format_time(start_time), "end_time": _format_time(end_time)},
        )


class SlurmClientError(OrchestratorError):
    pass


class SlurmAccountingEvidenceGap(OrchestratorError):
    def __init__(self, message: str, details: dict[str, Any] | None = None) -> None:
        super().__init__("SLURM_ACCOUNTING_EVIDENCE_GAP", message, details)


# Cycle status the convert_canonical stage consumes as input. A canonical-ready
# cycle is demoted back to this state when its converter_version is stale, so the
# next tick re-runs conversion with the current converter_version.
CANONICAL_DEMOTE_CYCLE_STATUS = "raw_complete"

# Canonical precipitation contract (mirrors the converter's STANDARD_UNITS /
# IFS_STANDARD_UNITS entry ``prcp_rate_or_amount: "mm/day"``, post-#269). Used as
# an orthogonal stale criterion: pre-#269 canonical precip rows were written with
# ``unit="mm"`` and often without a converter_version, so they slip past the
# version check below and would otherwise die terminally at the producer's
# mm/day unit gate (failed_forcing) with no self-heal path.
CANONICAL_PRECIP_VARIABLE = "prcp_rate_or_amount"
CANONICAL_PRECIP_UNIT = "mm/day"


@dataclass(frozen=True)
class OrchestratorConfig:
    workspace_root: Path | str
    object_store_root: Path | str
    object_store_prefix: str = ""
    slurm_gateway_url: str = "http://localhost:8000"
    templates_dir: Path | str | None = None
    poll_interval_seconds: float = 30.0
    job_timeout_seconds: float = 3600.0
    source_id: str = "gfs"
    forecast_horizon_hours: int = 168
    scenario_id: str | None = None
    scenario_id_explicit: bool = field(init=False, default=False, repr=False, compare=False)
    era5_area: str = "55,70,15,140"
    state_soft_stale_threshold_days: int = 7
    state_hard_stale_threshold_days: int = 30
    require_forecast_warm_start: bool = False
    slurm_job_type_templates: Mapping[str, str] = field(default_factory=dict)
    slurm_env: Mapping[str, str] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "workspace_root", Path(self.workspace_root).expanduser().resolve())
        object.__setattr__(self, "object_store_root", Path(self.object_store_root).expanduser().resolve())
        object.__setattr__(self, "source_id", normalize_source_id(self.source_id))
        object.__setattr__(self, "poll_interval_seconds", max(float(self.poll_interval_seconds), 1.0))
        object.__setattr__(self, "scenario_id_explicit", self.scenario_id is not None)
        if self.scenario_id is None:
            object.__setattr__(self, "scenario_id", scenario_for_source(self.source_id))
        if self.templates_dir is None:
            repo_root = Path(__file__).resolve().parents[2]
            object.__setattr__(self, "templates_dir", repo_root / "infra" / "sbatch")
        else:
            object.__setattr__(self, "templates_dir", Path(self.templates_dir).expanduser().resolve())
        object.__setattr__(
            self,
            "slurm_job_type_templates",
            {str(key): str(value) for key, value in dict(self.slurm_job_type_templates).items()},
        )
        object.__setattr__(self, "slurm_env", {str(key): str(value) for key, value in dict(self.slurm_env).items()})

    @classmethod
    def from_env(cls) -> OrchestratorConfig:
        workspace_root = os.getenv("WORKSPACE_ROOT", ".")
        return cls(
            workspace_root=workspace_root,
            object_store_root=os.getenv("OBJECT_STORE_ROOT", workspace_root),
            object_store_prefix=os.getenv("OBJECT_STORE_PREFIX", ""),
            slurm_gateway_url=os.getenv("SLURM_GATEWAY_URL", "http://localhost:8000"),
            poll_interval_seconds=float(os.getenv("ORCHESTRATOR_POLL_INTERVAL_SECONDS", "30")),
            job_timeout_seconds=float(os.getenv("ORCHESTRATOR_JOB_TIMEOUT_SECONDS", "3600")),
            source_id=os.getenv("FORECAST_SOURCE_ID", "gfs"),
            forecast_horizon_hours=int(os.getenv("FORECAST_HORIZON_HOURS", "168")),
            era5_area=os.getenv("ERA5_AREA", "55,70,15,140"),
            state_soft_stale_threshold_days=int(os.getenv("STATE_SOFT_STALE_THRESHOLD_DAYS", "7")),
            state_hard_stale_threshold_days=int(os.getenv("STATE_HARD_STALE_THRESHOLD_DAYS", "30")),
            require_forecast_warm_start=_env_flag("NHMS_REQUIRE_FORECAST_WARM_START", default=False),
        )


# Bound on the warm-start fallback loop to avoid unbounded scans of stale snapshots.
_MAX_STATE_FALLBACK_CANDIDATES = 8

# Re-exported from the shared module so chain and the forecast runtime share one
# implementation (single source of truth; see services.orchestrator.time_consistency).
_check_three_way_time_consistency = check_three_way_time_consistency


def _validate_state_lineage(
    state: StateSnapshot,
    *,
    source_id: str | None,
    model_package_version: str | None,
    model_package_checksum: str | None,
    max_lead_hours: int | None,
) -> str | None:
    """Return a stable rejection code if the candidate state's lineage is incompatible.

    Each check is skipped when the corresponding target value is unknown (None) so
    pre-lineage states and callers without full target metadata are not falsely
    rejected. Returns None when the candidate is compatible.
    """

    if source_id is not None and state.source_id is not None:
        if normalize_source_id(state.source_id) != normalize_source_id(source_id):
            return LINEAGE_SOURCE_MISMATCH

    if state.model_package_version is not None and model_package_version is not None:
        if state.model_package_version != model_package_version:
            return LINEAGE_PACKAGE_VERSION_MISMATCH
    if state.model_package_checksum is not None and model_package_checksum is not None:
        if state.model_package_checksum != model_package_checksum:
            return LINEAGE_PACKAGE_VERSION_MISMATCH

    if max_lead_hours is not None and state.lead_hours is not None:
        if int(state.lead_hours) > int(max_lead_hours):
            return LINEAGE_MAX_LEAD_EXCEEDED

    return None


def _validate_strict_state_lineage(
    state: StateSnapshot,
    *,
    source_id: str | None,
    model_package_version: str | None,
    model_package_checksum: str | None,
) -> str | None:
    if source_id is not None:
        if state.source_id is None:
            return LINEAGE_SOURCE_MISMATCH
        try:
            state_source_id = normalize_source_id(state.source_id)
            target_source_id = normalize_source_id(source_id)
        except (AttributeError, TypeError, ValueError):
            return LINEAGE_SOURCE_MISMATCH
        if state_source_id != target_source_id:
            return LINEAGE_SOURCE_MISMATCH

    if model_package_version is not None:
        if state.model_package_version is None or state.model_package_version != model_package_version:
            return LINEAGE_PACKAGE_VERSION_MISMATCH
    if (
        state.model_package_checksum in (None, "")
        or model_package_checksum in (None, "")
        or state.model_package_checksum != model_package_checksum
    ):
        return LINEAGE_PACKAGE_VERSION_MISMATCH

    return None


def _env_flag(name: str, *, default: bool) -> bool:
    value = os.getenv(name)
    if value is None or value.strip() == "":
        return default
    normalized = value.strip().lower()
    if normalized in {"1", "true", "t", "yes", "y", "on"}:
        return True
    if normalized in {"0", "false", "f", "no", "n", "off"}:
        return False
    raise ValueError(f"{name} must be a boolean value.")


class SlurmGatewayClient(Protocol):
    def submit_job(self, payload: dict[str, Any]) -> dict[str, Any]:
        raise NotImplementedError

    def submit_job_array(
        self,
        job_type: str | Mapping[str, Any],
        cycle_id: str | None = None,
        stage_name: str | None = None,
        tasks: Sequence[Mapping[str, Any]] | None = None,
        manifest: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        raise NotImplementedError

    def get_job_status(self, job_id: str) -> dict[str, Any]:
        raise NotImplementedError

    def get_array_task_results(self, job_id: str) -> list[dict[str, Any]]:
        raise NotImplementedError

    def fetch_logs(self, job_id: str) -> dict[str, Any]:
        raise NotImplementedError

    def cancel_job(self, job_id: str) -> dict[str, Any]:
        raise NotImplementedError


class OrchestratorRepository(Protocol):
    def has_active_orchestration(self, *, source_id: str, cycle_time: datetime) -> bool:
        raise NotImplementedError

    def has_active_pipeline(self, *, source_id: str, cycle_time: datetime, model_id: str) -> bool:
        raise NotImplementedError

    def has_active_analysis_run(self, *, model_id: str, start_time: datetime, end_time: datetime) -> bool:
        raise NotImplementedError

    def load_model_context(self, model_id: str) -> ModelContext:
        raise NotImplementedError

    def find_forcing_context(self, *, source_id: str, cycle_time: datetime, model_id: str) -> ForcingContext:
        raise NotImplementedError

    def ensure_forecast_cycle(self, *, source_id: str, cycle_time: datetime) -> dict[str, Any]:
        raise NotImplementedError

    def create_hydro_run(
        self,
        context: ForecastRunContext | AnalysisRunContext,
        manifest: dict[str, Any],
    ) -> dict[str, Any]:
        raise NotImplementedError

    def create_hydro_run_from_basin(
        self,
        basin: Mapping[str, Any],
        manifest: dict[str, Any],
    ) -> dict[str, Any]:
        raise NotImplementedError

    def update_hydro_run_status(
        self,
        run_id: str,
        status: str,
        *,
        slurm_job_id: str | None = None,
        error_code: str | None = None,
        error_message: str | None = None,
    ) -> dict[str, Any]:
        raise NotImplementedError

    def upsert_pipeline_job(self, record: dict[str, Any]) -> dict[str, Any]:
        raise NotImplementedError

    def reserve_pipeline_job(self, record: dict[str, Any]) -> dict[str, Any] | None:
        raise NotImplementedError

    def reclaim_pipeline_job_reservation(self, record: dict[str, Any]) -> dict[str, Any] | None:
        raise NotImplementedError

    def bind_pipeline_job_reservation(
        self,
        idempotency_key: str,
        *,
        slurm_job_id: str,
        status: str = "submitted",
        array_task_id: int | None = None,
    ) -> dict[str, Any] | None:
        raise NotImplementedError

    def query_candidate_state(self, idempotency_key: str) -> dict[str, Any] | None:
        raise NotImplementedError

    def update_pipeline_job_status(
        self,
        job_id: str,
        status: str,
        *,
        started_at: datetime | None = None,
        finished_at: datetime | None = None,
        exit_code: int | None = None,
        error_code: str | None = None,
        error_message: str | None = None,
        log_uri: str | None = None,
    ) -> tuple[str | None, dict[str, Any]]:
        raise NotImplementedError

    def get_pipeline_job(self, job_id: str) -> dict[str, Any] | None:
        raise NotImplementedError

    def query_pipeline_jobs_by_cycle(self, cycle_id: str) -> list[dict[str, Any]]:
        raise NotImplementedError

    def query_pipeline_jobs_by_run(self, run_id: str) -> list[dict[str, Any]]:
        raise NotImplementedError

    def query_pipeline_job_by_slurm_id(self, slurm_job_id: str) -> dict[str, Any] | None:
        raise NotImplementedError

    def insert_pipeline_event(
        self,
        *,
        entity_type: str,
        entity_id: str,
        event_type: str,
        status_from: str | None,
        status_to: str | None,
        message: str | None = None,
        details: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        raise NotImplementedError

    def update_forecast_cycle_status(
        self,
        *,
        source_id: str,
        cycle_time: datetime,
        status: str,
        error_code: str | None = None,
        error_message: str | None = None,
    ) -> dict[str, Any] | None:
        raise NotImplementedError

    def list_stage_statuses(
        self,
        *,
        source_id: str | None,
        cycle_time: datetime,
        model_id: str | None = None,
    ) -> list[dict[str, Any]]:
        raise NotImplementedError


class HttpSlurmGatewayClient(chain_slurm_client.HttpSlurmGatewayClient):
    def __init__(self, base_url: str, *, timeout: float = 30.0) -> None:
        super().__init__(
            base_url,
            timeout=timeout,
            error_cls=SlurmClientError,
            coerce_mapping=_coerce_mapping,
            response_json_or_text=_response_json_or_text,
            error_code_from_response=_error_code_from_response,
        )


class ForecastOrchestrator:
    stages: tuple[StageDefinition, ...] = STAGES
    final_pipeline_status = "complete"

    def __init__(
        self,
        *,
        config: OrchestratorConfig,
        repository: OrchestratorRepository,
        state_manager: StateManager | None = None,
        slurm_client: SlurmGatewayClient | None = None,
        object_store: LocalObjectStore | None = None,
        retry_service: RetryService | None = None,
    ) -> None:
        self.config = config
        self.repository = repository
        self.state_manager = state_manager
        self.slurm_client = slurm_client or HttpSlurmGatewayClient(config.slurm_gateway_url)
        self.object_store = object_store or LocalObjectStore(config.object_store_root, config.object_store_prefix)
        self.retry_service = retry_service
        self.retry_config = getattr(retry_service, "config", None) or RetryConfig()
        self._active_cycles: set[str] = set()
        # M24 §3A: evidence of submits skipped because a concurrent pass already
        # held an active reservation (proof the reserve gate prevented a double
        # submission). Consumed by the scheduler when it persists the overlap
        # receipt artifact.
        self.duplicate_submission_skips: list[dict[str, Any]] = []

    @classmethod
    def from_env(cls) -> ForecastOrchestrator:
        config = OrchestratorConfig.from_env()
        retry_service = _retry_service_from_env()
        return cls(
            config=config,
            repository=PsycopgOrchestratorRepository.from_env(),
            state_manager=StateManager.from_env(),
            retry_service=retry_service,
        )

    def orchestrate_cycle(
        self,
        source: str,
        cycle_time: str | datetime,
        basins: Sequence[Mapping[str, Any] | ModelContext],
    ) -> PipelineResult:
        return chain_forecast_control.orchestrate_cycle(self, source, cycle_time, basins)

    def sync_cycle_statuses(self, cycle_id: str) -> list[dict[str, Any]]:
        return chain_forecast_control.sync_cycle_statuses(self, cycle_id)

    def cancel_active_cycle_jobs(self, cycle_id: str, *, reason: str = "operator_requested") -> list[dict[str, Any]]:
        return chain_forecast_control.cancel_active_cycle_jobs(self, cycle_id, reason=reason)

    def _log_uri_for_pipeline_job(self, job: Mapping[str, Any]) -> str | None:
        return chain_workspace.log_uri_for_pipeline_job(
            self,
            job,
            source_id_from_cycle_id=_source_id_from_cycle_id,
            cycle_time_from_cycle_id=_cycle_time_from_cycle_id,
        )

    def _display_log_publication_for_stage(
        self,
        *,
        source_id: str,
        cycle_time: datetime | None,
        run_id: str,
        job_id: str,
        stage: str,
        existing_log_uri: str | None = None,
    ) -> DisplayLogPublication:
        return chain_workspace.display_log_publication_for_stage(
            self,
            source_id=source_id,
            cycle_time=cycle_time,
            run_id=run_id,
            job_id=job_id,
            stage=stage,
            existing_log_uri=existing_log_uri,
        )

    def _display_log_publication_for_pipeline_job(self, job: Mapping[str, Any]) -> DisplayLogPublication | None:
        return chain_workspace.display_log_publication_for_pipeline_job(self, job)

    def _try_publish_log_for_advertise(
        self, slurm_job_id: str, publication: DisplayLogPublication
    ) -> DisplayLogPublicationAttempt:
        return chain_workspace.try_publish_log_for_advertise(self, slurm_job_id, publication)

    @staticmethod
    def _log_persistence_error(candidate_uri: str, error: Exception) -> OrchestratorError:
        return chain_workspace.log_persistence_error(candidate_uri, error)

    @staticmethod
    def _raise_publish_error_after_durable_update(attempt: DisplayLogPublicationAttempt | None) -> None:
        return chain_workspace.raise_publish_error_after_durable_update(attempt)

    def _run_cycle_chain(self, context: CycleOrchestrationContext) -> PipelineResult:
        from services.orchestrator import chain_forecast_execution

        return chain_forecast_execution._run_cycle_chain(self, context)

    def _retry_cycle_stage_job_id(
        self,
        context: CycleOrchestrationContext,
        stage: StageDefinition,
        _existing_job: Mapping[str, Any],
    ) -> str:
        base_job_id = _pipeline_job_id(context.run_id, stage.stage)
        attempt = context.retry_attempt or _next_retry_attempt_for_stage(
            self._query_pipeline_jobs_for_cycle_context(context),
            base_job_id=base_job_id,
            stage=stage,
        )
        if attempt <= 0:
            attempt = 1
        return _pipeline_retry_job_id(base_job_id, attempt)

    @staticmethod
    def _terminal_stage_needs_manual_retry(
        context: CycleOrchestrationContext,
        job: Mapping[str, Any],
    ) -> bool:
        if context.retry_attempt is None:
            return False
        status = str(job.get("status") or "")
        return status in {"failed", "submission_failed", "permanently_failed", "cancelled", "partially_failed"}

    @staticmethod
    def _terminal_stage_can_retry_after_upstream_refresh(
        job: Mapping[str, Any],
        *,
        refreshed_upstream_finished_at: datetime | None,
    ) -> bool:
        if refreshed_upstream_finished_at is None:
            return False
        status = str(job.get("status") or "")
        if status not in {"failed", "submission_failed", "partially_failed"}:
            return False
        terminal_time = _pipeline_job_terminal_time(job)
        return terminal_time is None or terminal_time <= refreshed_upstream_finished_at

    def _schedule_cycle_stage_retry(self, result: StageRunResult, _failure_number: int) -> str | None:
        if self.retry_service is None:
            return None

        job = self._retry_job_for_stage_result(result)
        if job is None:
            return None

        retry_count = int(getattr(job, "retry_count", 0) or 0)
        backoff_seconds = compute_backoff_seconds(retry_count, self.retry_config.backoff_schedule)
        handled = self.retry_service.handle_failed_job(job)
        handled_status = str(getattr(handled, "status", ""))
        handled_job_id = str(getattr(handled, "job_id"))
        self._release_retry_store_transaction()
        if handled_status != "pending":
            return None
        time.sleep(backoff_seconds)
        return handled_job_id

    def _release_retry_store_transaction(self) -> None:
        service = self.retry_service
        if service is None:
            return
        session = getattr(getattr(service, "store", None), "session", None)
        if session is None:
            return
        in_transaction = getattr(session, "in_transaction", None)
        if callable(in_transaction):
            if in_transaction():
                session.commit()
            return
        commit = getattr(session, "commit", None)
        if callable(commit):
            commit()

    def _retry_job_for_stage_result(self, result: StageRunResult) -> PipelineJob | None:
        from services.orchestrator import chain_forecast_execution

        return chain_forecast_execution._retry_job_for_stage_result(self, result)

    def _retry_partial_array_stage(
        self,
        stage: StageDefinition,
        context: CycleOrchestrationContext,
        result: StageRunResult,
        aggregation: ArrayAggregation,
        had_partial_before_stage: bool,
        last_partial_before_stage: str | None,
    ) -> tuple[StageRunResult, ArrayAggregation] | None:
        from services.orchestrator import chain_forecast_execution

        return chain_forecast_execution._retry_partial_array_stage(
            self, stage, context, result, aggregation, had_partial_before_stage, last_partial_before_stage
        )

    @staticmethod
    def _reindexed_basins_for_task_ids(
        basins: Sequence[Mapping[str, Any]],
        task_ids: Sequence[int],
    ) -> list[dict[str, Any]]:
        by_task_id = {int(basin.get("task_id", index)): dict(basin) for index, basin in enumerate(basins)}
        reindexed: list[dict[str, Any]] = []
        for new_task_id, task_id in enumerate(task_ids):
            entry = dict(by_task_id[int(task_id)])
            entry["task_id"] = new_task_id
            entry["original_task_id"] = int(entry.get("original_task_id", task_id))
            reindexed.append(entry)
        return reindexed

    @staticmethod
    def _chain_stage_execution_dependencies() -> chain_stage_execution.StageExecutionDependencies:
        return chain_stage_execution.StageExecutionDependencies(
            terminal_job_statuses=frozenset(TERMINAL_JOB_STATUSES),
            pipeline_job_id=_pipeline_job_id,
            published_artifact_root_configured=_published_artifact_root_configured,
            cycle_stage_idempotency_key=_cycle_stage_idempotency_key,
            slurm_comment_for=slurm_comment_for,
            cycle_payload_model_id=_cycle_payload_model_id,
            cycle_pipeline_job_model_id=_cycle_pipeline_job_model_id,
            coerce_mapping=_coerce_mapping,
            coerce_array_task_id=_coerce_array_task_id,
            status_from_gateway_job=_status_from_gateway_job,
            parse_gateway_time=_parse_gateway_time,
            utcnow=_utcnow,
            format_time=_format_time,
            safe_pipeline_event_details=_safe_pipeline_event_details,
            submission_runtime_root_contract=_submission_runtime_root_contract,
            aggregation_error_code=_aggregation_error_code,
            aggregation_error_message=_aggregation_error_message,
            slurm_accounting_from_payload=_slurm_accounting_from_payload,
            resource_metrics_from_payload=_resource_metrics_from_payload,
            stage_task_result_evidence=_stage_task_result_evidence,
            stage_status_message=_stage_status_message,
            make_slurm_client_error=SlurmClientError,
            tile_publisher_cls=TilePublisher,
            publish_error_cls=PublishError,
            failure_payload=failure_payload,
            redact_payload=redact_payload,
        )

    def _submit_and_wait_cycle_stage(
        self,
        stage: StageDefinition,
        context: CycleOrchestrationContext,
        *,
        pipeline_job_id: str | None = None,
    ) -> tuple[StageRunResult, ArrayAggregation | None]:
        return chain_stage_execution.submit_and_wait_cycle_stage(
            self,
            stage,
            context,
            pipeline_job_id=pipeline_job_id,
        )

    def _run_local_publish_stage(
        self,
        stage: StageDefinition,
        context: CycleOrchestrationContext,
        *,
        pipeline_job_id: str,
    ) -> StageRunResult:
        return chain_stage_execution.run_local_publish_stage(
            self,
            stage,
            context,
            pipeline_job_id=pipeline_job_id,
        )

    def _write_local_stage_log(self, log_uri: str, payload: Mapping[str, Any]) -> str:
        return chain_workspace.write_local_stage_log(
            self,
            log_uri,
            payload,
            redact_payload_fn=redact_payload,
            absolute_configured_path=_absolute_configured_path,
            ensure_directory=ensure_directory_no_follow,
            atomic_write_bytes=atomic_write_bytes_no_follow,
            safe_filesystem_error_cls=SafeFilesystemError,
        )

    def _resume_cycle_stage(
        self,
        stage: StageDefinition,
        context: CycleOrchestrationContext,
        job: dict[str, Any],
    ) -> tuple[StageRunResult, ArrayAggregation | None]:
        return chain_stage_execution.resume_cycle_stage(self, stage, context, job)

    def _poll_cycle_stage_until_terminal(
        self,
        *,
        stage: StageDefinition,
        context: CycleOrchestrationContext,
        pipeline_job_id: str,
        initial_job: dict[str, Any],
        initial_status: str,
        log_publication: DisplayLogPublication | None,
    ) -> TerminalJobObservation:
        return chain_stage_execution.poll_cycle_stage_until_terminal(
            self,
            stage=stage,
            context=context,
            pipeline_job_id=pipeline_job_id,
            initial_job=initial_job,
            initial_status=initial_status,
            log_publication=log_publication,
        )

    def _record_cycle_stage_poll_timeout(
        self,
        *,
        stage: StageDefinition,
        context: CycleOrchestrationContext,
        pipeline_job_id: str,
        job: dict[str, Any],
        current_status: str,
        log_publication: DisplayLogPublication | None,
    ) -> TerminalJobObservation:
        return chain_stage_execution.record_cycle_stage_poll_timeout(
            self,
            stage=stage,
            context=context,
            pipeline_job_id=pipeline_job_id,
            job=job,
            current_status=current_status,
            log_publication=log_publication,
        )

    def _submit_array_stage(
        self,
        stage: StageDefinition,
        context: CycleOrchestrationContext,
        tasks: list[dict[str, Any]],
        manifest: dict[str, Any],
    ) -> dict[str, Any]:
        return chain_stage_execution.submit_array_stage(self, stage, context, tasks, manifest)

    def _slurm_submission_manifest(self, manifest: Mapping[str, Any]) -> dict[str, Any]:
        return chain_stage_execution.slurm_submission_manifest(self, manifest)

    def _aggregate_array_stage(
        self,
        stage: StageDefinition,
        context: CycleOrchestrationContext,
        slurm_job_id: str,
        terminal: dict[str, Any],
        pipeline_job_id: str,
    ) -> ArrayAggregation:
        return chain_array_accounting.aggregate_array_stage(
            self,
            stage,
            context,
            slurm_job_id,
            terminal,
            pipeline_job_id,
            deps=_array_accounting_dependencies(),
        )

    def _require_complete_array_accounting(
        self,
        aggregation: ArrayAggregation,
        *,
        stage: StageDefinition,
        context: CycleOrchestrationContext,
        slurm_job_id: str,
    ) -> ArrayAggregation:
        return chain_array_accounting.require_complete_array_accounting(
            aggregation,
            stage=stage,
            context=context,
            slurm_job_id=slurm_job_id,
        )

    def _record_cycle_stage_status_override(
        self,
        stage: StageDefinition,
        context: CycleOrchestrationContext,
        pipeline_job_id: str,
        terminal: dict[str, Any],
        aggregation: ArrayAggregation,
        log_uri: str | None,
    ) -> None:
        chain_array_accounting.record_cycle_stage_status_override(
            self,
            stage,
            context,
            pipeline_job_id,
            terminal,
            aggregation,
            log_uri,
            deps=_array_accounting_dependencies(),
        )

    def _record_cycle_stage_accounting_event(
        self,
        stage: StageDefinition,
        context: CycleOrchestrationContext,
        pipeline_job_id: str,
        terminal: Mapping[str, Any],
        *,
        log_uri: str | None,
    ) -> None:
        chain_array_accounting.record_cycle_stage_accounting_event(
            self,
            stage,
            context,
            pipeline_job_id,
            terminal,
            log_uri=log_uri,
            deps=_array_accounting_dependencies(),
        )

    def _record_cycle_stage_accounting_gap(
        self,
        stage: StageDefinition,
        context: CycleOrchestrationContext,
        pipeline_job_id: str,
        *,
        slurm_job_id: str,
        message: str,
        details: Mapping[str, Any],
    ) -> None:
        chain_array_accounting.record_cycle_stage_accounting_gap(
            self,
            stage,
            context,
            pipeline_job_id,
            slurm_job_id=slurm_job_id,
            message=message,
            details=details,
            deps=_array_accounting_dependencies(),
        )

    def _after_cycle_stage_terminal(
        self,
        stage: StageDefinition,
        context: CycleOrchestrationContext,
        result_status: str,
        terminal: dict[str, Any],
        aggregation: ArrayAggregation | None,
    ) -> None:
        from services.orchestrator import chain_forecast_execution

        return chain_forecast_execution._after_cycle_stage_terminal(
            self, stage, context, result_status, terminal, aggregation
        )

    def _reserve_cycle_stage(
        self,
        stage: StageDefinition,
        context: CycleOrchestrationContext,
        pipeline_job_id: str,
        idempotency_key: str,
    ) -> ReservationResult | None:
        """Phase 1 durable reservation; best-effort for legacy repositories.

        Returns the ``ReservationResult`` so the submit path can gate sbatch on
        the DB win/lose signal (skip when a concurrent pass already reserved an
        active candidate). ``None`` only for legacy repositories without the
        reservation surface (gate is a no-op there).
        """

        if not hasattr(self.repository, "reserve_pipeline_job"):
            return None
        return reserve_candidate(
            self.repository,
            idempotency_key=idempotency_key,
            job_id=pipeline_job_id,
            run_id=context.run_id,
            cycle_id=context.cycle_id,
            job_type=stage.job_type,
            model_id=_cycle_pipeline_job_model_id(context),
            stage=stage.stage,
            candidate_id=context.run_id,
        )

    def _reservation_already_inflight(self, reservation: ReservationResult | None) -> bool:
        """True when THIS pass lost the reservation and must NOT sbatch.

        Gate for the submit path: a loss (``created=False``) means this pass
        neither inserted a fresh reservation nor reclaimed a dead one, so another
        row genuinely holds the idempotency_key in a live state (or a concurrent
        take-over won) — this pass skips submission. A dead, re-submittable row
        (``submission_failed`` / ``reservation_lost``, never bound) is instead
        taken over atomically inside ``reserve_candidate`` (via
        ``reclaim_pipeline_job_reservation``), turning that case into
        ``created=True`` — so it never reaches this gate. No re-read status is
        consulted here, hence no TOCTOU double-submit.
        """

        return reservation is not None and reservation.already_inflight and not reservation.created

    def _bind_cycle_stage_reservation(
        self,
        idempotency_key: str,
        *,
        slurm_job_id: str,
        array_task_id: int | None,
    ) -> None:
        """Phase 2 atomic bind; best-effort for legacy repositories."""

        if not hasattr(self.repository, "bind_pipeline_job_reservation"):
            return
        bind_reservation(
            self.repository,
            idempotency_key=idempotency_key,
            slurm_job_id=slurm_job_id,
            status="submitted",
            array_task_id=array_task_id,
        )

    def _before_cycle_stage_submit(self, stage: StageDefinition, context: CycleOrchestrationContext) -> None:
        if stage.stage == "download":
            self.repository.update_forecast_cycle_status(
                source_id=context.source_id,
                cycle_time=context.cycle_time,
                status="downloading",
            )
        elif stage.stage == "forecast":
            self.repository.update_forecast_cycle_status(
                source_id=context.source_id,
                cycle_time=context.cycle_time,
                status="forecast_running",
            )

    def _record_submission_failure(
        self,
        stage: StageDefinition,
        context: CycleOrchestrationContext,
        error: Exception,
        *,
        pipeline_job_id: str | None = None,
    ) -> StageRunResult:
        from services.orchestrator import chain_forecast_submission

        return chain_forecast_submission._record_submission_failure(
            self, stage, context, error, pipeline_job_id=pipeline_job_id
        )

    def _skip_duplicate_submission(
        self,
        stage: StageDefinition,
        context: CycleOrchestrationContext,
        pipeline_job_id: str,
        reservation: ReservationResult | None,
    ) -> StageRunResult:
        from services.orchestrator import chain_forecast_submission

        return chain_forecast_submission._skip_duplicate_submission(self, stage, context, pipeline_job_id, reservation)

    def _apply_array_progress(
        self,
        stage: StageDefinition,
        context: CycleOrchestrationContext,
        aggregation: ArrayAggregation,
    ) -> None:
        chain_array_accounting.apply_array_progress(
            self,
            stage,
            context,
            aggregation,
            deps=_array_accounting_dependencies(),
        )

    def _success_cycle_status(self, stage: StageDefinition, context: CycleOrchestrationContext) -> str:
        if not context.had_partial:
            return stage.success_cycle_status
        if stage.stage in {"parse", "state_save_qc", "frequency", "publish"}:
            return "parsed_partial"
        if stage.stage in {"forcing", "forecast"}:
            return "forcing_ready_partial"
        return context.last_partial_status or stage.success_cycle_status

    def _partial_cycle_status(self, stage: StageDefinition) -> str:
        if stage.stage in {"parse", "state_save_qc", "frequency"}:
            return "parsed_partial"
        return "forcing_ready_partial"

    def _build_cycle_stage_manifest(self, stage: StageDefinition, context: CycleOrchestrationContext) -> dict[str, Any]:
        return chain_manifests.build_cycle_stage_manifest(
            self,
            stage,
            context,
            model_run_stage_evidence=_model_run_stage_evidence,
            frequency_quality_state=_frequency_quality_state,
            publish_quality_state=_publish_quality_state,
            cycle_residual_blockers=_cycle_residual_blockers,
        )

    def _write_cycle_manifest_index(
        self,
        context: CycleOrchestrationContext,
        stage: StageDefinition,
        tasks: list[dict[str, Any]],
    ) -> Path:
        return chain_manifests.write_cycle_manifest_index(self, context, stage, tasks)

    def _prepare_forecast_runtime_manifests(
        self,
        stage: StageDefinition,
        context: CycleOrchestrationContext,
    ) -> None:
        chain_manifests.prepare_forecast_runtime_manifests(
            self,
            stage,
            context,
            assembly_payload_from_runtime_manifest=_assembly_payload_from_runtime_manifest,
        )

    def _mark_staged_hydro_runs_failed(
        self,
        run_ids: Sequence[str],
        *,
        error_code: str,
        error_message: str,
    ) -> None:
        for run_id in run_ids:
            try:
                self.repository.update_hydro_run_status(
                    run_id,
                    "failed",
                    error_code=error_code,
                    error_message=error_message,
                )
            except Exception:
                continue

    def _build_forecast_runtime_manifest(
        self,
        context: CycleOrchestrationContext,
        basin: Mapping[str, Any],
    ) -> dict[str, Any]:
        return chain_manifests.build_forecast_runtime_manifest(
            self,
            context,
            basin,
            assembly_builder=build_model_run_assembly,
            forecast_state_checkpoint_hours=_forecast_state_checkpoint_hours,
        )

    def _validate_forecast_runtime_manifest(
        self,
        manifest_path: Path,
        manifest: Mapping[str, Any],
        *,
        task_index: int,
    ) -> None:
        chain_manifests.validate_forecast_runtime_manifest(
            self,
            manifest_path,
            manifest,
            task_index=task_index,
        )

    def _reindexed_manifest_entries(self, basins: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
        return chain_manifests.reindexed_manifest_entries(
            self,
            basins,
            reindex_builder=build_reindexed_manifest,
            assembly_builder=build_model_run_assembly,
        )

    def _normalize_cycle_basins(
        self,
        basins: Sequence[Mapping[str, Any] | ModelContext],
        source_id: str,
        cycle_time: datetime,
    ) -> list[dict[str, Any]]:
        return chain_forecast_cycle.normalize_cycle_basins(self, basins, source_id, cycle_time)

    def _apply_cohort_warm_start(
        self,
        basins: Sequence[dict[str, Any]],
        source_id: str,
        cycle_time: datetime,
    ) -> None:
        chain_forecast_cycle.apply_cohort_warm_start(self, basins, source_id, cycle_time)

    def _validate_cycle_basin_identities(
        self,
        basins: Sequence[Mapping[str, Any]],
        source_id: str,
        cycle_time: datetime,
        cycle_id: str,
    ) -> None:
        chain_forecast_cycle.validate_cycle_basin_identities(self, basins, source_id, cycle_time, cycle_id)

    def _query_pipeline_jobs_by_cycle(self, cycle_id: str) -> list[dict[str, Any]]:
        return chain_forecast_cycle.query_pipeline_jobs_by_cycle(self, cycle_id)

    def _query_pipeline_jobs_for_cycle_context(self, context: CycleOrchestrationContext) -> list[dict[str, Any]]:
        return chain_forecast_cycle.query_pipeline_jobs_for_cycle_context(self, context)

    def _find_existing_stage_job(
        self,
        jobs: Sequence[Mapping[str, Any]],
        stage: StageDefinition,
        *,
        context: CycleOrchestrationContext,
    ) -> dict[str, Any] | None:
        return chain_forecast_cycle.find_existing_stage_job(self, jobs, stage, context=context)

    def _cycle_download_success_missing_raw_manifest(
        self,
        stage: StageDefinition,
        context: CycleOrchestrationContext,
        job: Mapping[str, Any],
    ) -> bool:
        return chain_forecast_cycle.cycle_download_success_missing_raw_manifest(self, stage, context, job)

    @staticmethod
    def _job_matches_stage(job: Mapping[str, Any], stage: StageDefinition) -> bool:
        return chain_forecast_cycle.job_matches_stage(job, stage)

    @staticmethod
    def _job_needs_submission(job: Mapping[str, Any]) -> bool:
        return chain_forecast_cycle.job_needs_submission(job)

    def trigger_forecast(
        self,
        *,
        source_id: str | None = None,
        cycle_time: str | datetime,
        model_id: str,
        basin_id: str | None = None,
        max_lead_hours: int | None = None,
    ) -> PipelineResult:
        return chain_forecast_trigger.trigger_forecast(
            self,
            source_id=source_id,
            cycle_time=cycle_time,
            model_id=model_id,
            basin_id=basin_id,
            max_lead_hours=max_lead_hours,
        )

    def trigger_forecast_from_canonical(
        self,
        *,
        source_id: str | None = None,
        cycle_time: str | datetime,
        model_id: str,
        basin_id: str | None = None,
        max_lead_hours: int | None = None,
    ) -> PipelineResult:
        return chain_forecast_trigger.trigger_forecast_from_canonical(
            self,
            source_id=source_id,
            cycle_time=cycle_time,
            model_id=model_id,
            basin_id=basin_id,
            max_lead_hours=max_lead_hours,
        )

    def _trigger_forecast(
        self,
        *,
        source_id: str,
        cycle_time: str | datetime,
        model_id: str,
        basin_id: str | None,
        max_lead_hours: int | None,
        stages: Sequence[StageDefinition],
    ) -> PipelineResult:
        return chain_forecast_trigger.trigger_forecast_impl(
            self,
            source_id=source_id,
            cycle_time=cycle_time,
            model_id=model_id,
            basin_id=basin_id,
            max_lead_hours=max_lead_hours,
            stages=stages,
        )

    def run_chain(
        self,
        context: ForecastRunContext | AnalysisRunContext,
        *,
        stages: Sequence[StageDefinition] | None = None,
    ) -> PipelineResult:
        stage_results: list[StageRunResult] = []
        selected_stages = tuple(stages or self.stages)
        for index, stage in enumerate(selected_stages):
            result = self._submit_and_wait(stage, context, first_stage=index == 0)
            stage_results.append(result)
            if result.status != "succeeded":
                return PipelineResult(context.run_id, context.cycle_id, "failed", tuple(stage_results))
        return PipelineResult(context.run_id, context.cycle_id, self.final_pipeline_status, tuple(stage_results))

    def stage_statuses(
        self,
        *,
        cycle_time: str | datetime,
        source_id: str | None = None,
        model_id: str | None = None,
    ) -> list[dict[str, Any]]:
        return chain_forecast_trigger.stage_statuses(
            self,
            cycle_time=cycle_time,
            source_id=source_id,
            model_id=model_id,
        )

    def trigger_ready_forecasts(
        self,
        *,
        source_id: str | None = None,
        model_ids: Sequence[str] | None = None,
        limit: int = 100,
    ) -> tuple[PipelineResult, ...]:
        return chain_forecast_trigger.trigger_ready_forecasts(
            self,
            source_id=source_id,
            model_ids=model_ids,
            limit=limit,
        )

    def _demote_stale_canonical_cycle(
        self,
        *,
        source_id: str,
        cycle_time: datetime,
        stale_versions: set[str | None],
    ) -> None:
        chain_forecast_trigger.demote_stale_canonical_cycle(
            self,
            source_id=source_id,
            cycle_time=cycle_time,
            stale_versions=stale_versions,
        )

    def _validate_auto_trigger_canonical_readiness(
        self,
        cycle: Mapping[str, Any],
        *,
        source_id: str,
        cycle_time: datetime,
        max_lead_hours: int | None,
    ) -> dict[str, Any]:
        return chain_forecast_trigger.validate_auto_trigger_canonical_readiness(
            self,
            cycle,
            source_id=source_id,
            cycle_time=cycle_time,
            max_lead_hours=max_lead_hours,
        )

    def _list_canonical_ready_cycles(self, *, source_id: str | None, limit: int) -> tuple[dict[str, Any], ...]:
        return chain_forecast_trigger.list_canonical_ready_cycles(self, source_id=source_id, limit=limit)

    def _list_forecast_model_ids(self) -> tuple[str, ...]:
        return chain_forecast_trigger.list_forecast_model_ids(self)

    def _has_completed_forecast(self, *, source_id: str, cycle_time: datetime, model_id: str) -> bool:
        return chain_forecast_trigger.has_completed_forecast(
            self,
            source_id=source_id,
            cycle_time=cycle_time,
            model_id=model_id,
        )

    def _submit_and_wait(
        self,
        stage: StageDefinition,
        context: ForecastRunContext | AnalysisRunContext,
        *,
        first_stage: bool,
    ) -> StageRunResult:
        from services.orchestrator import chain_forecast_execution

        return chain_forecast_execution._submit_and_wait(self, stage, context, first_stage=first_stage)

    def _build_stage_submission_manifest(
        self,
        stage: StageDefinition,
        context: ForecastRunContext | AnalysisRunContext,
    ) -> dict[str, Any]:
        from services.orchestrator import chain_forecast_execution

        return chain_forecast_execution._build_stage_submission_manifest(self, stage, context)

    def _validate_analysis_template_context(self, context: AnalysisRunContext) -> None:
        for label, val in [
            ("source_id", context.source_id),
            ("model_id", context.model_id),
            ("run_id", context.run_id),
            ("basin_version_id", context.basin_version_id),
            ("river_network_version_id", context.river_network_version_id),
        ]:
            if not _SAFE_ID_RE.match(val):
                raise OrchestratorError("UNSAFE_TEMPLATE_PARAM", f"{label} contains unsafe characters: {val!r}")
        if context.basin_id and not _SAFE_ID_RE.match(context.basin_id):
            raise OrchestratorError("UNSAFE_TEMPLATE_PARAM", f"basin_id unsafe: {context.basin_id!r}")
        if not _SAFE_AREA_RE.match(self.config.era5_area):
            raise OrchestratorError("UNSAFE_TEMPLATE_PARAM", f"era5_area unsafe: {self.config.era5_area!r}")

    def _poll_until_terminal(
        self,
        *,
        stage: StageDefinition,
        context: ForecastRunContext | AnalysisRunContext,
        pipeline_job_id: str,
        initial_job: dict[str, Any],
        initial_status: str,
        log_publication: DisplayLogPublication,
    ) -> TerminalJobObservation:
        from services.orchestrator import chain_forecast_execution

        return chain_forecast_execution._poll_until_terminal(
            self,
            stage=stage,
            context=context,
            pipeline_job_id=pipeline_job_id,
            initial_job=initial_job,
            initial_status=initial_status,
            log_publication=log_publication,
        )

    def _record_stage_poll_timeout(
        self,
        *,
        stage: StageDefinition,
        context: ForecastRunContext | AnalysisRunContext,
        pipeline_job_id: str,
        job: dict[str, Any],
        current_status: str,
        log_publication: DisplayLogPublication,
    ) -> TerminalJobObservation:
        from services.orchestrator import chain_forecast_execution

        return chain_forecast_execution._record_stage_poll_timeout(
            self,
            stage=stage,
            context=context,
            pipeline_job_id=pipeline_job_id,
            job=job,
            current_status=current_status,
            log_publication=log_publication,
        )

    def _before_stage_submit(
        self,
        stage: StageDefinition,
        context: ForecastRunContext | AnalysisRunContext,
    ) -> None:
        if stage.stage in {"download_gfs", "download"}:
            self.repository.update_forecast_cycle_status(
                source_id=context.source_id,
                cycle_time=context.cycle_time,
                status="downloading",
            )
        elif stage.stage in {"run_shud_forecast", "forecast"}:
            self.repository.update_forecast_cycle_status(
                source_id=context.source_id,
                cycle_time=context.cycle_time,
                status="forecast_running",
            )

    def _after_stage_success(
        self,
        stage: StageDefinition,
        context: ForecastRunContext | AnalysisRunContext,
        _terminal: dict[str, Any],
    ) -> None:
        self.repository.update_forecast_cycle_status(
            source_id=context.source_id,
            cycle_time=context.cycle_time,
            status=stage.success_cycle_status,
        )
        if stage.stage in {"run_shud_forecast", "forecast"}:
            self.repository.update_hydro_run_status(context.run_id, "succeeded")
        elif stage.stage in {"parse_output", "parse"}:
            self.repository.update_hydro_run_status(context.run_id, "parsed")

    def _after_stage_failure(
        self,
        stage: StageDefinition,
        context: ForecastRunContext | AnalysisRunContext,
        terminal: dict[str, Any],
    ) -> None:
        error_code = terminal.get("error_code") or f"{stage.stage.upper()}_{terminal['status'].upper()}"
        error_message = terminal.get("error_message") or f"Stage {stage.stage} ended with {terminal['status']}."
        self.repository.update_forecast_cycle_status(
            source_id=context.source_id,
            cycle_time=context.cycle_time,
            status=stage.failure_cycle_status,
            error_code=error_code,
            error_message=error_message,
        )
        self.repository.update_hydro_run_status(
            context.run_id,
            "failed",
            error_code=error_code,
            error_message=error_message,
        )

    def _after_stage_status_change(
        self,
        _stage: StageDefinition,
        _context: ForecastRunContext | AnalysisRunContext,
        _status_from: str | None,
        _status_to: str,
        _job: dict[str, Any],
    ) -> None:
        return None

    def _pipeline_event_target(
        self,
        _context: ForecastRunContext | AnalysisRunContext,
        pipeline_job_id: str,
    ) -> tuple[str, str]:
        return "pipeline_job", pipeline_job_id

    def render_stage_template(self, stage: StageDefinition, context: ForecastRunContext | AnalysisRunContext) -> str:
        from services.orchestrator import chain_forecast_templates

        return chain_forecast_templates.render_stage_template(self, stage, context)

    def _persist_gateway_logs(self, slurm_job_id: str, log_uri: str) -> None:
        return chain_workspace.persist_gateway_logs(
            self,
            slurm_job_id,
            log_uri,
            coerce_mapping=_coerce_mapping,
            absolute_configured_path=_absolute_configured_path,
            ensure_directory=ensure_directory_no_follow,
            atomic_write_bytes=atomic_write_bytes_no_follow,
            safe_filesystem_error_cls=SafeFilesystemError,
            artifact_log_error_cls=ArtifactLogError,
        )

    def _log_uri_for_stage(
        self,
        *,
        source_id: str,
        cycle_time: datetime | None,
        run_id: str,
        job_id: str,
        stage: str,
    ) -> str:
        return chain_workspace.log_uri_for_stage(
            self,
            source_id=source_id,
            cycle_time=cycle_time,
            run_id=run_id,
            job_id=job_id,
            stage=stage,
            published_artifact_root_configured=_published_artifact_root_configured,
            utcnow=_utcnow,
            log_stream_for_stage=_log_stream_for_stage,
            normalize_source_id_fn=normalize_source_id,
            published_log_uri_fn=published_log_uri,
        )

    def _published_log_path(self, log_uri: str) -> Path | None:
        return chain_workspace.published_log_path(
            log_uri,
            absolute_configured_path=_absolute_configured_path,
            published_log_relative_path_fn=published_log_relative_path,
        )

    def _build_run_context(
        self,
        source_id: str,
        cycle_time: datetime,
        model: ModelContext,
        forcing: ForcingContext,
        initial_state: InitialStateSelection | None = None,
        max_lead_hours: int | None = None,
    ) -> ForecastRunContext:
        from services.orchestrator import chain_forecast_state

        return chain_forecast_state._build_run_context(
            self, source_id, cycle_time, model, forcing, initial_state, max_lead_hours
        )

    def _forecast_scenario_id(self, source_id: str) -> str:
        from services.orchestrator import chain_forecast_state

        return chain_forecast_state._forecast_scenario_id(self, source_id)

    def _build_run_manifest(self, context: ForecastRunContext) -> dict[str, Any]:
        from services.orchestrator import chain_forecast_state

        return chain_forecast_state._build_run_manifest(self, context)

    def _state_passes_qc(self, state: StateSnapshot) -> bool:
        from services.orchestrator import chain_forecast_state

        return chain_forecast_state._state_passes_qc(self, state)

    def _select_forecast_initial_state(
        self,
        *,
        model_id: str,
        cycle_time: datetime,
        source_id: str | None = None,
        model_package_version: str | None = None,
        model_package_checksum: str | None = None,
        max_lead_hours: int | None = None,
    ) -> InitialStateSelection:
        from services.orchestrator import chain_forecast_state

        return chain_forecast_state._select_forecast_initial_state(
            self,
            model_id=model_id,
            cycle_time=cycle_time,
            source_id=source_id,
            model_package_version=model_package_version,
            model_package_checksum=model_package_checksum,
            max_lead_hours=max_lead_hours,
        )

    def _select_strict_forecast_initial_state(
        self,
        *,
        model_id: str,
        cycle_time: datetime,
        source_id: str | None,
        model_package_version: str | None,
        model_package_checksum: str | None,
    ) -> InitialStateSelection:
        from services.orchestrator import chain_forecast_state

        return chain_forecast_state._select_strict_forecast_initial_state(
            self,
            model_id=model_id,
            cycle_time=cycle_time,
            source_id=source_id,
            model_package_version=model_package_version,
            model_package_checksum=model_package_checksum,
        )

    def _validate_prefilled_forecast_initial_state(
        self,
        basin: Mapping[str, Any],
        *,
        source_id: str | None,
        cycle_time: datetime,
        model_package_version: str | None,
        model_package_checksum: str | None,
    ) -> InitialStateSelection:
        from services.orchestrator import chain_forecast_state

        return chain_forecast_state._validate_prefilled_forecast_initial_state(
            self,
            basin,
            source_id=source_id,
            cycle_time=cycle_time,
            model_package_version=model_package_version,
            model_package_checksum=model_package_checksum,
        )

    def _resolve_prefilled_forecast_state(
        self,
        basin: Mapping[str, Any],
        *,
        model_id: str,
        cycle_time: datetime,
        source_id: str | None,
    ) -> StateSnapshot | None:
        from services.orchestrator import chain_forecast_state

        return chain_forecast_state._resolve_prefilled_forecast_state(
            self, basin, model_id=model_id, cycle_time=cycle_time, source_id=source_id
        )

    def _validate_prefilled_state_identity(
        self,
        basin: Mapping[str, Any],
        selection: InitialStateSelection,
    ) -> None:
        from services.orchestrator import chain_forecast_state

        return chain_forecast_state._validate_prefilled_state_identity(self, basin, selection)

    def _validate_strict_forecast_state(
        self,
        state: StateSnapshot,
        *,
        model_id: str,
        cycle_time: datetime,
        source_id: str | None,
        model_package_version: str | None,
        model_package_checksum: str | None,
    ) -> InitialStateSelection:
        from services.orchestrator import chain_forecast_state

        return chain_forecast_state._validate_strict_forecast_state(
            self,
            state,
            model_id=model_id,
            cycle_time=cycle_time,
            source_id=source_id,
            model_package_version=model_package_version,
            model_package_checksum=model_package_checksum,
        )

    def _get_exact_forecast_state(
        self,
        *,
        model_id: str,
        cycle_time: datetime,
        source_id: str | None,
    ) -> StateSnapshot | None:
        from services.orchestrator import chain_forecast_state

        return chain_forecast_state._get_exact_forecast_state(
            self, model_id=model_id, cycle_time=cycle_time, source_id=source_id
        )

    def _exact_or_latest_usable_state(
        self,
        *,
        model_id: str,
        cycle_time: datetime,
        before_time: datetime,
        source_id: str | None,
    ) -> StateSnapshot | None:
        from services.orchestrator import chain_forecast_state

        return chain_forecast_state._exact_or_latest_usable_state(
            self, model_id=model_id, cycle_time=cycle_time, before_time=before_time, source_id=source_id
        )

    def _write_run_manifest(self, context: ForecastRunContext | AnalysisRunContext, manifest: dict[str, Any]) -> None:
        chain_manifests.write_run_manifest(self, context, manifest)

    def _workspace_path(self, *parts: str) -> Path:
        return chain_workspace.workspace_path(self, *parts)

    def _safe_workspace_write_bytes(self, path: Path, content: bytes) -> Path:
        return chain_workspace.safe_workspace_write_bytes(
            self,
            path,
            content,
            workspace_relative_parts_fn=_workspace_relative_parts,
            ensure_directory=ensure_directory_no_follow,
            atomic_write_bytes=atomic_write_bytes_no_follow,
        )

    def _safe_workspace_read_bytes(self, path: Path) -> bytes:
        return chain_workspace.safe_workspace_read_bytes(
            self,
            path,
            workspace_relative_parts_fn=_workspace_relative_parts,
            read_bytes=read_bytes_no_follow,
        )


_CHAIN_STAGE_EXECUTION_COMPAT_FACADE_MISSING = tuple(
    name
    for name in _CHAIN_STAGE_EXECUTION_COMPAT_FORWARDER_NAMES
    if not callable(getattr(ForecastOrchestrator, name, None))
)
if _CHAIN_STAGE_EXECUTION_COMPAT_FACADE_MISSING:
    raise RuntimeError(
        "chain stage execution compatibility forwarders missing from facade: "
        f"{', '.join(_CHAIN_STAGE_EXECUTION_COMPAT_FACADE_MISSING)}"
    )

_CHAIN_RESERVATION_COMPAT_METHOD_FACADE_MISSING = tuple(
    name
    for name in _CHAIN_RESERVATION_COMPAT_METHOD_FORWARDER_NAMES
    if not callable(getattr(ForecastOrchestrator, name, None))
)
if _CHAIN_RESERVATION_COMPAT_METHOD_FACADE_MISSING:
    raise RuntimeError(
        "chain reservation compatibility methods missing from facade: "
        f"{', '.join(_CHAIN_RESERVATION_COMPAT_METHOD_FACADE_MISSING)}"
    )
_CHAIN_RESERVATION_COMPAT_OWNER_METHOD_FORWARDERS = MappingProxyType(
    {
        facade_name: getattr(reservation, owner_name)
        for facade_name, owner_name in zip(
            _CHAIN_RESERVATION_COMPAT_OWNER_METHOD_FORWARDER_NAMES,
            _CHAIN_RESERVATION_COMPAT_OWNER_METHOD_OWNER_FUNCTION_NAMES,
            strict=True,
        )
    }
)
_chain_retry_init_code = ForecastOrchestrator.__init__.__code__
_chain_retry_init_param_names = _chain_retry_init_code.co_varnames[
    : _chain_retry_init_code.co_argcount + _chain_retry_init_code.co_kwonlyargcount
]
_CHAIN_RETRY_COMPAT_CONSTRUCTOR_PARAM_MISSING = tuple(
    name for name in _CHAIN_RETRY_COMPAT_CONSTRUCTOR_PARAM_NAMES if name not in _chain_retry_init_param_names
)
_CHAIN_RETRY_COMPAT_CONSTRUCTOR_CONFIG_DRIFT = tuple(
    name for name in _CHAIN_RETRY_COMPAT_INSTANCE_CONFIG_NAMES if name not in _chain_retry_init_code.co_names
)
if _CHAIN_RETRY_COMPAT_CONSTRUCTOR_PARAM_MISSING:
    raise RuntimeError(
        "chain retry constructor parameters missing from facade: "
        f"{', '.join(_CHAIN_RETRY_COMPAT_CONSTRUCTOR_PARAM_MISSING)}"
    )
if _CHAIN_RETRY_COMPAT_CONSTRUCTOR_CONFIG_DRIFT:
    raise RuntimeError(
        "chain retry constructor config binding drifted from facade: "
        f"{', '.join(_CHAIN_RETRY_COMPAT_CONSTRUCTOR_CONFIG_DRIFT)}"
    )
_CHAIN_RETRY_COMPAT_LOCAL_METHOD_MISSING = tuple(
    name for name in _CHAIN_RETRY_COMPAT_LOCAL_METHOD_NAMES if not callable(getattr(ForecastOrchestrator, name, None))
)
if _CHAIN_RETRY_COMPAT_LOCAL_METHOD_MISSING:
    raise RuntimeError(
        f"chain retry local bridge methods missing from facade: {', '.join(_CHAIN_RETRY_COMPAT_LOCAL_METHOD_MISSING)}"
    )
_CHAIN_RETRY_COMPAT_CHAIN_LOCAL_METHODS = MappingProxyType(
    {name: getattr(ForecastOrchestrator, name) for name in _CHAIN_RETRY_COMPAT_LOCAL_METHOD_NAMES}
)
del _chain_retry_init_code, _chain_retry_init_param_names


AnalysisOrchestrator = importlib.import_module(
    "services.orchestrator.chain_analysis_orchestrator"
).AnalysisOrchestrator
AnalysisOrchestrator.__module__ = __name__


_CHAIN_MANIFEST_COMPAT_FORECAST_METHOD_FACADE_MISSING = tuple(
    name
    for name in _CHAIN_MANIFEST_COMPAT_FORECAST_METHOD_FORWARDER_NAMES
    if not callable(getattr(ForecastOrchestrator, name, None))
)
if _CHAIN_MANIFEST_COMPAT_FORECAST_METHOD_FACADE_MISSING:
    raise RuntimeError(
        "chain manifest compatibility forecast methods missing from facade: "
        f"{', '.join(_CHAIN_MANIFEST_COMPAT_FORECAST_METHOD_FACADE_MISSING)}"
    )
_CHAIN_MANIFEST_COMPAT_ANALYSIS_METHOD_FACADE_MISSING = tuple(
    name
    for name in _CHAIN_MANIFEST_COMPAT_ANALYSIS_METHOD_FORWARDER_NAMES
    if not callable(getattr(AnalysisOrchestrator, name, None))
)
if _CHAIN_MANIFEST_COMPAT_ANALYSIS_METHOD_FACADE_MISSING:
    raise RuntimeError(
        "chain manifest compatibility analysis methods missing from facade: "
        f"{', '.join(_CHAIN_MANIFEST_COMPAT_ANALYSIS_METHOD_FACADE_MISSING)}"
    )
_CHAIN_MANIFEST_COMPAT_FORECAST_METHOD_FORWARDERS = MappingProxyType(
    {
        facade_name: getattr(chain_manifests, owner_name)
        for facade_name, owner_name in zip(
            _CHAIN_MANIFEST_COMPAT_FORECAST_METHOD_FORWARDER_NAMES,
            _CHAIN_MANIFEST_COMPAT_FORECAST_METHOD_OWNER_FUNCTION_NAMES,
            strict=True,
        )
    }
)
_CHAIN_MANIFEST_COMPAT_ANALYSIS_METHOD_FORWARDERS = MappingProxyType(
    {
        facade_name: getattr(chain_manifests, owner_name)
        for facade_name, owner_name in zip(
            _CHAIN_MANIFEST_COMPAT_ANALYSIS_METHOD_FORWARDER_NAMES,
            _CHAIN_MANIFEST_COMPAT_ANALYSIS_METHOD_OWNER_FUNCTION_NAMES,
            strict=True,
        )
    }
)
_CHAIN_MANIFEST_COMPAT_DEPENDENCY_BINDING_DRIFT = tuple(
    f"{binding_owner}.{field}"
    for binding_owner, bindings in _CHAIN_MANIFEST_COMPAT_DEPENDENCY_BINDINGS.items()
    for field, facade_name in bindings
    if not callable(globals().get(facade_name))
)
if _CHAIN_MANIFEST_COMPAT_DEPENDENCY_BINDING_DRIFT:
    raise RuntimeError(
        "chain manifest compatibility dependency bindings missing from facade: "
        f"{', '.join(_CHAIN_MANIFEST_COMPAT_DEPENDENCY_BINDING_DRIFT)}"
    )

from services.orchestrator.chain_repository import PsycopgOrchestratorRepository  # noqa: E402

PsycopgOrchestratorRepository.__module__ = __name__


_CHAIN_PERSISTENCE_REPOSITORY_COMPAT_LOCAL_REPOSITORY_MISSING = tuple(
    name
    for name in _CHAIN_PERSISTENCE_REPOSITORY_COMPAT_LOCAL_REPOSITORY_NAMES
    if not isinstance(globals().get(name), type)
)
if _CHAIN_PERSISTENCE_REPOSITORY_COMPAT_LOCAL_REPOSITORY_MISSING:
    raise RuntimeError(
        "chain persistence repository local classes missing from facade: "
        f"{', '.join(_CHAIN_PERSISTENCE_REPOSITORY_COMPAT_LOCAL_REPOSITORY_MISSING)}"
    )
_CHAIN_PERSISTENCE_REPOSITORY_COMPAT_FORWARDER_CLASSIFICATION_DRIFT = tuple(
    name
    for name, classification in _CHAIN_PERSISTENCE_REPOSITORY_COMPAT_CHAIN_LOCAL_CLASSIFICATIONS.items()
    if "forwarder" in classification or classification.startswith("owner-")
)
if _CHAIN_PERSISTENCE_REPOSITORY_COMPAT_FORWARDER_CLASSIFICATION_DRIFT:
    raise RuntimeError(
        "chain persistence repository classifications must remain chain-local, not pure owner forwarders: "
        f"{', '.join(_CHAIN_PERSISTENCE_REPOSITORY_COMPAT_FORWARDER_CLASSIFICATION_DRIFT)}"
    )
_CHAIN_PERSISTENCE_REPOSITORY_COMPAT_LOCAL_REPOSITORIES = MappingProxyType(
    {name: globals()[name] for name in _CHAIN_PERSISTENCE_REPOSITORY_COMPAT_LOCAL_REPOSITORY_NAMES}
)
_CHAIN_PERSISTENCE_REPOSITORY_COMPAT_LOCAL_REPOSITORY_OWNER_DRIFT = tuple(
    name
    for name, repository_type in _CHAIN_PERSISTENCE_REPOSITORY_COMPAT_LOCAL_REPOSITORIES.items()
    if getattr(repository_type, "__module__", None) != __name__
)
if _CHAIN_PERSISTENCE_REPOSITORY_COMPAT_LOCAL_REPOSITORY_OWNER_DRIFT:
    raise RuntimeError(
        "chain persistence repository classes must be defined in the chain facade until extraction: "
        f"{', '.join(_CHAIN_PERSISTENCE_REPOSITORY_COMPAT_LOCAL_REPOSITORY_OWNER_DRIFT)}"
    )
_CHAIN_PERSISTENCE_REPOSITORY_COMPAT_PROTOCOL_METHOD_MISSING = tuple(
    name
    for name in _CHAIN_PERSISTENCE_REPOSITORY_COMPAT_PROTOCOL_METHOD_NAMES
    if not callable(getattr(OrchestratorRepository, name, None))
)
if _CHAIN_PERSISTENCE_REPOSITORY_COMPAT_PROTOCOL_METHOD_MISSING:
    raise RuntimeError(
        "chain persistence repository protocol methods missing from local protocol: "
        f"{', '.join(_CHAIN_PERSISTENCE_REPOSITORY_COMPAT_PROTOCOL_METHOD_MISSING)}"
    )
_CHAIN_PERSISTENCE_REPOSITORY_COMPAT_IMPLEMENTATION_METHOD_MISSING = tuple(
    name
    for name in _CHAIN_PERSISTENCE_REPOSITORY_COMPAT_IMPLEMENTATION_METHOD_NAMES
    if not callable(getattr(PsycopgOrchestratorRepository, name, None))
)
if _CHAIN_PERSISTENCE_REPOSITORY_COMPAT_IMPLEMENTATION_METHOD_MISSING:
    raise RuntimeError(
        "chain persistence repository implementation methods missing from local implementation: "
        f"{', '.join(_CHAIN_PERSISTENCE_REPOSITORY_COMPAT_IMPLEMENTATION_METHOD_MISSING)}"
    )
_CHAIN_PERSISTENCE_REPOSITORY_COMPAT_PROTOCOL_METHODS = MappingProxyType(
    {name: getattr(OrchestratorRepository, name) for name in _CHAIN_PERSISTENCE_REPOSITORY_COMPAT_PROTOCOL_METHOD_NAMES}
)
_CHAIN_PERSISTENCE_REPOSITORY_COMPAT_IMPLEMENTATION_METHODS = MappingProxyType(
    {
        name: getattr(PsycopgOrchestratorRepository, name)
        for name in _CHAIN_PERSISTENCE_REPOSITORY_COMPAT_IMPLEMENTATION_METHOD_NAMES
    }
)


def _utcnow() -> datetime:
    return datetime.now(UTC)


def _retry_service_from_env() -> RetryService | None:
    database_url = os.getenv("DATABASE_URL", "").strip()
    if not database_url:
        return None

    from sqlalchemy import create_engine
    from sqlalchemy.orm import Session

    engine = create_engine(database_url, future=True)
    session = Session(engine)
    store = PipelineStore(session)
    return RetryService(store, RetryConfig.from_settings(SlurmGatewaySettings()))


_CHAIN_RETRY_COMPAT_LOCAL_FACTORY_MISSING = tuple(
    name for name in _CHAIN_RETRY_COMPAT_LOCAL_FACTORY_NAMES if not callable(globals().get(name))
)
if _CHAIN_RETRY_COMPAT_LOCAL_FACTORY_MISSING:
    raise RuntimeError(
        f"chain retry local factories missing from facade: {', '.join(_CHAIN_RETRY_COMPAT_LOCAL_FACTORY_MISSING)}"
    )
_CHAIN_RETRY_COMPAT_CHAIN_LOCAL_FACTORIES = MappingProxyType(
    {name: globals()[name] for name in _CHAIN_RETRY_COMPAT_LOCAL_FACTORY_NAMES}
)


def _coerce_mapping(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return dict(value)
    model_dump = getattr(value, "model_dump", None)
    if callable(model_dump):
        return dict(model_dump(mode="json"))
    if hasattr(value, "__dict__"):
        return dict(value.__dict__)
    raise TypeError(f"Expected mapping-like Slurm payload, got {type(value).__name__}")


def _validate_safe_id(label: str, value: str) -> None:
    if _SAFE_ID_RE.fullmatch(value):
        return
    raise OrchestratorError(
        "UNSAFE_IDENTIFIER",
        f"{label} contains unsafe characters: {value!r}",
        {"field": label, "value": value},
    )


def _status_from_gateway_job(job: Mapping[str, Any]) -> str:
    status = job.get("status", "submitted")
    value = getattr(status, "value", status)
    normalized = str(value)
    return "pending" if normalized == "submitted" else normalized


_annotated_source_cycle_repair_jobs = chain_source_cycle._annotated_source_cycle_repair_jobs
_bounded_candidate_state_event = chain_source_cycle._bounded_candidate_state_event
_bounded_candidate_state_task_result_sample = chain_source_cycle._bounded_candidate_state_task_result_sample
_bounded_candidate_state_task_results = chain_source_cycle._bounded_candidate_state_task_results
_bounded_retry_ancestor_ids = chain_source_cycle._bounded_retry_ancestor_ids
_candidate_failed_task_from_events = chain_source_cycle._candidate_failed_task_from_events
_datetime_sort_key = chain_source_cycle._datetime_sort_key
_event_task_truth_sort_key = chain_source_cycle._event_task_truth_sort_key
_event_truth_sort_key = chain_source_cycle._event_truth_sort_key
_first_pipeline_truth_timestamp = chain_source_cycle._first_pipeline_truth_timestamp
_inverse_datetime_sort_key = chain_source_cycle._inverse_datetime_sort_key
_is_source_cycle_download_job = chain_source_cycle._is_source_cycle_download_job
_job_belongs_to_candidate = chain_source_cycle._job_belongs_to_candidate
_job_has_source_cycle_download_stage = chain_source_cycle._job_has_source_cycle_download_stage
_linked_successful_source_cycle_retry = chain_source_cycle._linked_successful_source_cycle_retry
_numeric_sort_key = chain_source_cycle._numeric_sort_key
_pipeline_job_is_repaired_stage_evidence = chain_source_cycle._pipeline_job_is_repaired_stage_evidence
_pipeline_job_truth_sort_key = chain_source_cycle._pipeline_job_truth_sort_key
_raw_manifest_key_matches_source_cycle = chain_source_cycle._raw_manifest_key_matches_source_cycle
_raw_manifest_uri_matches_source_cycle = chain_source_cycle._raw_manifest_uri_matches_source_cycle
_source_cycle_download_repair_state = chain_source_cycle._source_cycle_download_repair_state
_source_cycle_failed_job_has_later_repair_candidate = (
    chain_source_cycle._source_cycle_failed_job_has_later_repair_candidate
)
_source_cycle_original_failure_sort_key = chain_source_cycle._source_cycle_original_failure_sort_key
_source_cycle_raw_manifest_binding = chain_source_cycle._source_cycle_raw_manifest_binding
_source_cycle_repair_evidence = chain_source_cycle._source_cycle_repair_evidence
_source_cycle_repaired_stage_evidence = chain_source_cycle._source_cycle_repaired_stage_evidence
_source_cycle_retry_job_repairs_failure = chain_source_cycle._source_cycle_retry_job_repairs_failure
_source_cycle_retry_provenance = chain_source_cycle._source_cycle_retry_provenance
_source_cycle_stage_terminal_time = chain_source_cycle._source_cycle_stage_terminal_time
_source_cycle_truncated_failure_resolution = chain_source_cycle._source_cycle_truncated_failure_resolution
_successful_sibling_task_count = chain_source_cycle._successful_sibling_task_count
_task_candidate_id = chain_source_cycle._task_candidate_id
_task_identity_key = chain_source_cycle._task_identity_key
_task_model_id = chain_source_cycle._task_model_id


_coerce_int = chain_runtime_utils._coerce_int
_coerce_optional_nonnegative_int = chain_runtime_utils._coerce_optional_nonnegative_int
_cycle_payload_model_id = chain_runtime_utils._cycle_payload_model_id
_cycle_pipeline_job_model_id = chain_runtime_utils._cycle_pipeline_job_model_id
_cycle_orchestration_run_id = chain_runtime_utils._cycle_orchestration_run_id
_active_orchestration_conflicts = chain_runtime_utils._active_orchestration_conflicts
_in_memory_active_cycle_conflicts = chain_runtime_utils._in_memory_active_cycle_conflicts
_candidate_scoped_cycle_execution = chain_runtime_utils._candidate_scoped_cycle_execution
_is_active_pipeline_job = chain_runtime_utils._is_active_pipeline_job
_restart_stage_from_basins = chain_runtime_utils._restart_stage_from_basins
_retry_attempt_from_basins = chain_runtime_utils._retry_attempt_from_basins
_coerce_positive_int = chain_runtime_utils._coerce_positive_int
_stage_result_finished_at = chain_runtime_utils._stage_result_finished_at
_pipeline_job_terminal_time = chain_runtime_utils._pipeline_job_terminal_time
_canonical_restart_stage = chain_runtime_utils._canonical_restart_stage
_restart_stage_index = chain_runtime_utils._restart_stage_index
_pipeline_job_id = chain_runtime_utils._pipeline_job_id
_pipeline_retry_job_id = chain_runtime_utils._pipeline_retry_job_id
_stage_job_sort_key = chain_runtime_utils._stage_job_sort_key
_cycle_stage_idempotency_key = chain_runtime_utils._cycle_stage_idempotency_key
_published_artifact_root_configured = chain_runtime_utils._published_artifact_root_configured
_absolute_configured_path = chain_runtime_utils._absolute_configured_path
_log_stream_for_stage = chain_runtime_utils._log_stream_for_stage
_source_id_from_cycle_id = chain_runtime_utils._source_id_from_cycle_id
_cycle_time_from_cycle_id = chain_runtime_utils._cycle_time_from_cycle_id
_stage_status_message = chain_runtime_utils._stage_status_message
_resolve_forecast_horizon_hours = chain_runtime_utils._resolve_forecast_horizon_hours
_ifs_max_lead_hours_for_cycle = chain_runtime_utils._ifs_max_lead_hours_for_cycle
_elapsed_hours = chain_runtime_utils._elapsed_hours
_optional_int = chain_runtime_utils._optional_int
_optional_str = chain_runtime_utils._optional_str
_first_optional_int = chain_runtime_utils._first_optional_int
_max_lead_hours_from_lineage = chain_runtime_utils._max_lead_hours_from_lineage
_basin_max_lead_hours = chain_runtime_utils._basin_max_lead_hours
_basin_has_prefilled_initial_state = chain_runtime_utils._basin_has_prefilled_initial_state
_apply_initial_state_selection_to_basin = chain_runtime_utils._apply_initial_state_selection_to_basin
_initial_state_lineage = chain_runtime_utils._initial_state_lineage
_auto_trigger_forecast_hours = chain_runtime_utils._auto_trigger_forecast_hours
_auto_trigger_source_policy_identity = chain_runtime_utils._auto_trigger_source_policy_identity
_auto_trigger_source_object_identity = chain_runtime_utils._auto_trigger_source_object_identity
_auto_trigger_source_identity_adapter = chain_runtime_utils._auto_trigger_source_identity_adapter
_stale_converter_versions_in_cycle = chain_runtime_utils._stale_converter_versions_in_cycle
_canonical_products_from_ready_cycle = chain_runtime_utils._canonical_products_from_ready_cycle
_canonical_product_row_from_ready_cycle = chain_runtime_utils._canonical_product_row_from_ready_cycle
_auto_trigger_canonical_readiness_unavailable_evidence = (
    chain_runtime_utils._auto_trigger_canonical_readiness_unavailable_evidence
)
_accepted_horizon_from_hours = chain_runtime_utils._accepted_horizon_from_hours
_skipped_ready_forecast_result = chain_runtime_utils._skipped_ready_forecast_result
_coerce_array_task_id = chain_runtime_utils._coerce_array_task_id
_parse_gateway_time = chain_runtime_utils._parse_gateway_time
_ensure_utc = chain_runtime_utils._ensure_utc
_format_time = chain_runtime_utils._format_time
_format_time_or_none = chain_runtime_utils._format_time_or_none
parse_date_range = chain_runtime_utils.parse_date_range
_parse_date_range_endpoint = chain_runtime_utils._parse_date_range_endpoint
_validated_date_range = chain_runtime_utils._validated_date_range
_analysis_error_code = chain_runtime_utils._analysis_error_code
_template_export_lines = chain_runtime_utils._template_export_lines
_python_runtime_export_lines = chain_runtime_utils._python_runtime_export_lines
_response_json_or_text = chain_runtime_utils._response_json_or_text
_error_code_from_response = chain_runtime_utils._error_code_from_response


def _basin_key(basin: Mapping[str, Any]) -> tuple[str, str]:
    return chain_array_accounting.basin_key(basin)


def _basin_identifier(basin: Mapping[str, Any]) -> str:
    return chain_array_accounting.basin_identifier(basin)


def _basin_original_task_id(basin: Mapping[str, Any], fallback: int) -> int:
    return chain_array_accounting.basin_original_task_id(basin, fallback)


def _array_accounting_dependencies() -> chain_array_accounting.ArrayAccountingDependencies:
    return chain_array_accounting.ArrayAccountingDependencies(
        coerce_mapping=_coerce_mapping,
        safe_candidate_outcome_payload=_safe_candidate_outcome_payload,
        safe_pipeline_event_details=_safe_pipeline_event_details,
        record_array_task_outcomes=_record_array_task_outcomes,
        stage_task_result_evidence=_stage_task_result_evidence,
        parse_sacct_array_results=parse_sacct_array_results,
        coerce_array_aggregation=_coerce_array_aggregation,
        aggregation_from_task_results=_aggregation_from_task_results,
        aggregation_error_code=_aggregation_error_code,
        aggregation_error_message=_aggregation_error_message,
        sacct_extra_fields=_sacct_extra_fields,
        slurm_accounting_from_payload=_slurm_accounting_from_payload,
        resource_metrics_from_payload=_resource_metrics_from_payload,
        production_status_for=production_status_for,
        context_array_log_uri=_context_array_log_uri,
        array_task_status=_array_task_status,
        parse_slurm_exit_code=_parse_slurm_exit_code,
        basin_key=_basin_key,
        basin_original_task_id=_basin_original_task_id,
        status_from_gateway_job=_status_from_gateway_job,
        parse_gateway_time=_parse_gateway_time,
        utcnow=_utcnow,
        build_reindexed_manifest=build_reindexed_manifest,
    )


def _record_array_task_outcomes(
    context: CycleOrchestrationContext,
    *,
    stage: str,
    aggregation: ArrayAggregation,
) -> None:
    chain_array_accounting.record_array_task_outcomes(
        context,
        stage=stage,
        aggregation=aggregation,
        deps=_array_accounting_dependencies(),
    )


def _candidate_outcomes(context: CycleOrchestrationContext, *, final_status: str) -> tuple[dict[str, Any], ...]:
    return chain_array_accounting.candidate_outcomes(
        context,
        final_status=final_status,
        deps=_array_accounting_dependencies(),
    )


def _safe_candidate_outcome_payload(payload: Mapping[str, Any]) -> dict[str, Any]:
    return chain_array_accounting.safe_candidate_outcome_payload(payload)


def _nested_mapping(value: Any) -> dict[str, Any]:
    return dict(value) if isinstance(value, Mapping) else {}


def _workspace_relative_parts(path: Path, workspace_root: Path) -> tuple[str, ...]:
    return chain_workspace.workspace_relative_parts(path, workspace_root)


_CHAIN_WORKSPACE_LOG_COMPAT_METHOD_FACADE_MISSING = tuple(
    name
    for name in _CHAIN_WORKSPACE_LOG_COMPAT_METHOD_FORWARDER_NAMES
    if not callable(getattr(ForecastOrchestrator, name, None))
)
_CHAIN_WORKSPACE_LOG_COMPAT_TOP_LEVEL_FACADE_MISSING = tuple(
    name for name in _CHAIN_WORKSPACE_LOG_COMPAT_TOP_LEVEL_FORWARDER_NAMES if not callable(globals().get(name))
)
if _CHAIN_WORKSPACE_LOG_COMPAT_METHOD_FACADE_MISSING:
    raise RuntimeError(
        "chain workspace/log compatibility methods missing from facade: "
        f"{', '.join(_CHAIN_WORKSPACE_LOG_COMPAT_METHOD_FACADE_MISSING)}"
    )
if _CHAIN_WORKSPACE_LOG_COMPAT_TOP_LEVEL_FACADE_MISSING:
    raise RuntimeError(
        "chain workspace/log compatibility top-level helpers missing from facade: "
        f"{', '.join(_CHAIN_WORKSPACE_LOG_COMPAT_TOP_LEVEL_FACADE_MISSING)}"
    )
_CHAIN_WORKSPACE_LOG_COMPAT_METHOD_FORWARDERS = MappingProxyType(
    {
        facade_name: getattr(chain_workspace, owner_name)
        for facade_name, owner_name in zip(
            _CHAIN_WORKSPACE_LOG_COMPAT_METHOD_FORWARDER_NAMES,
            _CHAIN_WORKSPACE_LOG_COMPAT_METHOD_OWNER_FUNCTION_NAMES,
            strict=True,
        )
    }
)
_CHAIN_WORKSPACE_LOG_COMPAT_TOP_LEVEL_FORWARDERS = MappingProxyType(
    {
        facade_name: getattr(chain_workspace, owner_name)
        for facade_name, owner_name in zip(
            _CHAIN_WORKSPACE_LOG_COMPAT_TOP_LEVEL_FORWARDER_NAMES,
            _CHAIN_WORKSPACE_LOG_COMPAT_TOP_LEVEL_OWNER_FUNCTION_NAMES,
            strict=True,
        )
    }
)


def parse_sacct_array_results(
    stdout: str,
    master_job_id: str,
    *,
    context: CycleOrchestrationContext | None = None,
    object_store: LocalObjectStore | None = None,
) -> ArrayAggregation:
    return chain_array_accounting.parse_sacct_array_results(
        stdout,
        master_job_id,
        context=context,
        object_store=object_store,
        deps=_array_accounting_dependencies(),
    )


def _coerce_array_aggregation(
    raw_results: Any,
    master_job_id: str,
    *,
    context: CycleOrchestrationContext | None = None,
    object_store: LocalObjectStore | None = None,
) -> ArrayAggregation:
    return chain_array_accounting.coerce_array_aggregation(
        raw_results,
        master_job_id,
        context=context,
        object_store=object_store,
        deps=_array_accounting_dependencies(),
    )


def _aggregation_from_task_results(results: Sequence[ArrayTaskResult]) -> ArrayAggregation:
    return chain_array_accounting.aggregation_from_task_results(results)


def _aggregation_error_code(aggregation: ArrayAggregation | None) -> str | None:
    return chain_array_accounting.aggregation_error_code(aggregation)


def _aggregation_error_message(aggregation: ArrayAggregation | None) -> str | None:
    return chain_array_accounting.aggregation_error_message(aggregation)


def _sacct_extra_fields(fields: Sequence[str]) -> dict[str, Any]:
    return chain_array_accounting.sacct_extra_fields(fields)


def _slurm_accounting_from_payload(payload: Mapping[str, Any]) -> dict[str, Any]:
    return chain_array_accounting.slurm_accounting_from_payload(payload)


def _resource_metrics_from_payload(payload: Mapping[str, Any]) -> dict[str, Any]:
    return chain_array_accounting.resource_metrics_from_payload(
        payload,
        slurm_accounting=_slurm_accounting_from_payload,
    )


def _safe_pipeline_event_details(details: Mapping[str, Any]) -> dict[str, Any]:
    redacted = redact_payload(_json_safe_pipeline_event_value(details))
    return dict(redacted) if isinstance(redacted, Mapping) else {}


def _submission_runtime_root_contract(manifest: Mapping[str, Any]) -> dict[str, Any]:
    fields = (
        "workspace_dir",
        "object_store_root",
        "object_store_prefix",
        "published_artifact_root",
        "published_artifact_uri_prefix",
    )
    return {field: manifest[field] for field in fields if manifest.get(field) not in (None, "")}


def _json_safe_pipeline_event_value(value: Any) -> Any:
    if isinstance(value, datetime):
        return _format_time(value)
    if isinstance(value, Mapping):
        return {str(key): _json_safe_pipeline_event_value(nested) for key, nested in value.items()}
    if isinstance(value, tuple):
        return tuple(_json_safe_pipeline_event_value(item) for item in value)
    if isinstance(value, Sequence) and not isinstance(value, str | bytes | bytearray):
        return [_json_safe_pipeline_event_value(item) for item in value]
    return value


def _stage_task_result_evidence(
    aggregation: ArrayAggregation | None,
    *,
    context: CycleOrchestrationContext | None = None,
) -> tuple[Mapping[str, Any], ...]:
    return chain_array_accounting.stage_task_result_evidence(
        aggregation,
        context=context,
        deps=_array_accounting_dependencies(),
    )


def _context_array_log_uri(
    context: CycleOrchestrationContext | None,
    object_store: LocalObjectStore | None,
    master_job_id: str,
    task_id: int,
) -> str | None:
    if context is None or object_store is None:
        return None
    return _array_task_log_uri(object_store, context.run_id, master_job_id, task_id)


def _array_task_log_uri(object_store: LocalObjectStore, run_id: str, master_job_id: str, task_id: int) -> str:
    return chain_array_accounting.array_task_log_uri(object_store, run_id, master_job_id, task_id)


def _array_task_status(raw_state: str) -> str:
    return chain_array_accounting.array_task_status(raw_state)


def _parse_slurm_exit_code(raw_exit_code: str) -> int | None:
    return chain_array_accounting.parse_slurm_exit_code(raw_exit_code)


_CHAIN_ARRAY_ACCOUNTING_COMPAT_TOP_LEVEL_FACADE_MISSING = tuple(
    name
    for name in (
        *_CHAIN_ARRAY_ACCOUNTING_COMPAT_TOP_LEVEL_FORWARDER_NAMES,
        *_CHAIN_ARRAY_ACCOUNTING_COMPAT_LOCAL_BINDING_NAMES,
    )
    if not callable(globals().get(name))
)
if _CHAIN_ARRAY_ACCOUNTING_COMPAT_TOP_LEVEL_FACADE_MISSING:
    raise RuntimeError(
        "chain array accounting compatibility names missing from facade: "
        f"{', '.join(_CHAIN_ARRAY_ACCOUNTING_COMPAT_TOP_LEVEL_FACADE_MISSING)}"
    )
_CHAIN_ARRAY_ACCOUNTING_COMPAT_METHOD_FACADE_MISSING = tuple(
    name
    for name in _CHAIN_ARRAY_ACCOUNTING_COMPAT_METHOD_FORWARDER_NAMES
    if not callable(getattr(ForecastOrchestrator, name, None))
)
if _CHAIN_ARRAY_ACCOUNTING_COMPAT_METHOD_FACADE_MISSING:
    raise RuntimeError(
        "chain array accounting compatibility methods missing from facade: "
        f"{', '.join(_CHAIN_ARRAY_ACCOUNTING_COMPAT_METHOD_FACADE_MISSING)}"
    )
_CHAIN_ARRAY_ACCOUNTING_COMPAT_TOP_LEVEL_FORWARDERS = MappingProxyType(
    {
        facade_name: getattr(chain_array_accounting, owner_name)
        for facade_name, owner_name in zip(
            _CHAIN_ARRAY_ACCOUNTING_COMPAT_TOP_LEVEL_FORWARDER_NAMES,
            _CHAIN_ARRAY_ACCOUNTING_COMPAT_TOP_LEVEL_OWNER_FUNCTION_NAMES,
            strict=True,
        )
    }
)
_CHAIN_ARRAY_ACCOUNTING_COMPAT_METHOD_FORWARDERS = MappingProxyType(
    {
        facade_name: getattr(chain_array_accounting, owner_name)
        for facade_name, owner_name in zip(
            _CHAIN_ARRAY_ACCOUNTING_COMPAT_METHOD_FORWARDER_NAMES,
            _CHAIN_ARRAY_ACCOUNTING_COMPAT_METHOD_OWNER_FUNCTION_NAMES,
            strict=True,
        )
    }
)


def _next_retry_attempt_for_stage(
    jobs: Sequence[Mapping[str, Any]],
    *,
    base_job_id: str,
    stage: StageDefinition,
) -> int:
    prefix = f"{base_job_id}_retry_"
    attempts: list[int] = []
    for job in jobs:
        if not ForecastOrchestrator._job_matches_stage(job, stage):
            continue
        job_id = str(job.get("job_id") or "")
        if not job_id.startswith(prefix):
            continue
        try:
            attempts.append(int(job_id.removeprefix(prefix)))
        except ValueError:
            continue
    return max(attempts, default=0) + 1


_chain_array_accounting_deps = _array_accounting_dependencies()
_CHAIN_ARRAY_ACCOUNTING_COMPAT_DEPENDENCY_BINDING_DRIFT = tuple(
    field
    for field, facade_name in _CHAIN_ARRAY_ACCOUNTING_COMPAT_DEPENDENCY_BINDINGS
    if getattr(_chain_array_accounting_deps, field) is not globals()[facade_name]
)
if _CHAIN_ARRAY_ACCOUNTING_COMPAT_DEPENDENCY_BINDING_DRIFT:
    raise RuntimeError(
        "chain array accounting dependency bindings drifted from legacy facade: "
        f"{', '.join(_CHAIN_ARRAY_ACCOUNTING_COMPAT_DEPENDENCY_BINDING_DRIFT)}"
    )
del _chain_array_accounting_deps


_CHAIN_WORKER_ADAPTER_COMPAT_CANONICAL_ALIAS_NAMES = (
    "evaluate_canonical_readiness",
    "expected_converter_version",
)
_CHAIN_WORKER_ADAPTER_COMPAT_CYCLE_ALIAS_NAMES = (
    "parse_cycle_time",
    "format_cycle_time",
    "cycle_id_for",
)
_CHAIN_WORKER_ADAPTER_COMPAT_ALIAS_NAMES = (
    *_CHAIN_WORKER_ADAPTER_COMPAT_CANONICAL_ALIAS_NAMES,
    *_CHAIN_WORKER_ADAPTER_COMPAT_CYCLE_ALIAS_NAMES,
)
_CHAIN_WORKER_ADAPTER_COMPAT_TIME_ALIAS_NAMES = ("_check_three_way_time_consistency",)
_CHAIN_WORKER_ADAPTER_COMPAT_TIME_ALIAS_OWNER_NAMES = MappingProxyType(
    {"_check_three_way_time_consistency": "check_three_way_time_consistency"}
)
_CHAIN_WORKER_ADAPTER_COMPAT_LOCAL_HELPER_NAMES = (
    "scenario_for_source",
    "_auto_trigger_source_policy_identity",
    "_auto_trigger_source_object_identity",
    "_auto_trigger_source_identity_adapter",
)
_CHAIN_WORKER_ADAPTER_COMPAT_CHAIN_LOCAL_CLASSIFICATIONS = MappingProxyType(
    {
        "scenario_for_source": "chain-local-source-scenario-glue",
        "_auto_trigger_source_policy_identity": "chain-local-source-identity-glue",
        "_auto_trigger_source_object_identity": "chain-local-source-identity-glue",
        "_auto_trigger_source_identity_adapter": "chain-local-dynamic-adapter-glue",
    }
)
_CHAIN_WORKER_ADAPTER_COMPAT_DYNAMIC_ADAPTERS = MappingProxyType(
    {
        "gfs": ("workers.data_adapters.gfs_adapter", "GFSAdapter", "GFSAdapterConfig"),
        "IFS": ("workers.data_adapters.ifs_adapter", "IFSAdapter", "IFSAdapterConfig"),
    }
)
_CHAIN_WORKER_ADAPTER_COMPAT_CANONICAL_ALIAS_OWNER_MISSING = tuple(
    name
    for name in _CHAIN_WORKER_ADAPTER_COMPAT_CANONICAL_ALIAS_NAMES
    if not hasattr(_canonical_converter_module, name)
)
_CHAIN_WORKER_ADAPTER_COMPAT_CYCLE_ALIAS_OWNER_MISSING = tuple(
    name for name in _CHAIN_WORKER_ADAPTER_COMPAT_CYCLE_ALIAS_NAMES if not hasattr(_data_adapters_base_module, name)
)
_CHAIN_WORKER_ADAPTER_COMPAT_ALIAS_FACADE_MISSING = tuple(
    name for name in _CHAIN_WORKER_ADAPTER_COMPAT_ALIAS_NAMES if name not in globals()
)
_CHAIN_WORKER_ADAPTER_COMPAT_TIME_ALIAS_OWNER_MISSING = tuple(
    facade_name
    for facade_name, owner_name in _CHAIN_WORKER_ADAPTER_COMPAT_TIME_ALIAS_OWNER_NAMES.items()
    if not hasattr(_time_consistency_module, owner_name)
)
_CHAIN_WORKER_ADAPTER_COMPAT_TIME_ALIAS_FACADE_MISSING = tuple(
    name for name in _CHAIN_WORKER_ADAPTER_COMPAT_TIME_ALIAS_NAMES if name not in globals()
)
_CHAIN_WORKER_ADAPTER_COMPAT_LOCAL_HELPER_MISSING = tuple(
    name for name in _CHAIN_WORKER_ADAPTER_COMPAT_LOCAL_HELPER_NAMES if not callable(globals().get(name))
)
if _CHAIN_WORKER_ADAPTER_COMPAT_CANONICAL_ALIAS_OWNER_MISSING:
    raise RuntimeError(
        "chain worker canonical compatibility aliases missing from owner module: "
        f"{', '.join(_CHAIN_WORKER_ADAPTER_COMPAT_CANONICAL_ALIAS_OWNER_MISSING)}"
    )
if _CHAIN_WORKER_ADAPTER_COMPAT_CYCLE_ALIAS_OWNER_MISSING:
    raise RuntimeError(
        "chain worker cycle compatibility aliases missing from owner module: "
        f"{', '.join(_CHAIN_WORKER_ADAPTER_COMPAT_CYCLE_ALIAS_OWNER_MISSING)}"
    )
if _CHAIN_WORKER_ADAPTER_COMPAT_ALIAS_FACADE_MISSING:
    raise RuntimeError(
        "chain worker compatibility aliases missing from facade: "
        f"{', '.join(_CHAIN_WORKER_ADAPTER_COMPAT_ALIAS_FACADE_MISSING)}"
    )
if _CHAIN_WORKER_ADAPTER_COMPAT_TIME_ALIAS_OWNER_MISSING:
    raise RuntimeError(
        "chain worker time-consistency aliases missing from owner module: "
        f"{', '.join(_CHAIN_WORKER_ADAPTER_COMPAT_TIME_ALIAS_OWNER_MISSING)}"
    )
if _CHAIN_WORKER_ADAPTER_COMPAT_TIME_ALIAS_FACADE_MISSING:
    raise RuntimeError(
        "chain worker time-consistency aliases missing from facade: "
        f"{', '.join(_CHAIN_WORKER_ADAPTER_COMPAT_TIME_ALIAS_FACADE_MISSING)}"
    )
if _CHAIN_WORKER_ADAPTER_COMPAT_LOCAL_HELPER_MISSING:
    raise RuntimeError(
        "chain worker/source identity local helpers missing from facade: "
        f"{', '.join(_CHAIN_WORKER_ADAPTER_COMPAT_LOCAL_HELPER_MISSING)}"
    )
_CHAIN_WORKER_ADAPTER_COMPAT_OWNER_ALIASES = MappingProxyType(
    {
        **{
            name: getattr(_canonical_converter_module, name)
            for name in _CHAIN_WORKER_ADAPTER_COMPAT_CANONICAL_ALIAS_NAMES
        },
        **{name: getattr(_data_adapters_base_module, name) for name in _CHAIN_WORKER_ADAPTER_COMPAT_CYCLE_ALIAS_NAMES},
    }
)
_CHAIN_WORKER_ADAPTER_COMPAT_FACADE_ALIASES = MappingProxyType(
    {name: globals()[name] for name in _CHAIN_WORKER_ADAPTER_COMPAT_ALIAS_NAMES}
)
_CHAIN_WORKER_ADAPTER_COMPAT_TIME_OWNER_ALIASES = MappingProxyType(
    {
        facade_name: getattr(_time_consistency_module, owner_name)
        for facade_name, owner_name in _CHAIN_WORKER_ADAPTER_COMPAT_TIME_ALIAS_OWNER_NAMES.items()
    }
)
_CHAIN_WORKER_ADAPTER_COMPAT_TIME_FACADE_ALIASES = MappingProxyType(
    {name: globals()[name] for name in _CHAIN_WORKER_ADAPTER_COMPAT_TIME_ALIAS_NAMES}
)
_CHAIN_WORKER_ADAPTER_COMPAT_CHAIN_LOCAL_HELPERS = MappingProxyType(
    {name: globals()[name] for name in _CHAIN_WORKER_ADAPTER_COMPAT_LOCAL_HELPER_NAMES}
)
_CHAIN_WORKER_ADAPTER_COMPAT_ALIAS_DRIFT = tuple(
    name
    for name in _CHAIN_WORKER_ADAPTER_COMPAT_ALIAS_NAMES
    if _CHAIN_WORKER_ADAPTER_COMPAT_OWNER_ALIASES[name] is not _CHAIN_WORKER_ADAPTER_COMPAT_FACADE_ALIASES[name]
)
_CHAIN_WORKER_ADAPTER_COMPAT_TIME_ALIAS_DRIFT = tuple(
    name
    for name in _CHAIN_WORKER_ADAPTER_COMPAT_TIME_ALIAS_NAMES
    if _CHAIN_WORKER_ADAPTER_COMPAT_TIME_OWNER_ALIASES[name]
    is not _CHAIN_WORKER_ADAPTER_COMPAT_TIME_FACADE_ALIASES[name]
)
if _CHAIN_WORKER_ADAPTER_COMPAT_ALIAS_DRIFT:
    raise RuntimeError(
        "chain worker adapter direct alias drifted from owner module: "
        f"{', '.join(_CHAIN_WORKER_ADAPTER_COMPAT_ALIAS_DRIFT)}"
    )
if _CHAIN_WORKER_ADAPTER_COMPAT_TIME_ALIAS_DRIFT:
    raise RuntimeError(
        "chain worker time-consistency alias drifted from owner module: "
        f"{', '.join(_CHAIN_WORKER_ADAPTER_COMPAT_TIME_ALIAS_DRIFT)}"
    )


_CHAIN_RESERVATION_COMPAT_LOCAL_BINDING_MISSING = tuple(
    name for name in _CHAIN_RESERVATION_COMPAT_LOCAL_BINDING_NAMES if not callable(globals().get(name))
)
if _CHAIN_RESERVATION_COMPAT_LOCAL_BINDING_MISSING:
    raise RuntimeError(
        "chain reservation compatibility local bindings missing from facade: "
        f"{', '.join(_CHAIN_RESERVATION_COMPAT_LOCAL_BINDING_MISSING)}"
    )
_chain_stage_execution_deps = ForecastOrchestrator._chain_stage_execution_dependencies()
_CHAIN_TILE_PUBLISHER_COMPAT_STAGE_EXECUTION_DEPENDENCY_BINDING_DRIFT = tuple(
    field
    for field, facade_name in _CHAIN_TILE_PUBLISHER_COMPAT_STAGE_EXECUTION_DEPENDENCY_BINDINGS
    if getattr(_chain_stage_execution_deps, field) is not globals()[facade_name]
)
if _CHAIN_TILE_PUBLISHER_COMPAT_STAGE_EXECUTION_DEPENDENCY_BINDING_DRIFT:
    raise RuntimeError(
        "chain tile-publisher stage execution dependency bindings drifted from legacy facade: "
        f"{', '.join(_CHAIN_TILE_PUBLISHER_COMPAT_STAGE_EXECUTION_DEPENDENCY_BINDING_DRIFT)}"
    )
_CHAIN_RESERVATION_COMPAT_STAGE_EXECUTION_DEPENDENCY_BINDING_DRIFT = tuple(
    field
    for field, facade_name in _CHAIN_RESERVATION_COMPAT_STAGE_EXECUTION_DEPENDENCY_BINDINGS
    if getattr(_chain_stage_execution_deps, field) is not globals()[facade_name]
)
if _CHAIN_RESERVATION_COMPAT_STAGE_EXECUTION_DEPENDENCY_BINDING_DRIFT:
    raise RuntimeError(
        "chain reservation stage execution dependency bindings drifted from legacy facade: "
        f"{', '.join(_CHAIN_RESERVATION_COMPAT_STAGE_EXECUTION_DEPENDENCY_BINDING_DRIFT)}"
    )
del _chain_stage_execution_deps
