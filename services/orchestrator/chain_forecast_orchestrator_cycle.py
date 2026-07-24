from __future__ import annotations

from datetime import UTC, datetime

from services.orchestrator import chain as _chain
from services.orchestrator.accepted_submit_identity import (
    ACCEPTED_SUBMIT_CONTRACT_VERSION,
    accepted_submit_contract_is_current,
    accepted_submit_pipeline_job_model_id,
    accepted_submit_row_kind,
    canonical_forecast_cohort_members,
    forecast_cohort_digest,
    forecast_cohort_identity_is_valid,
)

_FORCE_TERMINAL_RESUBMIT_DECISIONS = {
    "retry_missing_forecast_output",
    "retry_strict_warm_start_terminal_init_state_mismatch",
    "retry_strict_warm_start_terminal_run_manifest_missing",
    "retry_strict_warm_start_retry_run_manifest_mismatch",
    "retry_terminal_run_manifest_missing",
}
_STAGE_ORDER = {
    "convert": 0,
    "forcing": 1,
    "forecast": 2,
    "state_save_qc": 3,
    "parse": 4,
    "publish": 5,
}


class ForecastOrchestratorCycleMixin:
    def __init__(
        self,
        *,
        config: _chain.OrchestratorConfig,
        repository: _chain.OrchestratorRepository,
        state_manager: _chain.StateManager | None = None,
        slurm_client: _chain.SlurmGatewayClient | None = None,
        object_store: _chain.LocalObjectStore | None = None,
        retry_service: _chain.RetryService | None = None,
    ) -> None:
        self.config = config
        self.repository = repository
        self.state_manager = state_manager
        self.slurm_client = slurm_client or _chain.HttpSlurmGatewayClient(config.slurm_gateway_url)
        self.object_store = object_store or _chain.LocalObjectStore(
            config.object_store_root, config.object_store_prefix
        )
        self.retry_service = retry_service
        self.retry_config = getattr(retry_service, "config", None) or _chain.RetryConfig()
        if config.terminal_stage is not None:
            self.stages = _chain.stages_through(self.stages, config.terminal_stage)
            self.final_pipeline_status = "succeeded"
        self._active_cycles: set[str] = set()
        self.duplicate_submission_skips: list[dict[str, _chain.Any]] = []

    @classmethod
    def from_env(cls) -> _chain.ForecastOrchestrator:
        config = _chain.OrchestratorConfig.from_env()
        retry_service = _chain._retry_service_from_env()
        return cls(
            config=config,
            repository=_chain.PsycopgOrchestratorRepository.from_env(),
            state_manager=_chain.StateManager.from_env(),
            retry_service=retry_service,
        )

    def orchestrate_cycle(
        self,
        source: str,
        cycle_time: str | _chain.datetime,
        basins: _chain.Sequence[_chain.Mapping[str, _chain.Any] | _chain.ModelContext],
    ) -> _chain.PipelineResult:
        return _chain.chain_forecast_control.orchestrate_cycle(self, source, cycle_time, basins)

    def sync_cycle_statuses(self, cycle_id: str) -> list[dict[str, _chain.Any]]:
        return _chain.chain_forecast_control.sync_cycle_statuses(self, cycle_id)

    def cancel_active_cycle_jobs(
        self, cycle_id: str, *, reason: str = "operator_requested"
    ) -> list[dict[str, _chain.Any]]:
        return _chain.chain_forecast_control.cancel_active_cycle_jobs(self, cycle_id, reason=reason)

    def _log_uri_for_pipeline_job(self, job: _chain.Mapping[str, _chain.Any]) -> str | None:
        return _chain.chain_workspace.log_uri_for_pipeline_job(
            self,
            job,
            source_id_from_cycle_id=_chain._source_id_from_cycle_id,
            cycle_time_from_cycle_id=_chain._cycle_time_from_cycle_id,
        )

    def _display_log_publication_for_stage(
        self,
        *,
        source_id: str,
        cycle_time: _chain.datetime | None,
        run_id: str,
        job_id: str,
        stage: str,
        existing_log_uri: str | None = None,
    ) -> _chain.DisplayLogPublication:
        return _chain.chain_workspace.display_log_publication_for_stage(
            self,
            source_id=source_id,
            cycle_time=cycle_time,
            run_id=run_id,
            job_id=job_id,
            stage=stage,
            existing_log_uri=existing_log_uri,
        )

    def _display_log_publication_for_pipeline_job(
        self, job: _chain.Mapping[str, _chain.Any]
    ) -> _chain.DisplayLogPublication | None:
        return _chain.chain_workspace.display_log_publication_for_pipeline_job(self, job)

    def _try_publish_log_for_advertise(
        self, slurm_job_id: str, publication: _chain.DisplayLogPublication
    ) -> _chain.DisplayLogPublicationAttempt:
        return _chain.chain_workspace.try_publish_log_for_advertise(self, slurm_job_id, publication)

    @staticmethod
    def _log_persistence_error(candidate_uri: str, error: Exception) -> _chain.OrchestratorError:
        return _chain.chain_workspace.log_persistence_error(candidate_uri, error)

    @staticmethod
    def _raise_publish_error_after_durable_update(attempt: _chain.DisplayLogPublicationAttempt | None) -> None:
        return _chain.chain_workspace.raise_publish_error_after_durable_update(attempt)

    def _run_cycle_chain(self, context: _chain.CycleOrchestrationContext) -> _chain.PipelineResult:
        from services.orchestrator import chain_forecast_execution

        return chain_forecast_execution._run_cycle_chain(self, context)

    def _retry_cycle_stage_job_id(
        self,
        context: _chain.CycleOrchestrationContext,
        stage: _chain.StageDefinition,
        _existing_job: _chain.Mapping[str, _chain.Any],
    ) -> str:
        base_job_id = _chain._pipeline_job_id(context.run_id, stage.stage)
        attempt = context.retry_attempt or _chain._next_retry_attempt_for_stage(
            self._query_pipeline_jobs_for_cycle_context(context), base_job_id=base_job_id, stage=stage
        )
        if attempt <= 0:
            attempt = 1
        return _chain._pipeline_retry_job_id(base_job_id, attempt)

    @staticmethod
    def _terminal_stage_needs_manual_retry(
        context: _chain.CycleOrchestrationContext, job: _chain.Mapping[str, _chain.Any]
    ) -> bool:
        if str(job.get("status") or "") == "reservation_lost" and job.get("slurm_job_id") in (None, ""):
            return _verified_accepted_submit_forecast_retry(job)
        if _terminal_stage_needs_forced_resubmit(context, job):
            return True
        if context.retry_attempt is None:
            return False
        status = str(job.get("status") or "")
        return status in {"failed", "submission_failed", "permanently_failed", "cancelled", "partially_failed"}

    @staticmethod
    def _terminal_stage_can_retry_after_upstream_refresh(
        job: _chain.Mapping[str, _chain.Any], *, refreshed_upstream_finished_at: _chain.datetime | None
    ) -> bool:
        if refreshed_upstream_finished_at is None:
            return False
        status = str(job.get("status") or "")
        if status not in {"failed", "submission_failed", "partially_failed"}:
            return False
        terminal_time = _chain._pipeline_job_terminal_time(job)
        return terminal_time is None or terminal_time <= refreshed_upstream_finished_at

    def _schedule_cycle_stage_retry(self, result: _chain.StageRunResult, failure_number: int) -> str | None:
        if self.retry_service is None:
            return None
        job = self._retry_job_for_stage_result(result)
        if job is None:
            return None
        retry_count = int(getattr(job, "retry_count", 0) or 0)
        backoff_seconds = _chain.compute_backoff_seconds(retry_count, self.retry_config.backoff_schedule)
        get_pipeline_job = getattr(self.repository, "get_pipeline_job", None)
        current = get_pipeline_job(result.pipeline_job_id) if callable(get_pipeline_job) else None
        if (
            isinstance(current, _chain.Mapping)
            and accepted_submit_contract_is_current(current)
            and accepted_submit_row_kind(current) == "master"
        ):
            should_retry = getattr(self.retry_service, "should_auto_retry", None)
            if callable(should_retry) and not bool(should_retry(job)):
                return None
            # The next call to _submit_and_wait_cycle_stage owns creation of a
            # clean, versioned reservation using the then-current basin cohort.
            # Do not let the legacy retry adapter clone a marker-free row.
            retry_marker = "_retry_"
            base_job_id = result.pipeline_job_id
            suffix_attempt = 0
            if retry_marker in base_job_id:
                possible_base, possible_attempt = base_job_id.rsplit(retry_marker, maxsplit=1)
                try:
                    suffix_attempt = max(int(possible_attempt), 0)
                    base_job_id = possible_base
                except ValueError:
                    pass
            next_attempt = max(retry_count, suffix_attempt, failure_number - 1) + 1
            next_job_id = f"{base_job_id}{retry_marker}{next_attempt}"
            _chain.time.sleep(backoff_seconds)
            return next_job_id
        handled = self.retry_service.handle_failed_job(job)
        handled_status = str(getattr(handled, "status", ""))
        handled_job_id = str(getattr(handled, "job_id"))
        self._release_retry_store_transaction()
        if handled_status != "pending":
            return None
        _chain.time.sleep(backoff_seconds)
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

    def _retry_job_for_stage_result(self, result: _chain.StageRunResult) -> _chain.PipelineJob | None:
        from services.orchestrator import chain_forecast_execution

        return chain_forecast_execution._retry_job_for_stage_result(self, result)

    def _retry_partial_array_stage(
        self,
        stage: _chain.StageDefinition,
        context: _chain.CycleOrchestrationContext,
        result: _chain.StageRunResult,
        aggregation: _chain.ArrayAggregation,
        had_partial_before_stage: bool,
        last_partial_before_stage: str | None,
    ) -> tuple[_chain.StageRunResult, _chain.ArrayAggregation] | None:
        from services.orchestrator import chain_forecast_execution

        return chain_forecast_execution._retry_partial_array_stage(
            self, stage, context, result, aggregation, had_partial_before_stage, last_partial_before_stage
        )

    @staticmethod
    def _reindexed_basins_for_task_ids(
        basins: _chain.Sequence[_chain.Mapping[str, _chain.Any]], task_ids: _chain.Sequence[int]
    ) -> list[dict[str, _chain.Any]]:
        by_task_id = {int(basin.get("task_id", index)): dict(basin) for index, basin in enumerate(basins)}
        reindexed: list[dict[str, _chain.Any]] = []
        for new_task_id, task_id in enumerate(task_ids):
            entry = dict(by_task_id[int(task_id)])
            entry["task_id"] = new_task_id
            entry["original_task_id"] = int(entry.get("original_task_id", task_id))
            reindexed.append(entry)
        return reindexed

    @staticmethod
    def _chain_stage_execution_dependencies() -> _chain.chain_stage_execution.StageExecutionDependencies:
        return _chain.chain_stage_execution.StageExecutionDependencies(
            terminal_job_statuses=frozenset(_chain.TERMINAL_JOB_STATUSES),
            pipeline_job_id=_chain._pipeline_job_id,
            published_artifact_root_configured=_chain._published_artifact_root_configured,
            cycle_stage_idempotency_key=_chain._cycle_stage_idempotency_key,
            slurm_comment_for=_chain.slurm_comment_for,
            cycle_payload_model_id=_chain._cycle_payload_model_id,
            cycle_pipeline_job_model_id=_chain._cycle_pipeline_job_model_id,
            coerce_mapping=_chain._coerce_mapping,
            coerce_array_task_id=_chain._coerce_array_task_id,
            status_from_gateway_job=_chain._status_from_gateway_job,
            parse_gateway_time=_chain._parse_gateway_time,
            utcnow=_chain._utcnow,
            format_time=_chain._format_time,
            safe_pipeline_event_details=_chain._safe_pipeline_event_details,
            submission_runtime_root_contract=_chain._submission_runtime_root_contract,
            aggregation_error_code=_chain._aggregation_error_code,
            aggregation_error_message=_chain._aggregation_error_message,
            slurm_accounting_from_payload=_chain._slurm_accounting_from_payload,
            resource_metrics_from_payload=_chain._resource_metrics_from_payload,
            stage_task_result_evidence=_chain._stage_task_result_evidence,
            stage_status_message=_chain._stage_status_message,
            make_slurm_client_error=_chain.SlurmClientError,
            tile_publisher_cls=_chain.TilePublisher,
            publish_error_cls=_chain.PublishError,
            failure_payload=_chain.failure_payload,
            redact_payload=_chain.redact_payload,
        )

    def _submit_and_wait_cycle_stage(
        self,
        stage: _chain.StageDefinition,
        context: _chain.CycleOrchestrationContext,
        *,
        pipeline_job_id: str | None = None,
    ) -> tuple[_chain.StageRunResult, _chain.ArrayAggregation | None]:
        return _chain.chain_stage_execution.submit_and_wait_cycle_stage(
            self, stage, context, pipeline_job_id=pipeline_job_id
        )

    def _run_local_publish_stage(
        self, stage: _chain.StageDefinition, context: _chain.CycleOrchestrationContext, *, pipeline_job_id: str
    ) -> _chain.StageRunResult:
        return _chain.chain_stage_execution.run_local_publish_stage(
            self, stage, context, pipeline_job_id=pipeline_job_id
        )

    def _write_local_stage_log(self, log_uri: str, payload: _chain.Mapping[str, _chain.Any]) -> str:
        return _chain.chain_workspace.write_local_stage_log(
            self,
            log_uri,
            payload,
            redact_payload_fn=_chain.redact_payload,
            absolute_configured_path=_chain._absolute_configured_path,
            ensure_directory=_chain.ensure_directory_no_follow,
            atomic_write_bytes=_chain.atomic_write_bytes_no_follow,
            safe_filesystem_error_cls=_chain.SafeFilesystemError,
        )

    def _resume_cycle_stage(
        self, stage: _chain.StageDefinition, context: _chain.CycleOrchestrationContext, job: dict[str, _chain.Any]
    ) -> tuple[_chain.StageRunResult, _chain.ArrayAggregation | None]:
        return _chain.chain_stage_execution.resume_cycle_stage(self, stage, context, job)

    def _poll_cycle_stage_until_terminal(
        self,
        *,
        stage: _chain.StageDefinition,
        context: _chain.CycleOrchestrationContext,
        pipeline_job_id: str,
        initial_job: dict[str, _chain.Any],
        initial_status: str,
        log_publication: _chain.DisplayLogPublication | None,
    ) -> _chain.TerminalJobObservation:
        return _chain.chain_stage_execution.poll_cycle_stage_until_terminal(
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
        stage: _chain.StageDefinition,
        context: _chain.CycleOrchestrationContext,
        pipeline_job_id: str,
        job: dict[str, _chain.Any],
        current_status: str,
        log_publication: _chain.DisplayLogPublication | None,
    ) -> _chain.TerminalJobObservation:
        return _chain.chain_stage_execution.record_cycle_stage_poll_timeout(
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
        stage: _chain.StageDefinition,
        context: _chain.CycleOrchestrationContext,
        tasks: list[dict[str, _chain.Any]],
        manifest: dict[str, _chain.Any],
    ) -> dict[str, _chain.Any]:
        return _chain.chain_stage_execution.submit_array_stage(self, stage, context, tasks, manifest)

    def _slurm_submission_manifest(self, manifest: _chain.Mapping[str, _chain.Any]) -> dict[str, _chain.Any]:
        return _chain.chain_stage_execution.slurm_submission_manifest(self, manifest)

    def _aggregate_array_stage(
        self,
        stage: _chain.StageDefinition,
        context: _chain.CycleOrchestrationContext,
        slurm_job_id: str,
        terminal: dict[str, _chain.Any],
        pipeline_job_id: str,
    ) -> _chain.ArrayAggregation:
        return _chain.chain_array_accounting.aggregate_array_stage(
            self, stage, context, slurm_job_id, terminal, pipeline_job_id, deps=_chain._array_accounting_dependencies()
        )

    def _require_complete_array_accounting(
        self,
        aggregation: _chain.ArrayAggregation,
        *,
        stage: _chain.StageDefinition,
        context: _chain.CycleOrchestrationContext,
        slurm_job_id: str,
    ) -> _chain.ArrayAggregation:
        return _chain.chain_array_accounting.require_complete_array_accounting(
            aggregation, stage=stage, context=context, slurm_job_id=slurm_job_id
        )

    def _record_cycle_stage_status_override(
        self,
        stage: _chain.StageDefinition,
        context: _chain.CycleOrchestrationContext,
        pipeline_job_id: str,
        terminal: dict[str, _chain.Any],
        aggregation: _chain.ArrayAggregation,
        log_uri: str | None,
    ) -> dict[str, _chain.Any]:
        return _chain.chain_array_accounting.record_cycle_stage_status_override(
            self,
            stage,
            context,
            pipeline_job_id,
            terminal,
            aggregation,
            log_uri,
            deps=_chain._array_accounting_dependencies(),
        )

    def _record_cycle_stage_accounting_event(
        self,
        stage: _chain.StageDefinition,
        context: _chain.CycleOrchestrationContext,
        pipeline_job_id: str,
        terminal: _chain.Mapping[str, _chain.Any],
        *,
        log_uri: str | None,
    ) -> None:
        _chain.chain_array_accounting.record_cycle_stage_accounting_event(
            self,
            stage,
            context,
            pipeline_job_id,
            terminal,
            log_uri=log_uri,
            deps=_chain._array_accounting_dependencies(),
        )

    def _record_cycle_stage_accounting_gap(
        self,
        stage: _chain.StageDefinition,
        context: _chain.CycleOrchestrationContext,
        pipeline_job_id: str,
        *,
        slurm_job_id: str,
        message: str,
        details: _chain.Mapping[str, _chain.Any],
    ) -> None:
        _chain.chain_array_accounting.record_cycle_stage_accounting_gap(
            self,
            stage,
            context,
            pipeline_job_id,
            slurm_job_id=slurm_job_id,
            message=message,
            details=details,
            deps=_chain._array_accounting_dependencies(),
        )

    def _after_cycle_stage_terminal(
        self,
        stage: _chain.StageDefinition,
        context: _chain.CycleOrchestrationContext,
        result_status: str,
        terminal: dict[str, _chain.Any],
        aggregation: _chain.ArrayAggregation | None,
    ) -> None:
        from services.orchestrator import chain_forecast_execution

        return chain_forecast_execution._after_cycle_stage_terminal(
            self, stage, context, result_status, terminal, aggregation
        )

    def _reserve_cycle_stage(
        self,
        stage: _chain.StageDefinition,
        context: _chain.CycleOrchestrationContext,
        pipeline_job_id: str,
        idempotency_key: str,
    ) -> _chain.ReservationResult | None:
        """Phase 1 durable reservation; best-effort for legacy repositories.

        Returns the ``ReservationResult`` so the submit path can gate sbatch on
        the DB win/lose signal (skip when a concurrent pass already reserved an
        active candidate). ``None`` only for legacy repositories without the
        reservation surface (gate is a no-op there).
        """
        if not hasattr(self.repository, "reserve_pipeline_job"):
            return None
        reservation_evidence = None
        if (
            getattr(self.repository, "supports_accepted_submit_reconcile", False)
            and _chain.chain_stage_execution.is_forecast_cohort_stage(stage)
        ):
            submission_attempt = max(int(context.retry_attempt or 1), 1)
            retry_marker = "_retry_"
            if retry_marker in pipeline_job_id:
                try:
                    submission_attempt = max(
                        submission_attempt,
                        int(pipeline_job_id.rsplit(retry_marker, maxsplit=1)[1]) + 1,
                    )
                except ValueError:
                    pass
            members = canonical_forecast_cohort_members(
                source_id=context.source_id,
                cycle_time=context.cycle_time,
                basins=context.active_basins,
            )
            expected_user = self.config.reconcile_slurm_user
            expected_account = self.config.reconcile_slurm_account
            reservation_evidence = {
                "accepted_submit_contract_version": ACCEPTED_SUBMIT_CONTRACT_VERSION,
                "slurm_comment": _chain.slurm_comment_for(idempotency_key),
                "cohort_members": list(members),
                "restart_stage": "forecast",
                "submission_attempt": submission_attempt,
                "submission_attempt_started_at": datetime.now(UTC),
                "slurm_ownership_required": bool(expected_user and expected_account),
                "expected_slurm_user": expected_user,
                "expected_slurm_account": expected_account,
                "native_shud_resubmitted": _chain.chain_stage_execution.is_forecast_cohort_stage(stage),
            }
        reserve_kwargs = {
            "idempotency_key": idempotency_key,
            "job_id": pipeline_job_id,
            "run_id": context.run_id,
            "cycle_id": context.cycle_id,
            "job_type": stage.job_type,
            "model_id": accepted_submit_pipeline_job_model_id(
                supports_accepted_submit_reconcile=getattr(
                    self.repository, "supports_accepted_submit_reconcile", False
                ),
                stage=stage.stage,
                job_type=stage.job_type,
                model_id=_chain._cycle_pipeline_job_model_id(context),
            ),
            "stage": stage.stage,
            "candidate_id": context.run_id,
        }
        if reservation_evidence is not None:
            reservation_evidence["cohort_digest"] = forecast_cohort_digest(
                {"source_id": context.source_id, **reserve_kwargs, **reservation_evidence}
            )
            reserve_kwargs["reservation_evidence"] = reservation_evidence
        return _chain.reserve_candidate(self.repository, **reserve_kwargs)

    def _reservation_already_inflight(self, reservation: _chain.ReservationResult | None) -> bool:
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
        return reservation is not None and reservation.already_inflight and (not reservation.created)

    def _bind_cycle_stage_reservation(
        self, idempotency_key: str, *, slurm_job_id: str, array_task_id: int | None
    ) -> None:
        """Phase 2 atomic bind; best-effort for legacy repositories."""
        if not hasattr(self.repository, "bind_pipeline_job_reservation"):
            return
        _chain.bind_reservation(
            self.repository,
            idempotency_key=idempotency_key,
            slurm_job_id=slurm_job_id,
            status="submitted",
            array_task_id=array_task_id,
        )

    def _before_cycle_stage_submit(
        self, stage: _chain.StageDefinition, context: _chain.CycleOrchestrationContext
    ) -> None:
        if stage.stage == "download":
            self.repository.update_forecast_cycle_status(
                source_id=context.source_id, cycle_time=context.cycle_time, status="downloading"
            )
        elif stage.stage == "forecast":
            self.repository.update_forecast_cycle_status(
                source_id=context.source_id, cycle_time=context.cycle_time, status="forecast_running"
            )

    def _record_submission_failure(
        self,
        stage: _chain.StageDefinition,
        context: _chain.CycleOrchestrationContext,
        error: Exception,
        *,
        pipeline_job_id: str | None = None,
        persist_pipeline_job: bool = True,
        persist_pipeline_event: bool = True,
    ) -> _chain.StageRunResult:
        from services.orchestrator import chain_forecast_submission

        return chain_forecast_submission._record_submission_failure(
            self,
            stage,
            context,
            error,
            pipeline_job_id=pipeline_job_id,
            persist_pipeline_job=persist_pipeline_job,
            persist_pipeline_event=persist_pipeline_event,
        )

    def _skip_duplicate_submission(
        self,
        stage: _chain.StageDefinition,
        context: _chain.CycleOrchestrationContext,
        pipeline_job_id: str,
        reservation: _chain.ReservationResult | None,
    ) -> _chain.StageRunResult:
        from services.orchestrator import chain_forecast_submission

        return chain_forecast_submission._skip_duplicate_submission(self, stage, context, pipeline_job_id, reservation)

    def _apply_array_progress(
        self,
        stage: _chain.StageDefinition,
        context: _chain.CycleOrchestrationContext,
        aggregation: _chain.ArrayAggregation,
    ) -> None:
        _chain.chain_array_accounting.apply_array_progress(
            self, stage, context, aggregation, deps=_chain._array_accounting_dependencies()
        )

    def _success_cycle_status(self, stage: _chain.StageDefinition, context: _chain.CycleOrchestrationContext) -> str:
        if not context.had_partial:
            return stage.success_cycle_status
        if stage.stage in {"parse", "state_save_qc"}:
            return "parsed_partial"
        if stage.stage in {"forcing", "forecast"}:
            return "forcing_ready_partial"
        return context.last_partial_status or stage.success_cycle_status

    def _partial_cycle_status(self, stage: _chain.StageDefinition) -> str:
        if stage.stage in {"parse", "state_save_qc"}:
            return "parsed_partial"
        return "forcing_ready_partial"

    def _build_cycle_stage_manifest(
        self, stage: _chain.StageDefinition, context: _chain.CycleOrchestrationContext
    ) -> dict[str, _chain.Any]:
        return _chain.chain_manifests.build_cycle_stage_manifest(
            self,
            stage,
            context,
            model_run_stage_evidence=_chain._model_run_stage_evidence,
            publish_quality_state=_chain._publish_quality_state,
            cycle_residual_blockers=_chain._cycle_residual_blockers,
        )

    def _write_cycle_manifest_index(
        self,
        context: _chain.CycleOrchestrationContext,
        stage: _chain.StageDefinition,
        tasks: list[dict[str, _chain.Any]],
    ) -> _chain.Path:
        return _chain.chain_manifests.write_cycle_manifest_index(self, context, stage, tasks)

    def _prepare_forecast_runtime_manifests(
        self, stage: _chain.StageDefinition, context: _chain.CycleOrchestrationContext
    ) -> None:
        _chain.chain_manifests.prepare_forecast_runtime_manifests(
            self, stage, context, assembly_payload_from_runtime_manifest=_chain._assembly_payload_from_runtime_manifest
        )

    def _mark_staged_hydro_runs_failed(
        self, run_ids: _chain.Sequence[str], *, error_code: str, error_message: str
    ) -> None:
        for run_id in run_ids:
            try:
                self.repository.update_hydro_run_status(
                    run_id, "failed", error_code=error_code, error_message=error_message
                )
            except Exception:
                continue

    def _build_forecast_runtime_manifest(
        self, context: _chain.CycleOrchestrationContext, basin: _chain.Mapping[str, _chain.Any]
    ) -> dict[str, _chain.Any]:
        return _chain.chain_manifests.build_forecast_runtime_manifest(
            self,
            context,
            basin,
            assembly_builder=_chain.build_model_run_assembly,
            forecast_state_checkpoint_hours=_chain._forecast_state_checkpoint_hours,
        )

    def _validate_forecast_runtime_manifest(
        self, manifest_path: _chain.Path, manifest: _chain.Mapping[str, _chain.Any], *, task_index: int
    ) -> None:
        _chain.chain_manifests.validate_forecast_runtime_manifest(self, manifest_path, manifest, task_index=task_index)

    def _reindexed_manifest_entries(
        self, basins: _chain.Sequence[_chain.Mapping[str, _chain.Any]]
    ) -> list[dict[str, _chain.Any]]:
        return _chain.chain_manifests.reindexed_manifest_entries(
            self,
            basins,
            reindex_builder=_chain.build_reindexed_manifest,
            assembly_builder=_chain.build_model_run_assembly,
        )

    def _normalize_cycle_basins(
        self,
        basins: _chain.Sequence[_chain.Mapping[str, _chain.Any] | _chain.ModelContext],
        source_id: str,
        cycle_time: _chain.datetime,
    ) -> list[dict[str, _chain.Any]]:
        return _chain.chain_forecast_cycle.normalize_cycle_basins(self, basins, source_id, cycle_time)

    def _apply_cohort_warm_start(
        self, basins: _chain.Sequence[dict[str, _chain.Any]], source_id: str, cycle_time: _chain.datetime
    ) -> None:
        _chain.chain_forecast_cycle.apply_cohort_warm_start(self, basins, source_id, cycle_time)

    def _validate_cycle_basin_identities(
        self,
        basins: _chain.Sequence[_chain.Mapping[str, _chain.Any]],
        source_id: str,
        cycle_time: _chain.datetime,
        cycle_id: str,
    ) -> None:
        _chain.chain_forecast_cycle.validate_cycle_basin_identities(self, basins, source_id, cycle_time, cycle_id)

    def _query_pipeline_jobs_by_cycle(self, cycle_id: str) -> list[dict[str, _chain.Any]]:
        return _chain.chain_forecast_cycle.query_pipeline_jobs_by_cycle(self, cycle_id)

    def _query_pipeline_jobs_for_cycle_context(
        self, context: _chain.CycleOrchestrationContext
    ) -> list[dict[str, _chain.Any]]:
        return _chain.chain_forecast_cycle.query_pipeline_jobs_for_cycle_context(self, context)

    def _find_existing_stage_job(
        self,
        jobs: _chain.Sequence[_chain.Mapping[str, _chain.Any]],
        stage: _chain.StageDefinition,
        *,
        context: _chain.CycleOrchestrationContext,
    ) -> dict[str, _chain.Any] | None:
        return _chain.chain_forecast_cycle.find_existing_stage_job(self, jobs, stage, context=context)

    def _cycle_download_success_missing_raw_manifest(
        self,
        stage: _chain.StageDefinition,
        context: _chain.CycleOrchestrationContext,
        job: _chain.Mapping[str, _chain.Any],
    ) -> bool:
        return _chain.chain_forecast_cycle.cycle_download_success_missing_raw_manifest(self, stage, context, job)

    @staticmethod
    def _job_matches_stage(job: _chain.Mapping[str, _chain.Any], stage: _chain.StageDefinition) -> bool:
        return _chain.chain_forecast_cycle.job_matches_stage(job, stage)

    @staticmethod
    def _job_needs_submission(job: _chain.Mapping[str, _chain.Any]) -> bool:
        return _chain.chain_forecast_cycle.job_needs_submission(job)


