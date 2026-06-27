from __future__ import annotations

from datetime import datetime
from typing import Any

from packages.common.state_manager import StateSnapshot
from services.orchestrator import chain_manifests
from services.orchestrator.chain_types import (
    AnalysisRunContext,
    ForcingContext,
    ForecastRunContext,
    ModelContext,
    StageDefinition,
)
from workers.data_adapters.base import cycle_id_for, format_cycle_time

ANALYSIS_SOURCE_ID = "ERA5"


def latest_usable_state(self, *, model_id: str, before_time: datetime) -> StateSnapshot | None:
    if self.state_manager is None:
        return None
    return self.state_manager.get_latest_usable_state(model_id=model_id, before_time=before_time)


def build_analysis_context(
    self,
    start_time: datetime,
    end_time: datetime,
    model: ModelContext,
    forcing: ForcingContext,
    init_state: StateSnapshot | None,
    *,
    directory_uri,
    analysis_update_ic_step_minutes,
    analysis_forcing_causality,
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
        output_uri=directory_uri(self.object_store, f"runs/{run_id}/output/"),
        log_uri=directory_uri(self.object_store, f"runs/{run_id}/logs/"),
        forcing_package_manifest_uri=getattr(forcing, "forcing_package_manifest_uri", None),
        forcing_package_manifest_checksum=getattr(forcing, "forcing_package_manifest_checksum", None),
        init_state_id=init_state.state_id if init_state is not None else None,
        init_state_uri=init_state.state_uri if init_state is not None else None,
        init_state_valid_time=init_state.valid_time if init_state is not None else None,
        output_segment_count=model.output_segment_count,
        update_ic_step_minutes=analysis_update_ic_step_minutes(start_time, end_time),
        forcing_causality=analysis_forcing_causality(),
    )


def build_run_manifest(
    self,
    context: AnalysisRunContext,
    *,
    analysis_forcing_causality,
    analysis_update_ic_step_minutes,
) -> dict[str, Any]:
    return chain_manifests.build_analysis_run_manifest(
        context,
        analysis_forcing_causality=analysis_forcing_causality,
        analysis_update_ic_step_minutes=analysis_update_ic_step_minutes,
    )


def before_stage_submit(self, stage: StageDefinition, context: ForecastRunContext | AnalysisRunContext) -> None:
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


def after_stage_success(
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


def after_stage_failure(
    self,
    stage: StageDefinition,
    context: ForecastRunContext | AnalysisRunContext,
    terminal: dict[str, Any],
    *,
    analysis_error_code,
) -> None:
    error_code = analysis_error_code(stage, terminal)
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


def after_stage_status_change(
    self,
    stage: StageDefinition,
    context: ForecastRunContext | AnalysisRunContext,
    _status_from: str | None,
    status_to: str,
    job: dict[str, Any],
) -> None:
    if stage.stage == "analysis_run" and status_to == "running":
        self.repository.update_hydro_run_status(context.run_id, "running", slurm_job_id=str(job["job_id"]))


def pipeline_event_target(
    self,
    context: ForecastRunContext | AnalysisRunContext,
    _pipeline_job_id: str,
) -> tuple[str, str]:
    return "analysis_pipeline", context.run_id


def record_best_available(self, context: ForecastRunContext | AnalysisRunContext) -> None:
    if self.best_available_manager is None or context.forcing_version_id is None:
        return
    self.best_available_manager.write_forcing_version(context.forcing_version_id)
