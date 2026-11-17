"""Shared cycle constants and forcing fixtures of the forcing submit ambiguity suite (#3177).

Split out of ``tests/test_forcing_submit_ambiguity.py``; it and
``tests/test_forcing_submit_ambiguity_reservation.py`` import from here. Not a
test module: pytest does not collect it.
"""

from __future__ import annotations

from typing import Any

from services.orchestrator.chain import CycleOrchestrationContext
from services.orchestrator.file_orchestration_journal import (
    FileOrchestrationJournalRepository,
)
from services.orchestrator.reservation import reserve_candidate
from tests.test_orchestration_chain import _basins, _dt

_CYCLE = "2026050100"
_CYCLE_TIME = _dt("2026-05-01T00:00:00Z")
_CYCLE_ID = f"gfs_{_CYCLE}"


def _expected_forcing_attempt_comment(idempotency_key: str, submission_attempt: int) -> str:
    return f"nhms_forcing_attempt:{idempotency_key}:a{submission_attempt}"


def _forcing_basins(count: int, *, orchestration_run_id: str) -> list[dict[str, Any]]:
    basins = _basins(count)
    for index, basin in enumerate(basins):
        basin.update(
            {
                "run_id": f"fcst_gfs_{_CYCLE}_model_{index}",
                "candidate_id": (
                    f"gfs:2026-05-01T00:00:00Z:model_{index}:forecast_gfs_deterministic"
                ),
                "orchestration_run_id": orchestration_run_id,
                "restart_stage": "forcing",
                "state_evidence": {"restart_stage": "forcing"},
                "model_package_uri": f"s3://nhms/models/model_{index}.tar",
                "model_package_checksum": f"sha256:model-{index}",
            }
        )
    return basins


def _forcing_context(
    orchestrator: Any,
    basins: list[dict[str, Any]],
    *,
    run_id: str,
) -> CycleOrchestrationContext:
    normalized = orchestrator._normalize_cycle_basins(basins, "gfs", _CYCLE_TIME)
    return CycleOrchestrationContext(
        source_id="gfs",
        cycle_time=_CYCLE_TIME,
        cycle_id=_CYCLE_ID,
        run_id=run_id,
        all_basins=normalized,
        active_basins=list(normalized),
        restart_stage="forcing",
    )


def _forcing_members(model_ids: tuple[str, ...]) -> list[dict[str, Any]]:
    return [
        {
            "array_task_id": index,
            "candidate_id": f"gfs:2026-05-01T00:00:00Z:{model_id}:forecast_gfs_deterministic",
            "run_id": f"fcst_gfs_{_CYCLE}_{model_id}",
            "model_id": model_id,
            "basin_id": f"basin_{model_id}",
            "scenario_id": "forecast_gfs_deterministic",
            "restart_stage": "forcing",
        }
        for index, model_id in enumerate(model_ids)
    ]


def _reserve_forcing(
    repository: FileOrchestrationJournalRepository,
    *,
    suffix: str,
    model_ids: tuple[str, ...],
    expected_user: str | None = None,
    expected_account: str | None = None,
    submission_attempt_started_at: Any = _CYCLE_TIME,
):
    run_id = f"cycle_gfs_{_CYCLE}_forcing_{suffix}"
    idempotency_key = f"{run_id}:forcing"
    return reserve_candidate(
        repository,
        idempotency_key=idempotency_key,
        job_id=f"job_{run_id}",
        run_id=run_id,
        cycle_id=_CYCLE_ID,
        job_type="produce_forcing_array",
        model_id=None,
        stage="forcing",
        candidate_id=run_id,
        reservation_evidence={
            "slurm_comment": _expected_forcing_attempt_comment(idempotency_key, 1),
            "cohort_members": _forcing_members(model_ids),
            "restart_stage": "forcing",
            "submission_attempt": 1,
            "submission_attempt_started_at": submission_attempt_started_at,
            "slurm_ownership_required": bool(expected_user and expected_account),
            "expected_slurm_user": expected_user,
            "expected_slurm_account": expected_account,
        },
    )
