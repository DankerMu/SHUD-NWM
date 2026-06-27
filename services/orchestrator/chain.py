from __future__ import annotations

import json
import os
import re
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import MappingProxyType
from typing import Any, Mapping, Protocol, Sequence

import httpx

import services.orchestrator.time_consistency as _time_consistency_module
import services.tile_publisher as _tile_publisher_module
import services.tile_publisher.publisher as _tile_publisher_publisher_module
import workers.canonical_converter.converter as _canonical_converter_module
import workers.data_adapters.base as _data_adapters_base_module
from packages.common.best_available import BestAvailableManager
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
    STATE_QC_FAILED,
    STATE_TOO_STALE,
    WARM_START_LINEAGE_MISMATCH,
    WARM_START_SUCCESSOR_CHECKPOINT_MISSING,
    WARM_START_SUCCESSOR_CHECKPOINT_UNUSABLE,
)
from packages.common.state_manager import StateManager, StateSnapshot, assess_freshness
from services.artifacts import ArtifactLogError, published_log_relative_path, published_log_uri
from services.orchestrator import (
    chain_analysis,
    chain_array_accounting,
    chain_manifests,
    chain_runtime_utils,
    chain_source_cycle,
    chain_stage_execution,
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
    ANALYSIS_STAGES,
    LEGACY_FORECAST_STAGES,
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
    evaluate_canonical_readiness,
    expected_converter_version,
)
from workers.data_adapters.base import cycle_id_for, format_cycle_time, parse_cycle_time

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


