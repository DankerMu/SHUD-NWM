# ruff: noqa: E402,F401,F821,I001

from __future__ import annotations

import importlib
import os
import re
import time
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
    chain_config,
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

from services.orchestrator import chain_compat_static as _chain_compat_static

globals().update(_chain_compat_static.CHAIN_COMPAT_STATIC_EXPORTS)


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
        display_contract=_display_contract,
        assembly_quality_states=_assembly_quality_states,
        project_name_for_basin=_project_name_for_basin,
        model_package_manifest_uri=_model_package_manifest_uri,
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
COMPLETED_HYDRO_STATUSES = {"succeeded", "parsed", "published", "complete"}
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


scenario_for_source = chain_config.scenario_for_source
PipelineAlreadyActiveError = chain_config.PipelineAlreadyActiveError
AnalysisPipelineAlreadyActiveError = chain_config.AnalysisPipelineAlreadyActiveError
SlurmClientError = chain_config.SlurmClientError
SlurmAccountingEvidenceGap = chain_config.SlurmAccountingEvidenceGap


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


OrchestratorConfig = chain_config.OrchestratorConfig
_env_flag = chain_config._env_flag
for _chain_config_class in (
    PipelineAlreadyActiveError,
    AnalysisPipelineAlreadyActiveError,
    SlurmClientError,
    SlurmAccountingEvidenceGap,
    OrchestratorConfig,
):
    _chain_config_class.__module__ = __name__
del _chain_config_class


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
        if not _package_checksum_matches(state.model_package_checksum, model_package_checksum):
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
        or not _package_checksum_matches(state.model_package_checksum, model_package_checksum)
    ):
        return LINEAGE_PACKAGE_VERSION_MISMATCH

    return None


def _package_checksum_matches(expected: Any, actual: Any) -> bool:
    if expected in (None, "") or actual in (None, ""):
        return False
    return _package_checksum_value(expected) == _package_checksum_value(actual)


def _package_checksum_value(value: Any) -> str:
    text = str(value).strip().lower()
    if text.startswith("sha256:"):
        return text.split(":", 1)[1]
    return text


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


from services.orchestrator.chain_forecast_orchestrator_cycle import ForecastOrchestratorCycleMixin
from services.orchestrator.chain_forecast_orchestrator_runtime import ForecastOrchestratorRuntimeMixin


class ForecastOrchestrator(ForecastOrchestratorCycleMixin, ForecastOrchestratorRuntimeMixin):
    stages: tuple[StageDefinition, ...] = STAGES
    final_pipeline_status = "complete"




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



from services.orchestrator import chain_compat_runtime as _chain_compat_runtime

globals().update(_chain_compat_runtime.install_chain_runtime_compat())
