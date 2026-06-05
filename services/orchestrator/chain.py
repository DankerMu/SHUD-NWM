from __future__ import annotations

import json
import os
import re
import shlex
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Mapping, Protocol, Sequence

import httpx

from packages.common.best_available import BestAvailableManager
from packages.common.manifest_index import ManifestValidationError, serialize_manifest_index
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
)
from packages.common.state_manager import StateManager, StateSnapshot, assess_freshness
from services.artifacts import ArtifactLogError, published_log_relative_path, published_log_uri
from services.orchestrator.persistence import PipelineJob, PipelineStore
from services.orchestrator.production_contract import (
    PRODUCTION_CONTRACT_ID,
    PRODUCTION_CONTRACT_SCHEMA_VERSION,
    production_stage_for,
    production_status_for,
)
from services.orchestrator.reservation import (
    ReservationResult,
    bind_reservation,
    reserve_candidate,
    slurm_comment_for,
)
from services.orchestrator.retry import RetryConfig, RetryService, compute_backoff_seconds
from services.orchestrator.time_consistency import check_three_way_time_consistency
from services.slurm_gateway.config import SlurmGatewaySettings
from workers.canonical_converter.converter import (
    evaluate_canonical_readiness,
    expected_converter_version,
)
from workers.data_adapters.base import cycle_id_for, format_cycle_time, parse_cycle_time

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
ANALYSIS_SOURCE_ID = "ERA5"
ANALYSIS_SCENARIO_ID = "analysis_true_field"
# ERA5 reanalysis is published with a multi-day production delay; the analysis
# segment is therefore built from *delayed reanalysis*, never a real-time causal
# nowcast. We record a conservative default latency (5 days, ERA5T-style "initial
# release" lag) so the causality marker is honest about how far the reanalysis
# trails real time. Overridable via ERA5_REANALYSIS_LATENCY_MINUTES.
# TODO(M24): source the exact per-cycle latency from the ERA5 download metadata
# (publish_time - segment_end) once it is recorded; until then this is the floor.
DEFAULT_ERA5_REANALYSIS_LATENCY_MINUTES = 5 * 24 * 60
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


class OrchestratorError(RuntimeError):
    def __init__(self, error_code: str, message: str, details: dict[str, Any] | None = None) -> None:
        super().__init__(message)
        self.error_code = error_code
        self.message = message
        self.details = details or {}


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


@dataclass(frozen=True)
class StageDefinition:
    stage: str
    job_type: str
    template_name: str
    success_cycle_status: str
    failure_cycle_status: str
    is_array: bool = False


LEGACY_FORECAST_STAGES: tuple[StageDefinition, ...] = (
    StageDefinition("download_gfs", "download", "download_source_cycle.sbatch", "raw_complete", "failed_download"),
    StageDefinition("convert_canonical", "canonical", "convert_canonical.sbatch", "canonical_ready", "failed_convert"),
    StageDefinition("produce_forcing", "forcing", "produce_forcing.sbatch", "forcing_ready", "failed_forcing"),
    StageDefinition("run_shud_forecast", "forecast", "run_shud_forecast.sbatch", "forecast_running", "failed_run"),
    StageDefinition("parse_output", "parse", "parse_output.sbatch", "complete", "failed_parse"),
)

M3_STAGES: tuple[StageDefinition, ...] = (
    StageDefinition(
        "download",
        "download_source_cycle",
        "download_source_cycle.sbatch",
        "raw_complete",
        "failed_download",
        is_array=False,
    ),
    StageDefinition(
        "convert",
        "convert_canonical",
        "convert_canonical.sbatch",
        "canonical_ready",
        "failed_convert",
        is_array=False,
    ),
    StageDefinition(
        "forcing",
        "produce_forcing_array",
        "produce_forcing_array.sbatch",
        "forcing_ready",
        "failed_forcing",
        is_array=True,
    ),
    StageDefinition(
        "forecast",
        "run_shud_forecast_array",
        "run_shud_forecast_array.sbatch",
        "forecast_running",
        "failed_run",
        is_array=True,
    ),
    StageDefinition(
        "parse",
        "parse_output_array",
        "parse_output_array.sbatch",
        "complete",
        "failed_parse",
        is_array=True,
    ),
    StageDefinition(
        "frequency",
        "compute_frequency_array",
        "compute_frequency_array.sbatch",
        "complete",
        "failed_parse",
        is_array=True,
    ),
    StageDefinition("publish", "publish_tiles", "publish_tiles.sbatch", "complete", "failed_publish", is_array=False),
)

STAGES: tuple[StageDefinition, ...] = M3_STAGES

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

ANALYSIS_STAGES: tuple[StageDefinition, ...] = (
    StageDefinition(
        "era5_download",
        "analysis_download_source_cycle",
        "analysis_download_source_cycle.sbatch",
        "raw_complete",
        "failed_download",
    ),
    StageDefinition(
        "canonical_convert",
        "analysis_convert_canonical",
        "analysis_convert_canonical.sbatch",
        "canonical_ready",
        "failed_convert",
    ),
    StageDefinition(
        "forcing_produce",
        "analysis_produce_forcing",
        "analysis_produce_forcing.sbatch",
        "forcing_ready",
        "failed_forcing",
    ),
    StageDefinition("analysis_run", "run_shud_analysis", "run_shud_analysis.sbatch", "forecast_running", "failed_run"),
    StageDefinition(
        "parse_output",
        "parse_analysis_output",
        "parse_analysis_output.sbatch",
        "complete",
        "failed_parse",
    ),
    StageDefinition("state_save_qc", "save_state_snapshot", "save_state_snapshot.sbatch", "complete", "failed_publish"),
)


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
        )


@dataclass(frozen=True)
class ModelContext:
    model_id: str
    basin_id: str | None
    basin_version_id: str
    river_network_version_id: str
    segment_count: int
    model_package_uri: str
    output_segment_count: int | None = None


@dataclass(frozen=True)
class ForcingContext:
    forcing_version_id: str | None
    forcing_package_uri: str | None
    start_time: datetime | None = None
    end_time: datetime | None = None
    source_id: str | None = None
    max_lead_hours: int | None = None


@dataclass(frozen=True)
class InitialStateSelection:
    state_id: str | None
    state_uri: str | None
    valid_time: datetime | None
    checksum: str | None
    quality: str
    # Lineage (M24 §2 Lane 1) - optional, default None for backward compatibility.
    source_id: str | None = None
    cycle_id: str | None = None
    lead_hours: int | None = None
    model_package_version: str | None = None
    model_package_checksum: str | None = None
    rejection_code: str | None = None


# Bound on the warm-start fallback loop to avoid unbounded scans of stale snapshots.
_MAX_STATE_FALLBACK_CANDIDATES = 8

# Forcing causality modes for the analysis/nowcast segment (M24 §2 Lane 2). The
# analysis segment [T_N, T_{N+1}] must be built from data that does not leak the
# future relative to its own segment; ``causal`` is the real-time default,
# ``delayed_reanalysis`` is the only mode permitted to use ERA5-style reanalysis
# and must record the latency with which the reanalysis trails real time.
FORCING_CAUSALITY_CAUSAL = "causal"
FORCING_CAUSALITY_DELAYED_REANALYSIS = "delayed_reanalysis"


def _analysis_update_ic_step_minutes(start_time: datetime, end_time: datetime) -> int:
    """Restart cadence (minutes) that writes a SHUD restart state exactly at ``end_time``.

    The analysis segment is ``[T_N, T_{N+1}]`` with ``end_time == T_{N+1}``. SHUD writes
    a restart artifact every ``Update_IC_STEP`` minutes measured from the segment start,
    so the cadence must divide the segment so that a write lands on the final step. The
    simplest cadence guaranteed to land exactly on ``T_{N+1}`` (and never on an earlier
    modulo boundary, never on the default 1440-minute day) is the full segment length.

    Short 6h/12h cycles therefore get 360/720-minute cadences; a 24h cycle gets 1440.
    Returns the segment length in whole minutes. Raises on a non-positive or
    non-minute-aligned window so a wrong-time restart is a hard error, not silent.
    """

    duration_seconds = (_ensure_segment_utc(end_time) - _ensure_segment_utc(start_time)).total_seconds()
    if duration_seconds <= 0:
        raise OrchestratorError(
            "ANALYSIS_SEGMENT_INVALID_WINDOW",
            "Analysis segment end_time must be after start_time.",
            {"start_time": start_time.isoformat(), "end_time": end_time.isoformat()},
        )
    if duration_seconds % 60 != 0:
        raise OrchestratorError(
            "ANALYSIS_SEGMENT_NON_MINUTE_ALIGNED",
            "Analysis segment length must be a whole number of minutes for restart cadence.",
            {"duration_seconds": duration_seconds},
        )
    return int(duration_seconds // 60)


def _ensure_segment_utc(value: datetime) -> datetime:
    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)


def _era5_reanalysis_latency_minutes() -> int:
    """Recorded latency (minutes) by which ERA5 reanalysis trails its own segment.

    Overridable via ``ERA5_REANALYSIS_LATENCY_MINUTES``; falls back to a conservative
    default when unset or unparseable, so the analysis causality marker always carries
    a non-null latency. (TODO M24: replace with the exact per-cycle publish lag once
    the ERA5 download metadata records it.)
    """

    raw = os.getenv("ERA5_REANALYSIS_LATENCY_MINUTES", "").strip()
    if raw:
        try:
            value = int(raw)
            if value > 0:
                return value
        except ValueError:
            pass
    return DEFAULT_ERA5_REANALYSIS_LATENCY_MINUTES


def _analysis_forcing_causality(latency_minutes: int | None = None) -> dict[str, Any]:
    """Causality marker recorded on the analysis context/manifest.

    The marker must honestly reflect the *actual* forcing source of the segment.

    The analysis segment is currently built exclusively from whole-day ERA5
    reanalysis (``ANALYSIS_SOURCE_ID == "ERA5"``, stage chain
    ``era5_download -> analysis_*``). ERA5 is historical data published with a
    multi-day delay, so it is ``delayed_reanalysis`` with a recorded latency -- it is
    NOT a real-time ``causal`` nowcast and must never be marked as such. Callers that
    omit ``latency_minutes`` for the ERA5 analysis path therefore get the default
    recorded reanalysis latency, not a causal marker.

    ``causal`` is reserved for a *future* real-time implementation (cycle-N lead /
    nowcast with no future leak and, by construction, no reanalysis latency). When
    that path exists it will pass an explicit ``latency_minutes=0``-style causal
    marker; until then the ERA5 segment is always ``delayed_reanalysis``.
    """

    resolved_latency = _era5_reanalysis_latency_minutes() if latency_minutes is None else int(latency_minutes)
    return {
        "mode": FORCING_CAUSALITY_DELAYED_REANALYSIS,
        "latency_minutes": resolved_latency,
        "no_future_leak": True,
    }


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


@dataclass(frozen=True)
class ForecastRunContext:
    run_id: str
    source_id: str
    scenario_id: str
    cycle_id: str
    cycle_time: datetime
    model_id: str
    basin_id: str | None
    basin_version_id: str
    river_network_version_id: str
    segment_count: int
    model_package_uri: str
    forcing_version_id: str | None
    forcing_package_uri: str | None
    start_time: datetime
    end_time: datetime
    forecast_horizon_hours: int
    run_manifest_uri: str
    output_uri: str
    log_uri: str
    init_state_id: str | None = None
    init_state_uri: str | None = None
    init_state_valid_time: datetime | None = None
    init_state_checksum: str | None = None
    init_state_quality: str = "cold_start_no_state"
    output_segment_count: int | None = None


@dataclass(frozen=True)
class AnalysisRunContext:
    run_id: str
    source_id: str
    cycle_id: str
    cycle_time: datetime
    model_id: str
    basin_id: str | None
    basin_version_id: str
    river_network_version_id: str
    segment_count: int
    model_package_uri: str
    forcing_version_id: str | None
    forcing_package_uri: str | None
    start_time: datetime
    end_time: datetime
    run_manifest_uri: str
    output_uri: str
    log_uri: str
    init_state_id: str | None = None
    init_state_uri: str | None = None
    init_state_valid_time: datetime | None = None
    output_segment_count: int | None = None
    # M24 §2 Lane 2: restart cadence landing on T_{N+1} and forcing causality marker.
    update_ic_step_minutes: int | None = None
    forcing_causality: Mapping[str, Any] | None = None


@dataclass(frozen=True)
class StageRunResult:
    stage: str
    job_type: str
    pipeline_job_id: str
    slurm_job_id: str
    status: str
    exit_code: int | None = None
    error_code: str | None = None
    error_message: str | None = None
    log_uri: str | None = None
    accounting: Mapping[str, Any] = field(default_factory=dict)
    task_results: tuple[Mapping[str, Any], ...] = ()


@dataclass(frozen=True)
class PipelineResult:
    run_id: str
    cycle_id: str
    status: str
    stages: tuple[StageRunResult, ...]
    candidate_outcomes: tuple[dict[str, Any], ...] = ()


@dataclass(frozen=True)
class ArrayTaskResult:
    task_id: int
    slurm_job_id: str
    status: str
    exit_code: int | None = None
    error_code: str | None = None
    error_message: str | None = None
    log_uri: str | None = None
    accounting: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class ArrayAggregation:
    total: int
    succeeded: int
    failed: int
    cancelled: int
    task_results: tuple[ArrayTaskResult, ...]

    @property
    def succeeded_task_ids(self) -> tuple[int, ...]:
        return tuple(result.task_id for result in self.task_results if result.status == "succeeded")

    @property
    def failed_task_ids(self) -> tuple[int, ...]:
        return tuple(result.task_id for result in self.task_results if result.status == "failed")

    @property
    def cancelled_task_ids(self) -> tuple[int, ...]:
        return tuple(result.task_id for result in self.task_results if result.status == "cancelled")

    @property
    def status(self) -> str:
        if self.total == 0 or self.succeeded == 0:
            return "failed"
        if self.succeeded == self.total:
            return "succeeded"
        return "partially_failed"


@dataclass(frozen=True)
class DisplayLogPublication:
    candidate_uri: str
    advertised_uri: str | None
    should_persist_logs: bool

    @property
    def requires_publish_before_advertise(self) -> bool:
        return self.should_persist_logs


@dataclass(frozen=True)
class DisplayLogPublicationAttempt:
    advertised_uri: str | None
    error: OrchestratorError | None = None


@dataclass(frozen=True)
class TerminalJobObservation:
    job: dict[str, Any]
    publication_attempt: DisplayLogPublicationAttempt | None = None


@dataclass
class CycleOrchestrationContext:
    source_id: str
    cycle_time: datetime
    cycle_id: str
    run_id: str
    all_basins: list[dict[str, Any]]
    active_basins: list[dict[str, Any]]
    restart_stage: str | None = None
    had_partial: bool = False
    last_partial_status: str | None = None
    task_outcomes: dict[int, dict[str, Any]] = field(default_factory=dict)


