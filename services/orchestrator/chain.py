from __future__ import annotations

import json
import os
import re
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Mapping, Protocol, Sequence

import httpx

from packages.common.best_available import BestAvailableManager
from packages.common.object_store import LocalObjectStore
from packages.common.source_identity import normalize_source_id
from packages.common.state_manager import StateManager, StateSnapshot, assess_freshness
from services.orchestrator.persistence import PipelineJob, PipelineStore
from services.orchestrator.retry import RetryConfig, RetryService, compute_backoff_seconds
from services.slurm_gateway.config import SlurmGatewaySettings
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
ACTIVE_HYDRO_STATUSES = {"created", "staged", "submitted", "running", "succeeded"}
COMPLETED_HYDRO_STATUSES = {"succeeded", "parsed", "published", "complete"}
ANALYSIS_SOURCE_ID = "ERA5"
ANALYSIS_SCENARIO_ID = "analysis_true_field"


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

ANALYSIS_STAGES: tuple[StageDefinition, ...] = (
    StageDefinition("era5_download", "download", "download_era5.sbatch", "raw_complete", "failed_download"),
    StageDefinition(
        "canonical_convert",
        "canonical",
        "convert_canonical_era5.sbatch",
        "canonical_ready",
        "failed_convert",
    ),
    StageDefinition("forcing_produce", "forcing", "produce_forcing_analysis.sbatch", "forcing_ready", "failed_forcing"),
    StageDefinition("analysis_run", "analysis", "run_shud_analysis.sbatch", "forecast_running", "failed_run"),
    StageDefinition("parse_output", "parse", "parse_analysis_output.sbatch", "complete", "failed_parse"),
    StageDefinition("state_save_qc", "state", "save_state_snapshot.sbatch", "complete", "failed_publish"),
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
            object.__setattr__(self, "templates_dir", repo_root / "workers" / "sbatch_templates")
        else:
            object.__setattr__(self, "templates_dir", Path(self.templates_dir).expanduser().resolve())

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


@dataclass(frozen=True)
class PipelineResult:
    run_id: str
    cycle_id: str
    status: str
    stages: tuple[StageRunResult, ...]


@dataclass(frozen=True)
class ArrayTaskResult:
    task_id: int
    slurm_job_id: str
    status: str
    exit_code: int | None = None


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


@dataclass
class CycleOrchestrationContext:
    source_id: str
    cycle_time: datetime
    cycle_id: str
    run_id: str
    all_basins: list[dict[str, Any]]
    active_basins: list[dict[str, Any]]
    had_partial: bool = False
    last_partial_status: str | None = None


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
        if self.repository.has_active_orchestration(source_id=source, cycle_time=parsed_cycle_time):
            raise OrchestratorError(
                "PIPELINE_ALREADY_ACTIVE",
                f"An active orchestration already exists for {source} {format_cycle_time(parsed_cycle_time)}.",
                {"source_id": source, "cycle_time": _format_time(parsed_cycle_time), "cycle_id": cycle_id},
            )
        if cycle_id in self._active_cycles:
            raise OrchestratorError(
                "PIPELINE_ALREADY_ACTIVE",
                f"An active orchestration already exists for {source} {format_cycle_time(parsed_cycle_time)}.",
                {"source_id": source, "cycle_time": _format_time(parsed_cycle_time), "cycle_id": cycle_id},
            )

        normalized_basins = self._normalize_cycle_basins(basins, source, parsed_cycle_time)
        if not normalized_basins:
            raise OrchestratorError("EMPTY_BASIN_LIST", "orchestrate_cycle requires at least one basin.")

        self._active_cycles.add(cycle_id)
        try:
            self.repository.ensure_forecast_cycle(source_id=source, cycle_time=parsed_cycle_time)
            context = CycleOrchestrationContext(
                source_id=source,
                cycle_time=parsed_cycle_time,
                cycle_id=cycle_id,
                run_id=f"cycle_{source.lower()}_{format_cycle_time(parsed_cycle_time)}",
                all_basins=normalized_basins,
                active_basins=list(normalized_basins),
            )
            return self._run_cycle_chain(context)
        finally:
            self._active_cycles.discard(cycle_id)

    def sync_cycle_statuses(self, cycle_id: str) -> list[dict[str, Any]]:
        updates: list[dict[str, Any]] = []
        for job in self._query_pipeline_jobs_by_cycle(cycle_id):
            if str(job.get("status")) in TERMINAL_JOB_STATUSES or not job.get("slurm_job_id"):
                continue
            gateway_job = _coerce_mapping(self.slurm_client.get_job_status(str(job["slurm_job_id"])))
            new_status = _status_from_gateway_job(gateway_job)
            if new_status == str(job.get("status")):
                continue
            log_uri = self._log_uri_for_pipeline_job(job) if new_status in TERMINAL_JOB_STATUSES else None
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
            self.repository.insert_pipeline_event(
                entity_type="pipeline_job",
                entity_id=str(job["job_id"]),
                event_type="status_change",
                status_from=previous_status or str(job.get("status")),
                status_to=new_status,
                message=_stage_status_message(str(job.get("stage") or job.get("job_type")), new_status, gateway_job),
                details={
                    "cycle_id": cycle_id,
                    "slurm_job_id": job.get("slurm_job_id"),
                    "exit_code": gateway_job.get("exit_code"),
                    "error_code": gateway_job.get("error_code"),
                },
            )
            updates.append(record)
        return updates

    def _log_uri_for_pipeline_job(self, job: Mapping[str, Any]) -> str | None:
        if job.get("log_uri"):
            return str(job["log_uri"])
        run_id = job.get("run_id")
        stage = job.get("stage")
        if run_id and stage:
            return self.object_store.uri_for_key(f"runs/{run_id}/logs/{stage}.log")
        return None

    def _run_cycle_chain(self, context: CycleOrchestrationContext) -> PipelineResult:
        stage_results: list[StageRunResult] = []
        existing_jobs = self._query_pipeline_jobs_by_cycle(context.cycle_id)
        for stage_index, stage in enumerate(self.stages):
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
                    existing_jobs = self._query_pipeline_jobs_by_cycle(context.cycle_id)

                if len(stage_results) == stage_index:
                    stage_results.append(result)
                else:
                    stage_results[-1] = result

                if result.status in {"failed", "submission_failed", "permanently_failed"}:
                    retry_attempts += 1
                    retry_pipeline_job_id = self._schedule_cycle_stage_retry(result, retry_attempts)
                    if retry_pipeline_job_id is not None:
                        existing_jobs = [job for job in existing_jobs if not self._job_matches_stage(job, stage)]
                        continue
                    return PipelineResult(context.run_id, context.cycle_id, "failed", tuple(stage_results))

                if result.status == "cancelled":
                    return PipelineResult(context.run_id, context.cycle_id, "failed", tuple(stage_results))

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
            )
            for task in aggregation.task_results
        }
        pending_task_ids = [task.task_id for task in aggregation.task_results if task.status != "succeeded"]
        latest_result = result
        retry_attempts = 0

        try:
            while pending_task_ids:
                retry_attempts += 1
                if not self._schedule_cycle_stage_retry(latest_result, retry_attempts):
                    break

                retry_basins = self._reindexed_basins_for_task_ids(original_basins, pending_task_ids)
                retry_task_to_original = {index: task_id for index, task_id in enumerate(pending_task_ids)}
                context.active_basins = retry_basins
                latest_result, retry_aggregation = self._submit_and_wait_cycle_stage(stage, context)

                if retry_aggregation is None:
                    retry_status = "succeeded" if latest_result.status == "succeeded" else "failed"
                    for task_id in pending_task_ids:
                        task_results[task_id] = ArrayTaskResult(
                            task_id=task_id,
                            slurm_job_id=latest_result.slurm_job_id,
                            status=retry_status,
                            exit_code=latest_result.exit_code,
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
        final_result = StageRunResult(
            stage=stage.stage,
            job_type=stage.job_type,
            pipeline_job_id=latest_result.pipeline_job_id,
            slurm_job_id=latest_result.slurm_job_id,
            status=final_aggregation.status,
            exit_code=latest_result.exit_code,
            error_code=latest_result.error_code,
            error_message=latest_result.error_message,
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
                ),
                None,
            )

        self._before_cycle_stage_submit(stage, context)
        submitted: dict[str, Any]
        manifest_index_path: Path | None = None
        stage_manifest = self._build_cycle_stage_manifest(stage, context)
        try:
            if stage.is_array:
                tasks = self._reindexed_manifest_entries(context.active_basins)
                manifest_index_path = self._write_cycle_manifest_index(context, stage, tasks)
                stage_manifest["manifest_index_path"] = str(manifest_index_path)
                submitted = self._submit_array_stage(stage, context, tasks, stage_manifest)
            else:
                submitted = _coerce_mapping(
                    self.slurm_client.submit_job(
                        {
                            "run_id": context.run_id,
                            "model_id": _cycle_payload_model_id(context),
                            "job_type": stage.job_type,
                            "manifest": stage_manifest,
                        }
                    )
                )
        except Exception as error:
            return self._record_submission_failure(stage, context, error, pipeline_job_id=pipeline_job_id), None

        slurm_job_id = str(submitted["job_id"])
        log_uri = self.object_store.uri_for_key(f"runs/{context.run_id}/logs/{stage.stage}.log")
        submitted_status = _status_from_gateway_job(submitted)
        self.repository.upsert_pipeline_job(
            {
                "job_id": pipeline_job_id,
                "run_id": context.run_id,
                "cycle_id": context.cycle_id,
                "job_type": stage.job_type,
                "slurm_job_id": slurm_job_id,
                "model_id": None if not stage.is_array else _cycle_payload_model_id(context),
                "status": submitted_status,
                "stage": stage.stage,
                "submitted_at": _parse_gateway_time(submitted.get("submitted_at")) or _utcnow(),
                "started_at": _parse_gateway_time(submitted.get("started_at")),
                "finished_at": _parse_gateway_time(submitted.get("finished_at")),
                "exit_code": submitted.get("exit_code"),
                "error_code": submitted.get("error_code"),
                "error_message": submitted.get("error_message"),
                "log_uri": log_uri,
            }
        )
        self.repository.insert_pipeline_event(
            entity_type="pipeline_job",
            entity_id=pipeline_job_id,
            event_type="submission",
            status_from=None,
            status_to=submitted_status,
            message=f"{stage.stage} submitted as Slurm job {slurm_job_id}",
            details={
                "stage": stage.stage,
                "job_type": stage.job_type,
                "slurm_job_id": slurm_job_id,
                "manifest_index_path": str(manifest_index_path) if manifest_index_path else None,
            },
        )
        terminal = self._poll_cycle_stage_until_terminal(
            stage=stage,
            context=context,
            pipeline_job_id=pipeline_job_id,
            initial_job=submitted,
            initial_status=submitted_status,
            log_uri=log_uri,
        )
        self._persist_gateway_logs(slurm_job_id, log_uri)

        aggregation = self._aggregate_array_stage(stage, context, slurm_job_id, terminal) if stage.is_array else None
        result_status = aggregation.status if aggregation is not None else _status_from_gateway_job(terminal)
        if aggregation is not None:
            self._record_cycle_stage_status_override(stage, context, pipeline_job_id, terminal, aggregation, log_uri)

        self._after_cycle_stage_terminal(stage, context, result_status, terminal, aggregation)
        return (
            StageRunResult(
                stage=stage.stage,
                job_type=stage.job_type,
                pipeline_job_id=pipeline_job_id,
                slurm_job_id=slurm_job_id,
                status=result_status,
                exit_code=terminal.get("exit_code"),
                error_code=terminal.get("error_code"),
                error_message=terminal.get("error_message"),
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
        if status not in TERMINAL_JOB_STATUSES and job.get("slurm_job_id"):
            terminal = self._poll_cycle_stage_until_terminal(
                stage=stage,
                context=context,
                pipeline_job_id=str(job["job_id"]),
                initial_job={"job_id": job["slurm_job_id"], "status": status},
                initial_status=status,
                log_uri=job.get("log_uri"),
            )
            status = _status_from_gateway_job(terminal)

        aggregation = None
        if (
            stage.is_array
            and job.get("slurm_job_id")
            and status not in {"failed", "cancelled", "submission_failed", "permanently_failed"}
        ):
            aggregation = self._aggregate_array_stage(stage, context, str(job["slurm_job_id"]), terminal)
            status = aggregation.status
            if str(job.get("status")) not in TERMINAL_JOB_STATUSES or status != str(job.get("status")):
                log_uri = str(
                    job.get("log_uri")
                    or self._log_uri_for_pipeline_job(job)
                    or self.object_store.uri_for_key(f"runs/{context.run_id}/logs/{stage.stage}.log")
                )
                self._record_cycle_stage_status_override(
                    stage,
                    context,
                    str(job["job_id"]),
                    terminal,
                    aggregation,
                    log_uri,
                )
            if status == "partially_failed":
                context.had_partial = True
                context.last_partial_status = self._partial_cycle_status(stage)

        self._after_cycle_stage_terminal(stage, context, status, terminal, aggregation)
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
        log_uri: str | None,
    ) -> dict[str, Any]:
        job = dict(initial_job)
        current_status = initial_status
        deadline = time.monotonic() + self.config.job_timeout_seconds
        while _status_from_gateway_job(job) not in TERMINAL_JOB_STATUSES:
            if time.monotonic() >= deadline:
                raise OrchestratorError(
                    "SLURM_JOB_TIMEOUT",
                    f"Stage {stage.stage} did not reach a terminal status before timeout.",
                    {"stage": stage.stage, "cycle_id": context.cycle_id, "slurm_job_id": job["job_id"]},
                )
            time.sleep(self.config.poll_interval_seconds)
            job = _coerce_mapping(self.slurm_client.get_job_status(str(job["job_id"])))
            new_status = _status_from_gateway_job(job)
            if new_status == current_status:
                continue
            if stage.is_array and new_status in TERMINAL_JOB_STATUSES:
                current_status = new_status
                continue
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
            persisted_status = str(record.get("status") or new_status)
            if persisted_status != new_status:
                job["status"] = persisted_status
                current_status = persisted_status
                if persisted_status in TERMINAL_JOB_STATUSES:
                    return job
                continue
            self.repository.insert_pipeline_event(
                entity_type="pipeline_job",
                entity_id=pipeline_job_id,
                event_type="status_change",
                status_from=previous_status or current_status,
                status_to=new_status,
                message=_stage_status_message(stage.stage, new_status, job),
                details={
                    "stage": stage.stage,
                    "job_type": stage.job_type,
                    "slurm_job_id": job["job_id"],
                    "exit_code": job.get("exit_code"),
                    "error_code": job.get("error_code"),
                },
            )
            current_status = new_status
        return job

    def _submit_array_stage(
        self,
        stage: StageDefinition,
        context: CycleOrchestrationContext,
        tasks: list[dict[str, Any]],
        manifest: dict[str, Any],
    ) -> dict[str, Any]:
        submit_job_array = getattr(self.slurm_client, "submit_job_array", None)
        if callable(submit_job_array):
            return _coerce_mapping(
                submit_job_array(
                    stage.job_type,
                    cycle_id=context.cycle_id,
                    stage_name=stage.stage,
                    tasks=tasks,
                    manifest=manifest,
                )
            )
        return _coerce_mapping(
            self.slurm_client.submit_job(
                {
                    "run_id": context.run_id,
                    "model_id": _cycle_payload_model_id(context),
                    "job_type": stage.job_type,
                    "manifest": {**manifest, "tasks": tasks},
                }
            )
        )

    def _aggregate_array_stage(
        self,
        _stage: StageDefinition,
        context: CycleOrchestrationContext,
        slurm_job_id: str,
        terminal: dict[str, Any],
    ) -> ArrayAggregation:
        provider = getattr(self.slurm_client, "get_array_task_results", None)
        if callable(provider):
            try:
                raw_results = provider(slurm_job_id)
            except (KeyError, LookupError):
                raw_results = None
            if raw_results is not None:
                return _coerce_array_aggregation(raw_results, slurm_job_id)

        stdout_provider = getattr(self.slurm_client, "get_array_sacct_output", None)
        if callable(stdout_provider):
            return parse_sacct_array_results(str(stdout_provider(slurm_job_id)), slurm_job_id)

        status = _status_from_gateway_job(terminal)
        task_status = "succeeded" if status == "succeeded" else "cancelled" if status == "cancelled" else "failed"
        results = tuple(
            ArrayTaskResult(
                task_id=index,
                slurm_job_id=f"{slurm_job_id}_{index}",
                status=task_status,
                exit_code=terminal.get("exit_code"),
            )
            for index, _basin in enumerate(context.active_basins)
        )
        return _aggregation_from_task_results(results)

    def _record_cycle_stage_status_override(
        self,
        stage: StageDefinition,
        context: CycleOrchestrationContext,
        pipeline_job_id: str,
        terminal: dict[str, Any],
        aggregation: ArrayAggregation,
        log_uri: str,
    ) -> None:
        previous_status, record = self.repository.update_pipeline_job_status(
            pipeline_job_id,
            aggregation.status,
            finished_at=_parse_gateway_time(terminal.get("finished_at")) or _utcnow(),
            exit_code=terminal.get("exit_code"),
            error_code=terminal.get("error_code"),
            error_message=terminal.get("error_message"),
            log_uri=log_uri,
        )
        if str(record.get("status")) != aggregation.status:
            return
        self.repository.insert_pipeline_event(
            entity_type="pipeline_job",
            entity_id=pipeline_job_id,
            event_type="status_change",
            status_from=previous_status or _status_from_gateway_job(terminal),
            status_to=aggregation.status,
            message=f"{stage.stage} array aggregated as {aggregation.status}",
            details={
                "stage": stage.stage,
                "job_type": stage.job_type,
                "total": aggregation.total,
                "succeeded": aggregation.succeeded,
                "failed": aggregation.failed,
                "cancelled": aggregation.cancelled,
            },
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
        message = str(error)
        self.repository.upsert_pipeline_job(
            {
                "job_id": pipeline_job_id,
                "run_id": context.run_id,
                "cycle_id": context.cycle_id,
                "job_type": stage.job_type,
                "slurm_job_id": None,
                "model_id": None if not stage.is_array else _cycle_payload_model_id(context),
                "status": "submission_failed",
                "stage": stage.stage,
                "submitted_at": now,
                "started_at": None,
                "finished_at": now,
                "exit_code": None,
                "error_code": "SBATCH_SUBMISSION_FAILED",
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
            details={"stage": stage.stage, "job_type": stage.job_type, "error": message},
        )
        self.repository.update_forecast_cycle_status(
            source_id=context.source_id,
            cycle_time=context.cycle_time,
            status=stage.failure_cycle_status,
            error_code="SBATCH_SUBMISSION_FAILED",
            error_message=message,
        )
        return StageRunResult(
            stage=stage.stage,
            job_type=stage.job_type,
            pipeline_job_id=pipeline_job_id,
            slurm_job_id="",
            status="submission_failed",
            error_code="SBATCH_SUBMISSION_FAILED",
            error_message=message,
        )

    def _apply_array_progress(
        self,
        stage: StageDefinition,
        context: CycleOrchestrationContext,
        aggregation: ArrayAggregation,
    ) -> None:
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
            "total_basins": len(context.all_basins),
            "active_basins": len(context.active_basins),
        }
        if stage.stage == "publish":
            active_keys = {_basin_key(basin) for basin in context.active_basins}
            excluded = [basin for basin in context.all_basins if _basin_key(basin) not in active_keys]
            manifest["metadata"] = {
                "total_basins": len(context.all_basins),
                "published_basins": len(context.active_basins),
                "excluded_basins": [_basin_identifier(basin) for basin in excluded],
            }
            manifest["basins"] = list(context.active_basins)
        return manifest

    def _write_cycle_manifest_index(
        self,
        context: CycleOrchestrationContext,
        stage: StageDefinition,
        tasks: list[dict[str, Any]],
    ) -> Path:
        manifest_dir = self._workspace_path("runs", context.run_id, "input")
        manifest_dir.mkdir(parents=True, exist_ok=True)
        manifest_path = self._workspace_path("runs", context.run_id, "input", f"{stage.stage}_manifest_index.json")
        manifest_path.write_text(json.dumps(tasks, indent=2, sort_keys=True), encoding="utf-8")
        return manifest_path

    def _reindexed_manifest_entries(self, basins: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
        return build_reindexed_manifest([dict(basin) for basin in basins], range(len(basins)))

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
                    "model_package_uri": basin.model_package_uri,
                }
            else:
                entry = dict(basin)
            model_id = str(entry.get("model_id") or "")
            if not model_id:
                raise OrchestratorError("BASIN_MODEL_ID_MISSING", "Each basin entry requires model_id.")
            entry.setdefault("basin_id", entry.get("model_id"))
            entry.setdefault("basin_version_id", f"{model_id}_basin")
            entry.setdefault("river_network_version_id", f"{model_id}_river")
            entry.setdefault("run_id", f"fcst_{source_id.lower()}_{compact_cycle}_{model_id}")
            entry.setdefault("workspace_dir", str(Path(self.config.workspace_root)))
            entry.setdefault("source_id", source_id)
            entry.setdefault("cycle_time", compact_cycle)
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

    def _query_pipeline_jobs_by_cycle(self, cycle_id: str) -> list[dict[str, Any]]:
        query = getattr(self.repository, "query_pipeline_jobs_by_cycle", None)
        if callable(query):
            return [dict(job) for job in query(cycle_id)]
        return []

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
        initial_state = self._select_forecast_initial_state(model_id=model_id, cycle_time=parsed_cycle_time)
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
            for model_id in selected_model_ids:
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

        rendered_script = self.render_stage_template(stage, context)
        payload = {
            "run_id": context.run_id,
            "model_id": context.model_id,
            "script": rendered_script,
            "manifest": {
                "run_id": context.run_id,
                "model_id": context.model_id,
                "stage": stage.stage,
                "job_type": stage.job_type,
                "source_id": context.source_id,
                "cycle_time": _format_time(context.cycle_time),
                "script": rendered_script,
            },
        }
        submitted = self.slurm_client.submit_job(payload)
        slurm_job_id = str(submitted["job_id"])
        pipeline_job_id = _pipeline_job_id(context.run_id, stage.stage)
        log_uri = self.object_store.uri_for_key(f"runs/{context.run_id}/logs/{stage.stage}.log")
        current_status = _status_from_gateway_job(submitted)
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
                "log_uri": log_uri,
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
            details={"stage": stage.stage, "slurm_job_id": slurm_job_id},
        )
        if first_stage:
            self.repository.update_hydro_run_status(context.run_id, "submitted", slurm_job_id=slurm_job_id)

        terminal = self._poll_until_terminal(
            stage=stage,
            context=context,
            pipeline_job_id=pipeline_job_id,
            initial_job=submitted,
            initial_status=str(pipeline_record["status"]),
        )
        self._persist_gateway_logs(slurm_job_id, log_uri)

        if terminal["status"] == "succeeded":
            self._after_stage_success(stage, context, terminal)
        else:
            self._after_stage_failure(stage, context, terminal)

        return StageRunResult(
            stage=stage.stage,
            job_type=stage.job_type,
            pipeline_job_id=pipeline_job_id,
            slurm_job_id=slurm_job_id,
            status=str(terminal["status"]),
            exit_code=terminal.get("exit_code"),
            error_code=terminal.get("error_code"),
            error_message=terminal.get("error_message"),
        )

    def _poll_until_terminal(
        self,
        *,
        stage: StageDefinition,
        context: ForecastRunContext | AnalysisRunContext,
        pipeline_job_id: str,
        initial_job: dict[str, Any],
        initial_status: str,
    ) -> dict[str, Any]:
        job = initial_job
        current_status = initial_status
        deadline = time.monotonic() + self.config.job_timeout_seconds
        while _status_from_gateway_job(job) not in TERMINAL_JOB_STATUSES:
            if time.monotonic() >= deadline:
                raise OrchestratorError(
                    "SLURM_JOB_TIMEOUT",
                    f"Stage {stage.stage} did not reach a terminal status before timeout.",
                    {"stage": stage.stage, "run_id": context.run_id, "slurm_job_id": job["job_id"]},
                )
            time.sleep(self.config.poll_interval_seconds)
            job = self.slurm_client.get_job_status(str(job["job_id"]))
            new_status = _status_from_gateway_job(job)
            if new_status == current_status:
                continue
            previous_status, record = self.repository.update_pipeline_job_status(
                pipeline_job_id,
                new_status,
                started_at=_parse_gateway_time(job.get("started_at")),
                finished_at=_parse_gateway_time(job.get("finished_at")),
                exit_code=job.get("exit_code"),
                error_code=job.get("error_code"),
                error_message=job.get("error_message"),
            )
            persisted_status = str(record.get("status") or new_status)
            if persisted_status != new_status:
                job["status"] = persisted_status
                current_status = persisted_status
                if persisted_status in TERMINAL_JOB_STATUSES:
                    return job
                continue
            entity_type, entity_id = self._pipeline_event_target(context, pipeline_job_id)
            self.repository.insert_pipeline_event(
                entity_type=entity_type,
                entity_id=entity_id,
                event_type="status_change",
                status_from=previous_status or current_status,
                status_to=new_status,
                message=_stage_status_message(stage.stage, new_status, job),
                details={"stage": stage.stage, "slurm_job_id": job["job_id"]},
            )
            self._after_stage_status_change(stage, context, previous_status or current_status, new_status, job)
            current_status = new_status

        terminal_status = _status_from_gateway_job(job)
        if terminal_status != current_status:
            previous_status, record = self.repository.update_pipeline_job_status(
                pipeline_job_id,
                terminal_status,
                started_at=_parse_gateway_time(job.get("started_at")),
                finished_at=_parse_gateway_time(job.get("finished_at")),
                exit_code=job.get("exit_code"),
                error_code=job.get("error_code"),
                error_message=job.get("error_message"),
            )
            persisted_status = str(record.get("status") or terminal_status)
            if persisted_status != terminal_status:
                job["status"] = persisted_status
                return job
            entity_type, entity_id = self._pipeline_event_target(context, pipeline_job_id)
            self.repository.insert_pipeline_event(
                entity_type=entity_type,
                entity_id=entity_id,
                event_type="status_change",
                status_from=previous_status or current_status,
                status_to=terminal_status,
                message=_stage_status_message(stage.stage, terminal_status, job),
                details={"stage": stage.stage, "slurm_job_id": job["job_id"]},
            )
            self._after_stage_status_change(stage, context, previous_status or current_status, terminal_status, job)
        return job

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
        self.object_store.write_bytes_atomic(log_uri, content.encode("utf-8"))

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
            output_uri=self.object_store.uri_for_key(f"runs/{run_id}/output/"),
            log_uri=self.object_store.uri_for_key(f"runs/{run_id}/logs/"),
            init_state_id=selected_state.state_id,
            init_state_uri=selected_state.state_uri,
            init_state_valid_time=selected_state.valid_time,
            init_state_checksum=selected_state.checksum,
            init_state_quality=selected_state.quality,
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
            },
        }

    def _select_forecast_initial_state(self, *, model_id: str, cycle_time: datetime) -> InitialStateSelection:
        if self.state_manager is None:
            return InitialStateSelection(None, None, None, None, "cold_start_no_state")

        state = self.state_manager.get_latest_usable_state(model_id=model_id, before_time=cycle_time)
        if state is None:
            return InitialStateSelection(None, None, None, None, "cold_start_no_state")

        quality = assess_freshness(
            state.valid_time,
            cycle_time,
            soft_threshold_days=self.config.state_soft_stale_threshold_days,
            hard_threshold_days=self.config.state_hard_stale_threshold_days,
        )
        if quality == "cold_start_stale_state":
            return InitialStateSelection(None, None, None, None, quality)

        return InitialStateSelection(
            state_id=state.state_id,
            state_uri=state.state_uri,
            valid_time=state.valid_time,
            checksum=state.checksum,
            quality=quality,
        )

    def _write_run_manifest(self, context: ForecastRunContext | AnalysisRunContext, manifest: dict[str, Any]) -> None:
        content = json.dumps(manifest, indent=2, sort_keys=True).encode("utf-8")
        self.object_store.write_bytes_atomic(context.run_manifest_uri, content)
        workspace_manifest = self._workspace_path("runs", context.run_id, "input", "manifest.json")
        workspace_manifest.parent.mkdir(parents=True, exist_ok=True)
        workspace_manifest.write_bytes(content)

    def _workspace_path(self, *parts: str) -> Path:
        workspace_root = Path(self.config.workspace_root).expanduser().resolve()
        resolved = workspace_root.joinpath(*parts).resolve()
        try:
            resolved.relative_to(workspace_root)
        except ValueError as exc:
            raise OrchestratorError(
                "WORKSPACE_PATH_ESCAPE",
                "Resolved workspace path is outside workspace_root.",
                {"path": str(resolved), "workspace_root": str(workspace_root)},
            ) from exc
        return resolved


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
            output_uri=self.object_store.uri_for_key(f"runs/{run_id}/output/"),
            log_uri=self.object_store.uri_for_key(f"runs/{run_id}/logs/"),
            init_state_id=init_state.state_id if init_state is not None else None,
            init_state_uri=init_state.state_uri if init_state is not None else None,
            init_state_valid_time=init_state.valid_time if init_state is not None else None,
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
            "runtime": {
                "output_interval_minutes": 60,
                "init_mode": 3 if context.init_state_id else 1,
            },
            "outputs": {
                "run_manifest_uri": context.run_manifest_uri,
                "output_uri": context.output_uri,
                "log_uri": context.log_uri,
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
        row = self._fetch_optional(
            """
            SELECT 1 AS active
            FROM hydro.hydro_run h
            LEFT JOIN ops.pipeline_job pj ON pj.run_id = h.run_id
            WHERE h.source_id = %s
              AND h.cycle_time = %s
              AND h.model_id = %s
              AND (
                    h.status::text = ANY(%s)
                 OR COALESCE(pj.status, 'pending') NOT IN (
                    'succeeded', 'partially_failed', 'failed', 'cancelled', 'submission_failed', 'permanently_failed'
                 )
              )
            LIMIT 1
            """,
            (source_id, cycle_time, model_id, list(ACTIVE_HYDRO_STATUSES)),
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
                MAX(cmp.lead_time_hours) AS max_lead_hours
            FROM met.forecast_cycle fc
            LEFT JOIN met.canonical_met_product cmp
              ON cmp.source_id = fc.source_id
             AND cmp.cycle_time = fc.cycle_time
             AND cmp.quality_flag <> 'fail'
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
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (job_id) DO UPDATE SET
                slurm_job_id = EXCLUDED.slurm_job_id,
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


def _cycle_payload_model_id(context: CycleOrchestrationContext) -> str:
    if context.active_basins:
        return str(context.active_basins[0].get("model_id") or "cycle")
    return "cycle"


def _basin_key(basin: Mapping[str, Any]) -> tuple[str, str]:
    return (str(basin.get("model_id") or ""), str(basin.get("basin_id") or basin.get("model_id") or ""))


def _basin_identifier(basin: Mapping[str, Any]) -> str:
    return str(basin.get("basin_id") or basin.get("model_id") or "")


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


def parse_sacct_array_results(stdout: str, master_job_id: str) -> ArrayAggregation:
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
        results.append(
            ArrayTaskResult(
                task_id=int(match.group(1)),
                slurm_job_id=job_id,
                status=_array_task_status(raw_state),
                exit_code=_parse_slurm_exit_code(raw_exit_code),
            )
        )
    return _aggregation_from_task_results(tuple(sorted(results, key=lambda result: result.task_id)))


def _coerce_array_aggregation(raw_results: Any, master_job_id: str) -> ArrayAggregation:
    if isinstance(raw_results, ArrayAggregation):
        return raw_results
    if isinstance(raw_results, str):
        return parse_sacct_array_results(raw_results, master_job_id)
    if isinstance(raw_results, Mapping):
        if isinstance(raw_results.get("stdout"), str):
            return parse_sacct_array_results(str(raw_results["stdout"]), master_job_id)
        tasks = raw_results.get("tasks") or raw_results.get("task_results")
        if isinstance(tasks, Sequence) and not isinstance(tasks, str | bytes):
            return _coerce_array_aggregation(tasks, master_job_id)
    if isinstance(raw_results, Sequence) and not isinstance(raw_results, str | bytes):
        task_results = []
        for index, item in enumerate(raw_results):
            item_dict = _coerce_mapping(item)
            task_id = int(item_dict.get("task_id", index))
            status = str(item_dict.get("status") or _array_task_status(str(item_dict.get("state", ""))))
            task_results.append(
                ArrayTaskResult(
                    task_id=task_id,
                    slurm_job_id=str(
                        item_dict.get("slurm_job_id") or item_dict.get("job_id") or f"{master_job_id}_{task_id}"
                    ),
                    status=status,
                    exit_code=item_dict.get("exit_code"),
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


def _stage_status_message(stage: str, status: str, job: dict[str, Any]) -> str:
    if status == "failed":
        error_code = job.get("error_code") or "UNKNOWN"
        error_message = job.get("error_message") or "No error message provided."
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


def _max_lead_hours_from_lineage(value: Any) -> int | None:
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except json.JSONDecodeError:
            return None
    if not isinstance(value, Mapping):
        return None
    return _optional_int(value.get("max_lead_hours"))


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
