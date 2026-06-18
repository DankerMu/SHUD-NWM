from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Mapping


class OrchestratorError(RuntimeError):
    def __init__(self, error_code: str, message: str, details: dict[str, Any] | None = None) -> None:
        super().__init__(message)
        self.error_code = error_code
        self.message = message
        self.details = details or {}


@dataclass(frozen=True)
class StageDefinition:
    stage: str
    job_type: str
    template_name: str
    success_cycle_status: str
    failure_cycle_status: str
    is_array: bool = False


@dataclass(frozen=True)
class ModelContext:
    model_id: str
    basin_id: str | None
    basin_version_id: str
    river_network_version_id: str
    segment_count: int
    model_package_uri: str
    output_segment_count: int | None = None
    model_package_checksum: str | None = None


@dataclass(frozen=True)
class ForcingContext:
    forcing_version_id: str | None
    forcing_package_uri: str | None
    start_time: datetime | None = None
    end_time: datetime | None = None
    source_id: str | None = None
    max_lead_hours: int | None = None
    forcing_package_manifest_uri: str | None = None
    forcing_package_manifest_checksum: str | None = None


@dataclass(frozen=True)
class InitialStateSelection:
    state_id: str | None
    state_uri: str | None
    valid_time: datetime | None
    checksum: str | None
    quality: str
    # Lineage (M24 section 2 Lane 1) - optional, default None for backward compatibility.
    source_id: str | None = None
    cycle_id: str | None = None
    lead_hours: int | None = None
    model_package_version: str | None = None
    model_package_checksum: str | None = None
    rejection_code: str | None = None


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
    forcing_package_manifest_uri: str | None = None
    forcing_package_manifest_checksum: str | None = None
    init_state_id: str | None = None
    init_state_uri: str | None = None
    init_state_valid_time: datetime | None = None
    init_state_checksum: str | None = None
    init_state_quality: str = "cold_start_no_state"
    init_state_lineage: Mapping[str, Any] | None = None
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
    # M24 section 2 Lane 2: restart cadence landing on T_{N+1} and forcing causality marker.
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
    finished_at: datetime | None = None


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
    retry_attempt: int | None = None


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


__all__ = [
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
]
