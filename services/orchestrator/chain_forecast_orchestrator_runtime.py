from __future__ import annotations

from services.orchestrator import chain as _chain


class ForecastOrchestratorRuntimeMixin:
    def trigger_forecast(
        self,
        *,
        source_id: str | None = None,
        cycle_time: str | _chain.datetime,
        model_id: str,
        basin_id: str | None = None,
        max_lead_hours: int | None = None,
    ) -> _chain.PipelineResult:
        return _chain.chain_forecast_trigger.trigger_forecast(
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
        cycle_time: str | _chain.datetime,
        model_id: str,
        basin_id: str | None = None,
        max_lead_hours: int | None = None,
    ) -> _chain.PipelineResult:
        return _chain.chain_forecast_trigger.trigger_forecast_from_canonical(
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
        cycle_time: str | _chain.datetime,
        model_id: str,
        basin_id: str | None,
        max_lead_hours: int | None,
        stages: _chain.Sequence[_chain.StageDefinition],
    ) -> _chain.PipelineResult:
        return _chain.chain_forecast_trigger.trigger_forecast_impl(
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
        context: _chain.ForecastRunContext | _chain.AnalysisRunContext,
        *,
        stages: _chain.Sequence[_chain.StageDefinition] | None = None,
    ) -> _chain.PipelineResult:
        stage_results: list[_chain.StageRunResult] = []
        selected_stages = tuple(stages or self.stages)
        for index, stage in enumerate(selected_stages):
            result = self._submit_and_wait(stage, context, first_stage=index == 0)
            stage_results.append(result)
            if result.status != "succeeded":
                return _chain.PipelineResult(context.run_id, context.cycle_id, "failed", tuple(stage_results))
        return _chain.PipelineResult(context.run_id, context.cycle_id, self.final_pipeline_status, tuple(stage_results))

    def stage_statuses(
        self, *, cycle_time: str | _chain.datetime, source_id: str | None = None, model_id: str | None = None
    ) -> list[dict[str, _chain.Any]]:
        return _chain.chain_forecast_trigger.stage_statuses(
            self, cycle_time=cycle_time, source_id=source_id, model_id=model_id
        )

    def trigger_ready_forecasts(
        self, *, source_id: str | None = None, model_ids: _chain.Sequence[str] | None = None, limit: int = 100
    ) -> tuple[_chain.PipelineResult, ...]:
        return _chain.chain_forecast_trigger.trigger_ready_forecasts(
            self, source_id=source_id, model_ids=model_ids, limit=limit
        )

    def _demote_stale_canonical_cycle(
        self, *, source_id: str, cycle_time: _chain.datetime, stale_versions: set[str | None]
    ) -> None:
        _chain.chain_forecast_trigger.demote_stale_canonical_cycle(
            self, source_id=source_id, cycle_time=cycle_time, stale_versions=stale_versions
        )

    def _validate_auto_trigger_canonical_readiness(
        self,
        cycle: _chain.Mapping[str, _chain.Any],
        *,
        source_id: str,
        cycle_time: _chain.datetime,
        max_lead_hours: int | None,
    ) -> dict[str, _chain.Any]:
        return _chain.chain_forecast_trigger.validate_auto_trigger_canonical_readiness(
            self, cycle, source_id=source_id, cycle_time=cycle_time, max_lead_hours=max_lead_hours
        )

    def _list_canonical_ready_cycles(self, *, source_id: str | None, limit: int) -> tuple[dict[str, _chain.Any], ...]:
        return _chain.chain_forecast_trigger.list_canonical_ready_cycles(self, source_id=source_id, limit=limit)

    def _list_forecast_model_ids(self) -> tuple[str, ...]:
        return _chain.chain_forecast_trigger.list_forecast_model_ids(self)

    def _has_completed_forecast(self, *, source_id: str, cycle_time: _chain.datetime, model_id: str) -> bool:
        return _chain.chain_forecast_trigger.has_completed_forecast(
            self, source_id=source_id, cycle_time=cycle_time, model_id=model_id
        )

    def _submit_and_wait(
        self,
        stage: _chain.StageDefinition,
        context: _chain.ForecastRunContext | _chain.AnalysisRunContext,
        *,
        first_stage: bool,
    ) -> _chain.StageRunResult:
        from services.orchestrator import chain_forecast_execution

        return chain_forecast_execution._submit_and_wait(self, stage, context, first_stage=first_stage)

    def _build_stage_submission_manifest(
        self, stage: _chain.StageDefinition, context: _chain.ForecastRunContext | _chain.AnalysisRunContext
    ) -> dict[str, _chain.Any]:
        from services.orchestrator import chain_forecast_execution

        return chain_forecast_execution._build_stage_submission_manifest(self, stage, context)

    def _validate_analysis_template_context(self, context: _chain.AnalysisRunContext) -> None:
        for label, val in [
            ("source_id", context.source_id),
            ("model_id", context.model_id),
            ("run_id", context.run_id),
            ("basin_version_id", context.basin_version_id),
            ("river_network_version_id", context.river_network_version_id),
        ]:
            if not _chain._SAFE_ID_RE.match(val):
                raise _chain.OrchestratorError("UNSAFE_TEMPLATE_PARAM", f"{label} contains unsafe characters: {val!r}")
        if context.basin_id and (not _chain._SAFE_ID_RE.match(context.basin_id)):
            raise _chain.OrchestratorError("UNSAFE_TEMPLATE_PARAM", f"basin_id unsafe: {context.basin_id!r}")
        if not _chain._SAFE_AREA_RE.match(self.config.era5_area):
            raise _chain.OrchestratorError("UNSAFE_TEMPLATE_PARAM", f"era5_area unsafe: {self.config.era5_area!r}")

    def _poll_until_terminal(
        self,
        *,
        stage: _chain.StageDefinition,
        context: _chain.ForecastRunContext | _chain.AnalysisRunContext,
        pipeline_job_id: str,
        initial_job: dict[str, _chain.Any],
        initial_status: str,
        log_publication: _chain.DisplayLogPublication,
    ) -> _chain.TerminalJobObservation:
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
        stage: _chain.StageDefinition,
        context: _chain.ForecastRunContext | _chain.AnalysisRunContext,
        pipeline_job_id: str,
        job: dict[str, _chain.Any],
        current_status: str,
        log_publication: _chain.DisplayLogPublication,
    ) -> _chain.TerminalJobObservation:
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
        self, stage: _chain.StageDefinition, context: _chain.ForecastRunContext | _chain.AnalysisRunContext
    ) -> None:
        if stage.stage in {"download_gfs", "download"}:
            self.repository.update_forecast_cycle_status(
                source_id=context.source_id, cycle_time=context.cycle_time, status="downloading"
            )
        elif stage.stage in {"run_shud_forecast", "forecast"}:
            self.repository.update_forecast_cycle_status(
                source_id=context.source_id, cycle_time=context.cycle_time, status="forecast_running"
            )

    def _after_stage_success(
        self,
        stage: _chain.StageDefinition,
        context: _chain.ForecastRunContext | _chain.AnalysisRunContext,
        _terminal: dict[str, _chain.Any],
    ) -> None:
        self.repository.update_forecast_cycle_status(
            source_id=context.source_id, cycle_time=context.cycle_time, status=stage.success_cycle_status
        )
        if stage.stage in {"run_shud_forecast", "forecast"}:
            self.repository.update_hydro_run_status(context.run_id, "succeeded")
        elif stage.stage in {"parse_output", "parse"}:
            self.repository.update_hydro_run_status(context.run_id, "parsed")

    def _after_stage_failure(
        self,
        stage: _chain.StageDefinition,
        context: _chain.ForecastRunContext | _chain.AnalysisRunContext,
        terminal: dict[str, _chain.Any],
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
            context.run_id, "failed", error_code=error_code, error_message=error_message
        )

    def _after_stage_status_change(
        self,
        _stage: _chain.StageDefinition,
        _context: _chain.ForecastRunContext | _chain.AnalysisRunContext,
        _status_from: str | None,
        _status_to: str,
        _job: dict[str, _chain.Any],
    ) -> None:
        return None

    def _pipeline_event_target(
        self, _context: _chain.ForecastRunContext | _chain.AnalysisRunContext, pipeline_job_id: str
    ) -> tuple[str, str]:
        return ("pipeline_job", pipeline_job_id)

    def render_stage_template(
        self, stage: _chain.StageDefinition, context: _chain.ForecastRunContext | _chain.AnalysisRunContext
    ) -> str:
        from services.orchestrator import chain_forecast_templates

        return chain_forecast_templates.render_stage_template(self, stage, context)

    def _persist_gateway_logs(self, slurm_job_id: str, log_uri: str) -> None:
        return _chain.chain_workspace.persist_gateway_logs(
            self,
            slurm_job_id,
            log_uri,
            coerce_mapping=_chain._coerce_mapping,
            absolute_configured_path=_chain._absolute_configured_path,
            ensure_directory=_chain.ensure_directory_no_follow,
            atomic_write_bytes=_chain.atomic_write_bytes_no_follow,
            safe_filesystem_error_cls=_chain.SafeFilesystemError,
            artifact_log_error_cls=_chain.ArtifactLogError,
        )

    def _log_uri_for_stage(
        self, *, source_id: str, cycle_time: _chain.datetime | None, run_id: str, job_id: str, stage: str
    ) -> str:
        return _chain.chain_workspace.log_uri_for_stage(
            self,
            source_id=source_id,
            cycle_time=cycle_time,
            run_id=run_id,
            job_id=job_id,
            stage=stage,
            published_artifact_root_configured=_chain._published_artifact_root_configured,
            utcnow=_chain._utcnow,
            log_stream_for_stage=_chain._log_stream_for_stage,
            normalize_source_id_fn=_chain.normalize_source_id,
            published_log_uri_fn=_chain.published_log_uri,
        )

    def _published_log_path(self, log_uri: str) -> _chain.Path | None:
        return _chain.chain_workspace.published_log_path(
            log_uri,
            absolute_configured_path=_chain._absolute_configured_path,
            published_log_relative_path_fn=_chain.published_log_relative_path,
        )

    def _build_run_context(
        self,
        source_id: str,
        cycle_time: _chain.datetime,
        model: _chain.ModelContext,
        forcing: _chain.ForcingContext,
        initial_state: _chain.InitialStateSelection | None = None,
        max_lead_hours: int | None = None,
    ) -> _chain.ForecastRunContext:
        from services.orchestrator import chain_forecast_state

        return chain_forecast_state._build_run_context(
            self, source_id, cycle_time, model, forcing, initial_state, max_lead_hours
        )

    def _forecast_scenario_id(self, source_id: str) -> str:
        from services.orchestrator import chain_forecast_state

        return chain_forecast_state._forecast_scenario_id(self, source_id)

    def _build_run_manifest(self, context: _chain.ForecastRunContext) -> dict[str, _chain.Any]:
        from services.orchestrator import chain_forecast_state

        return chain_forecast_state._build_run_manifest(self, context)

    def _state_passes_qc(self, state: _chain.StateSnapshot) -> bool:
        from services.orchestrator import chain_forecast_state

        return chain_forecast_state._state_passes_qc(self, state)

    def _select_forecast_initial_state(
        self,
        *,
        model_id: str,
        cycle_time: _chain.datetime,
        source_id: str | None = None,
        model_package_version: str | None = None,
        model_package_checksum: str | None = None,
        max_lead_hours: int | None = None,
    ) -> _chain.InitialStateSelection:
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
        cycle_time: _chain.datetime,
        source_id: str | None,
        model_package_version: str | None,
        model_package_checksum: str | None,
    ) -> _chain.InitialStateSelection:
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
        basin: _chain.Mapping[str, _chain.Any],
        *,
        source_id: str | None,
        cycle_time: _chain.datetime,
        model_package_version: str | None,
        model_package_checksum: str | None,
    ) -> _chain.InitialStateSelection:
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
        basin: _chain.Mapping[str, _chain.Any],
        *,
        model_id: str,
        cycle_time: _chain.datetime,
        source_id: str | None,
    ) -> _chain.StateSnapshot | None:
        from services.orchestrator import chain_forecast_state

        return chain_forecast_state._resolve_prefilled_forecast_state(
            self, basin, model_id=model_id, cycle_time=cycle_time, source_id=source_id
        )

    def _validate_prefilled_state_identity(
        self, basin: _chain.Mapping[str, _chain.Any], selection: _chain.InitialStateSelection
    ) -> None:
        from services.orchestrator import chain_forecast_state

        return chain_forecast_state._validate_prefilled_state_identity(self, basin, selection)

    def _validate_strict_forecast_state(
        self,
        state: _chain.StateSnapshot,
        *,
        model_id: str,
        cycle_time: _chain.datetime,
        source_id: str | None,
        model_package_version: str | None,
        model_package_checksum: str | None,
    ) -> _chain.InitialStateSelection:
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
        self, *, model_id: str, cycle_time: _chain.datetime, source_id: str | None
    ) -> _chain.StateSnapshot | None:
        from services.orchestrator import chain_forecast_state

        return chain_forecast_state._get_exact_forecast_state(
            self, model_id=model_id, cycle_time=cycle_time, source_id=source_id
        )

    def _exact_or_latest_usable_state(
        self, *, model_id: str, cycle_time: _chain.datetime, before_time: _chain.datetime, source_id: str | None
    ) -> _chain.StateSnapshot | None:
        from services.orchestrator import chain_forecast_state

        return chain_forecast_state._exact_or_latest_usable_state(
            self, model_id=model_id, cycle_time=cycle_time, before_time=before_time, source_id=source_id
        )

    def _write_run_manifest(
        self, context: _chain.ForecastRunContext | _chain.AnalysisRunContext, manifest: dict[str, _chain.Any]
    ) -> None:
        _chain.chain_manifests.write_run_manifest(self, context, manifest)

    def _workspace_path(self, *parts: str) -> _chain.Path:
        return _chain.chain_workspace.workspace_path(self, *parts)

    def _safe_workspace_write_bytes(self, path: _chain.Path, content: bytes) -> _chain.Path:
        return _chain.chain_workspace.safe_workspace_write_bytes(
            self,
            path,
            content,
            workspace_relative_parts_fn=_chain._workspace_relative_parts,
            ensure_directory=_chain.ensure_directory_no_follow,
            atomic_write_bytes=_chain.atomic_write_bytes_no_follow,
        )

    def _safe_workspace_read_bytes(self, path: _chain.Path) -> bytes:
        return _chain.chain_workspace.safe_workspace_read_bytes(
            self,
            path,
            workspace_relative_parts_fn=_chain._workspace_relative_parts,
            read_bytes=_chain.read_bytes_no_follow,
        )