def _terminal_stage_needs_forced_resubmit(
    context: _chain.CycleOrchestrationContext,
    job: _chain.Mapping[str, _chain.Any],
) -> bool:
    status = str(job.get("status") or "")
    if status not in _chain.TERMINAL_JOB_STATUSES:
        return False
    if not context.active_basins:
        return False
    job_stage = _canonical_stage(job.get("stage") or job.get("job_type"))
    if job_stage is None:
        return False
    for basin in context.active_basins:
        state_evidence = basin.get("state_evidence")
        if not isinstance(state_evidence, _chain.Mapping):
            return False
        if state_evidence.get("decision") not in _FORCE_TERMINAL_RESUBMIT_DECISIONS:
            return False
        restart_stage = _canonical_stage(
            state_evidence.get("restart_stage")
            or state_evidence.get("restart_from_stage")
            or context.restart_stage
        )
        if restart_stage is None or _STAGE_ORDER[job_stage] < _STAGE_ORDER[restart_stage]:
            return False
    return True


def _verified_accepted_submit_forecast_retry(job: _chain.Mapping[str, _chain.Any]) -> bool:
    """Allow the reclaim shortcut only for reconcile-verified forecast cohorts."""

    return bool(
        job.get("submit_outcome") in {"accepted", "submit_result_ambiguous"}
        and job.get("reconciliation_source") == "slurm_exact_comment"
        and job.get("reconciliation_decision") == "absence_retry_permitted"
        and job.get("matched_slurm_job_id") is None
        and forecast_cohort_identity_is_valid(job)
    )


def _canonical_stage(value: _chain.Any) -> str | None:
    if value in (None, ""):
        return None
    stage = str(value)
    aliases = {
        "convert_canonical": "convert",
        "produce_forcing_array": "forcing",
        "run_shud_forecast_array": "forecast",
        "save_state_snapshot_array": "state_save_qc",
        "parse_output_array": "parse",
        "publish_tiles": "publish",
    }
    stage = aliases.get(stage, stage)
    return stage if stage in _STAGE_ORDER else None
