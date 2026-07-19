from __future__ import annotations

import os
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from services.orchestrator import chain as _chain
from services.orchestrator.run_tree_copyback import RunTreeCopybackError, copyback_run_trees
from services.orchestrator.scheduler_timing import (
    current_scheduler_pass_timing,
    current_stage_span,
    set_current_stage_span,
)


@contextmanager
def _null_candidate_span() -> Any:
    """No-op stand-in for ``SchedulerPassTiming.candidate_span``.

    Used by SUB-4 (#862) inside ``_submit_and_wait`` when no scheduler-pass
    collector is bound (``trigger_forecast`` from CLI / unit-test paths).
    Yields a throwaway dict so the branchless per-sub-phase writes below
    stay uniform; nothing is retained. A fresh context manager is minted
    per invocation so nested / repeated ``_submit_and_wait`` calls are safe.
    """

    yield {}

# SUB-3 (#861): canonical five-entry ``stage_name`` domain per spec.md §
# "Stage-layer timing" — mirrors ``chain_repository_state._FORECAST_STAGE_ORDER``.
# Only stages in this set open a ``stage_span`` inside ``_run_cycle_chain``;
# the ``publish`` stage runs locally on the control node (no Slurm dispatch)
# and is intentionally out of the canonical timing domain.
_CANONICAL_TIMING_STAGES = frozenset(
    ("convert", "forcing", "forecast", "parse", "state_save_qc")
)

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
    # SUB-3 (#861) Phase 6.5: per-pipeline-stage ``stage_span`` — the collector
    # is bound to a ``ContextVar`` in ``scheduler_execution.execute_candidate_cohort``
    # before ``orchestrate_cycle`` is called, so this read is per-worker-thread
    # safe under ``concurrent_submit_bound > 1``. ``None`` in the ``trigger_forecast``
    # / test-fixture code paths that never enter a scheduler pass.
    collector = current_scheduler_pass_timing()
    for stage_index, stage in enumerate(self.stages):
        if stage_index < start_stage_index:
            continue
        existing_jobs = self._query_pipeline_jobs_for_cycle_context(context)
        had_partial_before_stage = context.had_partial
        last_partial_before_stage = context.last_partial_status
        # Open one ``stage_span`` per (source_id, cycle_id, stage.stage) tuple —
        # the canonical five-entry ``_FORECAST_STAGE_ORDER`` domain matches
        # spec.md §"Stage-layer timing"; the non-canonical ``publish`` stage
        # (local control-node work, no Slurm dispatch) is intentionally
        # excluded. ``retry_attempts`` loop runs INSIDE the span so retries
        # collapse into a single stage record.
        with _open_stage_timing_span(collector, stage, context) as span:
            with set_current_stage_span(span):
                basin_count_at_entry = len(context.active_basins)
                retry_attempts = 0
                retry_pipeline_job_id: str | None = None
                pipeline_result: PipelineResult | None = None
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
                        pipeline_result = PipelineResult(
                            context.run_id,
                            context.cycle_id,
                            "failed",
                            tuple(stage_results),
                            _candidate_outcomes(context, final_status="failed"),
                        )
                        break

                    if result.status == "cancelled":
                        pipeline_result = PipelineResult(
                            context.run_id,
                            context.cycle_id,
                            "failed",
                            tuple(stage_results),
                            _candidate_outcomes(context, final_status="failed"),
                        )
                        break

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

            # Populate stage-span counters from the final ``StageRunResult`` for
            # this stage BEFORE ``stage_span.__exit__`` finalises the record so
            # the per-stage invariant
            # ``python_time_ms + slurm_wait_ms == total_wall_ms`` computed inside
            # ``SchedulerPassTiming.stage_span`` holds. ``build_candidates_ms`` is
            # a SUB-4 concern (per-basin sub-phases; see spec.md §candidate-layer
            # and tasks.md §2.4) — SUB-3 leaves it at ``0.0`` so ``dispatch_ms``
            # picks up the full python side.
            if span is not None:
                _populate_stage_span_counters(
                    span, basin_count_at_entry=basin_count_at_entry, result=result
                )

        # After ``stage_span.__exit__`` has finalised ``python_time_ms`` /
        # ``total_wall_ms`` on the record, backfill ``dispatch_ms`` = python-time
        # (spec.md §"Stage-layer timing": ``python_time_ms = build_candidates_ms +
        # dispatch_ms``; SUB-3 leaves ``build_candidates_ms`` at ``0`` so
        # ``dispatch_ms`` equals ``python_time_ms``). The record dict is
        # aliased into ``collector._stages`` so mutation after the ``with`` is
        # immediately visible to ``finalize_evidence``.
        if span is not None:
            span.set_dispatch_ms(float(span.record.get("python_time_ms", 0.0)))

        if pipeline_result is not None:
            return pipeline_result

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


