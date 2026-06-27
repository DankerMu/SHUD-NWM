from __future__ import annotations

import os
from pathlib import Path

from services.orchestrator import chain as _chain

CycleOrchestrationContext = _chain.CycleOrchestrationContext
OrchestratorError = _chain.OrchestratorError
ReservationResult = _chain.ReservationResult
StageDefinition = _chain.StageDefinition
StageRunResult = _chain.StageRunResult
redact_payload = _chain.redact_payload


def _cycle_pipeline_job_model_id(*args, **kwargs):
    return getattr(_chain, "_cycle_pipeline_job_model_id")(*args, **kwargs)


def _pipeline_job_id(*args, **kwargs):
    return getattr(_chain, "_pipeline_job_id")(*args, **kwargs)


def _safe_pipeline_event_details(*args, **kwargs):
    return getattr(_chain, "_safe_pipeline_event_details")(*args, **kwargs)


def _submission_runtime_root_contract(*args, **kwargs):
    return getattr(_chain, "_submission_runtime_root_contract")(*args, **kwargs)


def _utcnow(*args, **kwargs):
    return getattr(_chain, "_utcnow")(*args, **kwargs)


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
    message = str(redact_payload(str(error)))
    error_code = getattr(error, "error_code", None) or "SBATCH_SUBMISSION_FAILED"
    self.repository.upsert_pipeline_job(
        {
            "job_id": pipeline_job_id,
            "run_id": context.run_id,
            "cycle_id": context.cycle_id,
            "job_type": stage.job_type,
            "slurm_job_id": None,
            "model_id": _cycle_pipeline_job_model_id(context),
            "status": "submission_failed",
            "stage": stage.stage,
            "submitted_at": now,
            "started_at": None,
            "finished_at": now,
            "exit_code": None,
            "error_code": error_code,
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
        details=_safe_pipeline_event_details(
            {
                "stage": stage.stage,
                "job_type": stage.job_type,
                "error": message,
                "runtime_root_contract": _submission_runtime_root_contract(
                    {
                        "workspace_dir": str(Path(self.config.workspace_root)),
                        "object_store_root": str(Path(self.config.object_store_root)),
                        "object_store_prefix": self.config.object_store_prefix,
                        "published_artifact_root": os.getenv("NHMS_PUBLISHED_ARTIFACT_ROOT", ""),
                        "published_artifact_uri_prefix": os.getenv(
                            "NHMS_PUBLISHED_ARTIFACT_URI_PREFIX",
                            "published://",
                        ),
                    }
                ),
            }
        ),
    )
    self.repository.update_forecast_cycle_status(
        source_id=context.source_id,
        cycle_time=context.cycle_time,
        status=stage.failure_cycle_status,
        error_code=error_code,
        error_message=message,
    )
    return StageRunResult(
        stage=stage.stage,
        job_type=stage.job_type,
        pipeline_job_id=pipeline_job_id,
        slurm_job_id="",
        status="submission_failed",
        error_code=error_code,
        error_message=message,
        task_results=(),
    )


def _skip_duplicate_submission(
    self,
    stage: StageDefinition,
    context: CycleOrchestrationContext,
    pipeline_job_id: str,
    reservation: ReservationResult | None,
) -> StageRunResult:
    """Record skip evidence and return without sbatch (candidate in flight).

    Invoked when the reserve gate proves another pass already holds an active
    reservation for this candidate+stage. We emit a durable skip event so the
    overlap receipt can prove the double-submission was prevented, then
    return a typed ``skipped_duplicate_submission`` result; no sbatch runs.
    """

    idempotency_key = reservation.idempotency_key if reservation is not None else ""
    active_status = reservation.status if reservation is not None else None
    skip = {
        "stage": stage.stage,
        "job_type": stage.job_type,
        "idempotency_key": idempotency_key,
        "pipeline_job_id": pipeline_job_id,
        "reservation_status": active_status,
        "reason": "candidate_already_inflight",
    }
    self.duplicate_submission_skips.append(skip)
    try:
        self.repository.insert_pipeline_event(
            entity_type="pipeline_job",
            entity_id=pipeline_job_id,
            event_type="submission_skipped",
            status_from=active_status,
            status_to="skipped_duplicate_submission",
            message=(
                f"{stage.stage} sbatch skipped: candidate already in flight "
                f"(idempotency_key={idempotency_key}, status={active_status})."
            ),
            details=_safe_pipeline_event_details(skip),
        )
    except OrchestratorError:
        # Evidence emission must never abort a correct skip decision.
        pass
    return StageRunResult(
        stage=stage.stage,
        job_type=stage.job_type,
        pipeline_job_id=pipeline_job_id,
        slurm_job_id="",
        status="skipped_duplicate_submission",
        error_code=None,
        error_message=(
            f"Skipped duplicate submission for idempotency_key={idempotency_key}; candidate already in flight."
        ),
        task_results=(),
    )
