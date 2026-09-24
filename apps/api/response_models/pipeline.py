"""Response models for ``apps/api/routes/pipeline.py`` (#2348).

``PipelineJob`` ORM timestamps are ``datetime`` objects. ``pipeline_status``
passes the ``met.forecast_cycle`` driver values through untouched (``datetime``
on PostgreSQL, text on the SQLite test double), hence ``datetime | str``.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from apps.api.response_models.envelope import OkEnvelope, OpenModel

Number = int | float


class OpsIdentity(OpenModel):
    """``_success_identity_payload``; present only on strict-identity successes."""

    source: str
    cycle_time: str
    run_id: str
    model_id: str
    job_id: str | None = None


class OpsLogIdentity(OpsIdentity):
    """The job-log identity: ``job_log`` always passes ``job_id`` (``pipeline._ok``)."""

    job_id: str


class JobStatusCounts(OpenModel):
    succeeded: int
    failed: int
    running: int
    pending: int


class PipelineStatus(OpenModel):
    cycle_id: str
    source: str | None
    cycle_time: datetime | str | None
    current_state: str
    started_at: datetime | str | None
    updated_at: datetime | str | None
    job_counts: JobStatusCounts


class BasinProgress(OpenModel):
    completed: int
    total: int
    failed: int


class BasinResult(OpenModel):
    """``_basin_result``."""

    job_id: str
    run_id: str | None
    cycle_id: str | None
    job_type: str
    slurm_job_id: str | None
    model_id: str | None
    basin_id: str | None
    status: str
    stage: str | None
    submitted_at: datetime | None
    started_at: datetime | None
    finished_at: datetime | None
    duration_seconds: int | None
    retry_count: int
    error_code: str | None
    error_message: str | None
    log_uri: str | None


class PipelineStage(OpenModel):
    """``_stage_summaries`` / ``_stage_summaries_for_strict_identity``."""

    stage: str
    display_status: str
    status: str | None = None
    duration_seconds: int | None
    basin_progress: BasinProgress
    basin_results_limit: int
    basin_results_total: int
    basin_results_returned: int
    basin_results_truncated: bool
    basin_results: list[BasinResult]


class PipelineJob(OpenModel):
    """``_job_payload``."""

    job_id: str
    run_id: str | None
    cycle_id: str | None
    run_type: str | None
    scenario: str | None
    job_type: str
    slurm_job_id: str | None
    model_id: str | None
    status: str
    stage: str | None
    submitted_at: datetime | None
    started_at: datetime | None
    finished_at: datetime | None
    exit_code: int | None
    retry_count: int
    error_code: str | None
    error_message: str | None
    log_uri: str | None
    duration_seconds: int | None


class PipelineJobPage(OpenModel):
    items: list[PipelineJob]
    total: int
    limit: int
    offset: int


class JobLogs(OpenModel):
    job_id: str
    log_uri: str
    content: str


class RetryRunResult(OpenModel):
    job_id: str
    pipeline_job_id: str
    run_id: str | None
    retry_count: int
    status: str
    slurm_job_id: str | None
    execution_status: str


class CancellationGap(OpenModel):
    """``_slurm_cancellation_gap_payload`` (``error``) or
    ``_unproven_slurm_cancellation_payload`` (``gateway_response``)."""

    job_id: str
    run_id: str
    status: str
    slurm_job_id: str | None
    cancellation_proven: bool
    error: dict[str, Any] | None = None
    gateway_response: Any = None


class IdempotentCancelJob(OpenModel):
    job_id: str
    slurm_job_id: str | None
    note: str
    error_code: str


class HydroRunCancelTransition(OpenModel):
    """``_cancel_hydro_run``."""

    run_id: str
    previous_status: str
    status: str
    preserved: bool


class ForecastCycleCancelTransition(OpenModel):
    """``_cancel_forecast_cycle``."""

    cycle_id: str
    previous_status: str
    status: str
    preserved: bool


class CancelRunResult(OpenModel):
    """``cancel_run``: the alias pairs (``cancelled``/``cancelled_jobs``, ...)
    carry the same list."""

    run_id: str
    cancelled_jobs: list[PipelineJob]
    cancelled: list[PipelineJob]
    failed_jobs: list[CancellationGap]
    slurm_failures: list[CancellationGap]
    blocked_jobs: list[CancellationGap]
    slurm_cancellation_gaps: list[CancellationGap]
    partial_failure: bool
    idempotent_jobs: list[IdempotentCancelJob]
    hydro_run: HydroRunCancelTransition | None
    forecast_cycle: ForecastCycleCancelTransition | None


class StageDurationMetric(OpenModel):
    date: str
    stage: str
    average_duration_seconds: Number
    job_count: int


class SuccessRateMetric(OpenModel):
    date: str
    success_rate: Number
    succeeded_cycles: int
    total_cycles: int


class QueueDepth(OpenModel):
    running: int
    pending: int
    idle: int


class _OpsEnvelope(OkEnvelope):
    """``pipeline._ok``: ``identity`` only on strict-identity successes."""

    identity: OpsIdentity | None = None


class PipelineStatusEnvelope(_OpsEnvelope):
    data: PipelineStatus


class PipelineStageListEnvelope(_OpsEnvelope):
    data: list[PipelineStage]


class PipelineJobPageEnvelope(_OpsEnvelope):
    data: PipelineJobPage


class JobLogsEnvelope(_OpsEnvelope):
    identity: OpsLogIdentity | None = None
    data: JobLogs


class RetryRunResultEnvelope(OkEnvelope):
    data: RetryRunResult


class CancelRunResultEnvelope(OkEnvelope):
    data: CancelRunResult


class StageDurationMetricListEnvelope(OkEnvelope):
    data: list[StageDurationMetric]


class SuccessRateMetricListEnvelope(OkEnvelope):
    data: list[SuccessRateMetric]


class QueueDepthEnvelope(OkEnvelope):
    data: QueueDepth
