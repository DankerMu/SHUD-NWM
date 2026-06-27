from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime
from typing import Any

from services.orchestrator import scheduler as _scheduler
from services.orchestrator.scheduler_evidence import UNKNOWN_AFTER_ATTEMPT
from workers.data_adapters.base import cycle_id_for


def _bounded_active_slurm_jobs(*args, **kwargs):
    return getattr(_scheduler, "_bounded_active_slurm_jobs")(*args, **kwargs)


def _cancelled_job_pipeline_event_write(*args, **kwargs):
    return getattr(_scheduler, "_cancelled_job_pipeline_event_write")(*args, **kwargs)


def _cancelled_job_pipeline_status_write(*args, **kwargs):
    return getattr(_scheduler, "_cancelled_job_pipeline_status_write")(*args, **kwargs)


def _ensure_utc(*args, **kwargs):
    return getattr(_scheduler, "_ensure_utc")(*args, **kwargs)


def _evidence_safe(*args, **kwargs):
    return getattr(_scheduler, "_evidence_safe")(*args, **kwargs)


def _scheduler_cancellation_status(*args, **kwargs):
    return getattr(_scheduler, "_scheduler_cancellation_status")(*args, **kwargs)


def _cancel_requested_active_slurm(self, skipped_candidates: Sequence[dict[str, Any]]) -> list[dict[str, Any]]:
    grouped: dict[tuple[str, str], dict[str, Any]] = {}
    for candidate in skipped_candidates:
        if candidate.get("reason") != "cancel_requested_active_slurm":
            continue
        source_id = str(candidate.get("source_id") or "")
        cycle_time_text = candidate.get("cycle_time_utc")
        if not source_id or not cycle_time_text:
            continue
        grouped.setdefault((source_id, str(cycle_time_text)), candidate)

    evidence: list[dict[str, Any]] = []
    for (source_id, cycle_time_text), skipped in sorted(grouped.items()):
        cycle_time = _ensure_utc(datetime.fromisoformat(cycle_time_text.replace("Z", "+00:00")))
        cycle_id = cycle_id_for(source_id, cycle_time)
        orchestrator = self._cancel_orchestrator_for(source_id)
        cancel = getattr(orchestrator, "cancel_active_cycle_jobs", None)
        if not callable(cancel):
            evidence.append(
                {
                    "source_id": source_id,
                    "cycle_id": cycle_id,
                    "cycle_time_utc": cycle_time_text,
                    "status": "blocked",
                    "error_code": "SLURM_CANCEL_UNSUPPORTED",
                    "cancel_attempted": False,
                    "mutation_occurred": False,
                    "replacement_submitted": False,
                }
            )
            continue
        try:
            cancelled = _bounded_active_slurm_jobs(
                [dict(item) for item in cancel(cycle_id, reason="scheduler_cancel_requested")],
                max_jobs=self.config.candidate_state_job_limit,
            )
        except Exception as error:
            evidence.append(
                {
                    "source_id": source_id,
                    "cycle_id": cycle_id,
                    "cycle_time_utc": cycle_time_text,
                    "status": "failed",
                    "error_code": getattr(error, "error_code", "SLURM_CANCEL_FAILED"),
                    "error_message": _evidence_safe(getattr(error, "message", str(error))),
                    "cancel_attempted": True,
                    "mutation_outcome": UNKNOWN_AFTER_ATTEMPT,
                    "replacement_submitted": False,
                    "active_slurm_jobs": _evidence_safe(skipped.get("active_slurm_jobs", [])),
                    "residual_blockers": [
                        {
                            "code": getattr(error, "error_code", "SLURM_CANCEL_FAILED"),
                            "state": "blocked",
                            "quality_flag": "slurm_cancellation_failed",
                            "residual_risk": (
                                "Slurm cancellation raised after the downstream cancellation method was called; "
                                "mutation outcome is unknown."
                            ),
                        }
                    ],
                }
            )
            continue
        cancellation_status = _scheduler_cancellation_status(cancelled)
        cancellation_item: dict[str, Any] = {
            "source_id": source_id,
            "cycle_id": cycle_id,
            "cycle_time_utc": cycle_time_text,
            "status": cancellation_status,
            "cancelled_jobs": _evidence_safe(cancelled),
            "cancel_attempted": True,
            "mutation_occurred": cancellation_status in {"cancelled", "partially_cancelled"},
            "replacement_submitted": False,
            "active_slurm_jobs": _evidence_safe(skipped.get("active_slurm_jobs", [])),
        }
        pipeline_status_write = any(_cancelled_job_pipeline_status_write(item) for item in cancelled)
        pipeline_event_write = any(_cancelled_job_pipeline_event_write(item) for item in cancelled)
        if pipeline_status_write:
            cancellation_item["pipeline_status_write"] = True
        if pipeline_event_write:
            cancellation_item["pipeline_event_write"] = True
        if cancellation_status != "cancelled":
            cancellation_item["error_code"] = "SLURM_CANCELLATION_GAP"
            cancellation_item["cancellation_proven"] = False
        evidence.append(cancellation_item)
    return evidence
