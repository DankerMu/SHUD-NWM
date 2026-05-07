from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Protocol

import httpx

from packages.common.object_store import LocalObjectStore
from workers.data_adapters.base import cycle_id_for, format_cycle_time, parse_cycle_time

TERMINAL_JOB_STATUSES = {"succeeded", "failed", "cancelled"}
ACTIVE_HYDRO_STATUSES = {"created", "staged", "submitted", "running", "succeeded"}


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


class SlurmClientError(OrchestratorError):
    pass


@dataclass(frozen=True)
class StageDefinition:
    stage: str
    job_type: str
    template_name: str
    success_cycle_status: str
    failure_cycle_status: str


STAGES: tuple[StageDefinition, ...] = (
    StageDefinition("download_gfs", "download", "download_gfs.sbatch", "raw_complete", "failed_download"),
    StageDefinition("convert_canonical", "canonical", "convert_canonical.sbatch", "canonical_ready", "failed_convert"),
    StageDefinition("produce_forcing", "forcing", "produce_forcing.sbatch", "forcing_ready", "failed_forcing"),
    StageDefinition("run_shud_forecast", "forecast", "run_shud_forecast.sbatch", "forecast_running", "failed_run"),
    StageDefinition("parse_output", "parse", "parse_output.sbatch", "complete", "failed_parse"),
)


@dataclass(frozen=True)
class OrchestratorConfig:
    workspace_root: Path | str
    object_store_root: Path | str
    object_store_prefix: str = ""
    slurm_gateway_url: str = "http://localhost:8000"
    templates_dir: Path | str | None = None
    poll_interval_seconds: float = 1.0
    job_timeout_seconds: float = 3600.0
    forecast_horizon_hours: int = 168
    scenario_id: str = "forecast_gfs_deterministic"

    def __post_init__(self) -> None:
        object.__setattr__(self, "workspace_root", Path(self.workspace_root).expanduser().resolve())
        object.__setattr__(self, "object_store_root", Path(self.object_store_root).expanduser().resolve())
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
            poll_interval_seconds=float(os.getenv("ORCHESTRATOR_POLL_INTERVAL_SECONDS", "1")),
            job_timeout_seconds=float(os.getenv("ORCHESTRATOR_JOB_TIMEOUT_SECONDS", "3600")),
            forecast_horizon_hours=int(os.getenv("FORECAST_HORIZON_HOURS", "168")),
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


@dataclass(frozen=True)
class ForecastRunContext:
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


class SlurmGatewayClient(Protocol):
    def submit_job(self, payload: dict[str, Any]) -> dict[str, Any]:
        raise NotImplementedError

    def get_job_status(self, job_id: str) -> dict[str, Any]:
        raise NotImplementedError

    def fetch_logs(self, job_id: str) -> dict[str, Any]:
        raise NotImplementedError