def _open_stage_timing_span(
    collector: Any | None,
    stage: StageDefinition,
    context: CycleOrchestrationContext,
) -> Any:
    """Return a stage_span context manager for canonical stages, or a no-op stub.

    Returning a context manager (rather than an optional value) lets the caller
    use ``with _open_stage_timing_span(...) as span:`` regardless of whether the
    collector is present or the stage is in the canonical timing domain.
    ``span`` is ``None`` in the no-op case; the caller guards counter population
    on ``span is not None``.
    """

    if collector is None or stage.stage not in _CANONICAL_TIMING_STAGES:
        return _NoopStageSpanContext()
    return collector.stage_span(
        stage.stage, source_id=context.source_id, cycle_id=context.cycle_id
    )


class _NoopStageSpanContext:
    """Context manager that yields ``None`` — used when timing is inactive."""

    def __enter__(self) -> None:
        return None

    def __exit__(self, exc_type: Any, exc_val: Any, exc_tb: Any) -> None:
        return None


def _populate_stage_span_counters(
    span: Any,
    *,
    basin_count_at_entry: int,
    result: StageRunResult,
) -> None:
    """Attribute ``basin_count`` / ``submitted_count`` / ``failed_count`` to the span.

    Per spec.md §"Stage-layer timing" each stage record carries these three
    counters. Interpretation matches the ``StageRunResult.status`` domain:

    - ``succeeded`` — all entering basins reached Slurm dispatch and completed
      successfully, so ``submitted_count == basin_count`` and ``failed_count == 0``.
    - ``partially_failed`` — array stages returning a mixed aggregation; treated
      the same as ``succeeded`` for ``submitted_count`` because Slurm did dispatch
      every basin (the partial failure is per-basin task outcome, tracked
      separately in ``task_results``).
    - Everything else (``failed`` / ``submission_failed`` / ``permanently_failed``
      / ``cancelled`` / etc.) — none of the entering basins reached a successful
      terminal state at this stage.
    """

    span.set_basin_count(basin_count_at_entry)
    if result.status in TERMINAL_PIPELINE_SUCCESS_STATUSES or result.status == "partially_failed":
        span.set_submitted_count(basin_count_at_entry)
        span.set_failed_count(0)
    else:
        span.set_submitted_count(0)
        span.set_failed_count(basin_count_at_entry)


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
    if stage.stage == "forecast" and aggregation is not None:
        _update_array_forecast_hydro_statuses(self, context, aggregation)
    if result_status == "succeeded":
        if _stage_should_copyback_run_trees(self, stage):
            _copyback_stage_run_trees(self, context, stage=stage.stage)
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


def _update_array_forecast_hydro_statuses(
    self,
    context: CycleOrchestrationContext,
    aggregation: ArrayAggregation,
) -> None:
    basins_by_task = {
        int(basin.get("task_id", index)): basin
        for index, basin in enumerate(context.active_basins)
    }
    for task in aggregation.task_results:
        basin = basins_by_task.get(task.task_id)
        if basin is None:
            continue
        run_id = str(basin.get("run_id") or "").strip()
        if not run_id:
            continue
        if task.status == "succeeded":
            self.repository.update_hydro_run_status(
                run_id,
                "succeeded",
                slurm_job_id=task.slurm_job_id,
            )
            continue
        if task.status in {"failed", "cancelled"}:
            self.repository.update_hydro_run_status(
                run_id,
                "failed",
                slurm_job_id=task.slurm_job_id,
                error_code=task.error_code or f"FORECAST_TASK_{task.status.upper()}",
                error_message=task.error_message or f"Forecast array task {task.task_id} {task.status}.",
            )


