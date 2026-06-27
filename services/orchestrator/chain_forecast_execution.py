from __future__ import annotations

import time
from pathlib import Path
from typing import Any

from services.orchestrator import chain as _chain

AnalysisRunContext = _chain.AnalysisRunContext
ArrayAggregation = _chain.ArrayAggregation
ArrayTaskResult = _chain.ArrayTaskResult
CycleOrchestrationContext = _chain.CycleOrchestrationContext
DisplayLogPublication = _chain.DisplayLogPublication
DisplayLogPublicationAttempt = _chain.DisplayLogPublicationAttempt
ForecastRunContext = _chain.ForecastRunContext
Mapping = _chain.Mapping
PipelineJob = _chain.PipelineJob
PipelineResult = _chain.PipelineResult
RetryService = _chain.RetryService
StageDefinition = _chain.StageDefinition
StageRunResult = _chain.StageRunResult
TERMINAL_JOB_STATUSES = _chain.TERMINAL_JOB_STATUSES
TERMINAL_PIPELINE_SUCCESS_STATUSES = _chain.TERMINAL_PIPELINE_SUCCESS_STATUSES
TerminalJobObservation = _chain.TerminalJobObservation
datetime = _chain.datetime


def _aggregation_from_task_results(*args, **kwargs):
    return getattr(_chain, "_aggregation_from_task_results")(*args, **kwargs)


def _candidate_outcomes(*args, **kwargs):
    return getattr(_chain, "_candidate_outcomes")(*args, **kwargs)


def _format_time(*args, **kwargs):
    return getattr(_chain, "_format_time")(*args, **kwargs)


def _parse_gateway_time(*args, **kwargs):
    return getattr(_chain, "_parse_gateway_time")(*args, **kwargs)


def _pipeline_job_id(*args, **kwargs):
    return getattr(_chain, "_pipeline_job_id")(*args, **kwargs)


def _record_array_task_outcomes(*args, **kwargs):
    return getattr(_chain, "_record_array_task_outcomes")(*args, **kwargs)


def _resource_metrics_from_payload(*args, **kwargs):
    return getattr(_chain, "_resource_metrics_from_payload")(*args, **kwargs)


def _restart_stage_index(*args, **kwargs):
    return getattr(_chain, "_restart_stage_index")(*args, **kwargs)


def _safe_pipeline_event_details(*args, **kwargs):
    return getattr(_chain, "_safe_pipeline_event_details")(*args, **kwargs)


def _slurm_accounting_from_payload(*args, **kwargs):
    return getattr(_chain, "_slurm_accounting_from_payload")(*args, **kwargs)


def _stage_result_finished_at(*args, **kwargs):
    return getattr(_chain, "_stage_result_finished_at")(*args, **kwargs)


def _stage_status_message(*args, **kwargs):
    return getattr(_chain, "_stage_status_message")(*args, **kwargs)


def _stage_task_result_evidence(*args, **kwargs):
    return getattr(_chain, "_stage_task_result_evidence")(*args, **kwargs)


def _status_from_gateway_job(*args, **kwargs):
    return getattr(_chain, "_status_from_gateway_job")(*args, **kwargs)


def _utcnow(*args, **kwargs):
    return getattr(_chain, "_utcnow")(*args, **kwargs)


def _run_cycle_chain(self, context: CycleOrchestrationContext) -> PipelineResult:
    stage_results: list[StageRunResult] = []
    start_stage_index = _restart_stage_index(context.restart_stage, self.stages)
    existing_jobs = self._query_pipeline_jobs_for_cycle_context(context)
    refreshed_upstream_finished_at: datetime | None = None
    for stage_index, stage in enumerate(self.stages):
        if stage_index < start_stage_index:
            continue
        existing_jobs = self._query_pipeline_jobs_for_cycle_context(context)
        had_partial_before_stage = context.had_partial
        last_partial_before_stage = context.last_partial_status
        retry_attempts = 0
        retry_pipeline_job_id: str | None = None
        while True:
            existing_job = self._find_existing_stage_job(existing_jobs, stage, context=context)
            if (
                existing_job is not None
                and retry_pipeline_job_id is None
                and self._cycle_download_success_missing_raw_manifest(stage, context, existing_job)
            ):
                retry_pipeline_job_id = self._retry_cycle_stage_job_id(context, stage, existing_job)
            if (
                existing_job is not None
                and retry_pipeline_job_id is None
                and self._terminal_stage_can_retry_after_upstream_refresh(
                    existing_job,
                    refreshed_upstream_finished_at=refreshed_upstream_finished_at,
                )
            ):
                retry_pipeline_job_id = self._retry_cycle_stage_job_id(context, stage, existing_job)
            if (
                existing_job is not None
                and retry_pipeline_job_id is None
                and not self._job_needs_submission(existing_job)
                and not self._terminal_stage_needs_manual_retry(context, existing_job)
            ):
                result, aggregation = self._resume_cycle_stage(stage, context, existing_job)
            else:
                pipeline_job_id = retry_pipeline_job_id
                if pipeline_job_id is None and existing_job is not None:
                    pipeline_job_id = (
                        self._retry_cycle_stage_job_id(context, stage, existing_job)
                        if str(existing_job.get("status")) in TERMINAL_JOB_STATUSES
                        else str(existing_job["job_id"])
                    )
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

        if result.status in TERMINAL_PIPELINE_SUCCESS_STATUSES and result.pipeline_job_id != _pipeline_job_id(
            context.run_id, stage.stage
        ):
            refreshed_upstream_finished_at = _stage_result_finished_at(result)

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

    final_aggregation = _aggregation_from_task_results(tuple(task_results[task_id] for task_id in sorted(task_results)))
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
