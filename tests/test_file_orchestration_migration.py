from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from services.orchestrator import scheduler as scheduler_module
from services.orchestrator.file_orchestration_journal import FileOrchestrationJournalRepository
from services.orchestrator.file_orchestration_migration import (
    MIGRATION_RECEIPT_SCHEMA_VERSION,
    import_historical_scheduler_state,
    write_migration_receipt,
)
from tests.test_production_scheduler import _dt
from workers.data_adapters.base import cycle_id_for


def _job(
    *,
    job_id: str,
    run_id: str,
    cycle_time: Any,
    model_id: str | None,
    status: str,
    job_type: str = "run_shud_forecast_array",
    retry_count: int = 0,
    error_code: str | None = None,
) -> dict[str, Any]:
    return {
        "job_id": job_id,
        "run_id": run_id,
        "cycle_id": cycle_id_for("gfs", cycle_time),
        "job_type": job_type,
        "model_id": model_id,
        "status": status,
        "stage": "download" if job_type == "download_source_cycle" else "forecast",
        "retry_count": retry_count,
        "manual_retry_marker": False,
        "error_code": error_code,
        "created_at": "2026-06-28T00:00:00Z",
        "updated_at": "2026-06-28T00:05:00Z",
    }


def _candidate_state(
    repository: FileOrchestrationJournalRepository,
    *,
    cycle_time: Any,
    model_id: str,
) -> dict[str, Any]:
    return repository.candidate_state(
        source_id="gfs",
        cycle_time=cycle_time,
        model_id=model_id,
        run_id=f"fcst_gfs_2026062800_{model_id}",
        forcing_version_id=f"forc_gfs_2026062800_{model_id}",
        candidate_id=f"migration:gfs:2026062800:{model_id}",
    )


def _historical_rows(cycle_time: Any) -> dict[str, list[dict[str, Any]]]:
    return {
        "forecast_cycles": [
            {
                "cycle_id": cycle_id_for("gfs", cycle_time),
                "source_id": "gfs",
                "cycle_time": cycle_time,
                "status": "forecast_running",
                "created_at": "2026-06-28T00:00:00Z",
            }
        ],
        "hydro_runs": [
            {
                "run_id": "fcst_gfs_2026062800_model_a",
                "run_type": "forecast",
                "scenario_id": "scenario_a",
                "model_id": "model_a",
                "basin_version_id": "basin_a_v1",
                "forcing_version_id": "forc_gfs_2026062800_model_a",
                "source_id": "gfs",
                "cycle_time": cycle_time,
                "start_time": cycle_time,
                "end_time": cycle_time,
                "status": "failed",
                "error_code": "INVALID_MANIFEST",
                "updated_at": "2026-06-28T00:05:00Z",
            }
        ],
        "pipeline_jobs": [
            _job(
                job_id="job_model_a_permanent",
                run_id="fcst_gfs_2026062800_model_a",
                cycle_time=cycle_time,
                model_id="model_a",
                status="permanently_failed",
                retry_count=3,
                error_code="INVALID_MANIFEST",
            ),
            _job(
                job_id="job_model_b_completed",
                run_id="fcst_gfs_2026062800_model_b",
                cycle_time=cycle_time,
                model_id="model_b",
                status="succeeded",
            ),
            _job(
                job_id="job_download_failed",
                run_id="cycle_gfs_2026062800",
                cycle_time=cycle_time,
                model_id=None,
                status="permanently_failed",
                job_type="download_source_cycle",
                retry_count=3,
                error_code="SOURCE_CYCLE_UNAVAILABLE",
            ),
        ],
        "pipeline_events": [
            {
                "entity_type": "pipeline_job",
                "entity_id": "job_model_a_permanent",
                "event_type": "retry",
                "status_from": "permanently_failed",
                "status_to": "manual_repair_requested",
                "details": {
                    "trigger": "manual",
                    "manual_retry_marker": True,
                    "retry_count": 4,
                    "previous_job_id": "job_model_a_permanent",
                    "previous_error": "INVALID_MANIFEST",
                    "prior_failure_reason": "INVALID_MANIFEST",
                },
                "created_at": "2026-06-28T00:06:00Z",
            },
            {
                "entity_type": "pipeline_job",
                "entity_id": "job_download_failed",
                "event_type": "retry",
                "status_from": "permanently_failed",
                "status_to": "manual_repair_requested",
                "details": {
                    "trigger": "manual",
                    "manual_retry_marker": True,
                    "retry_count": 4,
                    "previous_job_id": "job_download_failed",
                    "previous_error": "SOURCE_CYCLE_UNAVAILABLE",
                    "prior_failure_reason": "SOURCE_CYCLE_UNAVAILABLE",
                },
                "created_at": "2026-06-28T00:06:00Z",
            },
        ],
    }


