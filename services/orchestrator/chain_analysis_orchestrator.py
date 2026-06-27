from __future__ import annotations

from datetime import datetime
from typing import Any

from packages.common.best_available import BestAvailableManager
from packages.common.object_store import LocalObjectStore
from packages.common.state_manager import StateManager, StateSnapshot
from services.orchestrator import chain as _chain
from services.orchestrator import chain_analysis
from services.orchestrator.chain_types import (
    AnalysisRunContext,
    ForcingContext,
    ForecastRunContext,
    ModelContext,
    OrchestratorError,
    PipelineResult,
    StageDefinition,
)
from services.orchestrator.retry import RetryService

__all__ = ("AnalysisOrchestrator",)


def _analysis_error_code(*args: Any, **kwargs: Any) -> Any:
    return getattr(_chain, "_analysis_error_code")(*args, **kwargs)


def _directory_uri(*args: Any, **kwargs: Any) -> Any:
    return getattr(_chain, "_directory_uri")(*args, **kwargs)


class AnalysisOrchestrator(_chain.ForecastOrchestrator):
    stages = _chain.ANALYSIS_STAGES
    final_pipeline_status = "succeeded"

    def __init__(
        self,
        *,
        config: Any,
        repository: Any,
        state_manager: StateManager | None = None,
        best_available_manager: BestAvailableManager | None = None,
        slurm_client: Any | None = None,
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
        config = _chain.OrchestratorConfig.from_env()
        retry_service = _chain._retry_service_from_env()
        return cls(
            config=config,
            repository=_chain.PsycopgOrchestratorRepository.from_env(),
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
        start_time, end_time = _chain.parse_date_range(date_range)
        if self.repository.has_active_analysis_run(model_id=model_id, start_time=start_time, end_time=end_time):
            raise _chain.AnalysisPipelineAlreadyActiveError(model_id, start_time, end_time)

        model = self.repository.load_model_context(model_id)
        if basin_id is not None and model.basin_id is not None and model.basin_id != basin_id:
            raise OrchestratorError(
                "MODEL_BASIN_MISMATCH",
                f"Model {model_id} belongs to basin {model.basin_id}, not {basin_id}.",
            )

        forcing = self.repository.find_forcing_context(
            source_id=_chain.ANALYSIS_SOURCE_ID,
            cycle_time=start_time,
            model_id=model_id,
        )
        self.repository.ensure_forecast_cycle(source_id=_chain.ANALYSIS_SOURCE_ID, cycle_time=start_time)
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
            analysis_update_ic_step_minutes=getattr(_chain, "_analysis_update_ic_step_minutes"),
            analysis_forcing_causality=getattr(_chain, "_analysis_forcing_causality"),
        )

    def _build_run_manifest(self, context: AnalysisRunContext) -> dict[str, Any]:
        return chain_analysis.build_run_manifest(
            self,
            context,
            analysis_forcing_causality=getattr(_chain, "_analysis_forcing_causality"),
            analysis_update_ic_step_minutes=getattr(_chain, "_analysis_update_ic_step_minutes"),
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
