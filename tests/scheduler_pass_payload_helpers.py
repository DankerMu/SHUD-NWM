"""A writer-shaped scheduler pass payload with one submitted model run.

Not collected (no ``test_`` prefix). Used by
``tests/test_scheduler_evidence_decidability.py`` to drive the summary tier with a
pass whose ``model_run_evidence`` must persist verbatim.
"""

from __future__ import annotations

SCHEDULER_SCHEMA = "nhms.production_scheduler.pass_evidence.v1"


def _scheduler_evidence_payload(
    *,
    pass_id: str = "scheduler_20260521120000_fixed",
    status: str = "planned",
    execution_mode: str = "dry_run",
    final_claimed: bool = False,
    no_mutation_proof: dict[str, bool] | None = None,
    **extra: object,
) -> dict[str, object]:
    payload: dict[str, object] = {
        "schema": SCHEDULER_SCHEMA,
        "pass_id": pass_id,
        "status": status,
        "execution_mode": execution_mode,
        "started_at": "2026-05-21T12:00:00Z",
        "finished_at": "2026-05-21T12:00:01Z",
        "sources": ["gfs", "IFS"],
        "operator_filters": {
            "model_ids": ["model_a"],
            "basin_ids": [],
            "expression": "model_id in [model_a]",
            "excluded_runnable_count": 1,
        },
        "counts": {
            "candidate_count": 2,
            "blocked_candidate_count": 0,
            "skipped_candidate_count": 1,
            "selected_model_count": 1,
            "source_cycle_count": 2,
            "submitted_count": 0,
            "failed_count": 0,
            "partial_count": 0,
        },
        "candidates": [
            {
                "candidate_id": "gfs:2026-05-21T06:00:00Z:model_a:forecast_gfs_deterministic",
                "source_id": "gfs",
                "cycle_time_utc": "2026-05-21T06:00:00Z",
                "model_id": "model_a",
                "scenario_id": "forecast_gfs_deterministic",
                "run_id": "fcst_gfs_2026052106_model_a",
                "forcing_version_id": "forc_gfs_2026052106_model_a",
            }
        ],
        "blocked_candidates": [],
        "skipped_candidates": [
            {
                "candidate_id": "IFS:2026-05-21T06:00:00Z:model_a:forecast_ifs_deterministic",
                "source_id": "IFS",
                "cycle_time_utc": "2026-05-21T06:00:00Z",
                "model_id": "model_a",
                "scenario_id": "forecast_ifs_deterministic",
                "reason": "source_cycle_unavailable",
            }
        ],
        "model_run_evidence": [],
        "artifact_path": "workspace/.nhms-workspace/scheduler/evidence/scheduler_20260521120000_fixed.json",
        "execution_boundary": "planning_only",
        "no_mutation_proof": no_mutation_proof
        or {
            "adapter_download_called": False,
            "slurm_submit_called": False,
            "slurm_status_sync_called": False,
            "slurm_cancellation_called": False,
            "shud_runtime_called": False,
            "hydro_result_table_writes": False,
            "met_result_table_writes": False,
            "pipeline_status_writes": False,
            "pipeline_event_writes": False,
        },
        "readiness": {
            "deterministic_fixture": True,
            "live_receipts": [],
            "production_ready": final_claimed,
        },
        "final_production_readiness_claimed": final_claimed,
    }
    payload |= extra
    return payload


def _candidate_for_model(model_id: str) -> dict[str, object]:
    return {
        "candidate_id": f"gfs:2026-05-21T06:00:00Z:{model_id}:forecast_gfs_deterministic",
        "source_id": "gfs",
        "cycle_time_utc": "2026-05-21T06:00:00Z",
        "model_id": model_id,
        "scenario_id": "forecast_gfs_deterministic",
        "run_id": f"fcst_gfs_2026052106_{model_id}",
        "forcing_version_id": f"forc_gfs_2026052106_{model_id}",
    }


def _live_scheduler_payload(
    *,
    status: str = "submitted",
    model_run_status: str = "submitted",
) -> dict[str, object]:
    payload = _scheduler_evidence_payload(status=status, execution_mode="production_orchestration")
    payload["counts"]["candidate_count"] = 1
    payload["counts"]["skipped_candidate_count"] = 0
    payload["counts"]["submitted_count"] = 1
    payload["skipped_candidates"] = []
    payload["model_run_evidence"] = [
        {
            **_candidate_for_model("model_a"),
            "status": model_run_status,
            "submitted": True,
        }
    ]
    return payload