def test_historical_scheduler_state_import_writes_receipt_and_replays_equivalent_decisions(tmp_path: Path) -> None:
    cycle_time = _dt("2026-06-28T00:00:00Z")
    rows = _historical_rows(cycle_time)

    receipt = import_historical_scheduler_state(
        journal_root=tmp_path / "journal",
        cutoff_time=_dt("2026-06-28T00:10:00Z"),
        **rows,
    )
    repository = FileOrchestrationJournalRepository(tmp_path / "journal")
    permanent_state = _candidate_state(repository, cycle_time=cycle_time, model_id="model_a")
    completed_state = _candidate_state(repository, cycle_time=cycle_time, model_id="model_b")
    receipt_path = tmp_path / "receipt.json"
    write_migration_receipt(receipt, receipt_path)

    assert receipt["schema_version"] == MIGRATION_RECEIPT_SCHEMA_VERSION
    assert receipt["source"] == "node22:55433"
    assert receipt["cutoff_time"] == "2026-06-28T00:10:00Z"
    assert receipt["row_counts"] == {
        "forecast_cycles": 1,
        "hydro_runs": 1,
        "pipeline_jobs": 3,
        "pipeline_events": 2,
    }
    assert set(receipt["checksums"]) == {"forecast_cycles", "hydro_runs", "pipeline_jobs", "pipeline_events"}
    assert all(value.startswith("sha256:") for value in receipt["checksums"].values())
    assert receipt["replay_status"]["status"] == "ok"
    assert receipt["stale_download_source_cycle_supersession"]["count"] == 1
    assert permanent_state["pipeline_status"] == "permanently_failed"
    assert scheduler_module._manual_retry_requested(permanent_state) is True
    assert any(
        event["details"].get("prior_failure_reason") == "INVALID_MANIFEST"
        for event in permanent_state["pipeline_events"]
    )
    assert completed_state["pipeline_status"] == "succeeded"
    persisted = json.loads(receipt_path.read_text(encoding="utf-8"))
    assert persisted["checksums"] == receipt["checksums"]


def test_historical_scheduler_state_import_is_idempotent_for_replay_decisions(tmp_path: Path) -> None:
    cycle_time = _dt("2026-06-28T00:00:00Z")
    rows = _historical_rows(cycle_time)

    first = import_historical_scheduler_state(journal_root=tmp_path / "journal", cutoff_time=cycle_time, **rows)
    first_state = _candidate_state(
        FileOrchestrationJournalRepository(tmp_path / "journal"),
        cycle_time=cycle_time,
        model_id="model_a",
    )
    second = import_historical_scheduler_state(journal_root=tmp_path / "journal", cutoff_time=cycle_time, **rows)
    second_state = _candidate_state(
        FileOrchestrationJournalRepository(tmp_path / "journal"),
        cycle_time=cycle_time,
        model_id="model_a",
    )

    assert second["checksums"] == first["checksums"]
    assert second["row_counts"] == first["row_counts"]
    assert first_state["pipeline_status"] == second_state["pipeline_status"] == "permanently_failed"
    assert scheduler_module._manual_retry_payload(first_state) == scheduler_module._manual_retry_payload(second_state)