def _stage_should_copyback_run_trees(self, stage: StageDefinition) -> bool:
    if stage.stage == "parse":
        return True
    return stage.stage == "state_save_qc" and self.config.terminal_stage == "forecast_state_save_qc"


def _copyback_stage_run_trees(self, context: CycleOrchestrationContext, *, stage: str) -> None:
    copyback_root = os.getenv("NHMS_OBJECT_STORE_COPYBACK_ROOT", "").strip()
    if not copyback_root:
        return
    run_ids = [str(basin.get("run_id") or "").strip() for basin in context.active_basins]
    run_ids = [run_id for run_id in run_ids if run_id]
    if not run_ids:
        return
    try:
        summary = copyback_run_trees(
            object_store_root=self.config.object_store_root,
            copyback_root=copyback_root,
            run_ids=run_ids,
            extra_object_keys=_copyback_extra_object_keys(self.config.object_store_root, stage=stage),
            object_store_prefix=self.config.object_store_prefix,
        )
    except RunTreeCopybackError as error:
        self.repository.insert_pipeline_event(
            entity_type="forecast_cycle",
            entity_id=context.cycle_id,
            event_type="object_store_copyback",
            status_from=None,
            status_to="failed",
            message=f"Run-tree object-store copyback failed after {stage}.",
            details=_safe_pipeline_event_details(
                {
                    "stage": stage,
                    "run_ids": run_ids,
                    "error_code": error.code,
                    "error_message": error.message,
                    "details": error.details,
                }
            ),
        )
        raise _chain.OrchestratorError(error.code, error.message, error.details) from error
    if summary is None:
        return
    self.repository.insert_pipeline_event(
        entity_type="forecast_cycle",
        entity_id=context.cycle_id,
        event_type="object_store_copyback",
        status_from=None,
        status_to=str(summary.get("status") or "completed"),
        message=f"Run-tree object-store copyback completed after {stage}.",
        details=_safe_pipeline_event_details({"stage": stage, **summary}),
    )


def _copyback_extra_object_keys(object_store_root: str | Path, *, stage: str) -> tuple[str, ...]:
    if stage != "state_save_qc":
        return ()
    state_index_key = "scheduler/state-index/index-last.json"
    try:
        if (Path(object_store_root) / state_index_key).is_file():
            return (state_index_key,)
    except OSError:
        return ()
    return ()