class OrchestratorRepository(Protocol):
    def has_active_pipeline(self, *, source_id: str, cycle_time: datetime, model_id: str) -> bool:
        raise NotImplementedError

    def load_model_context(self, model_id: str) -> ModelContext:
        raise NotImplementedError

    def find_forcing_context(self, *, source_id: str, cycle_time: datetime, model_id: str) -> ForcingContext:
        raise NotImplementedError

    def ensure_forecast_cycle(self, *, source_id: str, cycle_time: datetime) -> dict[str, Any]:
        raise NotImplementedError

    def create_hydro_run(self, context: ForecastRunContext, manifest: dict[str, Any]) -> dict[str, Any]:
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
    ) -> tuple[str | None, dict[str, Any]]:
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

    def get_job_status(self, job_id: str) -> dict[str, Any]:
        return self._request("GET", f"/api/v1/slurm/jobs/{job_id}", expected=(200,))

    def fetch_logs(self, job_id: str) -> dict[str, Any]:
        return self._request("GET", f"/api/v1/slurm/jobs/{job_id}/logs", expected=(200,))

    def _request(
        self,
        method: str,
        path: str,
        *,
        expected: tuple[int, ...],
        json: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
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
    def __init__(
        self,
        *,
        config: OrchestratorConfig,
        repository: OrchestratorRepository,
        slurm_client: SlurmGatewayClient | None = None,
        object_store: LocalObjectStore | None = None,
    ) -> None:
        self.config = config
        self.repository = repository
        self.slurm_client = slurm_client or HttpSlurmGatewayClient(config.slurm_gateway_url)
        self.object_store = object_store or LocalObjectStore(config.object_store_root, config.object_store_prefix)

    @classmethod
    def from_env(cls) -> ForecastOrchestrator:
        config = OrchestratorConfig.from_env()
        return cls(config=config, repository=PsycopgOrchestratorRepository.from_env())

    def trigger_forecast(
        self,
        *,
        source_id: str,
        cycle_time: str | datetime,
        model_id: str,
        basin_id: str | None = None,
    ) -> PipelineResult:
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
        context = self._build_run_context(source_id, parsed_cycle_time, model, forcing)
        manifest = self._build_run_manifest(context)
        self._write_run_manifest(context, manifest)
        self.repository.create_hydro_run(context, manifest)
        self.repository.update_hydro_run_status(context.run_id, "staged")
        return self.run_chain(context)

    def run_chain(self, context: ForecastRunContext) -> PipelineResult:
        stage_results: list[StageRunResult] = []
        for index, stage in enumerate(STAGES):
            result = self._submit_and_wait(stage, context, first_stage=index == 0)
            stage_results.append(result)
            if result.status != "succeeded":
                return PipelineResult(context.run_id, context.cycle_id, "failed", tuple(stage_results))
        return PipelineResult(context.run_id, context.cycle_id, "parsed", tuple(stage_results))

    def stage_statuses(
        self,
        *,
        cycle_time: str | datetime,
        source_id: str | None = None,
        model_id: str | None = None,
    ) -> list[dict[str, Any]]:
        return self.repository.list_stage_statuses(
            source_id=source_id,
            cycle_time=parse_cycle_time(cycle_time),
            model_id=model_id,
        )

    def _submit_and_wait(
        self,
        stage: StageDefinition,
        context: ForecastRunContext,
        *,
        first_stage: bool,
    ) -> StageRunResult:
        if stage.stage == "download_gfs":
            self.repository.update_forecast_cycle_status(
                source_id=context.source_id,
                cycle_time=context.cycle_time,
                status="downloading",
            )
        elif stage.stage == "run_shud_forecast":
            self.repository.update_forecast_cycle_status(
                source_id=context.source_id,
                cycle_time=context.cycle_time,
                status="forecast_running",
            )

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
        current_status = str(submitted["status"])
        pipeline_record = self.repository.upsert_pipeline_job(
            {
                "job_id": pipeline_job_id,
                "run_id": context.run_id,
                "cycle_id": context.cycle_id,
                "job_type": stage.job_type,
                "slurm_job_id": slurm_job_id,
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
        self.repository.insert_pipeline_event(
            entity_type="pipeline_job",
            entity_id=pipeline_job_id,
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
            self.repository.update_forecast_cycle_status(
                source_id=context.source_id,
                cycle_time=context.cycle_time,
                status=stage.success_cycle_status,
            )
            if stage.stage == "run_shud_forecast":
                self.repository.update_hydro_run_status(context.run_id, "succeeded")
            elif stage.stage == "parse_output":
                self.repository.update_hydro_run_status(context.run_id, "parsed")
        else:
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
        context: ForecastRunContext,
        pipeline_job_id: str,
        initial_job: dict[str, Any],
        initial_status: str,
    ) -> dict[str, Any]:
        job = initial_job
        current_status = initial_status
        deadline = time.monotonic() + self.config.job_timeout_seconds
        while str(job["status"]) not in TERMINAL_JOB_STATUSES:
            if time.monotonic() >= deadline:
                raise OrchestratorError(
                    "SLURM_JOB_TIMEOUT",
                    f"Stage {stage.stage} did not reach a terminal status before timeout.",
                    {"stage": stage.stage, "run_id": context.run_id, "slurm_job_id": job["job_id"]},
                )
            time.sleep(self.config.poll_interval_seconds)
            job = self.slurm_client.get_job_status(str(job["job_id"]))
            new_status = str(job["status"])
            if new_status == current_status:
                continue
            previous_status, _record = self.repository.update_pipeline_job_status(
                pipeline_job_id,
                new_status,
                started_at=_parse_gateway_time(job.get("started_at")),
                finished_at=_parse_gateway_time(job.get("finished_at")),
                exit_code=job.get("exit_code"),
                error_code=job.get("error_code"),
                error_message=job.get("error_message"),
            )
            self.repository.insert_pipeline_event(
                entity_type="pipeline_job",
                entity_id=pipeline_job_id,
                event_type="status_change",
                status_from=previous_status or current_status,
                status_to=new_status,
                message=_stage_status_message(stage.stage, new_status, job),
                details={"stage": stage.stage, "slurm_job_id": job["job_id"]},
            )
            current_status = new_status

        if str(job["status"]) != current_status:
            previous_status, _record = self.repository.update_pipeline_job_status(
                pipeline_job_id,
                str(job["status"]),
                started_at=_parse_gateway_time(job.get("started_at")),
                finished_at=_parse_gateway_time(job.get("finished_at")),
                exit_code=job.get("exit_code"),
                error_code=job.get("error_code"),
                error_message=job.get("error_message"),
            )
            self.repository.insert_pipeline_event(
                entity_type="pipeline_job",
                entity_id=pipeline_job_id,
                event_type="status_change",
                status_from=previous_status or current_status,
                status_to=str(job["status"]),
                message=_stage_status_message(stage.stage, str(job["status"]), job),
                details={"stage": stage.stage, "slurm_job_id": job["job_id"]},
            )
        return job

    def render_stage_template(self, stage: StageDefinition, context: ForecastRunContext) -> str:
        template_path = Path(self.config.templates_dir) / stage.template_name
        if not template_path.exists():
            raise OrchestratorError("SBATCH_TEMPLATE_MISSING", f"Missing sbatch template: {template_path.name}")
        run_manifest_path = Path(self.config.workspace_root) / "runs" / context.run_id / "input" / "manifest.json"
        return template_path.read_text(encoding="utf-8").format(
            source_id=context.source_id,
            source_id_lower=context.source_id.lower(),
            cycle_time=format_cycle_time(context.cycle_time),
            cycle_time_iso=_format_time(context.cycle_time),
            model_id=context.model_id,
            basin_id=context.basin_id or "",
            basin_version_id=context.basin_version_id,
            river_network_version_id=context.river_network_version_id,
            run_id=context.run_id,
            stage_name=stage.stage,
            job_type=stage.job_type,
            workspace_dir=str(Path(self.config.workspace_root)),
            object_store_root=str(Path(self.config.object_store_root)),
            object_store_prefix=self.config.object_store_prefix,
            run_manifest_path=str(run_manifest_path),
        )

    def _persist_gateway_logs(self, slurm_job_id: str, log_uri: str) -> None:
        logs = self.slurm_client.fetch_logs(slurm_job_id)
        content = str(logs.get("logs", ""))
        self.object_store.write_bytes_atomic(log_uri, content.encode("utf-8"))

    def _build_run_context(
        self,
        source_id: str,
        cycle_time: datetime,
        model: ModelContext,
        forcing: ForcingContext,
    ) -> ForecastRunContext:
        compact_cycle = format_cycle_time(cycle_time)
        run_id = f"fcst_{source_id.lower()}_{compact_cycle}_{model.model_id}"
        start_time = cycle_time
        end_time = cycle_time + timedelta(hours=self.config.forecast_horizon_hours)
        fallback_forcing_uri = (
            f"forcing/{source_id.lower()}/{compact_cycle}/{model.basin_version_id}/{model.model_id}/"
        )
        return ForecastRunContext(
            run_id=run_id,
            source_id=source_id,
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
            run_manifest_uri=self.object_store.uri_for_key(f"runs/{run_id}/input/manifest.json"),
            output_uri=self.object_store.uri_for_key(f"runs/{run_id}/output/"),
            log_uri=self.object_store.uri_for_key(f"runs/{run_id}/logs/"),
        )

    def _build_run_manifest(self, context: ForecastRunContext) -> dict[str, Any]:
        return {
            "run_id": context.run_id,
            "run_type": "forecast",
            "scenario_id": self.config.scenario_id,
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
            "forcing": {
                "forcing_version_id": context.forcing_version_id,
                "forcing_uri": context.forcing_package_uri,
            },
            "runtime": {
                "output_interval_minutes": 60,
            },
            "outputs": {
                "run_manifest_uri": context.run_manifest_uri,
                "output_uri": context.output_uri,
                "log_uri": context.log_uri,
            },
        }

    def _write_run_manifest(self, context: ForecastRunContext, manifest: dict[str, Any]) -> None:
        content = json.dumps(manifest, indent=2, sort_keys=True).encode("utf-8")
        self.object_store.write_bytes_atomic(context.run_manifest_uri, content)
        workspace_manifest = Path(self.config.workspace_root) / "runs" / context.run_id / "input" / "manifest.json"
        workspace_manifest.parent.mkdir(parents=True, exist_ok=True)
        workspace_manifest.write_bytes(content)


@dataclass(frozen=True)
class PsycopgOrchestratorRepository:
    database_url: str

    @classmethod
    def from_env(cls) -> PsycopgOrchestratorRepository:
        database_url = os.getenv("DATABASE_URL", "").strip()
        if not database_url:
            raise OrchestratorError("DATABASE_URL_MISSING", "DATABASE_URL is required for orchestration.")
        return cls(database_url)

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
                 OR COALESCE(pj.status, 'pending') NOT IN ('succeeded', 'failed', 'cancelled')
              )
            LIMIT 1
            """,
            (source_id, cycle_time, model_id, list(ACTIVE_HYDRO_STATUSES)),
        )
        return row is not None

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
            SELECT forcing_version_id, forcing_package_uri
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
        return ForcingContext(row.get("forcing_version_id"), row.get("forcing_package_uri"))

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

    def create_hydro_run(self, context: ForecastRunContext, manifest: dict[str, Any]) -> dict[str, Any]:
        return self._fetch_one(
            """
            INSERT INTO hydro.hydro_run (
                run_id,
                run_type,
                scenario_id,
                model_id,
                basin_version_id,
                forcing_version_id,
                source_id,
                cycle_time,
                start_time,
                end_time,
                status,
                run_manifest_uri,
                output_uri,
                log_uri
            )
            VALUES (%s, 'forecast', %s, %s, %s, %s, %s, %s, %s, %s, 'created', %s, %s, %s)
            ON CONFLICT (run_id) DO UPDATE SET
                status = 'created',
                forcing_version_id = EXCLUDED.forcing_version_id,
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
                manifest["scenario_id"],
                context.model_id,
                context.basin_version_id,
                context.forcing_version_id,
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
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (job_id) DO UPDATE SET
                slurm_job_id = EXCLUDED.slurm_job_id,
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
    ) -> tuple[str | None, dict[str, Any]]:
        current = self._fetch_optional("SELECT status FROM ops.pipeline_job WHERE job_id = %s", (job_id,))
        previous_status = current.get("status") if current is not None else None
        record = self._fetch_one(
            """
            UPDATE ops.pipeline_job
            SET status = %s,
                started_at = COALESCE(%s, started_at),
                finished_at = COALESCE(%s, finished_at),
                exit_code = COALESCE(%s, exit_code),
                error_code = COALESCE(%s, error_code),
                error_message = COALESCE(%s, error_message),
                updated_at = now()
            WHERE job_id = %s
            RETURNING *
            """,
            (status, started_at, finished_at, exit_code, error_code, error_message, job_id),
        )
        return previous_status, record

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
                WHEN 'download_gfs' THEN 1
                WHEN 'convert_canonical' THEN 2
                WHEN 'produce_forcing' THEN 3
                WHEN 'run_shud_forecast' THEN 4
                WHEN 'parse_output' THEN 5
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


def _pipeline_job_id(run_id: str, stage: str) -> str:
    return f"job_{run_id}_{stage}"


def _stage_status_message(stage: str, status: str, job: dict[str, Any]) -> str:
    if status == "failed":
        error_code = job.get("error_code") or "UNKNOWN"
        error_message = job.get("error_message") or "No error message provided."
        return f"{stage} failed: {error_code} {error_message}"
    return f"{stage} status changed to {status}"


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