class HttpSlurmGatewayClient:
    def __init__(self, base_url: str, *, timeout: float = 30.0) -> None:
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout

    def submit_job(self, payload: dict[str, Any]) -> dict[str, Any]:
        return self._request("POST", "/api/v1/slurm/jobs", json=payload, expected=(200, 201))

    def submit_job_array(
        self,
        job_type: str | Mapping[str, Any],
        cycle_id: str | None = None,
        stage_name: str | None = None,
        tasks: Sequence[Mapping[str, Any]] | None = None,
        manifest: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        if isinstance(job_type, Mapping):
            payload = dict(job_type)
        else:
            payload = {"job_type": job_type}
        if cycle_id is not None:
            payload["cycle_id"] = cycle_id
        if stage_name is not None:
            payload["stage_name"] = stage_name
        if tasks is not None:
            payload["tasks"] = [dict(task) for task in tasks]
        if manifest is not None:
            payload["manifest"] = dict(manifest)
        return self._request("POST", "/api/v1/slurm/job-arrays", json=payload, expected=(200, 201))

    def get_job_status(self, job_id: str) -> dict[str, Any]:
        return self._request("GET", f"/api/v1/slurm/jobs/{job_id}", expected=(200,))

    def get_array_task_results(self, job_id: str) -> list[dict[str, Any]]:
        response = self._request("GET", f"/api/v1/slurm/jobs/{job_id}/array-tasks", expected=(200,))
        if isinstance(response, list):
            return [dict(item) for item in response]
        tasks = response.get("tasks") if isinstance(response, Mapping) else None
        if isinstance(tasks, Sequence) and not isinstance(tasks, str | bytes):
            return [dict(_coerce_mapping(item)) for item in tasks]
        raise SlurmClientError(
            "SLURM_GATEWAY_INVALID_RESPONSE",
            "Slurm Gateway returned an invalid array task response.",
            {"response": response},
        )

    def fetch_logs(self, job_id: str) -> dict[str, Any]:
        return self._request("GET", f"/api/v1/slurm/jobs/{job_id}/logs", expected=(200,))

    def cancel_job(self, job_id: str) -> dict[str, Any]:
        return self._request("DELETE", f"/api/v1/slurm/jobs/{job_id}", expected=(200,))

    def _request(
        self,
        method: str,
        path: str,
        *,
        expected: tuple[int, ...],
        json: dict[str, Any] | None = None,
    ) -> Any:
        try:
            with httpx.Client(base_url=self.base_url, timeout=self.timeout) as client:
                response = client.request(method, path, json=json)
        except httpx.HTTPError as error:
            raise SlurmClientError("SLURM_GATEWAY_UNAVAILABLE", f"Slurm Gateway request failed: {error}") from error
        if response.status_code not in expected:
            details = _response_json_or_text(response)
            code = _error_code_from_response(details)
            raise SlurmClientError(code, f"Slurm Gateway returned HTTP {response.status_code}.", {"response": details})
        return response.json()


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
        _validate_safe_id("source", source)
        source = normalize_source_id(source)
        parsed_cycle_time = parse_cycle_time(cycle_time)
        cycle_id = cycle_id_for(source, parsed_cycle_time)
        _validate_safe_id("cycle_id", cycle_id)

        normalized_basins = self._normalize_cycle_basins(basins, source, parsed_cycle_time)
        if not normalized_basins:
            raise OrchestratorError("EMPTY_BASIN_LIST", "orchestrate_cycle requires at least one basin.")
        self._apply_cohort_warm_start(normalized_basins, source, parsed_cycle_time)
        self._validate_cycle_basin_identities(normalized_basins, source, parsed_cycle_time, cycle_id)
        context_run_id = _cycle_orchestration_run_id(source, parsed_cycle_time, normalized_basins)
        if _active_orchestration_conflicts(
            self.repository,
            source_id=source,
            cycle_time=parsed_cycle_time,
            cycle_id=cycle_id,
            run_id=context_run_id,
            basins=normalized_basins,
        ):
            raise OrchestratorError(
                "PIPELINE_ALREADY_ACTIVE",
                f"An active orchestration already exists for {source} {format_cycle_time(parsed_cycle_time)}.",
                {"source_id": source, "cycle_time": _format_time(parsed_cycle_time), "cycle_id": cycle_id},
            )
        if _in_memory_active_cycle_conflicts(cycle_id, self._active_cycles, normalized_basins):
            raise OrchestratorError(
                "PIPELINE_ALREADY_ACTIVE",
                f"An active orchestration already exists for {source} {format_cycle_time(parsed_cycle_time)}.",
                {"source_id": source, "cycle_time": _format_time(parsed_cycle_time), "cycle_id": cycle_id},
            )

        self._active_cycles.add(cycle_id)
        try:
            self.repository.ensure_forecast_cycle(source_id=source, cycle_time=parsed_cycle_time)
            context = CycleOrchestrationContext(
                source_id=source,
                cycle_time=parsed_cycle_time,
                cycle_id=cycle_id,
                run_id=context_run_id,
                all_basins=normalized_basins,
                active_basins=list(normalized_basins),
                restart_stage=_restart_stage_from_basins(normalized_basins),
                retry_attempt=_retry_attempt_from_basins(normalized_basins),
            )
            return self._run_cycle_chain(context)
        finally:
            self._active_cycles.discard(cycle_id)

    def sync_cycle_statuses(self, cycle_id: str) -> list[dict[str, Any]]:
        updates: list[dict[str, Any]] = []
        deferred_publish_attempt: DisplayLogPublicationAttempt | None = None
        for job in self._query_pipeline_jobs_by_cycle(cycle_id):
            if str(job.get("status")) in TERMINAL_JOB_STATUSES or not job.get("slurm_job_id"):
                continue
            gateway_job = _coerce_mapping(self.slurm_client.get_job_status(str(job["slurm_job_id"])))
            new_status = _status_from_gateway_job(gateway_job)
            if new_status == str(job.get("status")):
                continue
            publication = (
                self._display_log_publication_for_pipeline_job(job) if new_status in TERMINAL_JOB_STATUSES else None
            )
            publication_attempt = (
                self._try_publish_log_for_advertise(str(job["slurm_job_id"]), publication)
                if publication is not None
                else None
            )
            log_uri = publication_attempt.advertised_uri if publication_attempt is not None else None
            previous_status, record = self.repository.update_pipeline_job_status(
                str(job["job_id"]),
                new_status,
                started_at=_parse_gateway_time(gateway_job.get("started_at")),
                finished_at=_parse_gateway_time(gateway_job.get("finished_at")),
                exit_code=gateway_job.get("exit_code"),
                error_code=gateway_job.get("error_code"),
                error_message=gateway_job.get("error_message"),
                log_uri=str(log_uri) if log_uri else None,
            )
            if str(record.get("status")) != new_status:
                continue
            details = _safe_pipeline_event_details(
                {
                    "cycle_id": cycle_id,
                    "slurm_job_id": job.get("slurm_job_id"),
                    "exit_code": gateway_job.get("exit_code"),
                    "error_code": gateway_job.get("error_code"),
                    "slurm": {
                        "job_id": job.get("slurm_job_id"),
                        "state": gateway_job.get("state") or gateway_job.get("status"),
                        "exit_code": gateway_job.get("exit_code"),
                        "log_uri": log_uri,
                        "accounting": _slurm_accounting_from_payload(gateway_job),
                        "resource_metrics": _resource_metrics_from_payload(gateway_job),
                    },
                }
            )
            self.repository.insert_pipeline_event(
                entity_type="pipeline_job",
                entity_id=str(job["job_id"]),
                event_type="status_change",
                status_from=previous_status or str(job.get("status")),
                status_to=new_status,
                message=_stage_status_message(str(job.get("stage") or job.get("job_type")), new_status, gateway_job),
                details=details,
            )
            updates.append(record)
            if (
                deferred_publish_attempt is None
                and publication_attempt is not None
                and publication_attempt.error is not None
            ):
                deferred_publish_attempt = publication_attempt
        self._raise_publish_error_after_durable_update(deferred_publish_attempt)
        return updates

    def cancel_active_cycle_jobs(self, cycle_id: str, *, reason: str = "operator_requested") -> list[dict[str, Any]]:
        cancelled: list[dict[str, Any]] = []
        cancel_job = getattr(self.slurm_client, "cancel_job", None)
        if not callable(cancel_job):
            raise SlurmClientError(
                "SLURM_CANCEL_UNSUPPORTED",
                "Slurm Gateway client does not expose a cancel contract.",
                {"cycle_id": cycle_id},
            )
        for job in self._query_pipeline_jobs_by_cycle(cycle_id):
            status = str(job.get("status") or "")
            slurm_job_id = job.get("slurm_job_id")
            if status in TERMINAL_JOB_STATUSES or not slurm_job_id:
                continue
            try:
                cancelled_payload = _coerce_mapping(cancel_job(str(slurm_job_id)))
            except SlurmClientError as error:
                details = dict(error.details or {})
                response = details.get("response")
                response_mapping = response if isinstance(response, Mapping) else {}
                error_mapping = response_mapping.get("error") if isinstance(response_mapping, Mapping) else None
                gateway_details = dict(error_mapping.get("details") or {}) if isinstance(error_mapping, Mapping) else {}
                if error.error_code == "JOB_ALREADY_TERMINAL":
                    details_payload = _safe_pipeline_event_details(
                        {
                            "cycle_id": cycle_id,
                            "stage": job.get("stage"),
                            "job_type": job.get("job_type"),
                            "reason": reason,
                            "replacement_submitted": False,
                            "error_code": error.error_code,
                            "gateway_status": gateway_details.get("status"),
                            "gateway_details": gateway_details,
                            "slurm": {
                                "job_id": slurm_job_id,
                                "state": gateway_details.get("status"),
                                "log_uri": job.get("log_uri"),
                                "cancellation_proven": False,
                            },
                        }
                    )
                    self.repository.insert_pipeline_event(
                        entity_type="pipeline_job",
                        entity_id=str(job["job_id"]),
                        event_type="slurm_cancellation_gap",
                        status_from=status,
                        status_to="blocked",
                        message=(
                            f"Slurm job {slurm_job_id} was already terminal at the gateway; "
                            "pipeline state was not rewritten to cancelled."
                        ),
                        details=details_payload,
                    )
                    cancelled.append(
                        {
                            **dict(job),
                            "status": status,
                            "error_code": error.error_code,
                            "cancellation_proven": False,
                            "replacement_submitted": False,
                        }
                    )
                    continue
                raise
            previous_status, record = self.repository.update_pipeline_job_status(
                str(job["job_id"]),
                "cancelled",
                finished_at=_parse_gateway_time(cancelled_payload.get("finished_at")) or _utcnow(),
                exit_code=cancelled_payload.get("exit_code"),
                error_code=cancelled_payload.get("error_code"),
                error_message=cancelled_payload.get("error_message"),
                log_uri=job.get("log_uri"),
            )
            details = _safe_pipeline_event_details(
                {
                    "cycle_id": cycle_id,
                    "stage": job.get("stage"),
                    "job_type": job.get("job_type"),
                    "reason": reason,
                    "replacement_submitted": False,
                    "slurm": {
                        "job_id": slurm_job_id,
                        "state": cancelled_payload.get("status", "cancelled"),
                        "exit_code": cancelled_payload.get("exit_code"),
                        "error_code": cancelled_payload.get("error_code"),
                        "error_message": cancelled_payload.get("error_message"),
                        "log_uri": job.get("log_uri") or cancelled_payload.get("log_uri"),
                    },
                }
            )
            self.repository.insert_pipeline_event(
                entity_type="pipeline_job",
                entity_id=str(job["job_id"]),
                event_type="cancel",
                status_from=previous_status or status,
                status_to="cancelled",
                message=f"Cancelled Slurm job {slurm_job_id}; no replacement submitted in this pass.",
                details=details,
            )
            cancelled.append(record)
        return cancelled

    def _log_uri_for_pipeline_job(self, job: Mapping[str, Any]) -> str | None:
        if job.get("log_uri"):
            return str(job["log_uri"])
        run_id = job.get("run_id")
        stage = job.get("stage")
        job_id = job.get("job_id")
        if run_id and stage and job_id:
            return self._log_uri_for_stage(
                source_id=_source_id_from_cycle_id(job.get("cycle_id")) or self.config.source_id,
                cycle_time=_cycle_time_from_cycle_id(job.get("cycle_id")),
                run_id=str(run_id),
                job_id=str(job_id),
                stage=str(stage),
            )
        if run_id and stage:
            return self.object_store.uri_for_key(f"runs/{run_id}/logs/{stage}.log")
        return None

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
        candidate_uri = existing_log_uri or self._log_uri_for_stage(
            source_id=source_id,
            cycle_time=cycle_time,
            run_id=run_id,
            job_id=job_id,
            stage=stage,
        )
        should_persist_logs = existing_log_uri is None
        advertised_uri = existing_log_uri
        return DisplayLogPublication(
            candidate_uri=candidate_uri,
            advertised_uri=advertised_uri,
            should_persist_logs=should_persist_logs,
        )

    def _display_log_publication_for_pipeline_job(self, job: Mapping[str, Any]) -> DisplayLogPublication | None:
        candidate_uri = self._log_uri_for_pipeline_job(job)
        if candidate_uri is None:
            return None
        existing_log_uri = str(job["log_uri"]) if job.get("log_uri") else None
        should_persist_logs = existing_log_uri is None
        advertised_uri = existing_log_uri
        return DisplayLogPublication(
            candidate_uri=candidate_uri,
            advertised_uri=advertised_uri,
            should_persist_logs=should_persist_logs,
        )

    def _try_publish_log_for_advertise(
        self, slurm_job_id: str, publication: DisplayLogPublication
    ) -> DisplayLogPublicationAttempt:
        if not publication.should_persist_logs:
            return DisplayLogPublicationAttempt(advertised_uri=publication.advertised_uri)
        try:
            self._persist_gateway_logs(slurm_job_id, publication.candidate_uri)
        except Exception as exc:
            publish_error = self._log_persistence_error(publication.candidate_uri, exc)
            return DisplayLogPublicationAttempt(advertised_uri=None, error=publish_error)
        return DisplayLogPublicationAttempt(advertised_uri=publication.candidate_uri)

    @staticmethod
    def _log_persistence_error(candidate_uri: str, error: Exception) -> OrchestratorError:
        if isinstance(error, OrchestratorError) and error.error_code == "PUBLISHED_LOG_WRITE_FAILED":
            details = dict(error.details)
            if details.get("log_uri") == candidate_uri:
                return error
        return OrchestratorError(
            "PUBLISHED_LOG_WRITE_FAILED",
            "Failed to publish gateway logs.",
            {"log_uri": candidate_uri},
        )

    @staticmethod
    def _raise_publish_error_after_durable_update(attempt: DisplayLogPublicationAttempt | None) -> None:
        if attempt is not None and attempt.error is not None:
            raise attempt.error

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
        content = json.dumps(redact_payload(dict(payload)), sort_keys=True).encode("utf-8")
        published_path = self._published_log_path(log_uri)
        if published_path is None:
            self.object_store.write_bytes_atomic(log_uri, content)
            return log_uri
        published_root = _absolute_configured_path(Path(os.environ["NHMS_PUBLISHED_ARTIFACT_ROOT"]))
        try:
            ensure_directory_no_follow(published_root)
            atomic_write_bytes_no_follow(
                published_path,
                content,
                containment_root=published_root,
                temp_suffix="part",
            )
        except (OSError, SafeFilesystemError) as exc:
            raise OrchestratorError(
                "PUBLISHED_LOG_WRITE_FAILED",
                "Failed to publish local stage logs.",
                {"log_uri": log_uri},
            ) from exc
        return log_uri

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
        source_id = normalize_source_id(source_id)
        entries: list[dict[str, Any]] = []
        compact_cycle = format_cycle_time(cycle_time)
        for index, basin in enumerate(basins):
            if isinstance(basin, ModelContext):
                entry = {
                    "model_id": basin.model_id,
                    "basin_id": basin.basin_id,
                    "basin_version_id": basin.basin_version_id,
                    "river_network_version_id": basin.river_network_version_id,
                    "segment_count": basin.segment_count,
                    "output_segment_count": basin.output_segment_count,
                    "model_package_uri": basin.model_package_uri,
                    "model_package_checksum": basin.model_package_checksum,
                }
            else:
                entry = dict(basin)
            if entry.get("model_package_checksum") in (None, "") and entry.get("package_checksum") not in (None, ""):
                entry["model_package_checksum"] = entry["package_checksum"]
            provided_identity_fields = {
                field_name
                for field_name in (
                    "candidate_id",
                    "source_id",
                    "cycle_id",
                    "cycle_time",
                    "scenario_id",
                    "run_id",
                    "forcing_version_id",
                    "run_manifest_uri",
                    "output_uri",
                )
                if entry.get(field_name) not in (None, "")
            }
            model_id = str(entry.get("model_id") or "")
            if not model_id:
                raise OrchestratorError("BASIN_MODEL_ID_MISSING", "Each basin entry requires model_id.")
            missing_production_metadata = [
                field_name
                for field_name in ("basin_version_id", "river_network_version_id", "model_package_uri")
                if entry.get(field_name) in (None, "")
            ]
            provided_run_id = str(entry.get("run_id") or "")
            production_candidate_scope = "candidate_id" in provided_identity_fields or provided_run_id.startswith(
                f"fcst_{source_id.lower()}_{compact_cycle}_"
            )
            if production_candidate_scope and missing_production_metadata:
                raise OrchestratorError(
                    "PRODUCTION_CANDIDATE_METADATA_UNAVAILABLE",
                    "Production candidate metadata is incomplete; registry/package identity fields are required.",
                    {
                        "model_id": model_id,
                        "task_id": index,
                        "missing_fields": missing_production_metadata,
                    },
                )
            scenario_id = str(entry.get("scenario_id") or self._forecast_scenario_id(source_id))
            entry.setdefault("basin_id", entry.get("model_id"))
            entry.setdefault("basin_version_id", f"{model_id}_basin")
            entry.setdefault("river_network_version_id", f"{model_id}_river")
            entry.setdefault("run_id", f"fcst_{source_id.lower()}_{compact_cycle}_{model_id}")
            entry.setdefault("forcing_version_id", f"forc_{source_id.lower()}_{compact_cycle}_{model_id}")
            entry.setdefault("workspace_dir", str(Path(self.config.workspace_root)))
            entry.setdefault("source_id", source_id)
            entry.setdefault("cycle_time", compact_cycle)
            entry.setdefault("cycle_id", cycle_id_for(source_id, cycle_time))
            entry.setdefault("scenario_id", scenario_id)
            entry.setdefault("candidate_id", f"{source_id}:{_format_time(cycle_time)}:{model_id}:{scenario_id}")
            entry.setdefault("model_package_uri", f"models/{model_id}/")
            entry.setdefault("output_uri", _directory_uri(self.object_store, f"runs/{entry['run_id']}/output/"))
            entry.setdefault(
                "run_manifest_uri",
                self.object_store.uri_for_key(f"runs/{entry['run_id']}/input/manifest.json"),
            )
            entry.setdefault("log_uri", _directory_uri(self.object_store, f"runs/{entry['run_id']}/logs/"))
            entry["_provided_identity_fields"] = sorted(provided_identity_fields)
            entry["task_id"] = index
            entry.setdefault("original_task_id", index)
            for field_name in (
                "model_id",
                "basin_id",
                "basin_version_id",
                "river_network_version_id",
                "run_id",
            ):
                field_value = entry.get(field_name)
                if field_value not in (None, ""):
                    _validate_safe_id(f"basins[{index}].{field_name}", str(field_value))
            entries.append(entry)
        return entries

    def _apply_cohort_warm_start(
        self,
        basins: Sequence[dict[str, Any]],
        source_id: str,
        cycle_time: datetime,
    ) -> None:
        """Select each basin's warm-start state so all three manifest faces agree.

        Populates ``init_state_uri`` / ``init_state_id`` / ``init_state_checksum`` /
        ``init_state_valid_time`` / ``init_state_quality`` plus lineage on each basin
        dict. These same fields flow unchanged into (1) the scheduler basin record we
        were handed, (2) the cycle-stage manifest index entries, and (3) the forecast
        runtime manifest (which reads ``basin.get('init_state_*')``), giving a single
        selected state across all three faces (M24 §2 Lane 2).
        """

        if self.state_manager is None:
            if self.config.require_forecast_warm_start:
                raise OrchestratorError(
                    WARM_START_SUCCESSOR_CHECKPOINT_MISSING,
                    "Strict forecast warm-start requires a state manager.",
                    {"source_id": source_id, "cycle_time": _format_time(cycle_time)},
                )
            return
        for basin in basins:
            if _basin_has_prefilled_initial_state(basin):
                if self.config.require_forecast_warm_start:
                    selection = self._validate_prefilled_forecast_initial_state(
                        basin,
                        source_id=str(basin.get("source_id") or source_id),
                        cycle_time=cycle_time,
                        model_package_version=basin.get("model_package_uri"),
                        model_package_checksum=basin.get("model_package_checksum"),
                    )
                    _apply_initial_state_selection_to_basin(basin, selection)
                continue
            model_id = str(basin.get("model_id") or "")
            if not model_id:
                continue
            selection = self._select_forecast_initial_state(
                model_id=model_id,
                cycle_time=cycle_time,
                source_id=str(basin.get("source_id") or source_id),
                model_package_version=basin.get("model_package_uri"),
                model_package_checksum=basin.get("model_package_checksum"),
                max_lead_hours=_basin_max_lead_hours(basin),
            )
            _apply_initial_state_selection_to_basin(basin, selection)
            if selection.rejection_code is not None:
                basin["init_state_rejection_code"] = selection.rejection_code

    def _validate_cycle_basin_identities(
        self,
        basins: Sequence[Mapping[str, Any]],
        source_id: str,
        cycle_time: datetime,
        cycle_id: str,
    ) -> None:
        seen: dict[str, dict[str, str]] = {
            "model_id": {},
            "candidate_id": {},
            "run_id": {},
            "forcing_version_id": {},
            "run_manifest_uri": {},
            "output_uri": {},
        }
        scenario_id_for_cycle = self._forecast_scenario_id(source_id)
        compact_cycle = format_cycle_time(cycle_time)
        canonical_cycle_time = _format_time(cycle_time)
        for index, basin in enumerate(basins):
            model_id = str(basin.get("model_id") or "")
            provided_identity_fields = set(basin.get("_provided_identity_fields") or [])
            strict_identity = bool(
                provided_identity_fields
                & {
                    "candidate_id",
                    "source_id",
                    "cycle_id",
                    "cycle_time",
                    "scenario_id",
                    "forcing_version_id",
                    "run_manifest_uri",
                }
            )
            strict_identity = strict_identity or (
                "run_id" in provided_identity_fields
                and str(basin.get("run_id") or "").startswith(f"fcst_{source_id.lower()}_")
            )
            expected = {
                "source_id": source_id,
                "cycle_id": cycle_id,
                "cycle_time": compact_cycle,
                "scenario_id": scenario_id_for_cycle,
                "candidate_id": f"{source_id}:{canonical_cycle_time}:{model_id}:{scenario_id_for_cycle}",
                "run_id": f"fcst_{source_id.lower()}_{compact_cycle}_{model_id}",
                "forcing_version_id": f"forc_{source_id.lower()}_{compact_cycle}_{model_id}",
                "run_manifest_uri": self.object_store.uri_for_key(
                    f"runs/fcst_{source_id.lower()}_{compact_cycle}_{model_id}/input/manifest.json"
                ),
                "output_uri": _directory_uri(
                    self.object_store,
                    f"runs/fcst_{source_id.lower()}_{compact_cycle}_{model_id}/output/",
                ),
            }
            output_uri = str(basin.get("output_uri") or expected["output_uri"])
            run_manifest_uri = str(basin.get("run_manifest_uri") or expected["run_manifest_uri"])
            values = {
                "model_id": model_id,
                "candidate_id": str(basin.get("candidate_id") or expected["candidate_id"]),
                "run_id": str(basin.get("run_id") or expected["run_id"]),
                "forcing_version_id": str(basin.get("forcing_version_id") or expected["forcing_version_id"]),
                "run_manifest_uri": run_manifest_uri,
                "output_uri": output_uri.rstrip("/") + "/" if _has_uri_scheme(output_uri) else output_uri.strip(),
            }
            for field_name, value in values.items():
                previous = seen[field_name].get(value)
                if previous is not None:
                    raise OrchestratorError(
                        "DUPLICATE_CANDIDATE_IDENTITY",
                        f"Duplicate {field_name} in cycle basin list.",
                        {
                            "field": field_name,
                            "value": value,
                            "first_model_id": previous,
                            "model_id": model_id,
                            "task_id": index,
                        },
                    )
                seen[field_name][value] = model_id
            for field_name, expected_value in expected.items():
                actual = basin.get(field_name)
                if actual in (None, ""):
                    continue
                if not strict_identity and field_name not in {"source_id", "cycle_id", "cycle_time", "scenario_id"}:
                    continue
                if not strict_identity and field_name in {"source_id", "cycle_id", "cycle_time", "scenario_id"}:
                    if field_name not in provided_identity_fields:
                        continue
                if field_name == "cycle_time":
                    try:
                        actual_value = format_cycle_time(actual)
                    except (TypeError, ValueError) as exc:
                        raise OrchestratorError(
                            "CANDIDATE_IDENTITY_MISMATCH",
                            f"basins[{index}].cycle_time is not a valid cycle time.",
                            {"field": field_name, "actual": actual, "task_id": index},
                        ) from exc
                elif field_name == "output_uri":
                    actual_text = str(actual).strip()
                    if _has_uri_scheme(actual_text):
                        actual_value = actual_text.rstrip("/") + "/"
                    elif actual_text.strip("/") == f"runs/{expected['run_id']}/output":
                        actual_value = str(expected_value)
                        if isinstance(basin, dict):
                            basin["output_uri"] = actual_value
                    else:
                        actual_value = actual_text
                    expected_value = str(expected_value)
                else:
                    actual_value = str(actual)
                    expected_value = str(expected_value)
                if actual_value != expected_value:
                    raise OrchestratorError(
                        "CANDIDATE_IDENTITY_MISMATCH",
                        f"basins[{index}].{field_name} does not match the orchestration context.",
                        {
                            "field": field_name,
                            "actual": actual_value,
                            "expected": expected_value,
                            "task_id": index,
                            "model_id": model_id,
                        },
                    )

    def _query_pipeline_jobs_by_cycle(self, cycle_id: str) -> list[dict[str, Any]]:
        query = getattr(self.repository, "query_pipeline_jobs_by_cycle", None)
        if callable(query):
            return [dict(job) for job in query(cycle_id)]
        return []

    def _query_pipeline_jobs_for_cycle_context(self, context: CycleOrchestrationContext) -> list[dict[str, Any]]:
        if _candidate_scoped_cycle_execution(context.all_basins):
            query = getattr(self.repository, "query_pipeline_jobs_by_run", None)
            if callable(query):
                return [dict(job) for job in query(context.run_id)]
            candidate_model_id = _cycle_pipeline_job_model_id(context)
            return [
                job
                for job in self._query_pipeline_jobs_by_cycle(context.cycle_id)
                if str(job.get("run_id") or "") == context.run_id
                or (candidate_model_id is not None and str(job.get("model_id") or "") == candidate_model_id)
            ]
        return self._query_pipeline_jobs_by_cycle(context.cycle_id)

    def _find_existing_stage_job(
        self,
        jobs: Sequence[Mapping[str, Any]],
        stage: StageDefinition,
        *,
        context: CycleOrchestrationContext,
    ) -> dict[str, Any] | None:
        matches = [dict(job) for job in jobs if ForecastOrchestrator._job_matches_stage(job, stage)]
        if not matches:
            return None
        active_matches = [job for job in matches if str(job.get("status")) not in TERMINAL_JOB_STATUSES]
        return dict(max(active_matches or matches, key=lambda job: _stage_job_sort_key(job, stage)))

    def _cycle_download_success_missing_raw_manifest(
        self,
        stage: StageDefinition,
        context: CycleOrchestrationContext,
        job: Mapping[str, Any],
    ) -> bool:
        if stage.stage != "download":
            return False
        if str(job.get("status") or "") not in TERMINAL_PIPELINE_SUCCESS_STATUSES:
            return False
        manifest_uri = self.object_store.uri_for_key(
            f"raw/{context.source_id}/{format_cycle_time(context.cycle_time)}/manifest.json"
        )
        return not self.object_store.exists(manifest_uri)

    @staticmethod
    def _job_matches_stage(job: Mapping[str, Any], stage: StageDefinition) -> bool:
        return job.get("stage") == stage.stage or job.get("job_type") == stage.job_type

    @staticmethod
    def _job_needs_submission(job: Mapping[str, Any]) -> bool:
        return str(job.get("status")) == "pending" and not job.get("slurm_job_id")

    def trigger_forecast(
        self,
        *,
        source_id: str | None = None,
        cycle_time: str | datetime,
        model_id: str,
        basin_id: str | None = None,
        max_lead_hours: int | None = None,
    ) -> PipelineResult:
        return self._trigger_forecast(
            source_id=source_id or self.config.source_id,
            cycle_time=cycle_time,
            model_id=model_id,
            basin_id=basin_id,
            max_lead_hours=max_lead_hours,
            stages=LEGACY_FORECAST_STAGES,
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
        return self._trigger_forecast(
            source_id=source_id or self.config.source_id,
            cycle_time=cycle_time,
            model_id=model_id,
            basin_id=basin_id,
            max_lead_hours=max_lead_hours,
            stages=LEGACY_FORECAST_STAGES[2:],
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
        source_id = normalize_source_id(source_id)
        parsed_cycle_time = parse_cycle_time(cycle_time)
        if self.repository.has_active_pipeline(source_id=source_id, cycle_time=parsed_cycle_time, model_id=model_id):
            raise PipelineAlreadyActiveError(source_id, parsed_cycle_time, model_id)

        model = self.repository.load_model_context(model_id)
        if basin_id is not None and model.basin_id is not None and model.basin_id != basin_id:
            raise OrchestratorError(
                "MODEL_BASIN_MISMATCH",
                f"Model {model_id} belongs to basin {model.basin_id}, not {basin_id}.",
            )
        forcing = self.repository.find_forcing_context(
            source_id=source_id,
            cycle_time=parsed_cycle_time,
            model_id=model_id,
        )
        initial_state = self._select_forecast_initial_state(
            model_id=model_id,
            cycle_time=parsed_cycle_time,
            source_id=source_id,
            model_package_version=model.model_package_uri,
            model_package_checksum=model.model_package_checksum,
            max_lead_hours=max_lead_hours,
        )
        self.repository.ensure_forecast_cycle(source_id=source_id, cycle_time=parsed_cycle_time)
        context = self._build_run_context(
            source_id,
            parsed_cycle_time,
            model,
            forcing,
            initial_state,
            max_lead_hours=max_lead_hours,
        )
        manifest = self._build_run_manifest(context)
        self._write_run_manifest(context, manifest)
        self.repository.create_hydro_run(context, manifest)
        self.repository.update_hydro_run_status(context.run_id, "staged")
        return self.run_chain(context, stages=stages)

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
        return self.repository.list_stage_statuses(
            source_id=normalize_source_id(source_id) if source_id is not None else None,
            cycle_time=parse_cycle_time(cycle_time),
            model_id=model_id,
        )

    def trigger_ready_forecasts(
        self,
        *,
        source_id: str | None = None,
        model_ids: Sequence[str] | None = None,
        limit: int = 100,
    ) -> tuple[PipelineResult, ...]:
        resolved_source_id = normalize_source_id(source_id or self.config.source_id)
        ready_cycles = self._list_canonical_ready_cycles(source_id=resolved_source_id, limit=limit)
        selected_model_ids = tuple(model_ids) if model_ids is not None else self._list_forecast_model_ids()
        results: list[PipelineResult] = []
        for cycle in ready_cycles:
            cycle_source_id = normalize_source_id(str(cycle.get("source_id") or resolved_source_id))
            cycle_time_value = cycle.get("cycle_time")
            if cycle_time_value is None:
                continue
            parsed_cycle_time = parse_cycle_time(cycle_time_value)
            max_lead_hours = _optional_int(cycle.get("max_lead_hours"))
            stale_versions = _stale_converter_versions_in_cycle(cycle, source_id=cycle_source_id)
            if stale_versions:
                self._demote_stale_canonical_cycle(
                    source_id=cycle_source_id,
                    cycle_time=parsed_cycle_time,
                    stale_versions=stale_versions,
                )
                for model_id in selected_model_ids:
                    results.append(
                        _skipped_ready_forecast_result(
                            source_id=cycle_source_id,
                            cycle_time=parsed_cycle_time,
                            model_id=model_id,
                            reason="canonical_converter_version_stale",
                            canonical_readiness={
                                "ready": False,
                                "reason": "canonical_converter_version_stale",
                                "expected_converter_version": expected_converter_version(cycle_source_id),
                                "observed_converter_versions": sorted(stale_versions),
                            },
                        )
                    )
                continue
            readiness = self._validate_auto_trigger_canonical_readiness(
                cycle,
                source_id=cycle_source_id,
                cycle_time=parsed_cycle_time,
                max_lead_hours=max_lead_hours,
            )
            for model_id in selected_model_ids:
                if not bool(readiness.get("ready")):
                    results.append(
                        _skipped_ready_forecast_result(
                            source_id=cycle_source_id,
                            cycle_time=parsed_cycle_time,
                            model_id=model_id,
                            reason=str(readiness.get("reason") or "canonical_readiness_not_trusted"),
                            canonical_readiness=readiness,
                        )
                    )
                    continue
                if self._has_completed_forecast(
                    source_id=cycle_source_id,
                    cycle_time=parsed_cycle_time,
                    model_id=model_id,
                ):
                    continue
                try:
                    results.append(
                        self.trigger_forecast_from_canonical(
                            source_id=cycle_source_id,
                            cycle_time=parsed_cycle_time,
                            model_id=model_id,
                            max_lead_hours=max_lead_hours,
                        )
                    )
                except PipelineAlreadyActiveError:
                    continue
        return tuple(results)

    def _demote_stale_canonical_cycle(
        self,
        *,
        source_id: str,
        cycle_time: datetime,
        stale_versions: set[str | None],
    ) -> None:
        """Roll a canonical-ready cycle back to ``raw_complete`` for re-conversion.

        Products written by a stale/missing converter_version cannot be consumed
        by the producer (it enforces mm/day canonical units post-#269). Demoting
        to the convert-stage input state makes the next tick re-run
        ``convert_canonical`` with the current converter_version. Only the status
        is changed; canonical rows are left intact.
        """
        expected = expected_converter_version(source_id)
        self.repository.update_forecast_cycle_status(
            source_id=source_id,
            cycle_time=cycle_time,
            status=CANONICAL_DEMOTE_CYCLE_STATUS,
        )
        self.repository.insert_pipeline_event(
            entity_type="forecast_cycle",
            entity_id=cycle_id_for(source_id, cycle_time),
            event_type="canonical_converter_version_stale",
            status_from="canonical_ready",
            status_to=CANONICAL_DEMOTE_CYCLE_STATUS,
            message="Canonical products written by a stale converter_version; demoting for re-conversion.",
            details=_safe_pipeline_event_details(
                {
                    "source_id": source_id,
                    "cycle_time": _format_time(cycle_time),
                    "expected_converter_version": expected,
                    "observed_converter_versions": sorted(
                        "<missing>" if version is None else str(version) for version in stale_versions
                    ),
                }
            ),
        )

    def _validate_auto_trigger_canonical_readiness(
        self,
        cycle: Mapping[str, Any],
        *,
        source_id: str,
        cycle_time: datetime,
        max_lead_hours: int | None,
    ) -> dict[str, Any]:
        forecast_hours = _auto_trigger_forecast_hours(
            source_id=source_id,
            cycle_time=cycle_time,
            configured_horizon_hours=self.config.forecast_horizon_hours,
            max_lead_hours=max_lead_hours,
        )
        try:
            policy_identity = _auto_trigger_source_policy_identity(
                source_id=source_id,
                cycle_time=cycle_time,
                forecast_hours=forecast_hours,
                workspace_root=self.config.workspace_root,
                object_store_root=self.config.object_store_root,
                object_store_prefix=self.config.object_store_prefix,
            )
            source_object_identity = _auto_trigger_source_object_identity(
                source_id=source_id,
                cycle_time=cycle_time,
                forecast_hours=forecast_hours,
                workspace_root=self.config.workspace_root,
                object_store_root=self.config.object_store_root,
                object_store_prefix=self.config.object_store_prefix,
            )
            products = _canonical_products_from_ready_cycle(cycle, source_id=source_id, cycle_time=cycle_time)
            readiness = evaluate_canonical_readiness(
                source_id=source_id,
                cycle_time=cycle_time,
                products=products,
                forecast_hours=forecast_hours,
                policy_identity=policy_identity,
                source_object_identity=source_object_identity,
                canonical_product_id=f"canon_{source_id.lower()}_{format_cycle_time(cycle_time)}",
            )
            evidence = dict(readiness.evidence)
        except Exception as error:
            evidence = _auto_trigger_canonical_readiness_unavailable_evidence(
                source_id=source_id,
                cycle_time=cycle_time,
                forecast_hours=forecast_hours,
                reason="canonical_readiness_query_failed",
                error=error,
            )
        evidence.setdefault("entrypoint", "trigger_ready_forecasts")
        evidence.setdefault("source_id", source_id)
        evidence.setdefault("source", source_id)
        evidence.setdefault("cycle_time", _format_time(cycle_time))
        evidence.setdefault("accepted_horizon", _accepted_horizon_from_hours(forecast_hours))
        return dict(redact_payload(_json_safe_pipeline_event_value(evidence)))

    def _list_canonical_ready_cycles(self, *, source_id: str | None, limit: int) -> tuple[dict[str, Any], ...]:
        provider = getattr(self.repository, "list_canonical_ready_cycles", None)
        if not callable(provider):
            raise OrchestratorError(
                "READY_CYCLE_LIST_UNSUPPORTED",
                "The orchestrator repository does not support canonical-ready cycle listing.",
            )
        return tuple(dict(cycle) for cycle in provider(source_id=source_id, limit=limit))

    def _list_forecast_model_ids(self) -> tuple[str, ...]:
        provider = getattr(self.repository, "list_forecast_model_ids", None)
        if not callable(provider):
            raise OrchestratorError(
                "FORECAST_MODEL_LIST_UNSUPPORTED",
                "The orchestrator repository does not support forecast model listing.",
            )
        return tuple(str(model_id) for model_id in provider())

    def _has_completed_forecast(self, *, source_id: str, cycle_time: datetime, model_id: str) -> bool:
        provider = getattr(self.repository, "has_completed_pipeline", None)
        if callable(provider):
            return bool(provider(source_id=source_id, cycle_time=cycle_time, model_id=model_id))
        run_id = f"fcst_{source_id.lower()}_{format_cycle_time(cycle_time)}_{model_id}"
        hydro_runs = getattr(self.repository, "hydro_runs", None)
        if isinstance(hydro_runs, Mapping):
            run = hydro_runs.get(run_id)
            if isinstance(run, Mapping):
                return str(run.get("status")) in COMPLETED_HYDRO_STATUSES
        return False

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
        logs = _coerce_mapping(self.slurm_client.fetch_logs(slurm_job_id))
        content = str(logs.get("logs", ""))
        try:
            published_path = self._published_log_path(log_uri)
            if published_path is None:
                self.object_store.write_bytes_atomic(log_uri, content.encode("utf-8"))
                return
            published_root = _absolute_configured_path(Path(os.environ["NHMS_PUBLISHED_ARTIFACT_ROOT"]))
            try:
                ensure_directory_no_follow(published_root)
                atomic_write_bytes_no_follow(
                    published_path,
                    content.encode("utf-8"),
                    containment_root=published_root,
                    temp_suffix="part",
                )
            except (OSError, SafeFilesystemError) as exc:
                raise OrchestratorError(
                    "PUBLISHED_LOG_WRITE_FAILED",
                    "Failed to publish gateway logs.",
                    {"log_uri": log_uri},
                ) from exc
        except ArtifactLogError as exc:
            raise OrchestratorError(
                "PUBLISHED_LOG_WRITE_FAILED",
                "Failed to publish gateway logs.",
                {"log_uri": log_uri},
            ) from exc

    def _log_uri_for_stage(
        self,
        *,
        source_id: str,
        cycle_time: datetime | None,
        run_id: str,
        job_id: str,
        stage: str,
    ) -> str:
        if _published_artifact_root_configured():
            return published_log_uri(
                source=normalize_source_id(source_id),
                cycle_time=cycle_time or _utcnow(),
                run_id=run_id,
                job_id=job_id,
                stream=_log_stream_for_stage(stage),
            )
        return self.object_store.uri_for_key(f"runs/{run_id}/logs/{stage}.log")

    def _published_log_path(self, log_uri: str) -> Path | None:
        published_root = os.getenv("NHMS_PUBLISHED_ARTIFACT_ROOT", "").strip()
        if not published_root:
            return None
        prefix = os.getenv("NHMS_PUBLISHED_ARTIFACT_URI_PREFIX", "published://").strip() or "published://"
        if not log_uri.startswith(prefix):
            return None
        relative = published_log_relative_path(log_uri, uri_prefix=prefix)
        root = _absolute_configured_path(Path(published_root))
        return root / relative

    def _build_run_context(
        self,
        source_id: str,
        cycle_time: datetime,
        model: ModelContext,
        forcing: ForcingContext,
        initial_state: InitialStateSelection | None = None,
        max_lead_hours: int | None = None,
    ) -> ForecastRunContext:
        source_id = normalize_source_id(source_id)
        compact_cycle = format_cycle_time(cycle_time)
        run_id = f"fcst_{source_id.lower()}_{compact_cycle}_{model.model_id}"
        start_time = cycle_time
        forecast_horizon_hours = _resolve_forecast_horizon_hours(
            source_id=source_id,
            cycle_time=cycle_time,
            configured_horizon_hours=self.config.forecast_horizon_hours,
            forcing=forcing,
            max_lead_hours=max_lead_hours,
        )
        end_time = cycle_time + timedelta(hours=forecast_horizon_hours)
        fallback_forcing_uri = f"forcing/{source_id.lower()}/{compact_cycle}/{model.basin_version_id}/{model.model_id}/"
        selected_state = initial_state or InitialStateSelection(None, None, None, None, "cold_start_no_state")
        return ForecastRunContext(
            run_id=run_id,
            source_id=source_id,
            scenario_id=self._forecast_scenario_id(source_id),
            cycle_id=cycle_id_for(source_id, cycle_time),
            cycle_time=cycle_time,
            model_id=model.model_id,
            basin_id=model.basin_id,
            basin_version_id=model.basin_version_id,
            river_network_version_id=model.river_network_version_id,
            segment_count=model.segment_count,
            model_package_uri=model.model_package_uri,
            forcing_version_id=forcing.forcing_version_id,
            forcing_package_uri=forcing.forcing_package_uri or fallback_forcing_uri,
            start_time=start_time,
            end_time=end_time,
            forecast_horizon_hours=forecast_horizon_hours,
            run_manifest_uri=self.object_store.uri_for_key(f"runs/{run_id}/input/manifest.json"),
            output_uri=_directory_uri(self.object_store, f"runs/{run_id}/output/"),
            log_uri=_directory_uri(self.object_store, f"runs/{run_id}/logs/"),
            forcing_package_manifest_uri=getattr(forcing, "forcing_package_manifest_uri", None),
            forcing_package_manifest_checksum=getattr(forcing, "forcing_package_manifest_checksum", None),
            init_state_id=selected_state.state_id,
            init_state_uri=selected_state.state_uri,
            init_state_valid_time=selected_state.valid_time,
            init_state_checksum=selected_state.checksum,
            init_state_quality=selected_state.quality,
            init_state_lineage=_initial_state_lineage(selected_state),
            output_segment_count=model.output_segment_count,
        )

    def _forecast_scenario_id(self, source_id: str) -> str:
        if self.config.scenario_id_explicit and self.config.scenario_id:
            return self.config.scenario_id
        return scenario_for_source(source_id)

    def _build_run_manifest(self, context: ForecastRunContext) -> dict[str, Any]:
        return chain_manifests.build_forecast_run_manifest(
            context,
            forecast_state_checkpoint_hours=_forecast_state_checkpoint_hours,
        )

    def _state_passes_qc(self, state: StateSnapshot) -> bool:
        """Selection-time QC gate for a warm-start candidate.

        Defers to the state manager's optional ``state_variable_qc_passed`` hook when
        present; absent the hook, a usable snapshot is trusted (run-time/save-time QC
        already gated ``usable_flag``). Returns False to skip a candidate that fails QC.
        """

        hook = getattr(self.state_manager, "state_variable_qc_passed", None)
        if hook is None:
            return True
        try:
            return bool(hook(state))
        except Exception:  # noqa: BLE001 - a QC hook failure must not crash selection
            return False

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
        if self.config.require_forecast_warm_start:
            return self._select_strict_forecast_initial_state(
                model_id=model_id,
                cycle_time=cycle_time,
                source_id=source_id,
                model_package_version=model_package_version,
                model_package_checksum=model_package_checksum,
            )
        if self.state_manager is None:
            return InitialStateSelection(None, None, None, None, "cold_start_no_state")

        cursor = cycle_time
        last_rejection_code: str | None = None
        # Fallback loop: reject incompatible-lineage / failed-QC candidates and try
        # the next older usable state, never failing the cycle for a missing successor.
        for _ in range(_MAX_STATE_FALLBACK_CANDIDATES):
            state = self._exact_or_latest_usable_state(
                model_id=model_id,
                cycle_time=cycle_time,
                before_time=cursor,
                source_id=source_id,
            )
            if state is None:
                return InitialStateSelection(
                    None, None, None, None, "cold_start_no_state", rejection_code=last_rejection_code
                )

            quality = assess_freshness(
                state.valid_time,
                cycle_time,
                soft_threshold_days=self.config.state_soft_stale_threshold_days,
                hard_threshold_days=self.config.state_hard_stale_threshold_days,
            )
            if quality == "cold_start_stale_state":
                # Older states are even staler; stop and record stale cold start. The
                # primary cause here is staleness, so the rejection_code is the explicit
                # STATE_TOO_STALE marker -- never a carried-forward LINEAGE_* code from a
                # younger candidate, which would falsely conflate quality=stale with a
                # lineage rejection.
                return InitialStateSelection(None, None, None, None, quality, rejection_code=STATE_TOO_STALE)

            rejection_code = _validate_state_lineage(
                state,
                source_id=source_id,
                model_package_version=model_package_version,
                model_package_checksum=model_package_checksum,
                max_lead_hours=max_lead_hours,
            )
            if rejection_code is None and not self._state_passes_qc(state):
                rejection_code = STATE_QC_FAILED
            if rejection_code is not None:
                # Record the rejection on the candidate and advance to an older one.
                last_rejection_code = rejection_code
                cursor = state.valid_time - timedelta(microseconds=1)
                continue

            return InitialStateSelection(
                state_id=state.state_id,
                state_uri=state.state_uri,
                valid_time=state.valid_time,
                checksum=state.checksum,
                quality=quality,
                source_id=state.source_id,
                cycle_id=state.cycle_id,
                lead_hours=state.lead_hours,
                model_package_version=state.model_package_version,
                model_package_checksum=state.model_package_checksum,
                rejection_code=None,
            )

        return InitialStateSelection(None, None, None, None, "cold_start_no_state", rejection_code=last_rejection_code)

    def _select_strict_forecast_initial_state(
        self,
        *,
        model_id: str,
        cycle_time: datetime,
        source_id: str | None,
        model_package_version: str | None,
        model_package_checksum: str | None,
    ) -> InitialStateSelection:
        if self.state_manager is None:
            raise OrchestratorError(
                WARM_START_SUCCESSOR_CHECKPOINT_MISSING,
                "Strict forecast warm-start requires a state manager.",
                {"model_id": model_id, "source_id": source_id, "cycle_time": _format_time(cycle_time)},
            )
        state = self._get_exact_forecast_state(
            model_id=model_id,
            cycle_time=cycle_time,
            source_id=source_id,
        )
        if state is None:
            raise OrchestratorError(
                WARM_START_SUCCESSOR_CHECKPOINT_MISSING,
                "Exact successor checkpoint is required for strict forecast warm-start.",
                {"model_id": model_id, "source_id": source_id, "cycle_time": _format_time(cycle_time)},
            )
        return self._validate_strict_forecast_state(
            state,
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
        model_id = str(basin.get("model_id") or "")
        if not model_id:
            raise OrchestratorError("BASIN_MODEL_ID_MISSING", "Each basin entry requires model_id.")
        state = self._resolve_prefilled_forecast_state(
            basin,
            model_id=model_id,
            cycle_time=cycle_time,
            source_id=source_id,
        )
        if state is None:
            raise OrchestratorError(
                WARM_START_SUCCESSOR_CHECKPOINT_MISSING,
                "Scheduler-prefilled warm-start state was not found.",
                {
                    "model_id": model_id,
                    "source_id": source_id,
                    "cycle_time": _format_time(cycle_time),
                    "init_state_id": basin.get("init_state_id"),
                    "init_state_uri": basin.get("init_state_uri"),
                },
            )
        selection = self._validate_strict_forecast_state(
            state,
            model_id=model_id,
            cycle_time=cycle_time,
            source_id=source_id,
            model_package_version=model_package_version,
            model_package_checksum=model_package_checksum,
        )
        self._validate_prefilled_state_identity(basin, selection)
        return selection

    def _resolve_prefilled_forecast_state(
        self,
        basin: Mapping[str, Any],
        *,
        model_id: str,
        cycle_time: datetime,
        source_id: str | None,
    ) -> StateSnapshot | None:
        if self.state_manager is None:
            return None
        state_id = _optional_str(basin.get("init_state_id"))
        if state_id is not None:
            provider = getattr(self.state_manager, "get_state_snapshot", None)
            if callable(provider):
                state = provider(state_id)
                if state is not None:
                    return state
            repository_provider = getattr(getattr(self.state_manager, "repository", None), "get_state_snapshot", None)
            if callable(repository_provider):
                state = repository_provider(state_id)
                if state is not None:
                    return state
        return self._get_exact_forecast_state(model_id=model_id, cycle_time=cycle_time, source_id=source_id)

    def _validate_prefilled_state_identity(
        self,
        basin: Mapping[str, Any],
        selection: InitialStateSelection,
    ) -> None:
        raw_valid_time = basin.get("init_state_valid_time")
        try:
            valid_time = _parse_gateway_time(raw_valid_time)
        except (TypeError, ValueError) as exc:
            raise OrchestratorError(
                WARM_START_LINEAGE_MISMATCH,
                "Scheduler-prefilled warm-start valid_time is malformed.",
                {"field": "init_state_valid_time", "observed": raw_valid_time},
            ) from exc
        if raw_valid_time not in (None, "") and valid_time is None:
            raise OrchestratorError(
                WARM_START_LINEAGE_MISMATCH,
                "Scheduler-prefilled warm-start valid_time is malformed.",
                {"field": "init_state_valid_time", "observed": raw_valid_time},
            )
        raw_lineage = basin.get("init_state_lineage")
        if raw_lineage in (None, ""):
            lineage: dict[str, Any] = {}
        elif isinstance(raw_lineage, Mapping):
            lineage = dict(raw_lineage)
        else:
            raise OrchestratorError(
                WARM_START_LINEAGE_MISMATCH,
                "Scheduler-prefilled warm-start lineage is malformed.",
                {"field": "init_state_lineage", "observed_type": type(raw_lineage).__name__},
            )
        checks = (
            ("init_state_id", selection.state_id),
            ("init_state_uri", selection.state_uri),
            ("init_state_checksum", selection.checksum),
        )
        for field_name, expected in checks:
            observed = basin.get(field_name)
            if observed not in (None, "") and expected is not None and str(observed) != str(expected):
                raise OrchestratorError(
                    WARM_START_LINEAGE_MISMATCH,
                    "Scheduler-prefilled warm-start identity does not match the strict successor checkpoint.",
                    {"field": field_name, "observed": observed, "expected": expected},
                )
        if valid_time is not None and selection.valid_time is not None:
            if _ensure_utc(valid_time) != _ensure_utc(selection.valid_time):
                raise OrchestratorError(
                    WARM_START_LINEAGE_MISMATCH,
                    "Scheduler-prefilled warm-start valid_time does not match the strict successor checkpoint.",
                    {"observed": _format_time(valid_time), "expected": _format_time(selection.valid_time)},
                )
        lineage_checks = (
            ("cycle_id", selection.cycle_id),
            ("model_package_version", selection.model_package_version),
            ("model_package_checksum", selection.model_package_checksum),
        )
        observed_source_id = lineage.get("source_id")
        if observed_source_id not in (None, "") and selection.source_id not in (None, ""):
            try:
                observed_normalized_source = normalize_source_id(str(observed_source_id))
                expected_normalized_source = normalize_source_id(selection.source_id)
            except ValueError as exc:
                raise OrchestratorError(
                    WARM_START_LINEAGE_MISMATCH,
                    "Scheduler-prefilled warm-start lineage source_id is malformed.",
                    {"field": "source_id", "observed": observed_source_id, "expected": selection.source_id},
                ) from exc
            if observed_normalized_source != expected_normalized_source:
                raise OrchestratorError(
                    WARM_START_LINEAGE_MISMATCH,
                    "Scheduler-prefilled warm-start lineage does not match the strict successor checkpoint.",
                    {"field": "source_id", "observed": observed_source_id, "expected": selection.source_id},
                )
        for key, expected in lineage_checks:
            observed = lineage.get(key)
            if observed not in (None, "") and expected not in (None, "") and str(observed) != str(expected):
                raise OrchestratorError(
                    WARM_START_LINEAGE_MISMATCH,
                    "Scheduler-prefilled warm-start lineage does not match the strict successor checkpoint.",
                    {"field": key, "observed": observed, "expected": expected},
                )
        raw_lead_hours = lineage.get("lead_hours")
        if raw_lead_hours in (None, ""):
            lead_hours = None
        elif isinstance(raw_lead_hours, bool):
            raise OrchestratorError(
                WARM_START_LINEAGE_MISMATCH,
                "Scheduler-prefilled warm-start lead_hours is malformed.",
                {"field": "lead_hours", "observed": raw_lead_hours},
            )
        elif isinstance(raw_lead_hours, int):
            lead_hours = raw_lead_hours
        elif isinstance(raw_lead_hours, str) and raw_lead_hours.strip().lstrip("+-").isdigit():
            lead_hours = int(raw_lead_hours)
        else:
            raise OrchestratorError(
                WARM_START_LINEAGE_MISMATCH,
                "Scheduler-prefilled warm-start lead_hours is malformed.",
                {"field": "lead_hours", "observed": raw_lead_hours},
            )
        if lead_hours is not None and selection.lead_hours is not None and lead_hours != selection.lead_hours:
            raise OrchestratorError(
                WARM_START_LINEAGE_MISMATCH,
                "Scheduler-prefilled warm-start lead_hours does not match the strict successor checkpoint.",
                {"observed": lead_hours, "expected": selection.lead_hours},
            )

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
        if state.model_id != model_id or _ensure_utc(state.valid_time) != _ensure_utc(cycle_time):
            raise OrchestratorError(
                WARM_START_LINEAGE_MISMATCH,
                "Strict forecast warm-start state must match model_id and cycle_time.",
                {
                    "model_id": model_id,
                    "state_model_id": state.model_id,
                    "cycle_time": _format_time(cycle_time),
                    "state_valid_time": _format_time(state.valid_time),
                },
            )
        if not state.usable_flag or not self._state_passes_qc(state):
            raise OrchestratorError(
                WARM_START_SUCCESSOR_CHECKPOINT_UNUSABLE,
                "Exact successor checkpoint is unusable or failed state-variable QC.",
                {"state_id": state.state_id, "cycle_time": _format_time(cycle_time)},
            )
        rejection_code = _validate_strict_state_lineage(
            state,
            source_id=source_id,
            model_package_version=model_package_version,
            model_package_checksum=model_package_checksum,
        )
        if rejection_code is not None or state.lead_hours != 12:
            raise OrchestratorError(
                WARM_START_LINEAGE_MISMATCH,
                "Exact successor checkpoint lineage is incompatible with strict forecast warm-start.",
                {
                    "state_id": state.state_id,
                    "lineage_rejection_code": rejection_code,
                    "lead_hours": state.lead_hours,
                    "required_lead_hours": 12,
                },
            )
        return InitialStateSelection(
            state_id=state.state_id,
            state_uri=state.state_uri,
            valid_time=state.valid_time,
            checksum=state.checksum,
            quality="fresh",
            source_id=state.source_id,
            cycle_id=state.cycle_id,
            lead_hours=state.lead_hours,
            model_package_version=state.model_package_version,
            model_package_checksum=state.model_package_checksum,
            rejection_code=None,
        )

    def _get_exact_forecast_state(
        self,
        *,
        model_id: str,
        cycle_time: datetime,
        source_id: str | None,
    ) -> StateSnapshot | None:
        if self.state_manager is None:
            return None
        repository = getattr(self.state_manager, "repository", None)
        exact_provider = getattr(repository, "get_state_snapshot_by_model_time", None)
        if not callable(exact_provider):
            exact_provider = getattr(self.state_manager, "get_state_snapshot_by_model_time", None)
        if not callable(exact_provider):
            return None
        if source_id is not None:
            exact = exact_provider(model_id=model_id, valid_time=_ensure_utc(cycle_time), source_id=source_id)
            if exact is not None:
                return exact
        return exact_provider(model_id=model_id, valid_time=_ensure_utc(cycle_time), source_id=None)

    def _exact_or_latest_usable_state(
        self,
        *,
        model_id: str,
        cycle_time: datetime,
        before_time: datetime,
        source_id: str | None,
    ) -> StateSnapshot | None:
        if self.state_manager is None:
            return None
        repository = getattr(self.state_manager, "repository", None)
        exact_provider = getattr(repository, "get_state_snapshot_by_model_time", None)
        if callable(exact_provider) and _ensure_utc(before_time) == _ensure_utc(cycle_time):
            exact = exact_provider(model_id=model_id, valid_time=_ensure_utc(cycle_time), source_id=source_id)
            if exact is not None and exact.usable_flag:
                return exact
        return self.state_manager.get_latest_usable_state(model_id=model_id, before_time=before_time)

    def _write_run_manifest(self, context: ForecastRunContext | AnalysisRunContext, manifest: dict[str, Any]) -> None:
        chain_manifests.write_run_manifest(self, context, manifest)

    def _workspace_path(self, *parts: str) -> Path:
        workspace_root = Path(self.config.workspace_root).expanduser().resolve()
        if any(Path(part).is_absolute() or ".." in Path(part).parts for part in parts):
            raise OrchestratorError(
                "WORKSPACE_PATH_ESCAPE",
                "Workspace path components must be relative and must not contain traversal segments.",
                {"parts": list(parts), "workspace_root": str(workspace_root)},
            )
        resolved = workspace_root.joinpath(*parts)
        try:
            resolved.relative_to(workspace_root)
        except ValueError as exc:
            raise OrchestratorError(
                "WORKSPACE_PATH_ESCAPE",
                "Resolved workspace path is outside workspace_root.",
                {"path": str(resolved), "workspace_root": str(workspace_root)},
            ) from exc
        return resolved

    def _safe_workspace_write_bytes(self, path: Path, content: bytes) -> Path:
        workspace_root = Path(self.config.workspace_root).expanduser().resolve()
        _workspace_relative_parts(path, workspace_root)
        ensure_directory_no_follow(workspace_root)
        ensure_directory_no_follow(path.parent, containment_root=workspace_root)
        return atomic_write_bytes_no_follow(path, content, containment_root=workspace_root, temp_suffix="part")

    def _safe_workspace_read_bytes(self, path: Path) -> bytes:
        workspace_root = Path(self.config.workspace_root).expanduser().resolve()
        _workspace_relative_parts(path, workspace_root)
        return read_bytes_no_follow(path, containment_root=workspace_root)


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


class AnalysisOrchestrator(ForecastOrchestrator):
    stages = ANALYSIS_STAGES
    final_pipeline_status = "succeeded"

    def __init__(
        self,
        *,
        config: OrchestratorConfig,
        repository: OrchestratorRepository,
        state_manager: StateManager | None = None,
        best_available_manager: BestAvailableManager | None = None,
        slurm_client: SlurmGatewayClient | None = None,
        object_store: LocalObjectStore | None = None,
        retry_service: RetryService | None = None,
    ) -> None:
        super().__init__(
            config=config,
            repository=repository,
            state_manager=state_manager,
            slurm_client=slurm_client,
            object_store=object_store,
            retry_service=retry_service,
        )
        self.best_available_manager = best_available_manager

    @classmethod
    def from_env(cls) -> AnalysisOrchestrator:
        config = OrchestratorConfig.from_env()
        retry_service = _retry_service_from_env()
        return cls(
            config=config,
            repository=PsycopgOrchestratorRepository.from_env(),
            state_manager=StateManager.from_env(),
            best_available_manager=BestAvailableManager.from_env(),
            retry_service=retry_service,
        )

    def trigger_analysis(
        self,
        *,
        model_id: str,
        date_range: str | tuple[datetime, datetime],
        basin_id: str | None = None,
    ) -> PipelineResult:
        start_time, end_time = parse_date_range(date_range)
        if self.repository.has_active_analysis_run(model_id=model_id, start_time=start_time, end_time=end_time):
            raise AnalysisPipelineAlreadyActiveError(model_id, start_time, end_time)

        model = self.repository.load_model_context(model_id)
        if basin_id is not None and model.basin_id is not None and model.basin_id != basin_id:
            raise OrchestratorError(
                "MODEL_BASIN_MISMATCH",
                f"Model {model_id} belongs to basin {model.basin_id}, not {basin_id}.",
            )

        forcing = self.repository.find_forcing_context(
            source_id=ANALYSIS_SOURCE_ID,
            cycle_time=start_time,
            model_id=model_id,
        )
        self.repository.ensure_forecast_cycle(source_id=ANALYSIS_SOURCE_ID, cycle_time=start_time)
        init_state = self._latest_usable_state(model_id=model_id, before_time=start_time)
        context = self._build_analysis_context(start_time, end_time, model, forcing, init_state)
        manifest = self._build_run_manifest(context)
        self._write_run_manifest(context, manifest)
        self.repository.create_hydro_run(context, manifest)
        self.repository.update_hydro_run_status(context.run_id, "staged")
        return self.run_chain(context)

    def run_chain(self, context: AnalysisRunContext) -> PipelineResult:
        return super().run_chain(context)

    def _latest_usable_state(self, *, model_id: str, before_time: datetime) -> StateSnapshot | None:
        return chain_analysis.latest_usable_state(self, model_id=model_id, before_time=before_time)

    def _build_analysis_context(
        self,
        start_time: datetime,
        end_time: datetime,
        model: ModelContext,
        forcing: ForcingContext,
        init_state: StateSnapshot | None,
    ) -> AnalysisRunContext:
        return chain_analysis.build_analysis_context(
            self,
            start_time,
            end_time,
            model,
            forcing,
            init_state,
            directory_uri=_directory_uri,
            analysis_update_ic_step_minutes=_analysis_update_ic_step_minutes,
            analysis_forcing_causality=_analysis_forcing_causality,
        )

    def _build_run_manifest(self, context: AnalysisRunContext) -> dict[str, Any]:
        return chain_analysis.build_run_manifest(
            self,
            context,
            analysis_forcing_causality=_analysis_forcing_causality,
            analysis_update_ic_step_minutes=_analysis_update_ic_step_minutes,
        )

    def _before_stage_submit(self, stage: StageDefinition, context: ForecastRunContext | AnalysisRunContext) -> None:
        return chain_analysis.before_stage_submit(self, stage, context)

    def _after_stage_success(
        self,
        stage: StageDefinition,
        context: ForecastRunContext | AnalysisRunContext,
        _terminal: dict[str, Any],
    ) -> None:
        return chain_analysis.after_stage_success(self, stage, context, _terminal)

    def _after_stage_failure(
        self,
        stage: StageDefinition,
        context: ForecastRunContext | AnalysisRunContext,
        terminal: dict[str, Any],
    ) -> None:
        return chain_analysis.after_stage_failure(
            self,
            stage,
            context,
            terminal,
            analysis_error_code=_analysis_error_code,
        )

    def _after_stage_status_change(
        self,
        stage: StageDefinition,
        context: ForecastRunContext | AnalysisRunContext,
        _status_from: str | None,
        status_to: str,
        job: dict[str, Any],
    ) -> None:
        return chain_analysis.after_stage_status_change(self, stage, context, _status_from, status_to, job)

    def _pipeline_event_target(
        self,
        context: ForecastRunContext | AnalysisRunContext,
        _pipeline_job_id: str,
    ) -> tuple[str, str]:
        return chain_analysis.pipeline_event_target(self, context, _pipeline_job_id)

    def _record_best_available(self, context: ForecastRunContext | AnalysisRunContext) -> None:
        return chain_analysis.record_best_available(self, context)


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
    try:
        relative = path.relative_to(workspace_root)
    except ValueError as exc:
        raise SafeFilesystemError(f"Path must stay under workspace root: {path}") from exc
    parts = tuple(relative.parts)
    if not parts or any(part in {"", ".", ".."} for part in parts):
        raise SafeFilesystemError(f"Unsafe workspace path: {path}")
    return parts


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