def _submit_and_wait(
    self,
    stage: StageDefinition,
    context: ForecastRunContext | AnalysisRunContext,
    *,
    first_stage: bool,
) -> StageRunResult:
    self._before_stage_submit(stage, context)

    # SUB-4 (#862): open a per-candidate span so the four dispatch sub-phases
    # (``build_stage_manifest_ms``, ``submit_sbatch_ms``, ``poll_until_terminal_ms``,
    # ``post_stage_hook_ms``) attach to a persistent record when the collector
    # level is ``candidate``. ``candidate_span`` yields a throwaway dict below
    # level ``candidate`` so callers can populate fields unconditionally.
    # ``collector`` is ``None`` from CLI / unit-test paths that never enter a
    # scheduler pass — in that case we skip opening a span and use a local dict
    # (the writes are effectively free and simplify branchless code below).
    collector = current_scheduler_pass_timing()
    stage_span = current_stage_span()
    if collector is not None:
        _candidate_cm = collector.candidate_span(
            stage.stage,
            model_id=context.model_id,
            basin=context.basin_id,
            source_id=context.source_id,
        )
    else:
        _candidate_cm = _null_candidate_span()
    with _candidate_cm as candidate_record:
        ns_before_build = time.monotonic_ns()
        manifest = self._build_stage_submission_manifest(stage, context)
        candidate_record["build_stage_manifest_ms"] = (
            time.monotonic_ns() - ns_before_build
        ) / 1_000_000.0
        payload = {
            "run_id": context.run_id,
            "model_id": context.model_id,
            "job_type": stage.job_type,
            "manifest": self._slurm_submission_manifest(manifest),
        }
        # SUB-3 (#861): direct-measure the Slurm-boundary wall-clock at every
        # dispatch site. ``collector`` + ``stage_span`` come from the
        # ``ContextVar`` slots bound in ``scheduler_execution.execute_candidate_cohort``
        # (collector) and ``_run_cycle_chain`` per pipeline-stage iteration
        # (stage_span). ContextVar keeps this per-worker-thread safe under
        # ``concurrent_submit_bound > 1`` — the previous
        # ``getattr(self, "_scheduler_pass_timing", ...)`` + attribute stash raced
        # because a shared ``ForecastOrchestrator`` may serve multiple
        # ``ThreadPoolExecutor`` workers. Both slots are ``None`` in the
        # ``trigger_forecast`` path from CLI / unit-test fixtures that never enter
        # a scheduler pass, in which case the timing wrap is a no-op. Spec.md
        # "python-time and slurm-wait attribution is direct-measured, never
        # inferred": both ``submit_job`` and ``_poll_until_terminal`` MUST be
        # wrapped so ``slurm_wait_ms`` is a measurement, not an inference by
        # subtraction (also covers the already-terminal-on-submit fast path
        # at L568-572 because ``submit_job`` is wrapped regardless of branch).
        # SUB-4 (#862) reuses the same ``ns_before_submit`` / ``ns_after_submit``
        # bookends to also record ``submit_sbatch_ms`` on the candidate record.
        ns_before_submit = time.monotonic_ns()
        try:
            submitted = self.slurm_client.submit_job(payload)
        finally:
            # Attribute submit wall-clock unconditionally in a ``finally`` so
            # a raised ``submit_job`` (gateway timeout, transport error) still
            # accounts for the wall it actually consumed on the Slurm side.
            ns_after_submit = time.monotonic_ns()
            candidate_record["submit_sbatch_ms"] = (
                ns_after_submit - ns_before_submit
            ) / 1_000_000.0
            if collector is not None and stage_span is not None:
                stage_span.add_slurm_wait_interval(
                    collector._ms_from_pass_entry(ns_before_submit),
                    collector._ms_from_pass_entry(ns_after_submit),
                )
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
            # Fast path (spec.md L172-176): submit_job returned terminal, so the
            # ~100 ms of Slurm wall the test scenario attributes lives entirely
            # inside the submit wrap above; ``_poll_until_terminal`` is never
            # called and no additional interval is added here.
            # SUB-4 (#862): ``poll_until_terminal_ms`` is ``0`` on the fast path.
            candidate_record["poll_until_terminal_ms"] = 0.0
            terminal_observation = TerminalJobObservation(
                job=submitted,
                publication_attempt=submitted_publish_attempt,
            )
        else:
            ns_before_poll = time.monotonic_ns()
            try:
                terminal_observation = self._poll_until_terminal(
                    stage=stage,
                    context=context,
                    pipeline_job_id=pipeline_job_id,
                    initial_job=submitted,
                    initial_status=str(pipeline_record["status"]),
                    log_publication=log_publication,
                )
            finally:
                # Same rationale as the submit wrap: even if poll raises after
                # sleeping in ``time.sleep``, the wall-clock consumed on the
                # Slurm side is real and must be attributed.
                # SUB-4 (#862) reuses the same ``ns_before_poll`` / ``ns_after_poll``
                # bookends to also record ``poll_until_terminal_ms`` on the
                # candidate record.
                ns_after_poll = time.monotonic_ns()
                candidate_record["poll_until_terminal_ms"] = (
                    ns_after_poll - ns_before_poll
                ) / 1_000_000.0
                if collector is not None and stage_span is not None:
                    stage_span.add_slurm_wait_interval(
                        collector._ms_from_pass_entry(ns_before_poll),
                        collector._ms_from_pass_entry(ns_after_poll),
                    )
        terminal = terminal_observation.job
        publication_attempt = terminal_observation.publication_attempt
        log_uri = str(terminal.get("log_uri") or "")
        if not log_uri:
            if publication_attempt is None:
                publication_attempt = self._try_publish_log_for_advertise(slurm_job_id, log_publication)
            log_uri = str(publication_attempt.advertised_uri or "")

        # SUB-4 (#862): time the post-stage hook (whichever branch runs);
        # ``try/finally`` so a raising hook still attributes its wall-clock.
        ns_before_hook = time.monotonic_ns()
        try:
            if terminal["status"] == "succeeded":
                self._after_stage_success(stage, context, terminal)
            else:
                self._after_stage_failure(stage, context, terminal)
        finally:
            candidate_record["post_stage_hook_ms"] = (
                time.monotonic_ns() - ns_before_hook
            ) / 1_000_000.0
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