@dataclass(frozen=True)
class ModelRunAssembly:
    """Reusable per-model contract shared by scheduler, Slurm arrays, and workers."""

    identity: dict[str, Any]
    forcing: dict[str, Any]
    runtime: dict[str, Any]
    outputs: dict[str, Any]
    frequency: dict[str, Any]
    display: dict[str, Any]
    quality_states: dict[str, Any]
    residual_blockers: tuple[dict[str, Any], ...]

    def to_manifest_entry(self) -> dict[str, Any]:
        return {
            "identity": dict(self.identity),
            "forcing_metadata": dict(self.forcing),
            "shud_runtime": dict(self.runtime),
            "outputs": dict(self.outputs),
            "frequency_contract": dict(self.frequency),
            "display_contract": dict(self.display),
            "quality_states": dict(self.quality_states),
            "residual_blockers": [dict(item) for item in self.residual_blockers],
        }


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
        stage_results: list[StageRunResult] = []
        start_stage_index = _restart_stage_index(context.restart_stage, self.stages)
        existing_jobs = self._query_pipeline_jobs_for_cycle_context(context)
        for stage_index, stage in enumerate(self.stages):
            if stage_index < start_stage_index:
                continue
            existing_jobs = self._query_pipeline_jobs_for_cycle_context(context)
            had_partial_before_stage = context.had_partial
            last_partial_before_stage = context.last_partial_status
            retry_attempts = 0
            retry_pipeline_job_id: str | None = None
            while True:
                existing_job = self._find_existing_stage_job(existing_jobs, stage)
                if (
                    existing_job is not None
                    and retry_pipeline_job_id is None
                    and not self._job_needs_submission(existing_job)
                ):
                    result, aggregation = self._resume_cycle_stage(stage, context, existing_job)
                else:
                    pipeline_job_id = retry_pipeline_job_id
                    if pipeline_job_id is None and existing_job is not None:
                        pipeline_job_id = str(existing_job["job_id"])
                    result, aggregation = self._submit_and_wait_cycle_stage(
                        stage,
                        context,
                        pipeline_job_id=pipeline_job_id,
                    )
                    retry_pipeline_job_id = None
                    existing_jobs = self._query_pipeline_jobs_for_cycle_context(context)

                if stage_results and len(stage_results) > stage_index:
                    stage_results[stage_index] = result
                elif stage_results and stage_results[-1].stage == result.stage:
                    stage_results[-1] = result
                else:
                    stage_results.append(result)

                if result.status in {"failed", "submission_failed", "permanently_failed"}:
                    retry_attempts += 1
                    retry_pipeline_job_id = self._schedule_cycle_stage_retry(result, retry_attempts)
                    if retry_pipeline_job_id is not None:
                        existing_jobs = [job for job in existing_jobs if not self._job_matches_stage(job, stage)]
                        continue
                    if stage.is_array and aggregation is not None:
                        _record_array_task_outcomes(context, stage=stage.stage, aggregation=aggregation)
                    return PipelineResult(
                        context.run_id,
                        context.cycle_id,
                        "failed",
                        tuple(stage_results),
                        _candidate_outcomes(context, final_status="failed"),
                    )

                if result.status == "cancelled":
                    return PipelineResult(
                        context.run_id,
                        context.cycle_id,
                        "failed",
                        tuple(stage_results),
                        _candidate_outcomes(context, final_status="failed"),
                    )

                if stage.is_array and aggregation is not None and aggregation.status == "partially_failed":
                    retried = self._retry_partial_array_stage(
                        stage,
                        context,
                        result,
                        aggregation,
                        had_partial_before_stage,
                        last_partial_before_stage,
                    )
                    if retried is not None:
                        result, aggregation = retried
                        stage_results[-1] = result
                break

            if stage.is_array and aggregation is not None:
                self._apply_array_progress(stage, context, aggregation)

        final_status = context.last_partial_status if context.had_partial else self.final_pipeline_status
        return PipelineResult(
            context.run_id,
            context.cycle_id,
            final_status or self.final_pipeline_status,
            tuple(stage_results),
            _candidate_outcomes(context, final_status=final_status or self.final_pipeline_status),
        )

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
        service = self.retry_service
        if service is None:
            return None

        store = getattr(service, "store", None)
        session = getattr(store, "session", None)
        expire_all = getattr(session, "expire_all", None)
        if callable(expire_all):
            expire_all()
        get_job = getattr(store, "get_job", None)
        if callable(get_job):
            job = get_job(result.pipeline_job_id)
            if job is not None:
                return job
            if isinstance(service, RetryService):
                return None

        get_pipeline_job = getattr(self.repository, "get_pipeline_job", None)
        if callable(get_pipeline_job):
            record = get_pipeline_job(result.pipeline_job_id)
        else:
            repository_jobs = getattr(self.repository, "jobs", {})
            record = repository_jobs.get(result.pipeline_job_id) if isinstance(repository_jobs, Mapping) else None
        if record is None:
            return None

        job = PipelineJob(
            job_id=str(record.get("job_id") or result.pipeline_job_id),
            run_id=record.get("run_id"),
            cycle_id=record.get("cycle_id"),
            job_type=str(record.get("job_type") or result.job_type),
            slurm_job_id=record.get("slurm_job_id"),
            model_id=record.get("model_id"),
            status=str(record.get("status") or result.status),
            stage=record.get("stage") or result.stage,
        )
        job.retry_count = int(record.get("retry_count") or 0)
        job.error_code = record.get("error_code") or result.error_code
        job.error_message = record.get("error_message") or result.error_message
        return job

    def _retry_partial_array_stage(
        self,
        stage: StageDefinition,
        context: CycleOrchestrationContext,
        result: StageRunResult,
        aggregation: ArrayAggregation,
        had_partial_before_stage: bool,
        last_partial_before_stage: str | None,
    ) -> tuple[StageRunResult, ArrayAggregation] | None:
        if self.retry_service is None:
            return None

        original_basins = [dict(basin) for basin in context.active_basins]
        task_results = {
            task.task_id: ArrayTaskResult(
                task_id=task.task_id,
                slurm_job_id=task.slurm_job_id,
                status=task.status,
                exit_code=task.exit_code,
                error_code=task.error_code,
                error_message=task.error_message,
                log_uri=task.log_uri,
                accounting=dict(task.accounting),
            )
            for task in aggregation.task_results
        }
        pending_task_ids = [task.task_id for task in aggregation.task_results if task.status != "succeeded"]
        latest_result = result
        retry_attempts = 0

        try:
            while pending_task_ids:
                retry_attempts += 1
                retry_pipeline_job_id = self._schedule_cycle_stage_retry(latest_result, retry_attempts)
                if not retry_pipeline_job_id:
                    break

                retry_basins = self._reindexed_basins_for_task_ids(original_basins, pending_task_ids)
                retry_task_to_original = {index: task_id for index, task_id in enumerate(pending_task_ids)}
                context.active_basins = retry_basins
                latest_result, retry_aggregation = self._submit_and_wait_cycle_stage(
                    stage,
                    context,
                    pipeline_job_id=retry_pipeline_job_id,
                )

                if retry_aggregation is None:
                    retry_status = "succeeded" if latest_result.status == "succeeded" else "failed"
                    for task_id in pending_task_ids:
                        task_results[task_id] = ArrayTaskResult(
                            task_id=task_id,
                            slurm_job_id=latest_result.slurm_job_id,
                            status=retry_status,
                            exit_code=latest_result.exit_code,
                            error_code=latest_result.error_code,
                            error_message=latest_result.error_message,
                            log_uri=latest_result.log_uri,
                            accounting=dict(latest_result.accounting),
                        )
                    if retry_status == "succeeded":
                        pending_task_ids = []
                    continue

                next_pending_task_ids: list[int] = []
                updated_task_ids: set[int] = set()
                for retry_task in retry_aggregation.task_results:
                    original_task_id = retry_task_to_original.get(retry_task.task_id)
                    if original_task_id is None:
                        continue
                    updated_task_ids.add(original_task_id)
                    task_results[original_task_id] = ArrayTaskResult(
                        task_id=original_task_id,
                        slurm_job_id=retry_task.slurm_job_id,
                        status=retry_task.status,
                        exit_code=retry_task.exit_code,
                        error_code=retry_task.error_code,
                        error_message=retry_task.error_message,
                        log_uri=retry_task.log_uri,
                        accounting=dict(retry_task.accounting),
                    )
                    if retry_task.status != "succeeded":
                        next_pending_task_ids.append(original_task_id)

                missing_task_ids = [task_id for task_id in pending_task_ids if task_id not in updated_task_ids]
                next_pending_task_ids.extend(missing_task_ids)
                pending_task_ids = next_pending_task_ids
        finally:
            context.active_basins = original_basins

        final_aggregation = _aggregation_from_task_results(
            tuple(task_results[task_id] for task_id in sorted(task_results))
        )
        if final_aggregation.status == "succeeded":
            context.had_partial = had_partial_before_stage
            context.last_partial_status = last_partial_before_stage
            context.task_outcomes = {
                task_id: outcome for task_id, outcome in context.task_outcomes.items() if task_id not in task_results
            }
        final_result = StageRunResult(
            stage=stage.stage,
            job_type=stage.job_type,
            pipeline_job_id=latest_result.pipeline_job_id,
            slurm_job_id=latest_result.slurm_job_id,
            status=final_aggregation.status,
            exit_code=latest_result.exit_code,
            error_code=latest_result.error_code,
            error_message=latest_result.error_message,
            log_uri=latest_result.log_uri,
            accounting=dict(latest_result.accounting),
            task_results=_stage_task_result_evidence(final_aggregation, context=context),
        )
        if final_result.status != latest_result.status or final_aggregation.status == "succeeded":
            self._after_cycle_stage_terminal(
                stage,
                context,
                final_result.status,
                {
                    "status": final_result.status,
                    "exit_code": final_result.exit_code,
                    "error_code": final_result.error_code,
                    "error_message": final_result.error_message,
                },
                final_aggregation,
            )
        return final_result, final_aggregation

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

    def _submit_and_wait_cycle_stage(
        self,
        stage: StageDefinition,
        context: CycleOrchestrationContext,
        *,
        pipeline_job_id: str | None = None,
    ) -> tuple[StageRunResult, ArrayAggregation | None]:
        pipeline_job_id = pipeline_job_id or _pipeline_job_id(context.run_id, stage.stage)
        if stage.is_array and not context.active_basins:
            self.repository.update_forecast_cycle_status(
                source_id=context.source_id,
                cycle_time=context.cycle_time,
                status=stage.failure_cycle_status,
                error_code="NO_ACTIVE_BASINS",
                error_message=f"No basins available for {stage.stage}.",
            )
            return (
                StageRunResult(
                    stage=stage.stage,
                    job_type=stage.job_type,
                    pipeline_job_id=pipeline_job_id,
                    slurm_job_id="",
                    status="failed",
                    error_code="NO_ACTIVE_BASINS",
                    error_message=f"No basins available for {stage.stage}.",
                    task_results=(),
                ),
                None,
            )

        self._before_cycle_stage_submit(stage, context)

        # M24 §3A phase 1: durable reservation BEFORE sbatch. Idempotent across
        # overlapping passes and the submit-crash window. Best-effort against
        # repositories that predate the reservation methods.
        idempotency_key = _cycle_stage_idempotency_key(context, stage)
        reservation = self._reserve_cycle_stage(stage, context, pipeline_job_id, idempotency_key)

        # M24 §3A reserve gate: when a concurrent pass already holds an active
        # reservation for this candidate+stage, skip sbatch entirely (no double
        # submission). Only a pass that truly won the reservation (created) — or
        # a legacy repo without the reservation surface — proceeds to sbatch.
        if self._reservation_already_inflight(reservation):
            return self._skip_duplicate_submission(stage, context, pipeline_job_id, reservation), None

        submitted: dict[str, Any]
        manifest_index_path: Path | None = None
        stage_manifest = self._build_cycle_stage_manifest(stage, context)
        try:
            self._prepare_forecast_runtime_manifests(stage, context)
            if stage.stage == "forecast":
                stage_manifest = self._build_cycle_stage_manifest(stage, context)
            if stage.is_array:
                tasks = self._reindexed_manifest_entries(context.active_basins)
                manifest_index_path = self._write_cycle_manifest_index(context, stage, tasks)
                stage_manifest["manifest_index_path"] = str(manifest_index_path)
                # Array path must carry the same idempotency --comment as the
                # single-job path so crash-recovery can reconcile array masters.
                stage_manifest["comment"] = slurm_comment_for(idempotency_key)
                submitted = self._submit_array_stage(stage, context, tasks, stage_manifest)
            else:
                submitted = _coerce_mapping(
                    self.slurm_client.submit_job(
                        {
                            "run_id": context.run_id,
                            "model_id": _cycle_payload_model_id(context),
                            "job_type": stage.job_type,
                            "manifest": self._slurm_submission_manifest(stage_manifest),
                            "comment": slurm_comment_for(idempotency_key),
                        }
                    )
                )
        except Exception as error:
            result = self._record_submission_failure(stage, context, error, pipeline_job_id=pipeline_job_id)
            if stage.stage == "forecast":
                self._mark_staged_hydro_runs_failed(
                    [str(basin["run_id"]) for basin in context.active_basins if basin.get("run_id")],
                    error_code=result.error_code or "SBATCH_SUBMISSION_FAILED",
                    error_message=result.error_message or str(error),
                )
            return result, None

        slurm_job_id = str(submitted["job_id"])
        # M24 §3A phase 2: atomically bind slurm_job_id onto the reservation
        # (no-op if a concurrent pass already bound it). The full upsert below
        # remains authoritative for status/metadata.
        self._bind_cycle_stage_reservation(
            idempotency_key,
            slurm_job_id=slurm_job_id,
            array_task_id=_coerce_array_task_id(submitted.get("array_task_id")),
        )
        log_publication = self._display_log_publication_for_stage(
            source_id=context.source_id,
            cycle_time=context.cycle_time,
            run_id=context.run_id,
            job_id=pipeline_job_id,
            stage=stage.stage,
        )
        submitted_status = _status_from_gateway_job(submitted)
        submitted_log_uri = log_publication.advertised_uri
        submitted_publish_attempt: DisplayLogPublicationAttempt | None = None
        if submitted_status in TERMINAL_JOB_STATUSES:
            submitted_publish_attempt = self._try_publish_log_for_advertise(slurm_job_id, log_publication)
            submitted_log_uri = submitted_publish_attempt.advertised_uri
            if submitted_log_uri:
                submitted["log_uri"] = submitted_log_uri
        submitted_manifest = submitted.get("manifest") if isinstance(submitted.get("manifest"), Mapping) else {}
        submitted_manifest_index_path = (
            str(submitted_manifest.get("manifest_index_path") or submitted.get("manifest_index_path") or "")
            if isinstance(submitted_manifest, Mapping)
            else str(submitted.get("manifest_index_path") or "")
        )
        actual_manifest_index_path = submitted_manifest_index_path or (
            str(manifest_index_path) if manifest_index_path else ""
        )
        submitted_array_task_id = _coerce_array_task_id(submitted.get("array_task_id"))
        self.repository.upsert_pipeline_job(
            {
                "job_id": pipeline_job_id,
                "run_id": context.run_id,
                "cycle_id": context.cycle_id,
                "job_type": stage.job_type,
                "slurm_job_id": slurm_job_id,
                "array_task_id": submitted_array_task_id,
                "model_id": _cycle_pipeline_job_model_id(context),
                "status": submitted_status,
                "stage": stage.stage,
                "idempotency_key": idempotency_key,
                "submitted_at": _parse_gateway_time(submitted.get("submitted_at")) or _utcnow(),
                "started_at": _parse_gateway_time(submitted.get("started_at")),
                "finished_at": _parse_gateway_time(submitted.get("finished_at")),
                "exit_code": submitted.get("exit_code"),
                "error_code": submitted.get("error_code"),
                "error_message": submitted.get("error_message"),
                "log_uri": submitted_log_uri,
            }
        )
        self.repository.insert_pipeline_event(
            entity_type="pipeline_job",
            entity_id=pipeline_job_id,
            event_type="submission",
            status_from=None,
            status_to=submitted_status,
            message=f"{stage.stage} submitted as Slurm job {slurm_job_id}",
            details=_safe_pipeline_event_details(
                {
                    "stage": stage.stage,
                    "job_type": stage.job_type,
                    "slurm_job_id": slurm_job_id,
                    "slurm": {
                        "job_id": slurm_job_id,
                        "state": submitted_status,
                        "array_task_id": submitted_array_task_id,
                        "exit_code": submitted.get("exit_code"),
                        "log_uri": submitted_log_uri,
                    },
                    "manifest_index_path": actual_manifest_index_path or None,
                }
            ),
        )
        if submitted_status in TERMINAL_JOB_STATUSES:
            terminal_observation = TerminalJobObservation(
                job=submitted,
                publication_attempt=submitted_publish_attempt,
            )
        else:
            terminal_observation = self._poll_cycle_stage_until_terminal(
                stage=stage,
                context=context,
                pipeline_job_id=pipeline_job_id,
                initial_job=submitted,
                initial_status=submitted_status,
                log_publication=log_publication,
            )
        terminal = terminal_observation.job
        publication_attempt = terminal_observation.publication_attempt
        log_uri = str(terminal.get("log_uri") or "")
        if not log_uri:
            if publication_attempt is None:
                publication_attempt = self._try_publish_log_for_advertise(slurm_job_id, log_publication)
            log_uri = str(publication_attempt.advertised_uri or "")

        poll_timed_out = isinstance(terminal, dict) and terminal.get("error_code") == "SLURM_JOB_TIMEOUT"
        aggregation = (
            self._aggregate_array_stage(stage, context, slurm_job_id, terminal, pipeline_job_id)
            if stage.is_array and not poll_timed_out
            else None
        )
        result_status = aggregation.status if aggregation is not None else _status_from_gateway_job(terminal)
        result_error_code = (
            _aggregation_error_code(aggregation) if aggregation is not None else terminal.get("error_code")
        )
        result_error_message = (
            _aggregation_error_message(aggregation) if aggregation is not None else terminal.get("error_message")
        )
        if aggregation is not None:
            self._record_cycle_stage_status_override(
                stage,
                context,
                pipeline_job_id,
                terminal,
                aggregation,
                log_uri or None,
            )
        else:
            self._record_cycle_stage_accounting_event(stage, context, pipeline_job_id, terminal, log_uri=log_uri)

        self._after_cycle_stage_terminal(stage, context, result_status, terminal, aggregation)
        self._raise_publish_error_after_durable_update(publication_attempt)
        return (
            StageRunResult(
                stage=stage.stage,
                job_type=stage.job_type,
                pipeline_job_id=pipeline_job_id,
                slurm_job_id=slurm_job_id,
                status=result_status,
                exit_code=terminal.get("exit_code"),
                error_code=result_error_code,
                error_message=result_error_message,
                log_uri=log_uri,
                accounting=_slurm_accounting_from_payload(terminal),
                task_results=_stage_task_result_evidence(aggregation, context=context),
            ),
            aggregation,
        )

    def _resume_cycle_stage(
        self,
        stage: StageDefinition,
        context: CycleOrchestrationContext,
        job: dict[str, Any],
    ) -> tuple[StageRunResult, ArrayAggregation | None]:
        status = str(job.get("status"))
        terminal = dict(job)
        deferred_publish_attempt: DisplayLogPublicationAttempt | None = None
        if status not in TERMINAL_JOB_STATUSES and job.get("slurm_job_id"):
            terminal_observation = self._poll_cycle_stage_until_terminal(
                stage=stage,
                context=context,
                pipeline_job_id=str(job["job_id"]),
                initial_job={"job_id": job["slurm_job_id"], "status": status},
                initial_status=status,
                log_publication=self._display_log_publication_for_pipeline_job(job),
            )
            terminal = terminal_observation.job
            deferred_publish_attempt = terminal_observation.publication_attempt
            status = _status_from_gateway_job(terminal)

        aggregation = None
        if (
            stage.is_array
            and job.get("slurm_job_id")
            and status not in {"failed", "cancelled", "submission_failed", "permanently_failed"}
        ):
            aggregation = self._aggregate_array_stage(
                stage,
                context,
                str(job["slurm_job_id"]),
                terminal,
                str(job["job_id"]),
            )
            status = aggregation.status
            if str(job.get("status")) not in TERMINAL_JOB_STATUSES or status != str(job.get("status")):
                publication = self._display_log_publication_for_pipeline_job(job)
                publication_attempt: DisplayLogPublicationAttempt | None = None
                if publication is not None:
                    publication_attempt = self._try_publish_log_for_advertise(str(job["slurm_job_id"]), publication)
                    log_uri = publication_attempt.advertised_uri
                elif not _published_artifact_root_configured():
                    legacy_log_uri = self.object_store.uri_for_key(f"runs/{context.run_id}/logs/{stage.stage}.log")
                    publication_attempt = self._try_publish_log_for_advertise(
                        str(job["slurm_job_id"]),
                        DisplayLogPublication(
                            candidate_uri=legacy_log_uri,
                            advertised_uri=legacy_log_uri,
                            should_persist_logs=True,
                        ),
                    )
                    log_uri = publication_attempt.advertised_uri
                else:
                    raise OrchestratorError(
                        "PUBLISHED_LOG_URI_UNAVAILABLE",
                        "Cannot compute a published log URI for the recovered pipeline job.",
                        {"job_id": str(job["job_id"]), "stage": stage.stage},
                    )
                self._record_cycle_stage_status_override(
                    stage,
                    context,
                    str(job["job_id"]),
                    terminal,
                    aggregation,
                    log_uri,
                )
                if publication_attempt is not None:
                    deferred_publish_attempt = publication_attempt
            if status == "partially_failed":
                context.had_partial = True
                context.last_partial_status = self._partial_cycle_status(stage)

        result_log_uri = str(terminal.get("log_uri") or job.get("log_uri") or "") or None
        get_pipeline_job = getattr(self.repository, "get_pipeline_job", None)
        updated_job = get_pipeline_job(str(job["job_id"])) if callable(get_pipeline_job) else None
        if updated_job is not None:
            result_log_uri = str(updated_job.get("log_uri") or "") or None

        self._after_cycle_stage_terminal(stage, context, status, terminal, aggregation)
        self._raise_publish_error_after_durable_update(deferred_publish_attempt)
        return (
            StageRunResult(
                stage=stage.stage,
                job_type=stage.job_type,
                pipeline_job_id=str(job["job_id"]),
                slurm_job_id=str(job.get("slurm_job_id") or ""),
                status=status,
                exit_code=job.get("exit_code"),
                error_code=job.get("error_code"),
                error_message=job.get("error_message"),
                log_uri=result_log_uri,
                accounting=_slurm_accounting_from_payload(terminal),
                task_results=_stage_task_result_evidence(aggregation, context=context),
            ),
            aggregation,
        )

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
        job = dict(initial_job)
        current_status = initial_status
        deadline = time.monotonic() + self.config.job_timeout_seconds
        while _status_from_gateway_job(job) not in TERMINAL_JOB_STATUSES:
            if time.monotonic() >= deadline:
                return self._record_cycle_stage_poll_timeout(
                    stage=stage,
                    context=context,
                    pipeline_job_id=pipeline_job_id,
                    job=job,
                    current_status=current_status,
                    log_publication=log_publication,
                )
            time.sleep(self.config.poll_interval_seconds)
            job = _coerce_mapping(self.slurm_client.get_job_status(str(job["job_id"])))
            new_status = _status_from_gateway_job(job)
            if new_status == current_status:
                continue
            if stage.is_array and new_status in TERMINAL_JOB_STATUSES:
                current_status = new_status
                continue
            log_uri = log_publication.advertised_uri if log_publication is not None else None
            publication_attempt: DisplayLogPublicationAttempt | None = None
            if new_status in TERMINAL_JOB_STATUSES and log_publication is not None:
                publication_attempt = self._try_publish_log_for_advertise(str(job["job_id"]), log_publication)
                log_uri = publication_attempt.advertised_uri
            previous_status, record = self.repository.update_pipeline_job_status(
                pipeline_job_id,
                new_status,
                started_at=_parse_gateway_time(job.get("started_at")),
                finished_at=_parse_gateway_time(job.get("finished_at")),
                exit_code=job.get("exit_code"),
                error_code=job.get("error_code"),
                error_message=job.get("error_message"),
                log_uri=log_uri if new_status in TERMINAL_JOB_STATUSES else None,
            )
            if log_uri and new_status in TERMINAL_JOB_STATUSES:
                job["log_uri"] = log_uri
            persisted_status = str(record.get("status") or new_status)
            if persisted_status != new_status:
                job["status"] = persisted_status
                current_status = persisted_status
                if persisted_status in TERMINAL_JOB_STATUSES:
                    return TerminalJobObservation(job=job, publication_attempt=publication_attempt)
                continue
            self.repository.insert_pipeline_event(
                entity_type="pipeline_job",
                entity_id=pipeline_job_id,
                event_type="status_change",
                status_from=previous_status or current_status,
                status_to=new_status,
                message=_stage_status_message(stage.stage, new_status, job),
                details=_safe_pipeline_event_details(
                    {
                        "stage": stage.stage,
                        "job_type": stage.job_type,
                        "slurm_job_id": job["job_id"],
                        "exit_code": job.get("exit_code"),
                        "error_code": job.get("error_code"),
                        "slurm": {
                            "job_id": job["job_id"],
                            "state": job.get("state") or job.get("status"),
                            "exit_code": job.get("exit_code"),
                            "log_uri": log_uri if new_status in TERMINAL_JOB_STATUSES else None,
                            "accounting": _slurm_accounting_from_payload(job),
                            "resource_metrics": _resource_metrics_from_payload(job),
                        },
                    }
                ),
            )
            current_status = new_status
            if publication_attempt is not None and publication_attempt.error is not None:
                return TerminalJobObservation(job=job, publication_attempt=publication_attempt)
        return TerminalJobObservation(job=job)

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
        message = f"Stage {stage.stage} did not reach a terminal status before timeout."
        terminal = dict(job)
        terminal.update(
            {
                "status": "failed",
                "finished_at": _format_time(_utcnow()),
                "error_code": "SLURM_JOB_TIMEOUT",
                "error_message": message,
            }
        )
        publication_attempt = (
            self._try_publish_log_for_advertise(str(job["job_id"]), log_publication)
            if log_publication is not None
            else None
        )
        log_uri = publication_attempt.advertised_uri if publication_attempt is not None else None
        previous_status, record = self.repository.update_pipeline_job_status(
            pipeline_job_id,
            "failed",
            finished_at=_utcnow(),
            exit_code=terminal.get("exit_code"),
            error_code="SLURM_JOB_TIMEOUT",
            error_message=message,
            log_uri=log_uri,
        )
        terminal.update(record)
        self.repository.insert_pipeline_event(
            entity_type="pipeline_job",
            entity_id=pipeline_job_id,
            event_type="timeout",
            status_from=previous_status or current_status,
            status_to="failed",
            message=message,
            details=_safe_pipeline_event_details(
                {
                    "stage": stage.stage,
                    "job_type": stage.job_type,
                    "cycle_id": context.cycle_id,
                    "slurm_job_id": job["job_id"],
                    "timeout_seconds": self.config.job_timeout_seconds,
                    "error_code": "SLURM_JOB_TIMEOUT",
                }
            ),
        )
        self._record_cycle_stage_accounting_gap(
            stage,
            context,
            pipeline_job_id,
            slurm_job_id=str(job["job_id"]),
            message="Slurm accounting did not reach a terminal state before timeout.",
            details={"timeout_seconds": self.config.job_timeout_seconds},
        )
        self.repository.update_forecast_cycle_status(
            source_id=context.source_id,
            cycle_time=context.cycle_time,
            status=stage.failure_cycle_status,
            error_code="SLURM_JOB_TIMEOUT",
            error_message=message,
        )
        return TerminalJobObservation(job=terminal, publication_attempt=publication_attempt)

    def _submit_array_stage(
        self,
        stage: StageDefinition,
        context: CycleOrchestrationContext,
        tasks: list[dict[str, Any]],
        manifest: dict[str, Any],
    ) -> dict[str, Any]:
        submit_job_array = getattr(self.slurm_client, "submit_job_array", None)
        if callable(submit_job_array):
            # Carry the idempotency --comment into the array submission manifest
            # so the array master sbatch is stamped; real_backend.submit_job_array
            # reads ``manifest["comment"]`` and threads it to sbatch --comment,
            # making array-stage crash recovery reconcile-by-comment work.
            submission_manifest = self._slurm_submission_manifest(manifest)
            if manifest.get("comment"):
                submission_manifest["comment"] = manifest["comment"]
            return _coerce_mapping(
                submit_job_array(
                    stage.job_type,
                    cycle_id=context.cycle_id,
                    stage_name=stage.stage,
                    tasks=tasks,
                    manifest=submission_manifest,
                )
            )
        raise SlurmClientError(
            "SLURM_ARRAY_SUBMIT_UNSUPPORTED",
            f"Slurm client does not support array submission for {stage.stage}.",
            {"stage": stage.stage, "job_type": stage.job_type, "cycle_id": context.cycle_id},
        )

    def _slurm_submission_manifest(self, manifest: Mapping[str, Any]) -> dict[str, Any]:
        submission = dict(manifest)
        if self.config.slurm_job_type_templates:
            submission["slurm_job_type_templates"] = dict(self.config.slurm_job_type_templates)
        if self.config.slurm_env:
            submission["slurm_env"] = dict(self.config.slurm_env)
        return submission

    def _aggregate_array_stage(
        self,
        stage: StageDefinition,
        context: CycleOrchestrationContext,
        slurm_job_id: str,
        terminal: dict[str, Any],
        pipeline_job_id: str,
    ) -> ArrayAggregation:
        provider = getattr(self.slurm_client, "get_array_task_results", None)
        if callable(provider):
            try:
                raw_results = provider(slurm_job_id)
            except (KeyError, LookupError):
                raw_results = None
            except (TypeError, ValueError, OrchestratorError) as error:
                self._record_cycle_stage_accounting_gap(
                    stage,
                    context,
                    pipeline_job_id,
                    slurm_job_id=slurm_job_id,
                    message="Slurm array accounting was unavailable or malformed.",
                    details={"error": str(error), "error_code": getattr(error, "error_code", None)},
                )
                raw_results = None
            if raw_results is not None:
                try:
                    aggregation = _coerce_array_aggregation(
                        raw_results,
                        slurm_job_id,
                        context=context,
                        object_store=self.object_store,
                    )
                    return self._require_complete_array_accounting(
                        aggregation,
                        stage=stage,
                        context=context,
                        slurm_job_id=slurm_job_id,
                    )
                except (TypeError, ValueError, OrchestratorError) as error:
                    self._record_cycle_stage_accounting_gap(
                        stage,
                        context,
                        pipeline_job_id,
                        slurm_job_id=slurm_job_id,
                        message="Slurm array accounting was unavailable or malformed.",
                        details={"error": str(error), "error_code": getattr(error, "error_code", None)},
                    )
                raw_results = None

        stdout_provider = getattr(self.slurm_client, "get_array_sacct_output", None)
        if callable(stdout_provider):
            try:
                aggregation = parse_sacct_array_results(
                    str(stdout_provider(slurm_job_id)),
                    slurm_job_id,
                    context=context,
                    object_store=self.object_store,
                )
                return self._require_complete_array_accounting(
                    aggregation,
                    stage=stage,
                    context=context,
                    slurm_job_id=slurm_job_id,
                )
            except OrchestratorError as error:
                self._record_cycle_stage_accounting_gap(
                    stage,
                    context,
                    pipeline_job_id,
                    slurm_job_id=slurm_job_id,
                    message="Slurm array accounting was unavailable or malformed.",
                    details={"error": error.message, "error_code": error.error_code},
                )

        self._record_cycle_stage_accounting_gap(
            stage,
            context,
            pipeline_job_id,
            slurm_job_id=slurm_job_id,
            message="Slurm array accounting was unavailable or incomplete.",
            details={
                "reason": "array_task_accounting_unavailable",
                "master_status": _status_from_gateway_job(terminal),
                "expected_task_count": len(context.active_basins),
            },
        )
        return ArrayAggregation(total=0, succeeded=0, failed=0, cancelled=0, task_results=())

    def _require_complete_array_accounting(
        self,
        aggregation: ArrayAggregation,
        *,
        stage: StageDefinition,
        context: CycleOrchestrationContext,
        slurm_job_id: str,
    ) -> ArrayAggregation:
        expected_task_ids = set(range(len(context.active_basins)))
        observed_task_ids = {task.task_id for task in aggregation.task_results}
        if observed_task_ids == expected_task_ids:
            return aggregation
        missing_task_ids = sorted(expected_task_ids - observed_task_ids)
        unexpected_task_ids = sorted(observed_task_ids - expected_task_ids)
        raise OrchestratorError(
            "SLURM_ARRAY_ACCOUNTING_INCOMPLETE",
            "Slurm array accounting did not include exactly the submitted task ids.",
            {
                "slurm_job_id": slurm_job_id,
                "stage": stage.stage,
                "expected_task_count": len(context.active_basins),
                "observed_task_count": len(observed_task_ids),
                "missing_task_ids": missing_task_ids,
                "unexpected_task_ids": unexpected_task_ids,
            },
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
        previous_status, record = self.repository.update_pipeline_job_status(
            pipeline_job_id,
            aggregation.status,
            finished_at=_parse_gateway_time(terminal.get("finished_at")) or _utcnow(),
            exit_code=terminal.get("exit_code"),
            error_code=_aggregation_error_code(aggregation) or terminal.get("error_code"),
            error_message=_aggregation_error_message(aggregation) or terminal.get("error_message"),
            log_uri=log_uri,
        )
        if str(record.get("status")) != aggregation.status:
            return
        task_payload = _stage_task_result_evidence(aggregation, context=context)
        self.repository.insert_pipeline_event(
            entity_type="pipeline_job",
            entity_id=pipeline_job_id,
            event_type="status_change",
            status_from=previous_status or _status_from_gateway_job(terminal),
            status_to=aggregation.status,
            message=f"{stage.stage} array aggregated as {aggregation.status}",
            details=_safe_pipeline_event_details(
                {
                    "stage": stage.stage,
                    "job_type": stage.job_type,
                    "total": aggregation.total,
                    "succeeded": aggregation.succeeded,
                    "failed": aggregation.failed,
                    "cancelled": aggregation.cancelled,
                    "pipeline_job_id": pipeline_job_id,
                    "slurm": {
                        "job_id": terminal.get("job_id") or terminal.get("slurm_job_id"),
                        "state": aggregation.status,
                        "exit_code": terminal.get("exit_code"),
                        "log_uri": log_uri,
                        "accounting": _slurm_accounting_from_payload(terminal),
                        "task_results": task_payload,
                        "resource_metrics": _resource_metrics_from_payload(terminal),
                    },
                    "task_results": task_payload,
                }
            ),
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
        accounting = _slurm_accounting_from_payload(terminal)
        if not accounting:
            self._record_cycle_stage_accounting_gap(
                stage,
                context,
                pipeline_job_id,
                slurm_job_id=str(terminal.get("job_id") or terminal.get("slurm_job_id") or ""),
                message="Slurm accounting metrics were unavailable.",
                details={"reason": "accounting_unavailable"},
            )
            return
        self.repository.insert_pipeline_event(
            entity_type="pipeline_job",
            entity_id=pipeline_job_id,
            event_type="slurm_accounting",
            status_from=None,
            status_to=str(terminal.get("status") or ""),
            message=f"{stage.stage} Slurm accounting captured.",
            details=_safe_pipeline_event_details(
                {
                    "stage": stage.stage,
                    "job_type": stage.job_type,
                    "cycle_id": context.cycle_id,
                    "slurm": {
                        "job_id": terminal.get("job_id") or terminal.get("slurm_job_id"),
                        "state": terminal.get("state") or terminal.get("status"),
                        "array_task_id": terminal.get("array_task_id"),
                        "exit_code": terminal.get("exit_code"),
                        "log_uri": log_uri,
                        "accounting": accounting,
                        "resource_metrics": _resource_metrics_from_payload(terminal),
                    },
                }
            ),
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
        self.repository.insert_pipeline_event(
            entity_type="pipeline_job",
            entity_id=pipeline_job_id,
            event_type="slurm_accounting_gap",
            status_from=None,
            status_to="blocked",
            message=message,
            details=_safe_pipeline_event_details(
                {
                    "stage": stage.stage,
                    "job_type": stage.job_type,
                    "cycle_id": context.cycle_id,
                    "slurm_job_id": slurm_job_id,
                    "gap": dict(details),
                    "fabricated_metrics": False,
                }
            ),
        )

    def _after_cycle_stage_terminal(
        self,
        stage: StageDefinition,
        context: CycleOrchestrationContext,
        result_status: str,
        terminal: dict[str, Any],
        aggregation: ArrayAggregation | None,
    ) -> None:
        if result_status == "succeeded":
            status = self._success_cycle_status(stage, context)
            if not (stage.stage == "publish" and context.had_partial):
                self.repository.update_forecast_cycle_status(
                    source_id=context.source_id,
                    cycle_time=context.cycle_time,
                    status=status,
                )
            elif context.last_partial_status is not None:
                self.repository.update_forecast_cycle_status(
                    source_id=context.source_id,
                    cycle_time=context.cycle_time,
                    status=context.last_partial_status,
                )
            return

        if result_status == "partially_failed" and aggregation is not None:
            context.had_partial = True
            context.last_partial_status = self._partial_cycle_status(stage)
            self.repository.update_forecast_cycle_status(
                source_id=context.source_id,
                cycle_time=context.cycle_time,
                status=context.last_partial_status,
                error_code=None,
                error_message=None,
            )
            return

        error_code = terminal.get("error_code") or f"{stage.job_type.upper()}_{result_status.upper()}"
        error_message = terminal.get("error_message") or f"Stage {stage.stage} ended with {result_status}."
        self.repository.update_forecast_cycle_status(
            source_id=context.source_id,
            cycle_time=context.cycle_time,
            status=stage.failure_cycle_status,
            error_code=error_code,
            error_message=error_message,
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

        Gate for the submit path: a loss (``created=False``) means another row
        already holds the idempotency_key, so this pass skips submission. The
        decision is conservative — it does not consult the (non-atomic) re-read
        status, so a stale terminal / ``reservation_lost`` row still blocks
        re-submission within THIS pass (avoiding a TOCTOU double-submit). Such a
        stale candidate is reclaimed by a later, clean pass whose own reserve
        re-attempts the INSERT, never by re-submitting off this pass's re-read.
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
        pipeline_job_id = pipeline_job_id or _pipeline_job_id(context.run_id, stage.stage)
        now = _utcnow()
        message = str(redact_payload(str(error)))
        error_code = getattr(error, "error_code", None) or "SBATCH_SUBMISSION_FAILED"
        self.repository.upsert_pipeline_job(
            {
                "job_id": pipeline_job_id,
                "run_id": context.run_id,
                "cycle_id": context.cycle_id,
                "job_type": stage.job_type,
                "slurm_job_id": None,
                "model_id": _cycle_pipeline_job_model_id(context),
                "status": "submission_failed",
                "stage": stage.stage,
                "submitted_at": now,
                "started_at": None,
                "finished_at": now,
                "exit_code": None,
                "error_code": error_code,
                "error_message": message,
                "log_uri": None,
            }
        )
        self.repository.insert_pipeline_event(
            entity_type="pipeline_job",
            entity_id=pipeline_job_id,
            event_type="submission",
            status_from=None,
            status_to="submission_failed",
            message=f"{stage.stage} submission failed: {message}",
            details=_safe_pipeline_event_details({"stage": stage.stage, "job_type": stage.job_type, "error": message}),
        )
        self.repository.update_forecast_cycle_status(
            source_id=context.source_id,
            cycle_time=context.cycle_time,
            status=stage.failure_cycle_status,
            error_code=error_code,
            error_message=message,
        )
        return StageRunResult(
            stage=stage.stage,
            job_type=stage.job_type,
            pipeline_job_id=pipeline_job_id,
            slurm_job_id="",
            status="submission_failed",
            error_code=error_code,
            error_message=message,
            task_results=(),
        )

    def _skip_duplicate_submission(
        self,
        stage: StageDefinition,
        context: CycleOrchestrationContext,
        pipeline_job_id: str,
        reservation: ReservationResult | None,
    ) -> StageRunResult:
        """Record skip evidence and return without sbatch (candidate in flight).

        Invoked when the reserve gate proves another pass already holds an active
        reservation for this candidate+stage. We emit a durable skip event so the
        overlap receipt can prove the double-submission was prevented, then
        return a typed ``skipped_duplicate_submission`` result; no sbatch runs.
        """

        idempotency_key = reservation.idempotency_key if reservation is not None else ""
        active_status = reservation.status if reservation is not None else None
        skip = {
            "stage": stage.stage,
            "job_type": stage.job_type,
            "idempotency_key": idempotency_key,
            "pipeline_job_id": pipeline_job_id,
            "reservation_status": active_status,
            "reason": "candidate_already_inflight",
        }
        self.duplicate_submission_skips.append(skip)
        try:
            self.repository.insert_pipeline_event(
                entity_type="pipeline_job",
                entity_id=pipeline_job_id,
                event_type="submission_skipped",
                status_from=active_status,
                status_to="skipped_duplicate_submission",
                message=(
                    f"{stage.stage} sbatch skipped: candidate already in flight "
                    f"(idempotency_key={idempotency_key}, status={active_status})."
                ),
                details=_safe_pipeline_event_details(skip),
            )
        except OrchestratorError:
            # Evidence emission must never abort a correct skip decision.
            pass
        return StageRunResult(
            stage=stage.stage,
            job_type=stage.job_type,
            pipeline_job_id=pipeline_job_id,
            slurm_job_id="",
            status="skipped_duplicate_submission",
            error_code=None,
            error_message=(
                f"Skipped duplicate submission for idempotency_key={idempotency_key}; "
                "candidate already in flight."
            ),
            task_results=(),
        )

    def _apply_array_progress(
        self,
        stage: StageDefinition,
        context: CycleOrchestrationContext,
        aggregation: ArrayAggregation,
    ) -> None:
        _record_array_task_outcomes(context, stage=stage.stage, aggregation=aggregation)
        if aggregation.status == "succeeded":
            if context.had_partial and stage.stage in {"parse", "frequency"}:
                context.last_partial_status = "parsed_partial"
            return
        if aggregation.status == "failed":
            context.active_basins = []
            return
        context.had_partial = True
        context.last_partial_status = self._partial_cycle_status(stage)
        context.active_basins = build_reindexed_manifest(context.active_basins, aggregation.succeeded_task_ids)

    def _success_cycle_status(self, stage: StageDefinition, context: CycleOrchestrationContext) -> str:
        if not context.had_partial:
            return stage.success_cycle_status
        if stage.stage in {"parse", "frequency", "publish"}:
            return "parsed_partial"
        if stage.stage in {"forcing", "forecast"}:
            return "forcing_ready_partial"
        return context.last_partial_status or stage.success_cycle_status

    def _partial_cycle_status(self, stage: StageDefinition) -> str:
        if stage.stage in {"parse", "frequency"}:
            return "parsed_partial"
        return "forcing_ready_partial"

    def _build_cycle_stage_manifest(self, stage: StageDefinition, context: CycleOrchestrationContext) -> dict[str, Any]:
        manifest_index_entries = self._reindexed_manifest_entries(context.active_basins)
        manifest: dict[str, Any] = {
            "run_id": context.run_id,
            "model_id": _cycle_payload_model_id(context),
            "job_type": stage.job_type,
            "stage": stage.stage,
            "stage_name": stage.stage,
            "cycle_id": context.cycle_id,
            "source_id": context.source_id,
            "cycle_time": _format_time(context.cycle_time),
            "workspace_dir": str(Path(self.config.workspace_root)),
            "object_store_root": str(Path(self.config.object_store_root)),
            "object_store_prefix": self.config.object_store_prefix,
            "total_basins": len(context.all_basins),
            "active_basins": len(context.active_basins),
            "manifest_index": manifest_index_entries,
            "model_runs": [
                _model_run_stage_evidence(stage.stage, entry, cycle_id=context.cycle_id)
                for entry in manifest_index_entries
            ],
            "identity_contract": {
                "source_id": context.source_id,
                "cycle_id": context.cycle_id,
                "cycle_time": _format_time(context.cycle_time),
                "scenario_ids": sorted(
                    {
                        str(entry.get("scenario_id") or self._forecast_scenario_id(context.source_id))
                        for entry in manifest_index_entries
                    }
                ),
                "run_ids": [str(entry["run_id"]) for entry in manifest_index_entries],
                "model_ids": [str(entry["model_id"]) for entry in manifest_index_entries],
            },
        }
        if stage.stage == "frequency":
            manifest["quality_states"] = [
                _frequency_quality_state(entry, cycle_id=context.cycle_id) for entry in manifest_index_entries
            ]
        if stage.stage == "publish":
            active_keys = {_basin_key(basin) for basin in context.active_basins}
            excluded = [basin for basin in context.all_basins if _basin_key(basin) not in active_keys]
            quality_states = [
                _publish_quality_state(entry, cycle_id=context.cycle_id)
                for entry in manifest_index_entries
            ]
            manifest["metadata"] = {
                "total_basins": len(context.all_basins),
                "published_basins": len(context.active_basins),
                "excluded_basins": [_basin_identifier(basin) for basin in excluded],
                "quality_states": quality_states,
                "residual_blockers": _cycle_residual_blockers(manifest_index_entries),
            }
            manifest["basins"] = list(context.active_basins)
            manifest["quality_states"] = quality_states
        return manifest

    def _write_cycle_manifest_index(
        self,
        context: CycleOrchestrationContext,
        stage: StageDefinition,
        tasks: list[dict[str, Any]],
    ) -> Path:
        try:
            content = serialize_manifest_index(tasks)
        except ManifestValidationError as exc:
            raise OrchestratorError(
                "CYCLE_MANIFEST_INDEX_INVALID",
                f"Cycle manifest index for stage {stage.stage} exceeds the Slurm array manifest contract.",
                {"stage": stage.stage, **exc.details},
            ) from exc
        manifest_path = self._workspace_path("runs", context.run_id, "input", f"{stage.stage}_manifest_index.json")
        try:
            self._safe_workspace_write_bytes(manifest_path, content)
        except (OSError, SafeFilesystemError) as exc:
            raise OrchestratorError(
                "CYCLE_MANIFEST_INDEX_WRITE_FAILED",
                f"Failed to write cycle manifest index safely for stage {stage.stage}: {exc}",
                {"manifest_path": str(manifest_path), "stage": stage.stage},
            ) from exc
        return manifest_path

    def _prepare_forecast_runtime_manifests(
        self,
        stage: StageDefinition,
        context: CycleOrchestrationContext,
    ) -> None:
        if stage.stage != "forecast":
            return

        staged: list[tuple[int, dict[str, Any], dict[str, Any], bytes, Path, str]] = []
        for index, basin in enumerate(context.active_basins):
            manifest = self._build_forecast_runtime_manifest(context, basin)
            content = json.dumps(manifest, indent=2, sort_keys=True).encode("utf-8")
            manifest_uri = manifest["outputs"]["run_manifest_uri"]
            try:
                self.object_store.write_bytes_atomic(manifest_uri, content)
            except OSError as exc:
                raise OrchestratorError(
                    "RUNTIME_MANIFEST_WRITE_FAILED",
                    f"Failed to write runtime manifest to object store for task {index}: {exc}",
                    {"task_id": index, "manifest_uri": manifest_uri},
                ) from exc

            manifest_path = self._workspace_path("runs", str(basin["run_id"]), "input", "manifest.json")
            try:
                self._safe_workspace_write_bytes(manifest_path, content)
            except (OSError, SafeFilesystemError) as exc:
                raise OrchestratorError(
                    "RUNTIME_MANIFEST_WRITE_FAILED",
                    f"Failed to write runtime manifest safely for task {index}: {exc}",
                    {"task_id": index, "manifest_path": str(manifest_path)},
                ) from exc

            self._validate_forecast_runtime_manifest(manifest_path, manifest, task_index=index)
            staged.append((index, basin, manifest, content, manifest_path, manifest_uri))

        created_run_ids: list[str] = []
        try:
            for _index, basin, manifest, _content, manifest_path, _manifest_uri in staged:
                self.repository.create_hydro_run_from_basin(basin, manifest)
                created_run_ids.append(str(manifest["run_id"]))
                basin["manifest_path"] = str(manifest_path)
                basin["model_run_assembly"] = _assembly_payload_from_runtime_manifest(manifest)
                basin["output_uri"] = manifest["outputs"]["output_uri"]
                basin["run_manifest_uri"] = manifest["outputs"]["run_manifest_uri"]
                basin["log_uri"] = manifest["outputs"]["log_uri"]
        except Exception as exc:
            self._mark_staged_hydro_runs_failed(
                created_run_ids,
                error_code=getattr(exc, "error_code", "RUNTIME_MANIFEST_STAGING_FAILED"),
                error_message=getattr(exc, "message", str(exc)),
            )
            raise

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
        assembly = build_model_run_assembly(
            basin,
            source_id=context.source_id,
            cycle_id=context.cycle_id,
            cycle_time=context.cycle_time,
            scenario_id=str(basin.get("scenario_id") or self._forecast_scenario_id(context.source_id)),
            workspace_root=Path(self.config.workspace_root),
            object_store=self.object_store,
            default_forecast_horizon_hours=self.config.forecast_horizon_hours,
        )
        run_id = str(basin["run_id"])
        manifest = {
            "run_id": run_id,
            "run_type": "forecast",
            "candidate_id": assembly.identity["candidate_id"],
            "scenario_id": assembly.identity["scenario_id"],
            "source_id": context.source_id,
            "cycle_time": _format_time(context.cycle_time),
            "start_time": assembly.identity["start_time"],
            "end_time": assembly.identity["end_time"],
            "forecast_horizon_hours": assembly.identity["forecast_horizon_hours"],
            "workspace_dir": str(Path(self.config.workspace_root)),
            "object_store_root": str(Path(self.config.object_store_root)),
            "object_store_prefix": self.config.object_store_prefix,
            "identity": dict(assembly.identity),
            "model": {
                "model_id": assembly.identity["model_id"],
                "basin_id": basin.get("basin_id"),
                "basin_version_id": assembly.identity["basin_version_id"],
                "river_network_version_id": assembly.identity["river_network_version_id"],
                "model_package_uri": assembly.identity["model_package_uri"],
                "model_package_manifest_uri": assembly.identity["model_package_manifest_uri"],
                "model_package_checksum": assembly.identity.get("model_package_checksum"),
                "segment_count": assembly.identity["segment_count"],
                "project_name": assembly.runtime.get("project_name"),
            },
            "forcing": dict(assembly.forcing),
            "initial_state": {
                "state_id": basin.get("init_state_id"),
                "ic_file_uri": basin.get("init_state_uri"),
                "valid_time": _format_time_or_none(_parse_gateway_time(basin.get("init_state_valid_time"))),
                "checksum": basin.get("init_state_checksum"),
                "quality": basin.get("init_state_quality") or "cold_start_no_state",
                "lineage": dict(basin.get("init_state_lineage") or {}),
            },
            "runtime": dict(assembly.runtime),
            "outputs": dict(assembly.outputs),
            "frequency": dict(assembly.frequency),
            "display": dict(assembly.display),
            "quality_states": dict(assembly.quality_states),
            "residual_blockers": [dict(item) for item in assembly.residual_blockers],
        }
        manifest["runtime"]["init_mode"] = 3 if basin.get("init_state_id") or basin.get("init_state_uri") else 1
        return manifest

    def _validate_forecast_runtime_manifest(
        self,
        manifest_path: Path,
        manifest: Mapping[str, Any],
        *,
        task_index: int,
    ) -> None:
        if not manifest_path.exists():
            raise OrchestratorError(
                "RUNTIME_MANIFEST_MISSING",
                f"Forecast runtime manifest was not written for task {task_index}.",
                {"manifest_path": str(manifest_path), "task_id": task_index},
            )
        try:
            persisted = json.loads(self._safe_workspace_read_bytes(manifest_path).decode("utf-8"))
        except json.JSONDecodeError as exc:
            raise OrchestratorError(
                "RUNTIME_MANIFEST_INVALID_JSON",
                f"Forecast runtime manifest is not valid JSON for task {task_index}.",
                {"manifest_path": str(manifest_path), "task_id": task_index, "error": str(exc)},
            ) from exc
        except (OSError, SafeFilesystemError) as exc:
            raise OrchestratorError(
                "RUNTIME_MANIFEST_READ_FAILED",
                f"Forecast runtime manifest cannot be safely read for task {task_index}: {exc}",
                {"manifest_path": str(manifest_path), "task_id": task_index},
            ) from exc
        required_paths = (
            ("run_id",),
            ("run_type",),
            ("scenario_id",),
            ("source_id",),
            ("cycle_time",),
            ("start_time",),
            ("end_time",),
            ("model", "model_id"),
            ("model", "basin_version_id"),
            ("model", "river_network_version_id"),
            ("model", "model_package_uri"),
            ("forcing", "forcing_uri"),
            ("outputs", "run_manifest_uri"),
            ("outputs", "output_uri"),
            ("outputs", "log_uri"),
            ("identity",),
            ("workspace_dir",),
            ("object_store_root",),
        )
        missing = [".".join(path) for path in required_paths if _nested_value(persisted, path) in (None, "")]
        if missing:
            raise OrchestratorError(
                "RUNTIME_MANIFEST_INVALID",
                f"Forecast runtime manifest is missing required fields for task {task_index}: {', '.join(missing)}.",
                {"manifest_path": str(manifest_path), "task_id": task_index, "missing_fields": missing},
            )
        if persisted.get("run_id") != manifest.get("run_id"):
            raise OrchestratorError(
                "RUNTIME_MANIFEST_INVALID",
                f"Forecast runtime manifest run_id mismatch for task {task_index}.",
                {"manifest_path": str(manifest_path), "task_id": task_index},
            )

    def _reindexed_manifest_entries(self, basins: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
        entries = build_reindexed_manifest([dict(basin) for basin in basins], range(len(basins)))
        for entry in entries:
            if "model_run_assembly" not in entry:
                source_id = normalize_source_id(str(entry.get("source_id") or self.config.source_id))
                cycle_time = parse_cycle_time(entry["cycle_time"])
                assembly = build_model_run_assembly(
                    entry,
                    source_id=source_id,
                    cycle_id=str(entry.get("cycle_id") or cycle_id_for(source_id, cycle_time)),
                    cycle_time=cycle_time,
                    scenario_id=str(entry.get("scenario_id") or self._forecast_scenario_id(source_id)),
                    workspace_root=Path(self.config.workspace_root),
                    object_store=self.object_store,
                    default_forecast_horizon_hours=self.config.forecast_horizon_hours,
                )
                entry["model_run_assembly"] = assembly.to_manifest_entry()
                entry["output_uri"] = assembly.outputs["output_uri"]
                entry["run_manifest_uri"] = assembly.outputs["run_manifest_uri"]
                entry["log_uri"] = assembly.outputs["log_uri"]
        return entries

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
                }
            else:
                entry = dict(basin)
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
            return
        for basin in basins:
            if basin.get("init_state_uri") not in (None, ""):
                # Caller (scheduler) already selected a state; do not override it.
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
            basin["init_state_id"] = selection.state_id
            basin["init_state_uri"] = selection.state_uri
            basin["init_state_checksum"] = selection.checksum
            basin["init_state_valid_time"] = (
                _format_time(selection.valid_time) if selection.valid_time is not None else None
            )
            basin["init_state_quality"] = selection.quality
            basin["init_state_lineage"] = {
                "source_id": selection.source_id,
                "cycle_id": selection.cycle_id,
                "lead_hours": selection.lead_hours,
                "model_package_version": selection.model_package_version,
                "model_package_checksum": selection.model_package_checksum,
            }
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

    @staticmethod
    def _find_existing_stage_job(jobs: Sequence[Mapping[str, Any]], stage: StageDefinition) -> dict[str, Any] | None:
        matches = [dict(job) for job in jobs if ForecastOrchestrator._job_matches_stage(job, stage)]
        if not matches:
            return None
        active_matches = [job for job in matches if str(job.get("status")) not in TERMINAL_JOB_STATUSES]
        return dict((active_matches or matches)[-1])

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
        self.repository.ensure_forecast_cycle(source_id=source_id, cycle_time=parsed_cycle_time)
        initial_state = self._select_forecast_initial_state(
            model_id=model_id,
            cycle_time=parsed_cycle_time,
            source_id=source_id,
            model_package_version=model.model_package_uri,
            max_lead_hours=max_lead_hours,
        )
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
        self._before_stage_submit(stage, context)

        manifest = self._build_stage_submission_manifest(stage, context)
        payload = {
            "run_id": context.run_id,
            "model_id": context.model_id,
            "job_type": stage.job_type,
            "manifest": self._slurm_submission_manifest(manifest),
        }
        submitted = self.slurm_client.submit_job(payload)
        slurm_job_id = str(submitted["job_id"])
        pipeline_job_id = _pipeline_job_id(context.run_id, stage.stage)
        log_publication = self._display_log_publication_for_stage(
            source_id=context.source_id,
            cycle_time=context.cycle_time,
            run_id=context.run_id,
            job_id=pipeline_job_id,
            stage=stage.stage,
        )
        current_status = _status_from_gateway_job(submitted)
        submitted_log_uri = log_publication.advertised_uri
        submitted_publish_attempt: DisplayLogPublicationAttempt | None = None
        if current_status in TERMINAL_JOB_STATUSES:
            submitted_publish_attempt = self._try_publish_log_for_advertise(slurm_job_id, log_publication)
            submitted_log_uri = submitted_publish_attempt.advertised_uri
            if submitted_log_uri:
                submitted["log_uri"] = submitted_log_uri
        pipeline_record = self.repository.upsert_pipeline_job(
            {
                "job_id": pipeline_job_id,
                "run_id": context.run_id,
                "cycle_id": context.cycle_id,
                "job_type": stage.job_type,
                "slurm_job_id": slurm_job_id,
                "model_id": context.model_id,
                "status": current_status,
                "stage": stage.stage,
                "submitted_at": _parse_gateway_time(submitted.get("submitted_at")),
                "started_at": _parse_gateway_time(submitted.get("started_at")),
                "finished_at": _parse_gateway_time(submitted.get("finished_at")),
                "exit_code": submitted.get("exit_code"),
                "error_code": submitted.get("error_code"),
                "error_message": submitted.get("error_message"),
                "log_uri": submitted_log_uri,
            }
        )
        entity_type, entity_id = self._pipeline_event_target(context, pipeline_job_id)
        self.repository.insert_pipeline_event(
            entity_type=entity_type,
            entity_id=entity_id,
            event_type="status_change",
            status_from=None,
            status_to=current_status,
            message=f"{stage.stage} submitted to Slurm Gateway as {slurm_job_id}",
            details=_safe_pipeline_event_details(
                {
                    "stage": stage.stage,
                    "slurm_job_id": slurm_job_id,
                    "slurm": {
                        "job_id": slurm_job_id,
                        "state": current_status,
                        "exit_code": submitted.get("exit_code"),
                        "log_uri": submitted_log_uri,
                        "accounting": _slurm_accounting_from_payload(submitted),
                        "resource_metrics": _resource_metrics_from_payload(submitted),
                    },
                }
            ),
        )
        if first_stage:
            self.repository.update_hydro_run_status(context.run_id, "submitted", slurm_job_id=slurm_job_id)

        if current_status in TERMINAL_JOB_STATUSES:
            terminal_observation = TerminalJobObservation(
                job=submitted,
                publication_attempt=submitted_publish_attempt,
            )
        else:
            terminal_observation = self._poll_until_terminal(
                stage=stage,
                context=context,
                pipeline_job_id=pipeline_job_id,
                initial_job=submitted,
                initial_status=str(pipeline_record["status"]),
                log_publication=log_publication,
            )
        terminal = terminal_observation.job
        publication_attempt = terminal_observation.publication_attempt
        log_uri = str(terminal.get("log_uri") or "")
        if not log_uri:
            if publication_attempt is None:
                publication_attempt = self._try_publish_log_for_advertise(slurm_job_id, log_publication)
            log_uri = str(publication_attempt.advertised_uri or "")

        if terminal["status"] == "succeeded":
            self._after_stage_success(stage, context, terminal)
        else:
            self._after_stage_failure(stage, context, terminal)
        self._raise_publish_error_after_durable_update(publication_attempt)

        return StageRunResult(
            stage=stage.stage,
            job_type=stage.job_type,
            pipeline_job_id=pipeline_job_id,
            slurm_job_id=slurm_job_id,
            status=str(terminal["status"]),
            exit_code=terminal.get("exit_code"),
            error_code=terminal.get("error_code"),
            error_message=terminal.get("error_message"),
            log_uri=log_uri,
            accounting=_slurm_accounting_from_payload(terminal),
            task_results=(),
        )

    def _build_stage_submission_manifest(
        self,
        stage: StageDefinition,
        context: ForecastRunContext | AnalysisRunContext,
    ) -> dict[str, Any]:
        manifest = {
            "run_id": context.run_id,
            "model_id": context.model_id,
            "stage": stage.stage,
            "stage_name": stage.stage,
            "job_type": stage.job_type,
            "source_id": context.source_id,
            "cycle_id": context.cycle_id,
            "cycle_time": _format_time(context.cycle_time),
            "start_time": _format_time(context.start_time),
            "end_time": _format_time(context.end_time),
            "basin_id": context.basin_id,
            "basin_version_id": context.basin_version_id,
            "river_network_version_id": context.river_network_version_id,
            "segment_count": context.segment_count,
            "model_package_uri": context.model_package_uri,
            "forcing_version_id": context.forcing_version_id,
            "forcing_package_uri": context.forcing_package_uri,
            "run_manifest_uri": context.run_manifest_uri,
            "output_uri": context.output_uri,
            "log_uri": context.log_uri,
            "workspace_dir": str(Path(self.config.workspace_root)),
            "object_store_root": str(Path(self.config.object_store_root)),
            "object_store_prefix": self.config.object_store_prefix,
        }
        if isinstance(context, AnalysisRunContext):
            self._validate_analysis_template_context(context)
            manifest.update(
                {
                    "analysis_date": context.start_time.strftime("%Y-%m-%d"),
                    "analysis_start_time": _format_time(context.start_time),
                    "analysis_end_time": _format_time(context.end_time),
                    "analysis_date_range": f"{_format_time(context.start_time)}/{_format_time(context.end_time)}",
                    "era5_area": self.config.era5_area,
                }
            )
        return manifest

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
        job = initial_job
        current_status = initial_status
        deadline = time.monotonic() + self.config.job_timeout_seconds
        while _status_from_gateway_job(job) not in TERMINAL_JOB_STATUSES:
            if time.monotonic() >= deadline:
                return self._record_stage_poll_timeout(
                    stage=stage,
                    context=context,
                    pipeline_job_id=pipeline_job_id,
                    job=dict(job),
                    current_status=current_status,
                    log_publication=log_publication,
                )
            time.sleep(self.config.poll_interval_seconds)
            job = self.slurm_client.get_job_status(str(job["job_id"]))
            new_status = _status_from_gateway_job(job)
            if new_status == current_status:
                continue
            log_uri = log_publication.advertised_uri
            publication_attempt: DisplayLogPublicationAttempt | None = None
            if new_status in TERMINAL_JOB_STATUSES:
                publication_attempt = self._try_publish_log_for_advertise(str(job["job_id"]), log_publication)
                log_uri = publication_attempt.advertised_uri
            previous_status, record = self.repository.update_pipeline_job_status(
                pipeline_job_id,
                new_status,
                started_at=_parse_gateway_time(job.get("started_at")),
                finished_at=_parse_gateway_time(job.get("finished_at")),
                exit_code=job.get("exit_code"),
                error_code=job.get("error_code"),
                error_message=job.get("error_message"),
                log_uri=log_uri if new_status in TERMINAL_JOB_STATUSES else None,
            )
            if log_uri and new_status in TERMINAL_JOB_STATUSES:
                job["log_uri"] = log_uri
            persisted_status = str(record.get("status") or new_status)
            if persisted_status != new_status:
                job["status"] = persisted_status
                current_status = persisted_status
                if persisted_status in TERMINAL_JOB_STATUSES:
                    return TerminalJobObservation(job=job, publication_attempt=publication_attempt)
                continue
            entity_type, entity_id = self._pipeline_event_target(context, pipeline_job_id)
            self.repository.insert_pipeline_event(
                entity_type=entity_type,
                entity_id=entity_id,
                event_type="status_change",
                status_from=previous_status or current_status,
                status_to=new_status,
                message=_stage_status_message(stage.stage, new_status, job),
                details=_safe_pipeline_event_details(
                    {
                        "stage": stage.stage,
                        "slurm_job_id": job["job_id"],
                        "slurm": {
                            "job_id": job["job_id"],
                            "state": job.get("state") or job.get("status"),
                            "exit_code": job.get("exit_code"),
                            "log_uri": log_uri if new_status in TERMINAL_JOB_STATUSES else None,
                            "accounting": _slurm_accounting_from_payload(job),
                            "resource_metrics": _resource_metrics_from_payload(job),
                        },
                    }
                ),
            )
            self._after_stage_status_change(stage, context, previous_status or current_status, new_status, job)
            current_status = new_status
            if publication_attempt is not None and publication_attempt.error is not None:
                return TerminalJobObservation(job=job, publication_attempt=publication_attempt)

        terminal_status = _status_from_gateway_job(job)
        if terminal_status != current_status:
            publication_attempt = self._try_publish_log_for_advertise(str(job["job_id"]), log_publication)
            log_uri = publication_attempt.advertised_uri
            previous_status, record = self.repository.update_pipeline_job_status(
                pipeline_job_id,
                terminal_status,
                started_at=_parse_gateway_time(job.get("started_at")),
                finished_at=_parse_gateway_time(job.get("finished_at")),
                exit_code=job.get("exit_code"),
                error_code=job.get("error_code"),
                error_message=job.get("error_message"),
                log_uri=log_uri,
            )
            if log_uri:
                job["log_uri"] = log_uri
            persisted_status = str(record.get("status") or terminal_status)
            if persisted_status != terminal_status:
                job["status"] = persisted_status
                return TerminalJobObservation(job=job, publication_attempt=publication_attempt)
            entity_type, entity_id = self._pipeline_event_target(context, pipeline_job_id)
            self.repository.insert_pipeline_event(
                entity_type=entity_type,
                entity_id=entity_id,
                event_type="status_change",
                status_from=previous_status or current_status,
                status_to=terminal_status,
                message=_stage_status_message(stage.stage, terminal_status, job),
                details=_safe_pipeline_event_details(
                    {
                        "stage": stage.stage,
                        "slurm_job_id": job["job_id"],
                        "slurm": {
                            "job_id": job["job_id"],
                            "state": job.get("state") or job.get("status"),
                            "exit_code": job.get("exit_code"),
                            "log_uri": log_uri,
                            "accounting": _slurm_accounting_from_payload(job),
                            "resource_metrics": _resource_metrics_from_payload(job),
                        },
                    }
                ),
            )
            self._after_stage_status_change(stage, context, previous_status or current_status, terminal_status, job)
            if publication_attempt.error is not None:
                return TerminalJobObservation(job=job, publication_attempt=publication_attempt)
        return TerminalJobObservation(job=job)

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
        message = f"Stage {stage.stage} did not reach a terminal status before timeout."
        terminal = dict(job)
        terminal.update(
            {
                "status": "failed",
                "finished_at": _format_time(_utcnow()),
                "error_code": "SLURM_JOB_TIMEOUT",
                "error_message": message,
            }
        )
        publication_attempt = self._try_publish_log_for_advertise(str(job["job_id"]), log_publication)
        log_uri = publication_attempt.advertised_uri
        previous_status, record = self.repository.update_pipeline_job_status(
            pipeline_job_id,
            "failed",
            finished_at=_utcnow(),
            exit_code=terminal.get("exit_code"),
            error_code="SLURM_JOB_TIMEOUT",
            error_message=message,
            log_uri=log_uri,
        )
        terminal.update(record)
        entity_type, entity_id = self._pipeline_event_target(context, pipeline_job_id)
        self.repository.insert_pipeline_event(
            entity_type=entity_type,
            entity_id=entity_id,
            event_type="timeout",
            status_from=previous_status or current_status,
            status_to="failed",
            message=message,
            details=_safe_pipeline_event_details(
                {
                    "stage": stage.stage,
                    "job_type": stage.job_type,
                    "run_id": context.run_id,
                    "slurm_job_id": job["job_id"],
                    "timeout_seconds": self.config.job_timeout_seconds,
                    "error_code": "SLURM_JOB_TIMEOUT",
                    "slurm": {
                        "job_id": job["job_id"],
                        "state": job.get("state") or job.get("status"),
                        "exit_code": terminal.get("exit_code"),
                        "accounting": _slurm_accounting_from_payload(job),
                        "resource_metrics": _resource_metrics_from_payload(job),
                    },
                }
            ),
        )
        self.repository.update_forecast_cycle_status(
            source_id=context.source_id,
            cycle_time=context.cycle_time,
            status=stage.failure_cycle_status,
            error_code="SLURM_JOB_TIMEOUT",
            error_message=message,
        )
        self.repository.update_hydro_run_status(
            context.run_id,
            "failed",
            error_code="SLURM_JOB_TIMEOUT",
            error_message=message,
        )
        return TerminalJobObservation(job=terminal, publication_attempt=publication_attempt)

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
        template_path = Path(self.config.templates_dir) / stage.template_name
        if not template_path.exists():
            repo_template_path = Path(__file__).resolve().parents[2] / "infra" / "sbatch" / stage.template_name
            if repo_template_path.exists():
                template_path = repo_template_path
            else:
                raise OrchestratorError("SBATCH_TEMPLATE_MISSING", f"Missing sbatch template: {template_path.name}")
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
        if hasattr(self.config, "era5_area") and not _SAFE_AREA_RE.match(self.config.era5_area):
            raise OrchestratorError("UNSAFE_TEMPLATE_PARAM", f"era5_area unsafe: {self.config.era5_area!r}")
        if isinstance(context, AnalysisRunContext):
            self._validate_analysis_template_context(context)
        run_manifest_path = self._workspace_path("runs", context.run_id, "input", "manifest.json")
        template_context = {
            "source_id": context.source_id,
            "source_id_lower": context.source_id.lower(),
            "cycle_time": format_cycle_time(context.cycle_time),
            "cycle_time_iso": _format_time(context.cycle_time),
            "model_id": context.model_id,
            "basin_id": context.basin_id or "",
            "basin_version_id": context.basin_version_id,
            "river_network_version_id": context.river_network_version_id,
            "run_id": context.run_id,
            "stage_name": stage.stage,
            "job_type": stage.job_type,
            "workspace_dir": str(Path(self.config.workspace_root)),
            "object_store_root": str(Path(self.config.object_store_root)),
            "object_store_prefix": self.config.object_store_prefix,
            "run_manifest_path": str(run_manifest_path),
            "run_type": getattr(
                context,
                "run_type",
                "analysis" if isinstance(context, AnalysisRunContext) else "forecast",
            ),
            "analysis_date": context.start_time.strftime("%Y-%m-%d"),
            "analysis_start_time": _format_time(context.start_time),
            "analysis_end_time": _format_time(context.end_time),
            "analysis_date_range": f"{_format_time(context.start_time)}/{_format_time(context.end_time)}",
            "era5_area": self.config.era5_area,
            "cycle_id": context.cycle_id,
            "partition": "compute",
            "nodes": 1,
            "ntasks": 1,
            "cpus_per_task": 1,
            "memory_gb": 1,
            "walltime": "01:00:00",
            "max_concurrent": 1,
            "shud_threads": 1,
            "manifest_index_path": "",
        }
        template_context["export_lines"] = _template_export_lines(template_context)
        template_text = template_path.read_text(encoding="utf-8")
        if "{{" in template_text or "{%" in template_text:
            from jinja2 import StrictUndefined
            from jinja2.sandbox import SandboxedEnvironment

            return (
                SandboxedEnvironment(undefined=StrictUndefined, autoescape=False)
                .from_string(template_text)
                .render(**template_context)
            )
        return template_text.format(**template_context)

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
            init_state_id=selected_state.state_id,
            init_state_uri=selected_state.state_uri,
            init_state_valid_time=selected_state.valid_time,
            init_state_checksum=selected_state.checksum,
            init_state_quality=selected_state.quality,
            output_segment_count=model.output_segment_count,
        )

    def _forecast_scenario_id(self, source_id: str) -> str:
        if self.config.scenario_id_explicit and self.config.scenario_id:
            return self.config.scenario_id
        return scenario_for_source(source_id)

    def _build_run_manifest(self, context: ForecastRunContext) -> dict[str, Any]:
        return {
            "run_id": context.run_id,
            "run_type": "forecast",
            "scenario_id": context.scenario_id,
            "source_id": context.source_id,
            "cycle_time": _format_time(context.cycle_time),
            "start_time": _format_time(context.start_time),
            "end_time": _format_time(context.end_time),
            "forecast_horizon_hours": context.forecast_horizon_hours,
            "model": {
                "model_id": context.model_id,
                "basin_version_id": context.basin_version_id,
                "river_network_version_id": context.river_network_version_id,
                "model_package_uri": context.model_package_uri,
                "segment_count": context.segment_count,
                "output_segment_count": (
                    context.output_segment_count
                    if context.output_segment_count is not None
                    else context.segment_count
                ),
            },
            "forcing": {
                "forcing_version_id": context.forcing_version_id,
                "forcing_uri": context.forcing_package_uri,
            },
            "initial_state": {
                "state_id": context.init_state_id,
                "ic_file_uri": context.init_state_uri,
                "valid_time": _format_time_or_none(context.init_state_valid_time),
                "checksum": context.init_state_checksum,
                "quality": context.init_state_quality,
            },
            "runtime": {
                "output_interval_minutes": 60,
                "init_mode": 3 if context.init_state_id else 1,
            },
            "outputs": {
                "run_manifest_uri": context.run_manifest_uri,
                "output_uri": context.output_uri,
                "log_uri": context.log_uri,
                "output_segment_count": (
                    context.output_segment_count
                    if context.output_segment_count is not None
                    else context.segment_count
                ),
                "gis_segment_count": context.segment_count,
            },
        }

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
        if self.state_manager is None:
            return InitialStateSelection(None, None, None, None, "cold_start_no_state")

        cursor = cycle_time
        last_rejection_code: str | None = None
        # Fallback loop: reject incompatible-lineage / failed-QC candidates and try
        # the next older usable state, never failing the cycle for a missing successor.
        for _ in range(_MAX_STATE_FALLBACK_CANDIDATES):
            state = self.state_manager.get_latest_usable_state(model_id=model_id, before_time=cursor)
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
                return InitialStateSelection(
                    None, None, None, None, quality, rejection_code=STATE_TOO_STALE
                )

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

        return InitialStateSelection(
            None, None, None, None, "cold_start_no_state", rejection_code=last_rejection_code
        )

    def _write_run_manifest(self, context: ForecastRunContext | AnalysisRunContext, manifest: dict[str, Any]) -> None:
        content = json.dumps(manifest, indent=2, sort_keys=True).encode("utf-8")
        self.object_store.write_bytes_atomic(context.run_manifest_uri, content)
        workspace_manifest = self._workspace_path("runs", context.run_id, "input", "manifest.json")
        try:
            self._safe_workspace_write_bytes(workspace_manifest, content)
        except (OSError, SafeFilesystemError) as exc:
            raise OrchestratorError(
                "RUNTIME_MANIFEST_WRITE_FAILED",
                f"Failed to write run manifest safely: {exc}",
                {"manifest_path": str(workspace_manifest), "run_id": context.run_id},
            ) from exc

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
        if self.state_manager is None:
            return None
        return self.state_manager.get_latest_usable_state(model_id=model_id, before_time=before_time)

    def _build_analysis_context(
        self,
        start_time: datetime,
        end_time: datetime,
        model: ModelContext,
        forcing: ForcingContext,
        init_state: StateSnapshot | None,
    ) -> AnalysisRunContext:
        compact_start = format_cycle_time(start_time)
        compact_end = format_cycle_time(end_time)
        run_id = f"analysis_era5_{compact_start}_{compact_end}_{model.model_id}"
        fallback_forcing_uri = f"forcing/era5/{compact_start}/{model.basin_version_id}/{model.model_id}/"
        return AnalysisRunContext(
            run_id=run_id,
            source_id=ANALYSIS_SOURCE_ID,
            cycle_id=cycle_id_for(ANALYSIS_SOURCE_ID, start_time),
            cycle_time=start_time,
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
            run_manifest_uri=self.object_store.uri_for_key(f"runs/{run_id}/input/manifest.json"),
            output_uri=_directory_uri(self.object_store, f"runs/{run_id}/output/"),
            log_uri=_directory_uri(self.object_store, f"runs/{run_id}/logs/"),
            init_state_id=init_state.state_id if init_state is not None else None,
            init_state_uri=init_state.state_uri if init_state is not None else None,
            init_state_valid_time=init_state.valid_time if init_state is not None else None,
            output_segment_count=model.output_segment_count,
            update_ic_step_minutes=_analysis_update_ic_step_minutes(start_time, end_time),
            forcing_causality=_analysis_forcing_causality(),
        )

    def _build_run_manifest(self, context: AnalysisRunContext) -> dict[str, Any]:
        return {
            "run_id": context.run_id,
            "run_type": "analysis",
            "scenario_id": ANALYSIS_SCENARIO_ID,
            "source_id": context.source_id,
            "cycle_time": _format_time(context.cycle_time),
            "start_time": _format_time(context.start_time),
            "end_time": _format_time(context.end_time),
            "model": {
                "model_id": context.model_id,
                "basin_version_id": context.basin_version_id,
                "river_network_version_id": context.river_network_version_id,
                "model_package_uri": context.model_package_uri,
                "segment_count": context.segment_count,
                "output_segment_count": (
                    context.output_segment_count
                    if context.output_segment_count is not None
                    else context.segment_count
                ),
            },
            "initial_state": {
                "state_id": context.init_state_id,
                "ic_file_uri": context.init_state_uri,
                "valid_time": _format_time_or_none(context.init_state_valid_time),
            },
            "forcing": {
                "forcing_version_id": context.forcing_version_id,
                "forcing_uri": context.forcing_package_uri,
            },
            "forcing_causality": dict(
                context.forcing_causality
                if context.forcing_causality is not None
                else _analysis_forcing_causality()
            ),
            "runtime": {
                "output_interval_minutes": 60,
                "init_mode": 3 if context.init_state_id else 1,
                # Restart cadence lands exactly at end_time == T_{N+1} so the saved
                # interim state is valid at the next cycle's init time.
                "update_ic_step_minutes": (
                    context.update_ic_step_minutes
                    if context.update_ic_step_minutes is not None
                    else _analysis_update_ic_step_minutes(context.start_time, context.end_time)
                ),
            },
            "outputs": {
                "run_manifest_uri": context.run_manifest_uri,
                "output_uri": context.output_uri,
                "log_uri": context.log_uri,
                "output_segment_count": (
                    context.output_segment_count
                    if context.output_segment_count is not None
                    else context.segment_count
                ),
                "gis_segment_count": context.segment_count,
            },
        }

    def _before_stage_submit(self, stage: StageDefinition, context: ForecastRunContext | AnalysisRunContext) -> None:
        if stage.stage == "era5_download":
            self.repository.update_forecast_cycle_status(
                source_id=context.source_id,
                cycle_time=context.cycle_time,
                status="downloading",
            )
        elif stage.stage == "analysis_run":
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
        if stage.stage == "analysis_run":
            self.repository.update_hydro_run_status(context.run_id, "succeeded")
        elif stage.stage == "parse_output":
            self.repository.update_hydro_run_status(context.run_id, "parsed")
            self._record_best_available(context)

    def _after_stage_failure(
        self,
        stage: StageDefinition,
        context: ForecastRunContext | AnalysisRunContext,
        terminal: dict[str, Any],
    ) -> None:
        error_code = _analysis_error_code(stage, terminal)
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
        stage: StageDefinition,
        context: ForecastRunContext | AnalysisRunContext,
        _status_from: str | None,
        status_to: str,
        job: dict[str, Any],
    ) -> None:
        if stage.stage == "analysis_run" and status_to == "running":
            self.repository.update_hydro_run_status(context.run_id, "running", slurm_job_id=str(job["job_id"]))

    def _pipeline_event_target(
        self,
        context: ForecastRunContext | AnalysisRunContext,
        _pipeline_job_id: str,
    ) -> tuple[str, str]:
        return "analysis_pipeline", context.run_id

    def _record_best_available(self, context: ForecastRunContext | AnalysisRunContext) -> None:
        if self.best_available_manager is None or context.forcing_version_id is None:
            return
        self.best_available_manager.write_forcing_version(context.forcing_version_id)


@dataclass(frozen=True)
class PsycopgOrchestratorRepository:
    database_url: str

    @classmethod
    def from_env(cls) -> PsycopgOrchestratorRepository:
        database_url = os.getenv("DATABASE_URL", "").strip()
        if not database_url:
            raise OrchestratorError("DATABASE_URL_MISSING", "DATABASE_URL is required for orchestration.")
        return cls(database_url)

    def has_active_orchestration(self, *, source_id: str, cycle_time: datetime) -> bool:
        cycle_id = cycle_id_for(source_id, cycle_time)
        row = self._fetch_optional(
            """
            SELECT 1 AS active
            FROM ops.pipeline_job
            WHERE cycle_id = %s
              AND status NOT IN (
                'succeeded', 'partially_failed', 'failed', 'cancelled', 'submission_failed', 'permanently_failed'
              )
            LIMIT 1
            """,
            (cycle_id,),
        )
        return row is not None

    def has_active_pipeline(self, *, source_id: str, cycle_time: datetime, model_id: str) -> bool:
        cycle_id = cycle_id_for(source_id, cycle_time)
        cycle_run_id = f"cycle_{source_id.lower()}_{format_cycle_time(cycle_time)}"
        candidate_run_id = f"fcst_{source_id.lower()}_{format_cycle_time(cycle_time)}_{model_id}"
        row = self._fetch_optional(
            """
            SELECT 1 AS active
            FROM hydro.hydro_run h
            WHERE h.source_id = %s
              AND h.cycle_time = %s
              AND h.model_id = %s
              AND h.status::text = ANY(%s)
            UNION ALL
            SELECT 1 AS active
            FROM ops.pipeline_job pj
            WHERE pj.cycle_id = %s
              AND pj.status NOT IN (
                'succeeded', 'partially_failed', 'failed', 'cancelled', 'submission_failed', 'permanently_failed'
              )
              AND (
                    pj.run_id = %s
                 OR pj.run_id = %s
                 OR pj.model_id = %s
                 OR (pj.run_id = %s AND pj.model_id IS NULL)
              )
            LIMIT 1
            """,
            (
                source_id,
                cycle_time,
                model_id,
                list(ACTIVE_HYDRO_STATUSES),
                cycle_id,
                candidate_run_id,
                cycle_run_id,
                model_id,
                cycle_run_id,
            ),
        )
        return row is not None

    def has_completed_pipeline(self, *, source_id: str, cycle_time: datetime, model_id: str) -> bool:
        row = self._fetch_optional(
            """
            SELECT 1 AS completed
            FROM hydro.hydro_run
            WHERE source_id = %s
              AND cycle_time = %s
              AND model_id = %s
              AND status::text = ANY(%s)
            LIMIT 1
            """,
            (source_id, cycle_time, model_id, list(COMPLETED_HYDRO_STATUSES)),
        )
        return row is not None

    def candidate_state(
        self,
        *,
        source_id: str,
        cycle_time: datetime,
        model_id: str,
        run_id: str,
        forcing_version_id: str,
        candidate_id: str,
        retry_limit: int | None = None,
        job_limit: int = DEFAULT_CANDIDATE_STATE_JOB_LIMIT,
        event_limit: int = DEFAULT_CANDIDATE_STATE_EVENT_LIMIT,
    ) -> dict[str, Any] | None:
        cycle_id = cycle_id_for(source_id, cycle_time)
        cycle_run_id = f"cycle_{source_id.lower()}_{format_cycle_time(cycle_time)}"
        job_limit = max(int(job_limit), 1)
        event_limit = max(int(event_limit), 1)
        hydro_run = self._fetch_optional(
            """
            SELECT
                run_id,
                scenario_id,
                model_id,
                basin_version_id,
                forcing_version_id,
                source_id,
                cycle_time,
                status,
                slurm_job_id,
                output_uri,
                log_uri,
                error_code,
                error_message,
                updated_at
            FROM hydro.hydro_run
            WHERE run_id = %s
               OR (
                    source_id = %s
                AND cycle_time = %s
                AND model_id = %s
               )
            ORDER BY CASE WHEN run_id = %s THEN 0 ELSE 1 END, updated_at DESC
            LIMIT 1
            """,
            (run_id, source_id, cycle_time, model_id, run_id),
        )
        jobs = self._fetch_all(
            """
            SELECT
                job_id,
                run_id,
                cycle_id,
                job_type,
                slurm_job_id,
                array_task_id,
                model_id,
                status,
                stage,
                submitted_at,
                started_at,
                finished_at,
                exit_code,
                retry_count,
                error_code,
                error_message,
                log_uri,
                created_at,
                updated_at
            FROM ops.pipeline_job
            WHERE (
                    run_id = %s
                 OR (cycle_id = %s AND model_id = %s)
                 OR (cycle_id = %s AND run_id = %s)
                 OR (cycle_id = %s AND model_id IS NULL AND run_id = %s)
                  )
            ORDER BY
                COALESCE(updated_at, finished_at, submitted_at, started_at, created_at) DESC NULLS LAST,
                created_at DESC
            LIMIT %s
            """,
            (
                run_id,
                cycle_id,
                model_id,
                cycle_id,
                run_id,
                cycle_id,
                cycle_run_id,
                job_limit + 1,
            ),
        )
        jobs_total = len(jobs)
        jobs_truncated = jobs_total > job_limit
        jobs = sorted(
            jobs[:job_limit],
            key=lambda job: (
                _pipeline_job_truth_sort_key(job),
                _datetime_sort_key(job.get("created_at")),
            ),
        )
        events: list[dict[str, Any]] = []
        events_total = 0
        events_truncated = False
        events = self._fetch_all(
            """
            SELECT
                pe.event_id,
                pe.entity_type,
                pe.entity_id,
                pe.event_type,
                pe.status_from,
                pe.status_to,
                pe.message,
                pe.details,
                pe.created_at
            FROM ops.pipeline_event pe
            WHERE pe.entity_type = 'pipeline_job'
              AND pe.entity_id IN (
                SELECT pj.job_id
                FROM ops.pipeline_job pj
                WHERE (
                        pj.run_id = %s
                     OR (pj.cycle_id = %s AND pj.model_id = %s)
                     OR (pj.cycle_id = %s AND pj.run_id = %s)
                     OR (pj.cycle_id = %s AND pj.model_id IS NULL AND pj.run_id = %s)
                      )
              )
            ORDER BY pe.created_at DESC, pe.event_id DESC
            LIMIT %s
            """,
            (
                run_id,
                cycle_id,
                model_id,
                cycle_id,
                run_id,
                cycle_id,
                cycle_run_id,
                event_limit + 1,
            ),
        )
        events_total = len(events)
        events_truncated = events_total > event_limit
        events = sorted(
            events[:event_limit],
            key=lambda event: (
                _datetime_sort_key(event.get("created_at")),
                _numeric_sort_key(event.get("event_id")),
            )
        )
        events = [_bounded_candidate_state_event(event) for event in events]
        forcing_version = self._fetch_optional(
            """
            SELECT
                forcing_version_id,
                model_id,
                source_id,
                cycle_time,
                start_time,
                end_time,
                station_count,
                forcing_package_uri,
                checksum,
                lineage_json,
                created_at
            FROM met.forcing_version
            WHERE forcing_version_id = %s
               OR (source_id = %s AND cycle_time = %s AND model_id = %s)
            ORDER BY CASE WHEN forcing_version_id = %s THEN 0 ELSE 1 END, created_at DESC
            LIMIT 1
            """,
            (forcing_version_id, source_id, cycle_time, model_id, forcing_version_id),
        )
        forecast_cycle = self._fetch_optional(
            """
            SELECT
                cycle_id,
                source_id,
                cycle_time,
                issue_time,
                status,
                manifest_uri,
                retry_count,
                error_code,
                error_message,
                created_at
            FROM met.forecast_cycle
            WHERE cycle_id = %s OR (source_id = %s AND cycle_time = %s)
            ORDER BY CASE WHEN cycle_id = %s THEN 0 ELSE 1 END
            LIMIT 1
            """,
            (cycle_id, source_id, cycle_time, cycle_id),
        )
        if hydro_run is None and not jobs and forcing_version is None and forecast_cycle is None:
            return None
        candidate_jobs = [job for job in jobs if _job_belongs_to_candidate(job, run_id=run_id, model_id=model_id)]
        failed_task = _candidate_failed_task_from_events(
            events,
            model_id=model_id,
            candidate_id=candidate_id,
            run_id=run_id,
            cycle_id=cycle_id,
        )
        relevant_jobs = candidate_jobs or ([failed_task["job"]] if failed_task and failed_task.get("job") else [])
        latest_job = (relevant_jobs or jobs)[-1] if (relevant_jobs or jobs) else {}
        latest_shared_cycle_aggregate = bool(
            not candidate_jobs
            and latest_job.get("run_id") == cycle_run_id
            and latest_job.get("model_id") in (None, "")
        )
        latest_status = str(latest_job.get("status") or "")
        latest_failed_job = (
            latest_job
            if latest_status in {"failed", "submission_failed", "partially_failed", "permanently_failed"}
            else {}
        )
        latest_shared_cycle_success = bool(
            latest_shared_cycle_aggregate
            and latest_status in TERMINAL_PIPELINE_SUCCESS_STATUSES
        )
        latest_shared_cycle_failure = bool(
            latest_shared_cycle_aggregate
            and latest_status in {"failed", "submission_failed", "partially_failed", "permanently_failed"}
            and failed_task is None
        )
        exposed_latest_job = {} if latest_shared_cycle_success or latest_shared_cycle_failure else latest_job
        pipeline_status = latest_job.get("status")
        if failed_task is not None and (
            not latest_job or latest_status in {"", "partially_failed"} or latest_shared_cycle_success
        ):
            latest_failed_job = failed_task["job"] if failed_task.get("job") else latest_failed_job
            pipeline_status = latest_failed_job.get("status")
            exposed_latest_job = latest_failed_job
        elif latest_shared_cycle_success or latest_shared_cycle_failure:
            pipeline_status = None
            latest_failed_job = {}
        successful_siblings = _successful_sibling_task_count(events, model_id=model_id)
        return {
            "candidate_id": candidate_id,
            "run_id": run_id,
            "forcing_version_id": forcing_version_id,
            "retry_limit": retry_limit,
            "job_limit": job_limit,
            "event_limit": event_limit,
            "pipeline_jobs_total": jobs_total,
            "pipeline_events_total": events_total,
            "state_truncated": jobs_truncated or events_truncated,
            "hydro_run": hydro_run,
            "hydro_status": hydro_run.get("status") if hydro_run else None,
            "output_uri": hydro_run.get("output_uri") if hydro_run else None,
            "forcing_version": forcing_version,
            "forecast_cycle": forecast_cycle,
            "pipeline_jobs": jobs,
            "pipeline_events": events,
            "pipeline_status": pipeline_status,
            "stage": (
                (failed_task or {}).get("stage")
                or latest_failed_job.get("stage")
                or exposed_latest_job.get("stage")
            ),
            "failed_stage": (failed_task or {}).get("stage") or latest_failed_job.get("stage"),
            "array_task_id": (failed_task or {}).get("array_task_id"),
            "original_task_id": (failed_task or {}).get("original_task_id"),
            "hydro_truth_timestamp": hydro_run.get("updated_at") if hydro_run else None,
            "pipeline_truth_timestamp": _first_pipeline_truth_timestamp(latest_failed_job or exposed_latest_job or {}),
            "error_code": (failed_task or {}).get("error_code")
            or latest_failed_job.get("error_code")
            or exposed_latest_job.get("error_code"),
            "error_message": (failed_task or {}).get("error_message")
            or latest_failed_job.get("error_message")
            or exposed_latest_job.get("error_message"),
            "retry_count": max((int(job.get("retry_count") or 0) for job in relevant_jobs), default=0),
            "successful_sibling_outputs_reused": successful_siblings > 0,
            "successful_sibling_task_count": successful_siblings,
            "shared_cycle_aggregate": latest_shared_cycle_aggregate,
            "shared_cycle_ambiguous_failure": latest_shared_cycle_failure,
        }

    def active_slurm_jobs(
        self,
        *,
        source_id: str,
        cycle_time: datetime,
        model_id: str,
        limit: int = DEFAULT_CANDIDATE_STATE_JOB_LIMIT,
    ) -> list[dict[str, Any]]:
        cycle_id = cycle_id_for(source_id, cycle_time)
        cycle_run_id = f"cycle_{source_id.lower()}_{format_cycle_time(cycle_time)}"
        limit = max(int(limit), 1)
        return self._fetch_all(
            """
            SELECT
                pj.job_id,
                pj.run_id,
                pj.cycle_id,
                pj.job_type,
                pj.slurm_job_id,
                pj.model_id,
                pj.status,
                pj.stage,
                pj.submitted_at,
                pj.started_at,
                pj.finished_at,
                pj.exit_code,
                pj.error_code,
                pj.error_message,
                pj.log_uri
            FROM ops.pipeline_job pj
            LEFT JOIN hydro.hydro_run h ON h.run_id = pj.run_id
            WHERE pj.cycle_id = %s
              AND pj.slurm_job_id IS NOT NULL
              AND pj.status NOT IN (
                'succeeded', 'partially_failed', 'failed', 'cancelled', 'submission_failed', 'permanently_failed'
              )
              AND (
                    h.model_id = %s
                 OR pj.model_id = %s
                 OR pj.run_id = %s
                 OR pj.run_id = %s
                 OR (pj.model_id IS NULL AND pj.run_id = %s)
              )
            ORDER BY pj.submitted_at ASC NULLS LAST, pj.created_at ASC
            LIMIT %s
            """,
            (
                cycle_id,
                model_id,
                model_id,
                f"fcst_{source_id.lower()}_{format_cycle_time(cycle_time)}_{model_id}",
                cycle_run_id,
                cycle_run_id,
                limit,
            ),
        )

    def has_active_analysis_run(self, *, model_id: str, start_time: datetime, end_time: datetime) -> bool:
        row = self._fetch_optional(
            """
            SELECT 1 AS active
            FROM hydro.hydro_run h
            WHERE h.run_type = 'analysis'
              AND h.model_id = %s
              AND h.status NOT IN ('failed', 'cancelled', 'superseded')
              AND h.start_time < %s
              AND h.end_time > %s
            LIMIT 1
            """,
            (model_id, end_time, start_time),
        )
        return row is not None

    def list_canonical_ready_cycles(self, *, source_id: str | None = None, limit: int = 100) -> list[dict[str, Any]]:
        parameters: list[Any] = []
        source_filter = ""
        if source_id is not None:
            source_filter = "AND fc.source_id = %s"
            parameters.append(source_id)
        parameters.append(max(int(limit), 1))
        return self._fetch_all(
            f"""
            SELECT
                fc.source_id,
                fc.cycle_time,
                fc.cycle_id,
                MAX(cmp.lead_time_hours) AS max_lead_hours,
                COALESCE(
                    jsonb_agg(
                        jsonb_build_object(
                            'canonical_product_id', cmp.canonical_product_id,
                            'source_id', cmp.source_id,
                            'cycle_time', cmp.cycle_time,
                            'valid_time', cmp.valid_time,
                            'lead_time_hours', cmp.lead_time_hours,
                            'variable', cmp.variable,
                            'unit', cmp.unit,
                            'quality_flag', cmp.quality_flag,
                            'checksum', cmp.checksum,
                            'lineage_json', cmp.lineage_json
                        )
                        ORDER BY cmp.lead_time_hours, cmp.variable, cmp.canonical_product_id
                    ) FILTER (WHERE cmp.canonical_product_id IS NOT NULL),
                    '[]'::jsonb
                ) AS canonical_products
            FROM met.forecast_cycle fc
            LEFT JOIN met.canonical_met_product cmp
              ON cmp.source_id = fc.source_id
             AND cmp.cycle_time = fc.cycle_time
             AND cmp.quality_flag = 'ok'
             AND NULLIF(BTRIM(cmp.checksum), '') IS NOT NULL
            WHERE fc.status = 'canonical_ready'
              {source_filter}
            GROUP BY fc.source_id, fc.cycle_time, fc.cycle_id
            ORDER BY fc.cycle_time ASC, fc.source_id ASC
            LIMIT %s
            """,
            tuple(parameters),
        )

    def list_forecast_model_ids(self) -> list[str]:
        rows = self._fetch_all(
            """
            SELECT model_id
            FROM core.model_instance
            WHERE active_flag = true
              AND lifecycle_state = 'active'
            ORDER BY model_id
            """,
            (),
        )
        return [str(row["model_id"]) for row in rows]

    def load_model_context(self, model_id: str) -> ModelContext:
        row = self._fetch_one(
            """
            SELECT
                mi.model_id,
                bv.basin_id,
                mi.basin_version_id,
                mi.river_network_version_id,
                rn.segment_count,
                mi.resource_profile,
                mi.model_package_uri
            FROM core.model_instance mi
            JOIN core.basin_version bv ON bv.basin_version_id = mi.basin_version_id
            JOIN core.river_network_version rn ON rn.river_network_version_id = mi.river_network_version_id
            WHERE mi.model_id = %s
            """,
            (model_id,),
            missing_code="MODEL_NOT_FOUND",
            missing_message=f"model_instance not found: {model_id}",
        )
        return ModelContext(
            model_id=str(row["model_id"]),
            basin_id=row.get("basin_id"),
            basin_version_id=str(row["basin_version_id"]),
            river_network_version_id=str(row["river_network_version_id"]),
            segment_count=int(row["segment_count"]),
            model_package_uri=str(row["model_package_uri"]),
            output_segment_count=_first_optional_int(
                _nested_mapping(row.get("resource_profile")).get("output_segment_count"),
                row["segment_count"],
            ),
        )

    def find_forcing_context(self, *, source_id: str, cycle_time: datetime, model_id: str) -> ForcingContext:
        row = self._fetch_optional(
            """
            SELECT forcing_version_id, forcing_package_uri, start_time, end_time, source_id, lineage_json
            FROM met.forcing_version
            WHERE source_id = %s
              AND cycle_time = %s
              AND model_id = %s
            ORDER BY created_at DESC
            LIMIT 1
            """,
            (source_id, cycle_time, model_id),
        )
        if row is None:
            return ForcingContext(None, None)
        return ForcingContext(
            row.get("forcing_version_id"),
            row.get("forcing_package_uri"),
            row.get("start_time"),
            row.get("end_time"),
            row.get("source_id"),
            _max_lead_hours_from_lineage(row.get("lineage_json")),
        )

    def ensure_forecast_cycle(self, *, source_id: str, cycle_time: datetime) -> dict[str, Any]:
        return self._fetch_one(
            """
            INSERT INTO met.forecast_cycle (cycle_id, source_id, cycle_time, issue_time, status)
            VALUES (%s, %s, %s, %s, 'discovered')
            ON CONFLICT (source_id, cycle_time) DO UPDATE SET
                issue_time = COALESCE(met.forecast_cycle.issue_time, EXCLUDED.issue_time)
            RETURNING *
            """,
            (cycle_id_for(source_id, cycle_time), source_id, cycle_time, cycle_time),
        )

    def create_hydro_run(
        self,
        context: ForecastRunContext | AnalysisRunContext,
        manifest: dict[str, Any],
    ) -> dict[str, Any]:
        init_state_id = getattr(context, "init_state_id", None) or manifest.get("initial_state", {}).get("state_id")
        return self._fetch_one(
            """
            INSERT INTO hydro.hydro_run (
                run_id,
                run_type,
                scenario_id,
                model_id,
                basin_version_id,
                forcing_version_id,
                init_state_id,
                source_id,
                cycle_time,
                start_time,
                end_time,
                status,
                run_manifest_uri,
                output_uri,
                log_uri
            )
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, 'created', %s, %s, %s)
            ON CONFLICT (run_id) DO UPDATE SET
                status = 'created',
                forcing_version_id = EXCLUDED.forcing_version_id,
                init_state_id = EXCLUDED.init_state_id,
                run_manifest_uri = EXCLUDED.run_manifest_uri,
                output_uri = EXCLUDED.output_uri,
                log_uri = EXCLUDED.log_uri,
                error_code = NULL,
                error_message = NULL,
                updated_at = now()
            WHERE hydro.hydro_run.status IN ('failed', 'cancelled')
            RETURNING *
            """,
            (
                context.run_id,
                manifest.get("run_type", "forecast"),
                manifest["scenario_id"],
                context.model_id,
                context.basin_version_id,
                context.forcing_version_id,
                init_state_id,
                context.source_id,
                context.cycle_time,
                context.start_time,
                context.end_time,
                context.run_manifest_uri,
                context.output_uri,
                context.log_uri,
            ),
            missing_code="HYDRO_RUN_NOT_RETRIABLE",
            missing_message=f"hydro_run already exists and is not retriable: {context.run_id}",
        )

    def create_hydro_run_from_basin(
        self,
        basin: Mapping[str, Any],
        manifest: dict[str, Any],
    ) -> dict[str, Any]:
        run_id = str(manifest["run_id"])
        model = _coerce_mapping(manifest["model"])
        forcing = _coerce_mapping(manifest.get("forcing") or {})
        outputs = _coerce_mapping(manifest.get("outputs") or {})
        initial_state = _coerce_mapping(manifest.get("initial_state") or {})
        statement = """
            INSERT INTO hydro.hydro_run (
                run_id,
                run_type,
                scenario_id,
                model_id,
                basin_version_id,
                forcing_version_id,
                init_state_id,
                source_id,
                cycle_time,
                start_time,
                end_time,
                status,
                run_manifest_uri,
                output_uri,
                log_uri
            )
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, 'created', %s, %s, %s)
            ON CONFLICT (run_id) DO UPDATE SET
                status = 'created',
                forcing_version_id = EXCLUDED.forcing_version_id,
                init_state_id = EXCLUDED.init_state_id,
                run_manifest_uri = EXCLUDED.run_manifest_uri,
                output_uri = EXCLUDED.output_uri,
                log_uri = EXCLUDED.log_uri,
                error_code = NULL,
                error_message = NULL,
                updated_at = now()
            WHERE hydro.hydro_run.status IN ('failed', 'cancelled')
            RETURNING *
            """
        parameters = (
            run_id,
            manifest.get("run_type", "forecast"),
            manifest["scenario_id"],
            model["model_id"],
            model["basin_version_id"],
            forcing.get("forcing_version_id"),
            initial_state.get("state_id") or basin.get("init_state_id"),
            manifest.get("source_id") or basin.get("source_id"),
            parse_cycle_time(manifest["cycle_time"]),
            _parse_gateway_time(manifest["start_time"]),
            _parse_gateway_time(manifest["end_time"]),
            outputs.get("run_manifest_uri"),
            outputs.get("output_uri"),
            outputs.get("log_uri"),
        )
        try:
            return self._fetch_one(
                statement,
                parameters,
                missing_code="HYDRO_RUN_NOT_RETRIABLE",
                missing_message=f"hydro_run already exists and is not retriable: {run_id}",
            )
        except OrchestratorError as exc:
            if exc.error_code != "HYDRO_RUN_NOT_RETRIABLE":
                raise
            return self._fetch_one(
                "SELECT * FROM hydro.hydro_run WHERE run_id = %s",
                (run_id,),
                missing_code="HYDRO_RUN_NOT_FOUND",
                missing_message=f"hydro_run not found after conflict: {run_id}",
            )

    def update_hydro_run_status(
        self,
        run_id: str,
        status: str,
        *,
        slurm_job_id: str | None = None,
        error_code: str | None = None,
        error_message: str | None = None,
    ) -> dict[str, Any]:
        assignments = ["status = %s", "updated_at = now()"]
        parameters: list[Any] = [status]
        for column, value in (
            ("slurm_job_id", slurm_job_id),
            ("error_code", error_code),
            ("error_message", error_message),
        ):
            if value is not None:
                assignments.append(f"{column} = %s")
                parameters.append(value)
        parameters.append(run_id)
        return self._fetch_one(
            f"""
            UPDATE hydro.hydro_run
            SET {", ".join(assignments)}
            WHERE run_id = %s
            RETURNING *
            """,
            tuple(parameters),
        )

    def upsert_pipeline_job(self, record: dict[str, Any]) -> dict[str, Any]:
        return self._fetch_one(
            """
            INSERT INTO ops.pipeline_job (
                job_id,
                run_id,
                cycle_id,
                job_type,
                slurm_job_id,
                array_task_id,
                model_id,
                status,
                stage,
                submitted_at,
                started_at,
                finished_at,
                exit_code,
                error_code,
                error_message,
                log_uri
            )
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (job_id) DO UPDATE SET
                slurm_job_id = EXCLUDED.slurm_job_id,
                array_task_id = EXCLUDED.array_task_id,
                model_id = EXCLUDED.model_id,
                status = EXCLUDED.status,
                submitted_at = EXCLUDED.submitted_at,
                started_at = EXCLUDED.started_at,
                finished_at = EXCLUDED.finished_at,
                exit_code = EXCLUDED.exit_code,
                error_code = EXCLUDED.error_code,
                error_message = EXCLUDED.error_message,
                log_uri = EXCLUDED.log_uri,
                updated_at = now()
            RETURNING *
            """,
            (
                record["job_id"],
                record["run_id"],
                record["cycle_id"],
                record["job_type"],
                record["slurm_job_id"],
                record.get("array_task_id"),
                record.get("model_id"),
                record["status"],
                record["stage"],
                record["submitted_at"],
                record["started_at"],
                record["finished_at"],
                record["exit_code"],
                record["error_code"],
                record["error_message"],
                record["log_uri"],
            ),
        )

    def reserve_pipeline_job(self, record: dict[str, Any]) -> dict[str, Any] | None:
        """Phase 1: durable reservation row keyed by idempotency_key.

        ``ON CONFLICT DO NOTHING RETURNING`` (absorbing any unique conflict) is
        the authoritative win/lose signal: a returned row means THIS call
        inserted the reservation (won the race); ``None`` means a row already
        existed (lost / already in-flight). The caller never decides the winner
        by comparing a deterministic job_id — only the presence of the RETURNING
        row counts.

        The conflict target is deliberately omitted. A narrow
        ``ON CONFLICT (idempotency_key)`` only absorbs idempotency_key clashes;
        a pre-existing row carrying the SAME job_id but a NULL idempotency_key
        (a legacy / non-reserve row) would slip past the partial index and hit
        the job_id primary key instead, raising and aborting the whole pass. The
        protocol contract is "reserve never raises; RETURNING decides" — so we
        absorb ANY unique conflict (idempotency_key unique index OR job_id PK):
        any clash → DO NOTHING → zero rows → ``None`` → judged a loss, never an
        exception.

        ``submitted_at`` is deliberately left NULL at reserve time; it is stamped
        only when the reservation is bound to a real slurm_job_id (phase 2).
        """

        return self._fetch_optional(
            """
            INSERT INTO ops.pipeline_job (
                job_id, run_id, cycle_id, job_type, model_id, stage,
                status, idempotency_key, candidate_id
            )
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT DO NOTHING
            RETURNING *
            """,
            (
                record["job_id"],
                record.get("run_id"),
                record.get("cycle_id"),
                record["job_type"],
                record.get("model_id"),
                record.get("stage"),
                record.get("status", "reserved"),
                record["idempotency_key"],
                record.get("candidate_id"),
            ),
        )

    def bind_pipeline_job_reservation(
        self,
        idempotency_key: str,
        *,
        slurm_job_id: str,
        status: str = "submitted",
        array_task_id: int | None = None,
    ) -> dict[str, Any] | None:
        """Phase 2: atomically bind slurm_job_id; no-op if already bound."""

        return self._fetch_optional(
            """
            UPDATE ops.pipeline_job
            SET slurm_job_id = %s,
                array_task_id = COALESCE(%s, array_task_id),
                status = %s,
                submitted_at = COALESCE(submitted_at, now()),
                updated_at = now()
            WHERE idempotency_key = %s
              AND slurm_job_id IS NULL
            RETURNING *
            """,
            (slurm_job_id, array_task_id, status, idempotency_key),
        )

    def query_candidate_state(self, idempotency_key: str) -> dict[str, Any] | None:
        return self._fetch_optional(
            "SELECT * FROM ops.pipeline_job WHERE idempotency_key = %s",
            (idempotency_key,),
        )

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
        current = self._fetch_optional("SELECT status FROM ops.pipeline_job WHERE job_id = %s", (job_id,))
        previous_status = current.get("status") if current is not None else None
        record = self._fetch_optional(
            """
            UPDATE ops.pipeline_job
            SET status = %s,
                started_at = COALESCE(%s, started_at),
                finished_at = COALESCE(%s, finished_at),
                exit_code = COALESCE(%s, exit_code),
                error_code = COALESCE(%s, error_code),
                error_message = COALESCE(%s, error_message),
                log_uri = COALESCE(%s, log_uri),
                updated_at = now()
            WHERE job_id = %s
              AND status <> 'permanently_failed'
              AND (
                    status NOT IN ('succeeded', 'failed', 'cancelled')
                 OR %s IN ('partially_failed', 'permanently_failed')
              )
            RETURNING *
            """,
            (status, started_at, finished_at, exit_code, error_code, error_message, log_uri, job_id, status),
        )
        if record is None:
            record = self._fetch_one(
                "SELECT * FROM ops.pipeline_job WHERE job_id = %s",
                (job_id,),
                missing_code="PIPELINE_JOB_NOT_FOUND",
                missing_message=f"pipeline_job not found: {job_id}",
            )
        return previous_status, record

    def get_pipeline_job(self, job_id: str) -> dict[str, Any] | None:
        return self._fetch_optional("SELECT * FROM ops.pipeline_job WHERE job_id = %s", (job_id,))

    def query_pipeline_jobs_by_cycle(self, cycle_id: str) -> list[dict[str, Any]]:
        return self._fetch_all(
            """
            SELECT *
            FROM ops.pipeline_job
            WHERE cycle_id = %s
            ORDER BY submitted_at ASC NULLS LAST, created_at ASC
            """,
            (cycle_id,),
        )

    def query_pipeline_jobs_by_run(self, run_id: str) -> list[dict[str, Any]]:
        return self._fetch_all(
            """
            SELECT *
            FROM ops.pipeline_job
            WHERE run_id = %s
            ORDER BY submitted_at ASC NULLS LAST, created_at ASC
            """,
            (run_id,),
        )

    def query_pipeline_job_by_slurm_id(self, slurm_job_id: str) -> dict[str, Any] | None:
        return self._fetch_optional(
            "SELECT * FROM ops.pipeline_job WHERE slurm_job_id = %s LIMIT 1",
            (slurm_job_id,),
        )

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
        try:
            from psycopg2.extras import Json
        except ImportError as error:
            raise OrchestratorError("PSYCOPG2_MISSING", "psycopg2 is required for orchestration.") from error

        return self._fetch_one(
            """
            INSERT INTO ops.pipeline_event (
                entity_type,
                entity_id,
                event_type,
                status_from,
                status_to,
                message,
                details
            )
            VALUES (%s, %s, %s, %s, %s, %s, %s)
            RETURNING *
            """,
            (entity_type, entity_id, event_type, status_from, status_to, message, Json(details or {})),
        )

    def update_forecast_cycle_status(
        self,
        *,
        source_id: str,
        cycle_time: datetime,
        status: str,
        error_code: str | None = None,
        error_message: str | None = None,
    ) -> dict[str, Any] | None:
        return self._fetch_optional(
            """
            UPDATE met.forecast_cycle
            SET status = %s,
                error_code = %s,
                error_message = %s
            WHERE source_id = %s
              AND cycle_time = %s
            RETURNING *
            """,
            (status, error_code, error_message, source_id, cycle_time),
        )

    def list_stage_statuses(
        self,
        *,
        source_id: str | None,
        cycle_time: datetime,
        model_id: str | None = None,
    ) -> list[dict[str, Any]]:
        parameters: list[Any] = [cycle_time]
        filters = ["fc.cycle_time = %s"]
        if source_id is not None:
            filters.append("fc.source_id = %s")
            parameters.append(source_id)
        if model_id is not None:
            filters.append("h.model_id = %s")
            parameters.append(model_id)
        return self._fetch_all(
            f"""
            SELECT
                pj.job_id,
                pj.run_id,
                pj.cycle_id,
                pj.job_type,
                pj.slurm_job_id,
                pj.model_id,
                pj.status,
                pj.stage,
                pj.submitted_at,
                pj.started_at,
                pj.finished_at,
                pj.exit_code,
                pj.error_code,
                pj.error_message,
                pj.log_uri
            FROM ops.pipeline_job pj
            JOIN met.forecast_cycle fc ON fc.cycle_id = pj.cycle_id
            LEFT JOIN hydro.hydro_run h ON h.run_id = pj.run_id
            WHERE {" AND ".join(filters)}
            ORDER BY CASE pj.stage
                WHEN 'download' THEN 1
                WHEN 'convert' THEN 2
                WHEN 'forcing' THEN 3
                WHEN 'forecast' THEN 4
                WHEN 'parse' THEN 5
                WHEN 'frequency' THEN 6
                WHEN 'publish' THEN 7
                WHEN 'download_gfs' THEN 1
                WHEN 'convert_canonical' THEN 2
                WHEN 'produce_forcing' THEN 3
                WHEN 'run_shud_forecast' THEN 4
                WHEN 'parse_output' THEN 15
                WHEN 'era5_download' THEN 11
                WHEN 'canonical_convert' THEN 12
                WHEN 'forcing_produce' THEN 13
                WHEN 'analysis_run' THEN 14
                WHEN 'state_save_qc' THEN 16
                ELSE 99
            END
            """,
            tuple(parameters),
        )

    def _fetch_one(
        self,
        statement: str,
        parameters: tuple[Any, ...],
        *,
        missing_code: str = "DATABASE_ROW_MISSING",
        missing_message: str = "Database operation did not return a row.",
    ) -> dict[str, Any]:
        row = self._fetch_optional(statement, parameters)
        if row is None:
            raise OrchestratorError(missing_code, missing_message)
        return row

    def _fetch_optional(self, statement: str, parameters: tuple[Any, ...]) -> dict[str, Any] | None:
        rows = self._fetch_all(statement, parameters)
        return rows[0] if rows else None

    def _fetch_all(self, statement: str, parameters: tuple[Any, ...]) -> list[dict[str, Any]]:
        try:
            import psycopg2
        except ImportError as error:
            raise OrchestratorError("PSYCOPG2_MISSING", "psycopg2 is required for orchestration.") from error

        connection = None
        try:
            connection = psycopg2.connect(self.database_url)
            connection.autocommit = False
            with connection.cursor() as cursor:
                cursor.execute(statement, parameters)
                if cursor.description is None:
                    connection.commit()
                    return []
                rows = cursor.fetchall()
                columns = [description.name for description in cursor.description]
                connection.commit()
                return [dict(zip(columns, row, strict=True)) for row in rows]
        except psycopg2.Error as error:
            if connection is not None:
                connection.rollback()
            raise OrchestratorError(
                "ORCHESTRATOR_DB_ERROR",
                f"Orchestrator database operation failed: {error}",
            ) from error
        finally:
            if connection is not None:
                connection.close()


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


def _job_belongs_to_candidate(job: Mapping[str, Any], *, run_id: str, model_id: str) -> bool:
    if str(job.get("run_id") or "") == run_id:
        return True
    return str(job.get("model_id") or "") == model_id


def _first_pipeline_truth_timestamp(job: Mapping[str, Any]) -> Any:
    for key in ("updated_at", "finished_at", "submitted_at", "started_at", "created_at"):
        value = job.get(key)
        if value not in (None, ""):
            return value
    return None


def _pipeline_job_truth_sort_key(job: Mapping[str, Any]) -> datetime:
    return _datetime_sort_key(_first_pipeline_truth_timestamp(job))


def _datetime_sort_key(value: Any) -> datetime:
    parsed = _parse_gateway_time(value)
    if parsed is None:
        return datetime.min.replace(tzinfo=UTC)
    return parsed


def _numeric_sort_key(value: Any) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def _task_model_id(task: Mapping[str, Any]) -> str | None:
    value = task.get("model_id") or task.get("candidate_model_id")
    return str(value) if value not in (None, "") else None


def _task_candidate_id(task: Mapping[str, Any]) -> str | None:
    value = task.get("candidate_id")
    return str(value) if value not in (None, "") else None


def _task_identity_key(
    task: Mapping[str, Any],
    *,
    model_id: str,
    candidate_id: str | None = None,
) -> tuple[str, str, str] | None:
    task_candidate_id = _task_candidate_id(task)
    task_model_id = _task_model_id(task)
    if task_candidate_id is not None and candidate_id is not None and task_candidate_id != candidate_id:
        return None
    if task_candidate_id is not None and task_model_id is not None and task_model_id != model_id:
        return None
    if task_candidate_id is None and task_model_id != model_id:
        return None
    task_id = task.get("original_task_id", task.get("array_task_id", task.get("task_id")))
    if task_id in (None, ""):
        return None
    return (task_candidate_id or task_model_id or model_id, str(task_id), str(task.get("stage") or ""))


def _event_task_truth_sort_key(
    event: Mapping[str, Any],
    task: Mapping[str, Any],
    *,
    order: int,
    task_order: int,
) -> tuple[datetime, int, int, int]:
    timestamp = _parse_gateway_time(
        task.get("updated_at")
        or task.get("finished_at")
        or task.get("created_at")
        or event.get("created_at")
        or event.get("updated_at")
    ) or datetime.min.replace(tzinfo=UTC)
    return (
        timestamp,
        _numeric_sort_key(event.get("event_id")),
        order,
        task_order,
    )


def _candidate_failed_task_from_events(
    events: Sequence[Mapping[str, Any]],
    *,
    model_id: str,
    candidate_id: str | None = None,
    run_id: str | None = None,
    cycle_id: str | None = None,
) -> dict[str, Any] | None:
    latest_by_identity: dict[
        tuple[str, str, str],
        tuple[tuple[datetime, int, int, int], Mapping[str, Any], Mapping[str, Any]],
    ] = {}
    for order, event in enumerate(events):
        details = event.get("details")
        if not isinstance(details, Mapping):
            continue
        for task_order, task in enumerate(_bounded_candidate_state_task_results(details)):
            key = _task_identity_key(task, model_id=model_id, candidate_id=candidate_id)
            if key is None:
                continue
            truth_key = _event_task_truth_sort_key(event, task, order=order, task_order=task_order)
            previous = latest_by_identity.get(key)
            if previous is None or truth_key > previous[0]:
                latest_by_identity[key] = (truth_key, event, task)
    latest_failures: list[tuple[tuple[datetime, int, int, int], Mapping[str, Any], Mapping[str, Any]]] = []
    for truth_key, event, task in latest_by_identity.values():
        status = str(task.get("status") or task.get("state") or "")
        if status in {"", "succeeded"}:
            continue
        latest_failures.append((truth_key, event, task))
    if not latest_failures:
        return None
    _truth_key, event, task = max(latest_failures, key=lambda item: item[0])
    details = event.get("details") if isinstance(event.get("details"), Mapping) else {}
    stage = task.get("stage") or details.get("stage")
    return {
        "job": {
            "job_id": event.get("entity_id"),
            "run_id": run_id,
            "cycle_id": cycle_id,
            "model_id": model_id,
            "status": event.get("status_to") or task.get("status") or task.get("state"),
            "stage": stage,
            "job_type": details.get("job_type"),
            "error_code": task.get("error_code") or details.get("error_code") or "NODE_FAILURE",
            "error_message": task.get("error_message") or details.get("error_message"),
            "retry_count": task.get("retry_count") or details.get("retry_count"),
            "created_at": event.get("created_at"),
        },
        "stage": stage,
        "array_task_id": task.get("array_task_id", task.get("task_id")),
        "original_task_id": task.get("original_task_id", task.get("array_task_id", task.get("task_id"))),
        "error_code": task.get("error_code") or details.get("error_code") or "NODE_FAILURE",
        "error_message": task.get("error_message") or details.get("error_message"),
    }


def _successful_sibling_task_count(events: Sequence[Mapping[str, Any]], *, model_id: str) -> int:
    count = 0
    for event in events:
        details = event.get("details")
        if not isinstance(details, Mapping):
            continue
        for task in _bounded_candidate_state_task_results(details):
            if str(task.get("status") or task.get("state") or "") != "succeeded":
                continue
            task_model_id = _task_model_id(task)
            if task_model_id is None or task_model_id == model_id:
                continue
            count += 1
    return count


def _bounded_candidate_state_event(event: Mapping[str, Any]) -> dict[str, Any]:
    payload = dict(event)
    details = payload.get("details")
    if not isinstance(details, Mapping):
        return payload
    details_payload = dict(details)
    task_sample = _bounded_candidate_state_task_result_sample(details_payload)
    if task_sample is not None:
        task_rows, task_metadata = task_sample
        details_payload["task_results"] = task_rows
        details_payload.update(task_metadata)
    payload["details"] = details_payload
    return payload


def _bounded_candidate_state_task_results(details: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    task_sample = _bounded_candidate_state_task_result_sample(details)
    if task_sample is None:
        return []
    return task_sample[0]


def _bounded_candidate_state_task_result_sample(
    details: Mapping[str, Any],
) -> tuple[list[Mapping[str, Any]], dict[str, Any]] | None:
    task_results = details.get("task_results")
    if not isinstance(task_results, Sequence) or isinstance(task_results, str | bytes | bytearray):
        return None
    task_rows: list[Mapping[str, Any]] = []
    observed_count = 0
    overflow = False
    for index, task in enumerate(task_results):
        observed_count = index + 1
        if index >= MAX_CANDIDATE_STATE_TASK_RESULTS:
            overflow = True
            break
        if isinstance(task, Mapping):
            task_rows.append(dict(task))
    reported_total = _coerce_optional_nonnegative_int(details.get("task_results_total"))
    total = max(reported_total, observed_count) if reported_total is not None else observed_count
    included = len(task_rows)
    overflow = overflow or total > included
    metadata: dict[str, Any] = {
        "task_results_total": total,
        "task_results_included": included,
        "task_results_limit": MAX_CANDIDATE_STATE_TASK_RESULTS,
        "task_results_overflow": overflow,
    }
    if overflow:
        metadata["task_results_omitted"] = max(total - included, 0)
    return task_rows, metadata


def _coerce_int(value: Any, *, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _coerce_optional_nonnegative_int(value: Any) -> int | None:
    try:
        number = int(value)
    except (TypeError, ValueError):
        return None
    if number < 0:
        return None
    return number


def _cycle_payload_model_id(context: CycleOrchestrationContext) -> str:
    if context.active_basins:
        return str(context.active_basins[0].get("model_id") or "cycle")
    return "cycle"


def _cycle_pipeline_job_model_id(context: CycleOrchestrationContext) -> str | None:
    if len(context.all_basins) != 1:
        return None
    return str(context.all_basins[0].get("model_id") or "") or None


def _cycle_orchestration_run_id(
    source_id: str,
    cycle_time: datetime,
    basins: Sequence[Mapping[str, Any]],
) -> str:
    run_ids = {
        str(basin.get("orchestration_run_id"))
        for basin in basins
        if basin.get("orchestration_run_id") not in (None, "")
    }
    if len(run_ids) == 1:
        return run_ids.pop()
    return f"cycle_{source_id.lower()}_{format_cycle_time(cycle_time)}"


def _active_orchestration_conflicts(
    repository: OrchestratorRepository,
    *,
    source_id: str,
    cycle_time: datetime,
    cycle_id: str,
    run_id: str,
    basins: Sequence[Mapping[str, Any]],
) -> bool:
    if _candidate_scoped_cycle_execution(basins):
        for job in repository.query_pipeline_jobs_by_run(run_id):
            if _is_active_pipeline_job(job):
                return True
        return False
    if repository.has_active_orchestration(source_id=source_id, cycle_time=cycle_time):
        return True
    return any(_is_active_pipeline_job(job) for job in repository.query_pipeline_jobs_by_cycle(cycle_id))


def _in_memory_active_cycle_conflicts(
    cycle_id: str,
    active_cycles: set[str],
    basins: Sequence[Mapping[str, Any]],
) -> bool:
    return cycle_id in active_cycles and not _candidate_scoped_cycle_execution(basins)


def _candidate_scoped_cycle_execution(basins: Sequence[Mapping[str, Any]]) -> bool:
    return len(basins) == 1 and _restart_stage_from_basins(basins) is not None


def _is_active_pipeline_job(job: Mapping[str, Any]) -> bool:
    return str(job.get("status") or "") not in TERMINAL_JOB_STATUSES


def _restart_stage_from_basins(basins: Sequence[Mapping[str, Any]]) -> str | None:
    restart_stages: list[str] = []
    for basin in basins:
        restart_stage = _canonical_restart_stage(basin.get("restart_stage"))
        if restart_stage is not None:
            restart_stages.append(restart_stage)
            continue
        state_evidence = basin.get("state_evidence")
        if isinstance(state_evidence, Mapping):
            restart_stage = _canonical_restart_stage(
                state_evidence.get("restart_stage") or state_evidence.get("restart_from_stage")
            )
            if restart_stage is not None:
                restart_stages.append(restart_stage)
    if not restart_stages:
        return None
    stage_order = {stage.stage: index for index, stage in enumerate(STAGES)}
    return min(restart_stages, key=lambda stage: stage_order.get(stage, len(stage_order)))


def _canonical_restart_stage(value: Any) -> str | None:
    if value in (None, ""):
        return None
    normalized = str(value)
    aliases = {
        "parse_output": "parse",
        "compute_frequency": "frequency",
        "publish_tiles": "publish",
    }
    normalized = aliases.get(normalized, normalized)
    allowed = {stage.stage for stage in STAGES}
    return normalized if normalized in allowed else None


def _restart_stage_index(restart_stage: str | None, stages: Sequence[StageDefinition]) -> int:
    if restart_stage is None:
        return 0
    for index, stage in enumerate(stages):
        if stage.stage == restart_stage:
            return index
    return 0


def _basin_key(basin: Mapping[str, Any]) -> tuple[str, str]:
    return (str(basin.get("model_id") or ""), str(basin.get("basin_id") or basin.get("model_id") or ""))


def _basin_identifier(basin: Mapping[str, Any]) -> str:
    return str(basin.get("basin_id") or basin.get("model_id") or "")


def _basin_original_task_id(basin: Mapping[str, Any], fallback: int) -> int:
    try:
        return int(basin.get("original_task_id", basin.get("task_id", fallback)))
    except (TypeError, ValueError):
        return fallback


def _record_array_task_outcomes(
    context: CycleOrchestrationContext,
    *,
    stage: str,
    aggregation: ArrayAggregation,
) -> None:
    basins_by_task = {
        int(basin.get("task_id", index)): dict(basin) for index, basin in enumerate(context.active_basins)
    }
    for task in aggregation.task_results:
        basin = basins_by_task.get(task.task_id)
        if basin is None:
            continue
        original_task_id = _basin_original_task_id(basin, task.task_id)
        if task.status == "succeeded":
            previous = context.task_outcomes.get(original_task_id)
            if previous is None or previous.get("status") == "active":
                context.task_outcomes[original_task_id] = _safe_candidate_outcome_payload(
                    {
                        "status": "active",
                        "stage": stage,
                        "task_id": task.task_id,
                        "original_task_id": original_task_id,
                        "slurm_job_id": task.slurm_job_id,
                        "exit_code": task.exit_code,
                        "log_uri": task.log_uri,
                        "accounting": dict(task.accounting),
                    }
                )
            continue
        context.task_outcomes[original_task_id] = _safe_candidate_outcome_payload(
            {
                "status": task.status if task.status in {"failed", "cancelled"} else "unavailable",
                "stage": stage,
                "task_id": task.task_id,
                "original_task_id": original_task_id,
                "slurm_job_id": task.slurm_job_id,
                "exit_code": task.exit_code,
                "log_uri": task.log_uri,
                "accounting": dict(task.accounting),
                "reason": f"{stage}_task_{task.status}",
            }
        )


def _candidate_outcomes(context: CycleOrchestrationContext, *, final_status: str) -> tuple[dict[str, Any], ...]:
    active_keys = {_basin_key(basin) for basin in context.active_basins}
    outcomes: list[dict[str, Any]] = []
    for index, basin in enumerate(context.all_basins):
        original_task_id = _basin_original_task_id(basin, index)
        task_outcome = dict(context.task_outcomes.get(original_task_id) or {})
        is_active = _basin_key(basin) in active_keys
        status = str(task_outcome.get("status") or ("active" if is_active else "unavailable"))
        if final_status == "failed" and is_active and status == "active":
            status = "failed"
        reason = task_outcome.get("reason")
        if reason is None and not is_active:
            reason = str(task_outcome.get("stage") or "array_stage") + "_task_excluded"
        outcomes.append(
            _safe_candidate_outcome_payload(
                {
                    "candidate_id": basin.get("candidate_id"),
                    "run_id": basin.get("run_id"),
                    "model_id": basin.get("model_id"),
                    "basin_id": basin.get("basin_id"),
                    "basin_version_id": basin.get("basin_version_id"),
                    "river_network_version_id": basin.get("river_network_version_id"),
                    "task_id": int(basin.get("task_id", index)),
                    "original_task_id": original_task_id,
                    "status": status,
                    "reason": reason,
                    "failed_stage": (
                        task_outcome.get("stage") if status in {"failed", "cancelled", "unavailable"} else None
                    ),
                    "slurm_job_id": task_outcome.get("slurm_job_id"),
                    "exit_code": task_outcome.get("exit_code"),
                    "log_uri": task_outcome.get("log_uri"),
                    "accounting": task_outcome.get("accounting") or {},
                }
            )
        )
    return tuple(outcomes)


def _safe_candidate_outcome_payload(payload: Mapping[str, Any]) -> dict[str, Any]:
    redacted = redact_payload(_json_safe_pipeline_event_value(payload))
    return dict(redacted) if isinstance(redacted, Mapping) else {}


def build_reindexed_manifest(
    entries: Sequence[Mapping[str, Any]],
    succeeded_task_ids: Sequence[int],
) -> list[dict[str, Any]]:
    by_task_id = {int(entry.get("task_id", index)): dict(entry) for index, entry in enumerate(entries)}
    reindexed: list[dict[str, Any]] = []
    for new_task_id, previous_task_id in enumerate(succeeded_task_ids):
        entry = dict(by_task_id[int(previous_task_id)])
        entry["task_id"] = new_task_id
        entry["original_task_id"] = int(entry.get("original_task_id", previous_task_id))
        reindexed.append(entry)
    return reindexed


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
    source_id = normalize_source_id(source_id)
    cycle_time = _ensure_utc(cycle_time)
    compact_cycle = format_cycle_time(cycle_time)
    model_id = str(basin["model_id"])
    run_id = str(basin.get("run_id") or f"fcst_{source_id.lower()}_{compact_cycle}_{model_id}")
    forcing_version_id = str(
        basin.get("forcing_version_id") or f"forc_{source_id.lower()}_{compact_cycle}_{model_id}"
    )
    basin_version_id = str(basin["basin_version_id"])
    river_network_version_id = str(basin["river_network_version_id"])
    forecast_horizon_hours = int(
        basin.get("forecast_horizon_hours")
        or basin.get("max_lead_hours")
        or default_forecast_horizon_hours
    )
    start_time = cycle_time
    end_time = start_time + timedelta(hours=forecast_horizon_hours)
    model_package_uri = str(basin.get("model_package_uri") or f"models/{model_id}/")
    forcing_uri = str(
        basin.get("forcing_package_uri")
        or basin.get("forcing_uri")
        or _default_forcing_uri(source_id, compact_cycle, basin_version_id, model_id, object_store)
    )
    output_uri = _preserve_directory_uri(
        str(basin.get("output_uri")) if basin.get("output_uri") not in (None, "") else None,
        object_store,
        f"runs/{run_id}/output/",
    )
    run_manifest_uri = str(
        basin.get("run_manifest_uri") or object_store.uri_for_key(f"runs/{run_id}/input/manifest.json")
    )
    log_uri = _preserve_directory_uri(
        str(basin.get("log_uri")) if basin.get("log_uri") not in (None, "") else None,
        object_store,
        f"runs/{run_id}/logs/",
    )
    candidate_id = str(basin.get("candidate_id") or f"{source_id}:{_format_time(cycle_time)}:{model_id}:{scenario_id}")
    station_metadata = _station_metadata_for_basin(basin)
    output_river = _output_river_contract(basin)
    frequency = _frequency_contract(basin)
    display = _display_contract(basin, output_uri=output_uri)
    quality_states, blockers = _assembly_quality_states(
        basin,
        station_metadata=station_metadata,
        output_river=output_river,
        frequency=frequency,
        display=display,
    )
    runtime = {
        "command_style": str(
            basin.get("shud_command_style")
            or _nested_mapping(basin.get("runtime")).get("command_style")
            or "shud_project"
        ),
        "project_name": _project_name_for_basin(basin, fallback=model_id),
        "output_interval_minutes": int(
            basin.get("output_interval_minutes")
            or _nested_mapping(basin.get("runtime")).get("output_interval_minutes")
            or 60
        ),
        "threads": int(
            basin.get("shud_threads")
            or _nested_mapping(basin.get("resource_profile")).get("shud_threads")
            or _nested_mapping(basin.get("runtime")).get("threads")
            or 1
        ),
        "mode": "native_shud_project",
        "output_river": output_river,
    }
    identity = {
        "schema_version": PRODUCTION_CONTRACT_SCHEMA_VERSION,
        "contract_id": PRODUCTION_CONTRACT_ID,
        "candidate_id": candidate_id,
        "run_id": run_id,
        "hydro_run_id": str(basin.get("hydro_run_id") or run_id),
        "published_manifest_id": str(basin.get("published_manifest_id") or f"manifest_{run_id}"),
        "canonical_product_id": str(
            basin.get("canonical_product_id") or f"canon_{source_id.lower()}_{compact_cycle}"
        ),
        "forcing_version_id": forcing_version_id,
        "source": source_id,
        "source_id": source_id,
        "cycle_id": cycle_id,
        "cycle_time": _format_time(cycle_time),
        "scenario_id": scenario_id,
        "model_id": model_id,
        "basin_id": basin.get("basin_id"),
        "basin_version_id": basin_version_id,
        "river_network_version_id": river_network_version_id,
        "model_package_uri": model_package_uri,
        "model_package_manifest_uri": _model_package_manifest_uri(basin, model_package_uri),
        "model_package_checksum": basin.get("model_package_checksum") or basin.get("package_checksum"),
        "segment_count": int(output_river.get("segment_count") or 0),
        "forecast_horizon_hours": forecast_horizon_hours,
        "start_time": _format_time(start_time),
        "end_time": _format_time(end_time),
    }
    forcing = {
        "forcing_version_id": forcing_version_id,
        "forcing_uri": forcing_uri,
        "forcing_package_uri": forcing_uri,
        "station_metadata": station_metadata,
        "station_count": station_metadata.get("station_count"),
        "station_ids": station_metadata.get("station_ids", []),
        "quality_flag": station_metadata.get("quality_flag"),
    }
    if station_metadata.get("shud_station"):
        forcing["shud_station"] = station_metadata["shud_station"]
    outputs = {
        "run_manifest_uri": run_manifest_uri,
        "output_uri": output_uri,
        "log_uri": log_uri,
        "reuse_policy": "deterministic_run_uri",
        "output_segment_count": int(output_river.get("segment_count") or 0),
        "gis_segment_count": _optional_int(basin.get("segment_count")),
    }
    return ModelRunAssembly(
        identity=identity,
        forcing=forcing,
        runtime=runtime,
        outputs=outputs,
        frequency=frequency,
        display=display,
        quality_states=quality_states,
        residual_blockers=tuple(blockers),
    )


def _default_forcing_uri(
    source_id: str,
    compact_cycle: str,
    basin_version_id: str,
    model_id: str,
    object_store: LocalObjectStore,
) -> str:
    return _directory_uri(object_store, f"forcing/{source_id.lower()}/{compact_cycle}/{basin_version_id}/{model_id}/")


def _directory_uri(object_store: LocalObjectStore, key: str) -> str:
    return object_store.uri_for_key(key).rstrip("/") + "/"


def _preserve_directory_uri(value: str | None, object_store: LocalObjectStore, fallback_key: str) -> str:
    if value is not None and _has_uri_scheme(value):
        return value.rstrip("/") + "/"
    return _directory_uri(object_store, fallback_key)


def _has_uri_scheme(value: str) -> bool:
    candidate = value.strip()
    if not candidate:
        return False
    match = re.match(r"^[A-Za-z][A-Za-z0-9+.-]*:", candidate)
    return match is not None


def _model_package_manifest_uri(basin: Mapping[str, Any], model_package_uri: str) -> str:
    resource_profile = _nested_mapping(basin.get("resource_profile"))
    explicit = (
        basin.get("model_package_manifest_uri")
        or basin.get("manifest_uri")
        or resource_profile.get("manifest_uri")
    )
    if explicit not in (None, ""):
        return str(explicit)
    package_uri = model_package_uri.rstrip("/")
    if package_uri.endswith("/package"):
        return f"{package_uri.removesuffix('/package')}/manifest.json"
    return f"{package_uri}/manifest.json"


def _station_metadata_for_basin(basin: Mapping[str, Any]) -> dict[str, Any]:
    resource_profile = _nested_mapping(basin.get("resource_profile"))
    explicit = _nested_mapping(
        basin.get("forcing_station_metadata")
        or basin.get("station_metadata")
        or resource_profile.get("forcing_station_metadata")
    )
    if explicit:
        station_ids = [str(item) for item in explicit.get("station_ids") or []]
        station_count = _optional_int(explicit.get("station_count"))
        if station_count is None:
            station_count = len(station_ids)
        state = "ready" if station_count > 0 else "unavailable"
        return {
            "schema_version": "nhms.forcing_station_metadata.v1",
            "state": str(explicit.get("state") or state),
            "station_count": station_count,
            "station_ids": station_ids,
            "source": str(explicit.get("source") or "registry_package_metadata"),
            "shud_station": explicit.get("shud_station"),
            "quality_flag": str(
                explicit.get("quality_flag") or ("ok" if station_count > 0 else "station_forcing_unavailable")
            ),
        }
    station_count = _optional_int(basin.get("station_count"))
    raw_station_ids = basin.get("station_ids")
    station_ids = (
        [str(item) for item in raw_station_ids or []]
        if isinstance(raw_station_ids, Sequence) and not isinstance(raw_station_ids, str | bytes)
        else []
    )
    if station_count is None and station_ids:
        station_count = len(station_ids)
    if station_count is None:
        station_count = 0
    state = "ready" if station_count > 0 else "unavailable"
    return {
        "schema_version": "nhms.forcing_station_metadata.v1",
        "state": state,
        "station_count": station_count,
        "station_ids": station_ids,
        "source": "registry_package_metadata",
        "quality_flag": "ok" if station_count > 0 else "station_forcing_unavailable",
    }


def _output_river_contract(basin: Mapping[str, Any]) -> dict[str, Any]:
    explicit = _nested_mapping(basin.get("output_river") or basin.get("shud_output_river"))
    resource_profile = _nested_mapping(basin.get("resource_profile"))
    gis_segment_count = _optional_int(basin.get("segment_count"))
    profile_output_river = _nested_mapping(resource_profile.get("output_river"))
    output_segment_count = _first_optional_int(
        basin.get("output_segment_count"),
        basin.get("shud_output_segment_count"),
        basin.get("shud_output_river_count"),
        resource_profile.get("output_segment_count"),
        resource_profile.get("shud_output_segment_count"),
        resource_profile.get("shud_output_river_count"),
        profile_output_river.get("output_segment_count"),
        profile_output_river.get("segment_count"),
    )
    if explicit:
        state = str(explicit.get("state") or "ready")
        segment_ids = [str(item) for item in explicit.get("river_segment_ids") or explicit.get("segment_ids") or []]
        explicit_segment_count = _first_optional_int(
            explicit.get("output_segment_count"),
            explicit.get("segment_count"),
        )
        resolved_segment_count = _first_optional_int(
            explicit_segment_count,
            output_segment_count,
            len(segment_ids) if segment_ids else None,
            gis_segment_count,
        )
        if resolved_segment_count is None:
            state = "unavailable"
            resolved_segment_count = 0
        return {
            "state": state,
            "river_network_version_id": str(basin["river_network_version_id"]),
            "segment_count": resolved_segment_count,
            "output_segment_count": resolved_segment_count,
            "gis_segment_count": gis_segment_count,
            "river_segment_ids": segment_ids,
            "identity_source": str(explicit.get("identity_source") or "registry_package_metadata"),
            "quality_flag": str(
                explicit.get("quality_flag") or ("ok" if state == "ready" else "output_river_unavailable")
            ),
        }
    if output_segment_count is None and gis_segment_count is None:
        return {
            "state": "unavailable",
            "river_network_version_id": str(basin["river_network_version_id"]),
            "segment_count": 0,
            "output_segment_count": 0,
            "gis_segment_count": None,
            "river_segment_ids": [],
            "identity_source": "registry_package_metadata",
            "quality_flag": "output_river_unavailable",
        }
    resolved_segment_count = output_segment_count if output_segment_count is not None else gis_segment_count
    return {
        "state": "ready" if resolved_segment_count > 0 else "unavailable",
        "river_network_version_id": str(basin["river_network_version_id"]),
        "segment_count": resolved_segment_count,
        "output_segment_count": resolved_segment_count,
        "gis_segment_count": gis_segment_count,
        "river_segment_ids": [],
        "identity_source": (
            "resource_profile.output_segment_count" if output_segment_count is not None else "registry_package_metadata"
        ),
        "quality_flag": "ok" if resolved_segment_count > 0 else "output_river_unavailable",
    }


def _frequency_contract(basin: Mapping[str, Any]) -> dict[str, Any]:
    capabilities = _nested_mapping(basin.get("frequency_capabilities"))
    has_curves = _tri_state(
        basin.get("frequency_curves_available"),
        capabilities.get("curves_available"),
        capabilities.get("return_periods"),
    )
    has_thresholds = _tri_state(
        basin.get("warning_thresholds_available"),
        capabilities.get("warning_thresholds_available"),
        capabilities.get("warning_thresholds"),
    )
    unavailable: list[str] = []
    if has_curves is False:
        unavailable.append("frequency_curves")
    if has_thresholds is False:
        unavailable.append("warning_thresholds")
    state = "ready" if not unavailable else "unavailable"
    return {
        "state": state,
        "return_periods_enabled": bool(capabilities.get("return_periods", True)),
        "frequency_curves": "available" if has_curves is not False else "unavailable",
        "warning_thresholds": "available" if has_thresholds is not False else "unavailable",
        "quality_flag": "ok" if state == "ready" else "frequency_inputs_unavailable",
        "unavailable_products": unavailable,
    }


def _display_contract(basin: Mapping[str, Any], *, output_uri: str) -> dict[str, Any]:
    capabilities = _nested_mapping(basin.get("display_capabilities"))
    optional_weather = _tri_state(
        basin.get("optional_weather_available"),
        capabilities.get("optional_weather_available"),
        capabilities.get("weather_products"),
    )
    tiles_enabled = bool(capabilities.get("tiles", True))
    unavailable = []
    if optional_weather is False:
        unavailable.append("optional_weather_products")
    return {
        "state": "ready" if tiles_enabled else "unavailable",
        "tiles_enabled": tiles_enabled,
        "output_uri": output_uri,
        "optional_weather_products": "available" if optional_weather is not False else "unavailable",
        "quality_flag": "ok" if not unavailable and tiles_enabled else "display_inputs_unavailable",
        "unavailable_products": unavailable,
    }


def _assembly_quality_states(
    basin: Mapping[str, Any],
    *,
    station_metadata: Mapping[str, Any],
    output_river: Mapping[str, Any],
    frequency: Mapping[str, Any],
    display: Mapping[str, Any],
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    states = {
        "station_forcing": {
            "state": station_metadata.get("state"),
            "quality_flag": station_metadata.get("quality_flag"),
        },
        "frequency": {
            "state": frequency.get("state"),
            "quality_flag": frequency.get("quality_flag"),
            "unavailable_products": list(frequency.get("unavailable_products") or []),
        },
        "display": {
            "state": display.get("state"),
            "quality_flag": display.get("quality_flag"),
            "unavailable_products": list(display.get("unavailable_products") or []),
        },
    }
    states["output_river"] = {
        "state": output_river.get("state"),
        "quality_flag": output_river.get("quality_flag"),
        "segment_count": output_river.get("segment_count"),
    }
    blockers: list[dict[str, Any]] = []
    if station_metadata.get("state") != "ready":
        blockers.append(
            {
                "code": "STATION_FORCING_UNAVAILABLE",
                "state": "unavailable",
                "quality_flag": station_metadata.get("quality_flag"),
                "residual_risk": "No forcing station metadata is available for this model package.",
            }
        )
    if output_river.get("state") != "ready":
        blockers.append(
            {
                "code": "OUTPUT_RIVER_UNAVAILABLE",
                "state": "unavailable",
                "quality_flag": output_river.get("quality_flag"),
                "residual_risk": (
                    "SHUD output-river segment metadata is unavailable; segment_count was not fabricated."
                ),
            }
        )
    for product in frequency.get("unavailable_products") or []:
        blockers.append(
            {
                "code": str(product).upper() + "_UNAVAILABLE",
                "state": "unavailable",
                "quality_flag": frequency.get("quality_flag"),
                "residual_risk": (
                    f"{product} is unavailable; downstream products must carry null values or quality flags."
                ),
            }
        )
    for product in display.get("unavailable_products") or []:
        blockers.append(
            {
                "code": str(product).upper() + "_UNAVAILABLE",
                "state": "unavailable",
                "quality_flag": display.get("quality_flag"),
                "residual_risk": f"{product} is unavailable; durable model outputs remain reusable.",
            }
        )
    for item in basin.get("residual_blockers") or ():
        if isinstance(item, Mapping):
            blockers.append(dict(item))
    return states, blockers


def _model_run_stage_evidence(stage: str, entry: Mapping[str, Any], *, cycle_id: str) -> dict[str, Any]:
    assembly = _assembly_from_entry(entry)
    identity = dict(assembly.get("identity") or {})
    return {
        "stage": stage,
        "production_stage": production_stage_for(stage),
        "cycle_id": cycle_id,
        "candidate_id": identity.get("candidate_id") or entry.get("candidate_id"),
        "run_id": identity.get("run_id") or entry.get("run_id"),
        "hydro_run_id": identity.get("hydro_run_id") or entry.get("hydro_run_id") or entry.get("run_id"),
        "model_id": identity.get("model_id") or entry.get("model_id"),
        "source": identity.get("source") or identity.get("source_id") or entry.get("source_id"),
        "source_id": identity.get("source_id") or entry.get("source_id"),
        "cycle_time": identity.get("cycle_time") or entry.get("cycle_time"),
        "scenario_id": identity.get("scenario_id") or entry.get("scenario_id"),
        "canonical_product_id": identity.get("canonical_product_id") or entry.get("canonical_product_id"),
        "forcing_version_id": identity.get("forcing_version_id") or entry.get("forcing_version_id"),
        "published_manifest_id": identity.get("published_manifest_id") or entry.get("published_manifest_id"),
        "model_package_uri": identity.get("model_package_uri") or entry.get("model_package_uri"),
        "basin_id": identity.get("basin_id") or entry.get("basin_id"),
        "basin_version_id": identity.get("basin_version_id") or entry.get("basin_version_id"),
        "river_network_version_id": identity.get("river_network_version_id") or entry.get("river_network_version_id"),
        "output_uri": _nested_mapping(assembly.get("outputs")).get("output_uri") or entry.get("output_uri"),
        "quality_states": dict(assembly.get("quality_states") or entry.get("quality_states") or {}),
        "residual_blockers": list(assembly.get("residual_blockers") or entry.get("residual_blockers") or []),
    }


def _frequency_quality_state(entry: Mapping[str, Any], *, cycle_id: str) -> dict[str, Any]:
    evidence = _model_run_stage_evidence("frequency", entry, cycle_id=cycle_id)
    frequency_state = _nested_mapping(evidence.get("quality_states")).get("frequency") or {}
    return {
        **evidence,
        "state": _nested_mapping(frequency_state).get("state", "ready"),
        "quality_flag": _nested_mapping(frequency_state).get("quality_flag", "ok"),
        "unavailable_products": list(_nested_mapping(frequency_state).get("unavailable_products") or []),
    }


def _publish_quality_state(entry: Mapping[str, Any], *, cycle_id: str) -> dict[str, Any]:
    evidence = _model_run_stage_evidence("publish", entry, cycle_id=cycle_id)
    display_state = _nested_mapping(evidence.get("quality_states")).get("display") or {}
    return {
        **evidence,
        "state": _nested_mapping(display_state).get("state", "ready"),
        "quality_flag": _nested_mapping(display_state).get("quality_flag", "ok"),
        "unavailable_products": list(_nested_mapping(display_state).get("unavailable_products") or []),
    }


def _cycle_residual_blockers(entries: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    blockers: list[dict[str, Any]] = []
    for entry in entries:
        run_id = str(entry.get("run_id") or "")
        for blocker in entry.get("residual_blockers") or []:
            if isinstance(blocker, Mapping):
                blockers.append({"run_id": run_id, **dict(blocker)})
        assembly = _assembly_from_entry(entry)
        for blocker in assembly.get("residual_blockers") or []:
            if isinstance(blocker, Mapping):
                candidate = {"run_id": run_id, **dict(blocker)}
                if candidate not in blockers:
                    blockers.append(candidate)
    return blockers


def _assembly_from_entry(entry: Mapping[str, Any]) -> dict[str, Any]:
    assembly = entry.get("model_run_assembly")
    return dict(assembly) if isinstance(assembly, Mapping) else {}


def _assembly_payload_from_runtime_manifest(manifest: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "identity": dict(_nested_mapping(manifest.get("identity"))),
        "forcing": dict(_nested_mapping(manifest.get("forcing"))),
        "runtime": dict(_nested_mapping(manifest.get("runtime"))),
        "outputs": dict(_nested_mapping(manifest.get("outputs"))),
        "frequency": dict(_nested_mapping(manifest.get("frequency"))),
        "display": dict(_nested_mapping(manifest.get("display"))),
        "quality_states": dict(_nested_mapping(manifest.get("quality_states"))),
        "residual_blockers": list(manifest.get("residual_blockers") or []),
    }


def _nested_mapping(value: Any) -> dict[str, Any]:
    return dict(value) if isinstance(value, Mapping) else {}


def _tri_state(*values: Any) -> bool | None:
    for value in values:
        if isinstance(value, bool):
            return value
        if isinstance(value, str):
            normalized = value.strip().lower()
            if normalized in {"true", "available", "ready", "yes", "1"}:
                return True
            if normalized in {"false", "unavailable", "missing", "blocked", "no", "0"}:
                return False
    return None


def _safe_project_name(value: str) -> str:
    candidate = value.strip() or "shud"
    if _SAFE_ID_RE.fullmatch(candidate):
        return candidate
    return re.sub(r"[^A-Za-z0-9_.-]+", "_", candidate).strip("._-") or "shud"


def _project_name_for_basin(basin: Mapping[str, Any], *, fallback: str) -> str:
    resource_profile = _nested_mapping(basin.get("resource_profile"))
    runtime = _nested_mapping(basin.get("runtime"))
    for value in (
        basin.get("project_name"),
        basin.get("shud_input_name"),
        resource_profile.get("project_name"),
        resource_profile.get("shud_input_name"),
        runtime.get("project_name"),
        runtime.get("shud_input_name"),
        fallback,
    ):
        if value not in (None, ""):
            return _safe_project_name(str(value))
    return _safe_project_name(fallback)


def _nested_value(value: Mapping[str, Any], path: Sequence[str]) -> Any:
    current: Any = value
    for part in path:
        if not isinstance(current, Mapping):
            return None
        current = current.get(part)
    return current


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
    task_pattern = re.compile(rf"^{re.escape(master_job_id)}_(\d+)$")
    results: list[ArrayTaskResult] = []
    for raw_line in stdout.splitlines():
        if not raw_line.strip():
            continue
        fields = raw_line.rstrip("\n").split("|")
        if len(fields) < 3:
            raise OrchestratorError(
                "SLURM_SACCT_PARSE_ERROR",
                "Unable to parse array sacct output.",
                {"line": raw_line, "master_job_id": master_job_id},
            )
        job_id, raw_state, raw_exit_code = fields[0], fields[1], fields[2]
        match = task_pattern.fullmatch(job_id)
        if match is None:
            continue
        task_id = int(match.group(1))
        extras = _sacct_extra_fields(fields[3:])
        task_status = _array_task_status(raw_state)
        results.append(
            ArrayTaskResult(
                task_id=task_id,
                slurm_job_id=job_id,
                status=task_status,
                exit_code=_parse_slurm_exit_code(raw_exit_code),
                error_code=None if task_status == "succeeded" else "NODE_FAILURE",
                log_uri=_context_array_log_uri(context, object_store, master_job_id, task_id),
                accounting=extras,
            )
        )
    return _aggregation_from_task_results(tuple(sorted(results, key=lambda result: result.task_id)))


def _coerce_array_aggregation(
    raw_results: Any,
    master_job_id: str,
    *,
    context: CycleOrchestrationContext | None = None,
    object_store: LocalObjectStore | None = None,
) -> ArrayAggregation:
    if isinstance(raw_results, ArrayAggregation):
        return raw_results
    if isinstance(raw_results, str):
        return parse_sacct_array_results(raw_results, master_job_id, context=context, object_store=object_store)
    if isinstance(raw_results, Mapping):
        if isinstance(raw_results.get("stdout"), str):
            return parse_sacct_array_results(
                str(raw_results["stdout"]),
                master_job_id,
                context=context,
                object_store=object_store,
            )
        tasks = raw_results.get("tasks") or raw_results.get("task_results")
        if isinstance(tasks, Sequence) and not isinstance(tasks, str | bytes):
            return _coerce_array_aggregation(tasks, master_job_id, context=context, object_store=object_store)
    if isinstance(raw_results, Sequence) and not isinstance(raw_results, str | bytes):
        task_results = []
        for index, item in enumerate(raw_results):
            item_dict = _coerce_mapping(item)
            task_id = int(item_dict.get("task_id", index))
            status = str(item_dict.get("status") or _array_task_status(str(item_dict.get("state", ""))))
            accounting = _slurm_accounting_from_payload(item_dict)
            task_results.append(
                ArrayTaskResult(
                    task_id=task_id,
                    slurm_job_id=str(
                        item_dict.get("slurm_job_id") or item_dict.get("job_id") or f"{master_job_id}_{task_id}"
                    ),
                    status=status,
                    exit_code=item_dict.get("exit_code"),
                    error_code=(
                        str(item_dict.get("error_code"))
                        if item_dict.get("error_code") not in (None, "")
                        else (None if status == "succeeded" else "NODE_FAILURE")
                    ),
                    error_message=str(item_dict.get("error_message"))
                    if item_dict.get("error_message") not in (None, "")
                    else None,
                    log_uri=str(item_dict.get("log_uri"))
                    if item_dict.get("log_uri") not in (None, "")
                    else _context_array_log_uri(context, object_store, master_job_id, task_id),
                    accounting=accounting,
                )
            )
        return _aggregation_from_task_results(tuple(sorted(task_results, key=lambda result: result.task_id)))
    raise TypeError(f"Unsupported array task result payload: {type(raw_results).__name__}")


def _aggregation_from_task_results(results: Sequence[ArrayTaskResult]) -> ArrayAggregation:
    return ArrayAggregation(
        total=len(results),
        succeeded=sum(1 for result in results if result.status == "succeeded"),
        failed=sum(1 for result in results if result.status == "failed"),
        cancelled=sum(1 for result in results if result.status == "cancelled"),
        task_results=tuple(results),
    )


def _aggregation_error_code(aggregation: ArrayAggregation | None) -> str | None:
    if aggregation is None or aggregation.status == "succeeded":
        return None
    for task in aggregation.task_results:
        if task.status != "succeeded" and task.error_code not in (None, ""):
            return str(task.error_code)
    return "NODE_FAILURE" if aggregation.failed else None


def _aggregation_error_message(aggregation: ArrayAggregation | None) -> str | None:
    if aggregation is None or aggregation.status == "succeeded":
        return None
    for task in aggregation.task_results:
        if task.status != "succeeded" and task.error_message not in (None, ""):
            return str(task.error_message)
    return None


def _sacct_extra_fields(fields: Sequence[str]) -> dict[str, Any]:
    names = ("elapsed", "max_rss", "ave_rss", "alloc_tres", "max_disk_read", "max_disk_write")
    return {name: value for name, value in zip(names, fields, strict=False) if value not in (None, "")}


def _slurm_accounting_from_payload(payload: Mapping[str, Any]) -> dict[str, Any]:
    raw = payload.get("accounting") or payload.get("resource_metrics")
    accounting = dict(raw) if isinstance(raw, Mapping) else {}
    aliases = {
        "elapsed": ("elapsed", "elapsed_time"),
        "max_rss": ("max_rss", "MaxRSS", "maxrss"),
        "ave_rss": ("ave_rss", "AveRSS", "averss"),
        "alloc_tres": ("alloc_tres", "AllocTRES", "tres"),
        "max_disk_read": ("max_disk_read", "MaxDiskRead"),
        "max_disk_write": ("max_disk_write", "MaxDiskWrite"),
    }
    for normalized, keys in aliases.items():
        if normalized in accounting:
            continue
        for key in keys:
            if key in payload and payload[key] not in (None, ""):
                accounting[normalized] = payload[key]
                break
    return accounting


def _resource_metrics_from_payload(payload: Mapping[str, Any]) -> dict[str, Any]:
    accounting = _slurm_accounting_from_payload(payload)
    return {
        key: value
        for key, value in accounting.items()
        if key in {"elapsed", "max_rss", "ave_rss", "alloc_tres", "max_disk_read", "max_disk_write"}
    }


def _safe_pipeline_event_details(details: Mapping[str, Any]) -> dict[str, Any]:
    redacted = redact_payload(_json_safe_pipeline_event_value(details))
    return dict(redacted) if isinstance(redacted, Mapping) else {}


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
    if aggregation is None:
        return ()
    basins_by_task: dict[int, Mapping[str, Any]] = {}
    if context is not None:
        basins_by_task = {
            int(basin.get("task_id", index)): basin for index, basin in enumerate(context.active_basins)
        }
    results: list[Mapping[str, Any]] = []
    for task in aggregation.task_results:
        basin = basins_by_task.get(task.task_id)
        original_task_id = task.task_id if basin is None else _basin_original_task_id(basin, task.task_id)
        payload: dict[str, Any] = {
            "array_task_id": task.task_id,
            "task_id": task.task_id,
            "original_task_id": original_task_id,
            "slurm_job_id": task.slurm_job_id,
            "state": task.status,
            "status": task.status,
            "production_status": production_status_for(task.status),
            "exit_code": task.exit_code,
            "error_code": task.error_code,
            "error_message": task.error_message,
            "log_uri": task.log_uri,
            "accounting": dict(task.accounting),
            "resource_metrics": _resource_metrics_from_payload(task.accounting),
        }
        if basin is not None:
            for key in (
                "model_id",
                "basin_id",
                "candidate_id",
                "run_id",
                "source_id",
                "cycle_time",
                "canonical_product_id",
                "forcing_version_id",
                "hydro_run_id",
                "published_manifest_id",
            ):
                value = basin.get(key)
                if value not in (None, ""):
                    payload[key] = value
        results.append(_safe_pipeline_event_details(payload))
    return tuple(results)


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
    return object_store.uri_for_key(f"runs/{run_id}/logs/{master_job_id}_{task_id}.out")


def _array_task_status(raw_state: str) -> str:
    normalized = raw_state.strip().upper().split()[0].rstrip("+")
    if normalized == "COMPLETED":
        return "succeeded"
    if normalized == "CANCELLED":
        return "cancelled"
    return "failed"


def _parse_slurm_exit_code(raw_exit_code: str) -> int | None:
    if not raw_exit_code:
        return None
    try:
        return int(raw_exit_code.split(":", maxsplit=1)[0])
    except ValueError:
        return None


def _pipeline_job_id(run_id: str, stage: str) -> str:
    return f"job_{run_id}_{stage}"


def _cycle_stage_idempotency_key(context: CycleOrchestrationContext, stage: StageDefinition) -> str:
    """Stable idempotency key for a cohort's cycle-level stage submission.

    ``run_id`` deterministically encodes source/cycle/basin-cohort, so
    ``run_id:stage`` is the equivalent of the per-candidate
    ``source:cycle:basin:stage`` key and is constant across passes.
    """

    return f"{context.run_id}:{stage.stage}"


def _published_artifact_root_configured() -> bool:
    return bool(os.getenv("NHMS_PUBLISHED_ARTIFACT_ROOT", "").strip())


def _absolute_configured_path(path: Path) -> Path:
    expanded = Path(path).expanduser()
    return expanded if expanded.is_absolute() else Path.cwd() / expanded


def _log_stream_for_stage(stage: str) -> str:
    return "err" if stage in {"submission_failed", "error"} else "out"


def _source_id_from_cycle_id(value: object) -> str | None:
    if value in (None, ""):
        return None
    source = str(value).split("_", maxsplit=1)[0]
    try:
        return normalize_source_id(source)
    except ValueError:
        return source or None


def _cycle_time_from_cycle_id(value: object) -> datetime | None:
    if value in (None, ""):
        return None
    compact_cycle = str(value).rsplit("_", maxsplit=1)[-1]
    try:
        return parse_cycle_time(compact_cycle)
    except ValueError:
        return None


def _stage_status_message(stage: str, status: str, job: dict[str, Any]) -> str:
    if status == "failed":
        error_code = job.get("error_code") or "UNKNOWN"
        error_message = redact_payload(job.get("error_message") or "No error message provided.")
        return f"{stage} failed: {error_code} {error_message}"
    return f"{stage} status changed to {status}"


def _resolve_forecast_horizon_hours(
    *,
    source_id: str,
    cycle_time: datetime,
    configured_horizon_hours: int,
    forcing: ForcingContext,
    max_lead_hours: int | None,
) -> int:
    source_max_lead_hours = max_lead_hours or forcing.max_lead_hours
    normalized_source_id = normalize_source_id(source_id)
    if source_max_lead_hours is None and normalized_source_id == "IFS" and forcing.end_time is not None:
        source_max_lead_hours = _elapsed_hours(cycle_time, forcing.end_time)
    if source_max_lead_hours is None and normalized_source_id == "IFS":
        source_max_lead_hours = _ifs_max_lead_hours_for_cycle(cycle_time)
    if source_max_lead_hours is None:
        return int(configured_horizon_hours)
    return min(int(configured_horizon_hours), int(source_max_lead_hours))


def _ifs_max_lead_hours_for_cycle(cycle_time: datetime) -> int | None:
    hour = _ensure_utc(cycle_time).hour
    if hour in {6, 18}:
        return 144
    if hour in {0, 12}:
        return 168
    return None


def _elapsed_hours(start_time: datetime, end_time: datetime) -> int | None:
    elapsed_seconds = (_ensure_utc(end_time) - _ensure_utc(start_time)).total_seconds()
    if elapsed_seconds <= 0:
        return None
    return int(round(elapsed_seconds / 3600.0))


def _optional_int(value: Any) -> int | None:
    if value in (None, ""):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _first_optional_int(*values: Any) -> int | None:
    for value in values:
        coerced = _optional_int(value)
        if coerced is not None:
            return coerced
    return None


def _max_lead_hours_from_lineage(value: Any) -> int | None:
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except json.JSONDecodeError:
            return None
    if not isinstance(value, Mapping):
        return None
    return _optional_int(value.get("max_lead_hours"))


def _basin_max_lead_hours(basin: Mapping[str, Any]) -> int | None:
    """Configured ``max_lead`` policy (hours) for a cohort basin's warm-start chaining."""
    for key in ("max_lead_hours", "warm_start_max_lead_hours"):
        value = _optional_int(basin.get(key))
        if value is not None:
            return value
    horizon = basin.get("horizon")
    if isinstance(horizon, Mapping):
        return _optional_int(horizon.get("max_lead_hours"))
    return None


def _auto_trigger_forecast_hours(
    *,
    source_id: str,
    cycle_time: datetime,
    configured_horizon_hours: int,
    max_lead_hours: int | None,
) -> list[int]:
    source_max_lead_hours = max_lead_hours
    normalized_source_id = normalize_source_id(source_id)
    if source_max_lead_hours is None and normalized_source_id == "IFS":
        source_max_lead_hours = _ifs_max_lead_hours_for_cycle(cycle_time)
    if source_max_lead_hours is None:
        source_max_lead_hours = int(configured_horizon_hours)
    horizon = min(int(configured_horizon_hours), int(source_max_lead_hours))
    return list(range(0, horizon + 1, 3))


def _auto_trigger_source_policy_identity(
    *,
    source_id: str,
    cycle_time: datetime,
    forecast_hours: Sequence[int],
    workspace_root: Path | str,
    object_store_root: Path | str,
    object_store_prefix: str,
) -> dict[str, Any]:
    adapter = _auto_trigger_source_identity_adapter(
        source_id=source_id,
        workspace_root=workspace_root,
        object_store_root=object_store_root,
        object_store_prefix=object_store_prefix,
    )
    if adapter is not None and hasattr(adapter, "source_policy_identity"):
        try:
            return dict(adapter.source_policy_identity(cycle_time, list(forecast_hours)))
        except TypeError:
            return dict(adapter.source_policy_identity(list(forecast_hours)))
    return {
        "source": source_id,
        "cycle_hour": _ensure_utc(cycle_time).hour,
        "forecast_hours": list(forecast_hours),
    }


def _auto_trigger_source_object_identity(
    *,
    source_id: str,
    cycle_time: datetime,
    forecast_hours: Sequence[int],
    workspace_root: Path | str,
    object_store_root: Path | str,
    object_store_prefix: str,
) -> dict[str, Any]:
    adapter = _auto_trigger_source_identity_adapter(
        source_id=source_id,
        workspace_root=workspace_root,
        object_store_root=object_store_root,
        object_store_prefix=object_store_prefix,
    )
    if adapter is not None and hasattr(adapter, "source_object_identity"):
        return dict(adapter.source_object_identity(cycle_time, list(forecast_hours)))
    return {
        "source": source_id,
        "cycle_time": _format_time(cycle_time),
        "cycle_id": cycle_id_for(source_id, cycle_time),
        "forecast_hour_count": len(forecast_hours),
    }


def _auto_trigger_source_identity_adapter(
    *,
    source_id: str,
    workspace_root: Path | str,
    object_store_root: Path | str,
    object_store_prefix: str,
) -> Any | None:
    normalized_source_id = normalize_source_id(source_id)
    if normalized_source_id == "gfs":
        from workers.data_adapters.gfs_adapter import GFSAdapter, GFSAdapterConfig

        return GFSAdapter(
            config=GFSAdapterConfig(
                workspace_root=workspace_root,
                object_store_root=object_store_root,
                object_store_prefix=object_store_prefix,
            ),
            repository=None,
        )
    if normalized_source_id == "IFS":
        from workers.data_adapters.ifs_adapter import IFSAdapter, IFSAdapterConfig

        return IFSAdapter(
            config=IFSAdapterConfig(
                workspace_root=workspace_root,
                object_store_root=object_store_root,
                object_store_prefix=object_store_prefix,
            ),
            repository=None,
        )
    return None


def _stale_converter_versions_in_cycle(
    cycle: Mapping[str, Any],
    *,
    source_id: str,
) -> set[str | None]:
    """Return stale canonical markers in ``cycle`` (version mismatch or bad unit).

    Two orthogonal stale criteria are applied:

    1. Version: each canonical product carries ``converter_version`` inside
       ``lineage_json`` (with a top-level fallback). A value that is explicitly
       recorded and differs from the source's current expected version is stale.
       A missing version is NOT flagged (mirrors fixture/seed safety).

    2. Unit (precip): a precipitation product (``prcp_rate_or_amount``) whose
       ``unit`` is explicitly recorded and is not the post-#269 canonical
       ``mm/day`` is stale, marked as ``"unit:<observed>"``. A missing/empty
       unit is NOT flagged (same fixture/seed safety philosophy).

    WHY the unit criterion: pre-#269 precip rows were written with ``unit="mm"``
    and frequently lack a converter_version, so they slip past criterion 1.
    Without this orthogonal check they would pass readiness and then hit the
    producer's mm/day unit gate, terminating in ``failed_forcing`` with no
    self-heal path (a "break userspace" regression). Flagging the unit triggers
    the same demote -> re-conversion loop, restoring migration self-heal.

    Returns an empty set when every product is current (or none exist).
    """
    products = cycle.get("canonical_products")
    if isinstance(products, str):
        try:
            products = json.loads(products)
        except json.JSONDecodeError:
            products = []
    if not isinstance(products, Sequence) or isinstance(products, (bytes, bytearray, str)):
        return set()
    expected = expected_converter_version(source_id)
    stale: set[str | None] = set()
    for row in products:
        if not isinstance(row, Mapping):
            continue
        lineage = row.get("lineage_json")
        if isinstance(lineage, str):
            try:
                lineage = json.loads(lineage)
            except json.JSONDecodeError:
                lineage = {}
        version: str | None = None
        if isinstance(lineage, Mapping):
            raw = lineage.get("converter_version", row.get("converter_version"))
            version = str(raw) if raw is not None else None
        else:
            raw = row.get("converter_version")
            version = str(raw) if raw is not None else None
        # Criterion 1 (version): only an explicitly-recorded, different version is
        # treated as stale. A missing version (None) is left untouched so that
        # incomplete fixtures/seeds and post-#269 rows lacking a version are not
        # aggressively demoted.
        if version is not None and version != expected:
            stale.add(version)
        # Criterion 2 (precip unit): a precip product whose unit is explicitly
        # recorded and is not the canonical mm/day is stale. Missing/empty unit is
        # left untouched (same fixture/seed safety as the version criterion).
        if row.get("variable") == CANONICAL_PRECIP_VARIABLE:
            raw_unit = row.get("unit")
            if raw_unit is not None:
                normalized_unit = str(raw_unit).strip().lower()
                if normalized_unit and normalized_unit != CANONICAL_PRECIP_UNIT:
                    stale.add(f"unit:{normalized_unit}")
    return stale


def _canonical_products_from_ready_cycle(
    cycle: Mapping[str, Any],
    *,
    source_id: str,
    cycle_time: datetime,
) -> list[dict[str, Any]]:
    products = cycle.get("canonical_products")
    if isinstance(products, str):
        try:
            products = json.loads(products)
        except json.JSONDecodeError:
            products = []
    if not isinstance(products, Sequence) or isinstance(products, (bytes, bytearray, str)):
        products = []
    return [
        _canonical_product_row_from_ready_cycle(row, source_id=source_id, cycle_time=cycle_time)
        for row in products
    ]


def _canonical_product_row_from_ready_cycle(
    row: Any,
    *,
    source_id: str,
    cycle_time: datetime,
) -> dict[str, Any]:
    product = dict(row) if isinstance(row, Mapping) else {}
    product.setdefault("source_id", source_id)
    product.setdefault("cycle_time", cycle_time)
    lineage = product.get("lineage_json")
    if isinstance(lineage, str):
        try:
            product["lineage_json"] = json.loads(lineage)
        except json.JSONDecodeError:
            product["lineage_json"] = {}
    elif not isinstance(lineage, Mapping):
        product["lineage_json"] = {}
    return product


def _auto_trigger_canonical_readiness_unavailable_evidence(
    *,
    source_id: str,
    cycle_time: datetime,
    forecast_hours: Sequence[int],
    reason: str,
    error: Exception,
) -> dict[str, Any]:
    return {
        "source": source_id,
        "source_id": source_id,
        "cycle_time": _format_time(cycle_time),
        "status": "canonical_incomplete",
        "ready": False,
        "reason": reason,
        "canonical_product_id": f"canon_{source_id.lower()}_{format_cycle_time(cycle_time)}",
        "accepted_horizon": _accepted_horizon_from_hours(forecast_hours),
        "expected_leads": list(forecast_hours),
        "policy_identity_matched": False,
        "source_object_identity_matched": False,
        "dependency": {
            "name": "canonical_readiness_provider",
            "status": "unavailable",
            "retryable": True,
        },
        "failure": {
            "error_type": type(error).__name__,
            "message": str(redact_payload(str(error))),
        },
    }


def _accepted_horizon_from_hours(forecast_hours: Sequence[int]) -> dict[str, Any]:
    hours = sorted(int(hour) for hour in forecast_hours)
    return {
        "first_lead_hour": min(hours) if hours else None,
        "last_lead_hour": max(hours) if hours else None,
        "lead_count": len(hours),
    }


def _skipped_ready_forecast_result(
    *,
    source_id: str,
    cycle_time: datetime,
    model_id: str,
    reason: str,
    canonical_readiness: Mapping[str, Any],
) -> PipelineResult:
    cycle_id = cycle_id_for(source_id, cycle_time)
    run_id = f"fcst_{source_id.lower()}_{format_cycle_time(cycle_time)}_{model_id}"
    return PipelineResult(
        run_id=run_id,
        cycle_id=cycle_id,
        status="skipped",
        stages=(),
        candidate_outcomes=(
            {
                "candidate_id": run_id,
                "run_id": run_id,
                "cycle_id": cycle_id,
                "source_id": source_id,
                "cycle_time": _format_time(cycle_time),
                "model_id": model_id,
                "status": "skipped",
                "reason": reason,
                "state_evidence": {"canonical_readiness": dict(canonical_readiness)},
            },
        ),
    )


def _coerce_array_task_id(value: Any) -> int | None:
    """Best-effort int coercion for a gateway-reported array task id.

    ``ops.pipeline_job.array_task_id`` is an integer column; a master array job
    has no single task id and yields ``None``. Non-integer junk is dropped rather
    than raised so receipt persistence never breaks on an odd gateway payload.
    """

    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _parse_gateway_time(value: Any) -> datetime | None:
    if value is None or isinstance(value, datetime):
        return _ensure_utc(value) if isinstance(value, datetime) else None
    if isinstance(value, str):
        return _ensure_utc(datetime.fromisoformat(value.replace("Z", "+00:00")))
    return None


def _ensure_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def _format_time(value: datetime) -> str:
    return _ensure_utc(value).isoformat().replace("+00:00", "Z")


def _format_time_or_none(value: datetime | None) -> str | None:
    return _format_time(value) if value is not None else None


def parse_date_range(value: str | tuple[datetime, datetime]) -> tuple[datetime, datetime]:
    if isinstance(value, tuple):
        start_time, end_time = value
        return _validated_date_range(_ensure_utc(start_time), _ensure_utc(end_time))

    candidate = value.strip()
    separators = ("..", "/", ",")
    for separator in separators:
        if separator in candidate:
            left, right = candidate.split(separator, maxsplit=1)
            return _validated_date_range(_parse_date_range_endpoint(left), _parse_date_range_endpoint(right))
    raise OrchestratorError(
        "INVALID_DATE_RANGE",
        "date_range must use START/END, START..END, or START,END.",
        {"date_range": value},
    )


def _parse_date_range_endpoint(value: str) -> datetime:
    candidate = value.strip()
    if len(candidate) == 10 and candidate[4] == "-" and candidate[7] == "-":
        return datetime.fromisoformat(candidate).replace(tzinfo=UTC)
    return parse_cycle_time(candidate)


def _validated_date_range(start_time: datetime, end_time: datetime) -> tuple[datetime, datetime]:
    start = _ensure_utc(start_time)
    end = _ensure_utc(end_time)
    if end <= start:
        raise OrchestratorError(
            "INVALID_DATE_RANGE",
            "date_range end must be after start.",
            {"start_time": _format_time(start), "end_time": _format_time(end)},
        )
    return start, end


def _analysis_error_code(stage: StageDefinition, terminal: dict[str, Any]) -> str:
    raw_code = terminal.get("error_code")
    timeout_codes = {"TIMEOUT", "SLURM_TIMEOUT", "SLURM_JOB_TIMEOUT"}
    if stage.stage == "analysis_run" and str(raw_code or "").upper() in timeout_codes:
        return "SLURM_TIMEOUT"
    return str(raw_code or f"{stage.stage.upper()}_{terminal['status'].upper()}")


def _template_export_lines(context: Mapping[str, Any]) -> list[str]:
    export_fields = {
        "WORKSPACE_ROOT": context.get("workspace_dir", ""),
        "OBJECT_STORE_ROOT": context.get("object_store_root", context.get("workspace_dir", "")),
        "OBJECT_STORE_PREFIX": context.get("object_store_prefix", ""),
        "NHMS_RUN_ID": context.get("run_id", ""),
        "NHMS_MODEL_ID": context.get("model_id", ""),
        "NHMS_SOURCE_ID": context.get("source_id", "GFS"),
        "NHMS_CYCLE_ID": context.get("cycle_id", ""),
        "NHMS_CYCLE_TIME": context.get("cycle_time", ""),
        "NHMS_START_TIME": context.get("start_time", ""),
        "NHMS_END_TIME": context.get("end_time", ""),
        "NHMS_BASIN_VERSION_ID": context.get("basin_version_id", ""),
        "NHMS_RIVER_NETWORK_VERSION_ID": context.get("river_network_version_id", ""),
        "NHMS_FORCING_VERSION_ID": context.get("forcing_version_id", ""),
        "NHMS_FORCING_PACKAGE_URI": context.get("forcing_package_uri", ""),
        "NHMS_JOB_TYPE": context.get("job_type", ""),
        "NHMS_RUN_MANIFEST_URI": context.get("run_manifest_uri", ""),
        "NHMS_MANIFEST_INDEX": context.get("manifest_index_path", ""),
        "NHMS_MAX_CONCURRENT": context.get("max_concurrent", ""),
        "SHUD_THREADS": context.get("shud_threads", ""),
        "OMP_NUM_THREADS": context.get("shud_threads", ""),
    }
    lines = [f"export {key}={shlex.quote(str(value or ''))}" for key, value in export_fields.items()]
    grib_env_root = os.getenv("NHMS_GRIB_ENV_ROOT")
    if grib_env_root:
        # Compute nodes (cn01-24) lack cdo/libeccodes; inject the shared conda
        # env's PATH/LD_LIBRARY_PATH so GRIB clip/read works on the node.
        # Quote only the root segment; keep $PATH / ${LD_LIBRARY_PATH:-}
        # outside the quotes so the shell expands them at runtime.
        quoted_root = shlex.quote(grib_env_root)
        lines.append(f"export PATH={quoted_root}/bin:$PATH")
        lines.append(f"export LD_LIBRARY_PATH={quoted_root}/lib:${{LD_LIBRARY_PATH:-}}")
    return lines


def _response_json_or_text(response: httpx.Response) -> dict[str, Any] | str:
    try:
        return response.json()
    except ValueError:
        return response.text


def _error_code_from_response(details: dict[str, Any] | str) -> str:
    if isinstance(details, dict):
        error = details.get("error")
        if isinstance(error, dict) and isinstance(error.get("code"), str):
            return error["code"]
    return "SLURM_GATEWAY_ERROR"
