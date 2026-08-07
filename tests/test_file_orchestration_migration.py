from __future__ import annotations

import json
import sys
import types
from pathlib import Path
from typing import Any

import pytest

from services.orchestrator import cli as cli_module
from services.orchestrator import file_orchestration_migration as migration_module
from services.orchestrator import scheduler as scheduler_module
from services.orchestrator import scheduler_state_manual_retry as scheduler_state_manual_retry_module
from services.orchestrator.file_orchestration_journal import FileJournalRetryService, FileOrchestrationJournalRepository
from services.orchestrator.file_orchestration_migration import (
    MIGRATION_RECEIPT_SCHEMA_VERSION,
    export_scheduler_state_from_postgres,
    import_historical_scheduler_state,
    write_migration_receipt,
)
from services.orchestrator.retry import RetryConfig
from services.orchestrator.scheduler_state_failure import (
    _downstream_retry_evidence,
    _manual_retry_state_evidence,
    _retry_failure_evidence,
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
                "event_id": 101,
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
                "event_id": 102,
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
    assert permanent_state["pipeline_events"][0]["event_id"] == 101
    assert permanent_state["pipeline_events"][0]["created_at"] == "2026-06-28T00:06:00Z"
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
    assert first_state["pipeline_events_total"] == second_state["pipeline_events_total"]
    assert scheduler_module._manual_retry_payload(first_state) == scheduler_module._manual_retry_payload(second_state)


def test_historical_pipeline_event_messages_are_redacted_in_journal_latest_and_public_reads(tmp_path: Path) -> None:
    cycle_time = _dt("2026-06-28T00:00:00Z")
    rows = _historical_rows(cycle_time)
    rows["pipeline_events"][0]["message"] = (
        "historical repair read /secret/historical/path and s3://nhms-historical/raw/file.grib "
        "manifest=/secret/historical/manifest.json object=s3://nhms-historical/raw/assigned.grib "
        "token=historical-token Authorization: Bearer historical-bearer"
    )
    journal_root = tmp_path / "journal"

    import_historical_scheduler_state(journal_root=journal_root, cutoff_time=cycle_time, **rows)
    repository = FileOrchestrationJournalRepository(journal_root)
    state = _candidate_state(repository, cycle_time=cycle_time, model_id="model_a")
    raw_journal = (journal_root / "journal/gfs/2026062800.jsonl").read_text(encoding="utf-8")
    latest_rendered = "\n".join(path.read_text(encoding="utf-8") for path in (journal_root / "latest").rglob("*.json"))
    public_rendered = json.dumps(state, sort_keys=True)

    for rendered in (raw_journal, latest_rendered, public_rendered):
        for raw in (
            "/secret/historical/path",
            "/secret/historical/manifest.json",
            "s3://nhms-historical/raw/file.grib",
            "s3://nhms-historical/raw/assigned.grib",
            "historical-token",
            "historical-bearer",
        ):
            assert raw not in rendered
    assert "[local-path]" in raw_journal
    assert "[object-uri]" in raw_journal
    assert "[redacted]" in raw_journal


def test_historical_migration_receipt_redacts_skipped_row_identities(tmp_path: Path) -> None:
    cycle_time = _dt("2026-06-28T00:00:00Z")
    rows = _historical_rows(cycle_time)
    rows["hydro_runs"].append(
        {
            "run_id": "hindcast token=run-secret /local/path",
            "run_type": "hindcast",
            "source_id": "gfs",
            "cycle_time": cycle_time,
            "status": "failed",
        }
    )
    rows["pipeline_jobs"].append(
        {
            **_job(
                job_id="file:///local/path/job.log?token=job-secret",
                run_id="hindcast_gfs_2026062800_model_a",
                cycle_time=cycle_time,
                model_id="model_a",
                status="failed",
            ),
            "error_message": "Authorization: Bearer job-bearer",
        }
    )
    rows["pipeline_events"].append(
        {
            "entity_type": "pipeline_job",
            "entity_id": "s3://private-bucket/path/event.json?token=event-secret",
            "event_type": "retry",
            "status_from": "failed",
            "status_to": "manual_repair_requested",
            "details": {"manual_retry_marker": True},
            "created_at": "2026-06-28T00:07:00Z",
        }
    )

    receipt = import_historical_scheduler_state(
        journal_root=tmp_path / "journal",
        cutoff_time=_dt("2026-06-28T00:10:00Z"),
        **rows,
    )
    rendered = json.dumps(receipt["skipped_rows"], sort_keys=True)

    assert receipt["skipped_rows"]["count"] == 3
    for raw in (
        "/local/path",
        "file:///local/path/job.log",
        "s3://private-bucket/path/event.json",
        "run-secret",
        "job-secret",
        "event-secret",
        "job-bearer",
    ):
        assert raw not in rendered
    assert "[local-path]" in rendered
    assert "[uri]" in rendered
    assert "[object-uri]" in rendered
    assert "[redacted]" in rendered


def test_historical_migration_receipt_redacts_stale_download_supersession_sample(
    tmp_path: Path,
) -> None:
    cycle_time = _dt("2026-06-28T00:00:00Z")
    rows = _historical_rows(cycle_time)
    rows["pipeline_events"][1]["details"]["prior_failure_reason"] = (
        "download read s3://private-bucket/raw/file.grib token=download-secret "
        "Authorization: Bearer download-bearer"
    )

    receipt = import_historical_scheduler_state(
        journal_root=tmp_path / "journal",
        cutoff_time=_dt("2026-06-28T00:10:00Z"),
        **rows,
    )
    rendered = json.dumps(receipt["stale_download_source_cycle_supersession"], sort_keys=True)

    assert receipt["stale_download_source_cycle_supersession"]["count"] == 1
    for raw in ("s3://private-bucket/raw/file.grib", "download-secret", "download-bearer"):
        assert raw not in rendered
    assert "[object-uri]" in rendered
    assert "[redacted]" in rendered


def test_historical_scheduler_state_import_skips_unsupported_non_forecast_rows(tmp_path: Path) -> None:
    cycle_time = _dt("2026-06-28T00:00:00Z")
    rows = _historical_rows(cycle_time)
    rows["hydro_runs"].append(
        {
            "run_id": "hindcast_gfs_2026062800_model_a",
            "run_type": "hindcast",
            "scenario_id": "scenario_hindcast",
            "model_id": "model_a",
            "source_id": "gfs",
            "cycle_time": cycle_time,
            "status": "failed",
        }
    )
    rows["pipeline_jobs"].append(
        _job(
            job_id="job_hindcast_legacy",
            run_id="hindcast_gfs_2026062800_model_a",
            cycle_time=cycle_time,
            model_id="model_a",
            status="failed",
        )
    )
    rows["pipeline_events"].append(
        {
            "event_id": 201,
            "entity_type": "pipeline_job",
            "entity_id": "job_hindcast_legacy",
            "event_type": "retry",
            "status_from": "failed",
            "status_to": "manual_repair_requested",
            "details": {"manual_retry_marker": True},
            "created_at": "2026-06-28T00:07:00Z",
        }
    )

    receipt = import_historical_scheduler_state(
        journal_root=tmp_path / "journal",
        cutoff_time=_dt("2026-06-28T00:10:00Z"),
        **rows,
    )
    state = _candidate_state(
        FileOrchestrationJournalRepository(tmp_path / "journal"),
        cycle_time=cycle_time,
        model_id="model_a",
    )

    assert receipt["row_counts"]["hydro_runs"] == 2
    assert receipt["row_counts"]["pipeline_jobs"] == 4
    assert receipt["row_counts"]["pipeline_events"] == 3
    assert receipt["imported_row_counts"]["hydro_runs"] == 1
    assert receipt["imported_row_counts"]["pipeline_jobs"] == 3
    assert receipt["imported_row_counts"]["pipeline_events"] == 2
    assert receipt["skipped_rows"]["count"] == 3
    assert receipt["skipped_rows"]["by_reason"] == {
        "unsupported_pipeline_event_target": 1,
        "unsupported_run_identity": 2,
    }
    assert state["pipeline_status"] == "permanently_failed"
    assert all(job["job_id"] != "job_hindcast_legacy" for job in state["pipeline_jobs"])


def test_historical_scheduler_state_reimport_does_not_override_newer_live_success(tmp_path: Path) -> None:
    cycle_time = _dt("2026-06-28T00:00:00Z")
    rows = _historical_rows(cycle_time)
    journal_root = tmp_path / "journal"

    import_historical_scheduler_state(journal_root=journal_root, cutoff_time=cycle_time, **rows)
    repository = FileOrchestrationJournalRepository(journal_root)
    retry_success = _job(
        job_id="job_model_a_retry_success",
        run_id="fcst_gfs_2026062800_model_a",
        cycle_time=cycle_time,
        model_id="model_a",
        status="succeeded",
        retry_count=4,
    )
    retry_success["idempotency_key"] = "gfs:gfs_2026062800:model_a:retry-success"
    retry_success["created_at"] = "2026-06-28T00:20:00Z"
    retry_success["updated_at"] = "2026-06-28T00:20:00Z"
    repository.upsert_pipeline_job(retry_success)
    before = _candidate_state(repository, cycle_time=cycle_time, model_id="model_a")

    import_historical_scheduler_state(journal_root=journal_root, cutoff_time=cycle_time, **rows)
    after = _candidate_state(
        FileOrchestrationJournalRepository(journal_root),
        cycle_time=cycle_time,
        model_id="model_a",
    )

    assert before["pipeline_status"] == "succeeded"
    assert after["pipeline_status"] == "succeeded"
    assert any(job["job_id"] == "job_model_a_retry_success" for job in after["pipeline_jobs"])


def test_historical_event_ids_do_not_collide_with_new_file_events(tmp_path: Path) -> None:
    cycle_time = _dt("2026-06-28T00:00:00Z")
    rows = _historical_rows(cycle_time)
    rows["pipeline_events"] = [{**rows["pipeline_events"][0], "event_id": 3}]
    journal_root = tmp_path / "journal"

    import_historical_scheduler_state(journal_root=journal_root, cutoff_time=cycle_time, **rows)
    repository = FileOrchestrationJournalRepository(journal_root)
    inserted = repository.insert_pipeline_event(
        entity_type="pipeline_job",
        entity_id="job_model_a_permanent",
        event_type="operator_note",
        status_from="permanently_failed",
        status_to="manual_repair_requested",
        details={"note": "post-migration event"},
    )
    state = _candidate_state(repository, cycle_time=cycle_time, model_id="model_a")

    assert inserted["event_id"] > 3
    assert state["pipeline_events_total"] == 2
    assert {event["event_id"] for event in state["pipeline_events"]} == {3, inserted["event_id"]}
    assert scheduler_module._manual_retry_requested(state) is True


_CYCLE_SCOPE_JOB = {
    "job_id": "job_cycle_gfs_2026070100_download",
    "run_id": "cycle_gfs_2026070100",
    "model_id": None,
    "stage": "download",
    "status": "failed",
    "retry_count": 0,
    "updated_at": "2026-07-01T00:00:00Z",
}
_OWN_MODEL_JOB = {
    "job_id": "job_model_a_forecast",
    "run_id": "fcst_gfs_2026070100_model_a",
    "model_id": "model_a",
    "stage": "forecast",
    "status": "failed",
    "retry_count": 0,
    "updated_at": "2026-07-01T00:00:00Z",
}


def _manual_retry_marker_event(
    *,
    entity_id: str,
    retry_count: int,
    entity_type: str = "pipeline_job",
    event_id: int = 9,
    details_model_id: str | None = None,
    previous_job_id: str | None = None,
    **extra: Any,
) -> dict[str, Any]:
    details: dict[str, Any] = {
        "trigger": "manual",
        "manual_retry_marker": True,
        "retry_count": retry_count,
        "created_at": "2026-07-01T01:00:00Z",
    }
    if details_model_id is not None:
        details["model_id"] = details_model_id
    if previous_job_id is not None:
        details["previous_job_id"] = previous_job_id
    return {
        "event_id": event_id,
        "entity_type": entity_type,
        "entity_id": entity_id,
        "event_type": "retry",
        "created_at": "2026-07-01T01:00:00Z",
        "details": details,
        **extra,
    }


def test_unattributed_cycle_granularity_manual_marker_is_not_adopted_by_model_candidate() -> None:
    state = {
        "pipeline_status": "failed",
        "retry_count": 0,
        "failed_stage": "forecast",
        "pipeline_jobs": [dict(_OWN_MODEL_JOB)],
        "pipeline_events": [
            _manual_retry_marker_event(
                entity_id="gfs_2026070100",
                retry_count=5,
                entity_type="forecast_cycle",
            )
        ],
    }

    payload = scheduler_module._manual_retry_payload(state)

    assert scheduler_module._manual_retry_requested(state) is False
    assert "marker" not in payload
    assert "requested" not in payload
    assert "new_attempt" not in payload


@pytest.mark.parametrize("attribution", ["details", "event_top_level"])
def test_cycle_granularity_manual_marker_naming_candidate_model_is_adopted(attribution: str) -> None:
    if attribution == "details":
        event = _manual_retry_marker_event(
            entity_id="gfs_2026070100",
            retry_count=5,
            entity_type="forecast_cycle",
            details_model_id="model_a",
        )
    else:
        event = _manual_retry_marker_event(
            entity_id="gfs_2026070100",
            retry_count=5,
            entity_type="forecast_cycle",
            model_id="model_a",
        )
    state = {
        "pipeline_status": "failed",
        "retry_count": 0,
        "failed_stage": "forecast",
        # The escape hatch compares against model ids derived from job rows: an
        # empty derived set closes the escape, so the state must carry one.
        "pipeline_jobs": [dict(_OWN_MODEL_JOB)],
        "pipeline_events": [event],
    }

    payload = scheduler_module._manual_retry_payload(state)

    assert scheduler_module._manual_retry_requested(state) is True
    assert payload["marker"] is True
    assert payload["requested"] is True


def test_same_stage_cycle_scope_manual_marker_pins_attempt() -> None:
    """Same-stage cycle-scope manual retry keeps operator/master attempt parity.

    ``failed_stage`` names the very stage the marker's job failed at, so the cycle job's
    attempt counter IS the budget this decision spends.
    """

    candidate = types.SimpleNamespace(candidate_id="candidate_gfs", run_id="fcst_gfs_2026070100_model_a")
    state = {
        "pipeline_status": "failed",
        "retry_count": 0,
        "failed_stage": "download",
        "pipeline_jobs": [{**_CYCLE_SCOPE_JOB, "retry_count": 4}, dict(_OWN_MODEL_JOB)],
        "pipeline_events": [
            _manual_retry_marker_event(
                entity_id="job_cycle_gfs_2026070100_download",
                retry_count=5,
                previous_job_id="job_cycle_gfs_2026070100_download",
            )
        ],
    }

    evidence = _manual_retry_state_evidence(candidate, state, {})

    assert scheduler_module._manual_retry_requested(state) is True
    assert scheduler_module._manual_retry_new_attempt(state, previous_attempt=0) == 5
    assert scheduler_module._manual_retry_payload(state)["new_attempt"] == 5
    assert evidence["manual_retry"]["new_attempt"] == 5


def test_cross_stage_cycle_marker_does_not_pin_forecast_attempt() -> None:
    """Channel 2 harm shape: a download-stage cycle counter must not enter the forecast budget."""

    candidate = types.SimpleNamespace(candidate_id="candidate_gfs", run_id="fcst_gfs_2026070100_model_a")
    state = {
        "pipeline_status": "failed",
        "retry_count": 0,
        "failed_stage": "forecast",
        "pipeline_jobs": [{**_CYCLE_SCOPE_JOB, "retry_count": 4}, dict(_OWN_MODEL_JOB)],
        "pipeline_events": [
            _manual_retry_marker_event(
                entity_id="job_cycle_gfs_2026070100_download",
                retry_count=5,
                previous_job_id="job_cycle_gfs_2026070100_download",
            )
        ],
    }

    evidence = _manual_retry_state_evidence(candidate, state, {})

    assert scheduler_module._manual_retry_requested(state) is True
    assert scheduler_module._manual_retry_new_attempt(state, previous_attempt=0) == 1
    assert "new_attempt" not in scheduler_module._manual_retry_payload(state)
    assert evidence["manual_retry"]["new_attempt"] == 1


def test_cross_stage_unresolvable_cohort_master_marker_does_not_pin_attempt() -> None:
    """Same channel-2 harm through the UNRESOLVED-row gate: the stage suffix must still decide.

    The cohort master row is gone (deleted by the identity filter or pushed out by truncation),
    so the marker's entity resolves to nothing and only its id carries evidence.  The id's cycle
    is the candidate's own, but its stage is ``convert`` while this decision repairs
    ``download`` -- so the counter belongs to another budget and must not be pinned.
    """

    state = {
        "run_id": "fcst_gfs_2026070100_model_a",
        "pipeline_status": "failed",
        "retry_count": 0,
        "failed_stage": "download",
        "pipeline_jobs": [dict(_OWN_MODEL_JOB)],
        "pipeline_events": [
            _manual_retry_marker_event(
                entity_id="job_cycle_gfs_2026070100_convert_cohort_ab12cd34ef56_convert",
                retry_count=5,
            )
        ],
    }

    assert scheduler_module._manual_retry_requested(state) is True
    assert scheduler_module._manual_retry_new_attempt(state, previous_attempt=0) == 1
    assert "new_attempt" not in scheduler_module._manual_retry_payload(state)


def test_unresolvable_cohort_master_marker_pins_when_it_is_the_only_failure_left() -> None:
    """The unresolved-row gate's "only failure left" arm, mirroring the resolved-row rule.

    Production's cohort-restart projection carries no explicit ``failed_stage``, so the stage
    suffix cannot decide; with the candidate's own model job succeeded, nothing but the cycle
    failure could be the repair target, and the operator-pinned attempt must survive.
    """

    state = {
        "run_id": "fcst_gfs_2026070100_model_a",
        "pipeline_status": "failed",
        "retry_count": 0,
        "stage": "forecast",
        "pipeline_jobs": [
            {**_CYCLE_SCOPE_JOB, "retry_count": 4},
            {**_OWN_MODEL_JOB, "status": "succeeded"},
        ],
        "pipeline_events": [
            _manual_retry_marker_event(
                entity_id="job_cycle_gfs_2026070100_convert_cohort_ab12cd34ef56_convert",
                retry_count=5,
            )
        ],
    }

    assert scheduler_module._manual_retry_requested(state) is True
    assert scheduler_module._manual_retry_new_attempt(state, previous_attempt=0) == 5
    assert scheduler_module._manual_retry_payload(state)["new_attempt"] == 5


def test_unresolvable_cohort_master_marker_does_not_pin_beside_a_live_model_failure() -> None:
    """Negative twin of the arm above: one live candidate-scope failure closes it.

    Isomorphic to ``test_unresolvable_cohort_master_marker_pins_when_it_is_the_only_failure_left``
    except that the candidate's own forecast job is still failing, so the cycle failure is not
    the only possible repair target and its counter must stay out of this budget.
    """

    state = {
        "run_id": "fcst_gfs_2026070100_model_a",
        "pipeline_status": "failed",
        "retry_count": 0,
        "stage": "forecast",
        "pipeline_jobs": [
            {**_CYCLE_SCOPE_JOB, "retry_count": 4},
            dict(_OWN_MODEL_JOB),
        ],
        "pipeline_events": [
            _manual_retry_marker_event(
                entity_id="job_cycle_gfs_2026070100_convert_cohort_ab12cd34ef56_convert",
                retry_count=5,
            )
        ],
    }

    assert scheduler_module._manual_retry_requested(state) is True
    assert scheduler_module._manual_retry_new_attempt(state, previous_attempt=0) == 1
    assert "new_attempt" not in scheduler_module._manual_retry_payload(state)


def test_stale_cycle_marker_on_succeeded_job_does_not_pin() -> None:
    """A marker whose resolved cycle job already succeeded is stale: it pins nothing."""

    state = {
        "pipeline_status": "failed",
        "retry_count": 0,
        "failed_stage": "forecast",
        "pipeline_jobs": [
            {**_CYCLE_SCOPE_JOB, "status": "succeeded", "retry_count": 5},
            dict(_OWN_MODEL_JOB),
        ],
        "pipeline_events": [
            _manual_retry_marker_event(
                entity_id="job_cycle_gfs_2026070100_download",
                retry_count=5,
                previous_job_id="job_cycle_gfs_2026070100_download",
            )
        ],
    }

    assert scheduler_module._manual_retry_requested(state) is True
    assert scheduler_module._manual_retry_new_attempt(state, previous_attempt=0) == 1
    assert "new_attempt" not in scheduler_module._manual_retry_payload(state)


def test_repaired_stage_evidence_row_does_not_block_cycle_marker_pin() -> None:
    """A repaired candidate-scope row is not a live failure: it must not close the pin rule.

    ``repair_status == "repaired"`` rows keep a literal ``failed`` status while carrying no
    blocking force anywhere else in this module (see the blocker scan's skip).  Treating one
    as a live candidate-scope failure would make the cycle download the *not-the-only-failure*
    case and silently drop the operator-pinned attempt number.
    """

    candidate = types.SimpleNamespace(candidate_id="candidate_gfs", run_id="fcst_gfs_2026070100_model_a")
    state = {
        "pipeline_status": "failed",
        "retry_count": 1,
        "failed_stage": "forecast",
        "pipeline_jobs": [
            {**_CYCLE_SCOPE_JOB, "retry_count": 4},
            {**_OWN_MODEL_JOB, "repair_status": "repaired"},
        ],
        "pipeline_events": [
            _manual_retry_marker_event(
                entity_id="job_cycle_gfs_2026070100_download",
                retry_count=5,
                previous_job_id="job_cycle_gfs_2026070100_download",
            )
        ],
    }

    evidence = _manual_retry_state_evidence(candidate, state, {})

    assert scheduler_module._manual_retry_requested(state) is True
    assert scheduler_module._manual_retry_new_attempt(state, previous_attempt=1) == 5
    assert scheduler_module._manual_retry_payload(state)["new_attempt"] == 5
    assert evidence["manual_retry"]["new_attempt"] == 5


def test_unsubmitted_auto_retry_placeholder_does_not_block_cycle_marker_pin() -> None:
    """An unsubmitted auto-retry placeholder is not a live failure either.

    Same live-failure domain as the blocker scan: ``submission_failed`` placeholders that were
    never handed to Slurm carry no blocking force, so they must not steal the cycle marker's pin.
    """

    candidate = types.SimpleNamespace(candidate_id="candidate_gfs", run_id="fcst_gfs_2026070100_model_a")
    state = {
        "pipeline_status": "failed",
        "retry_count": 0,
        "failed_stage": "forecast",
        "pipeline_jobs": [
            {**_CYCLE_SCOPE_JOB, "retry_count": 4},
            {
                **_OWN_MODEL_JOB,
                "job_id": "job_model_a_forecast_retry_1",
                "status": "submission_failed",
                "retry_count": 1,
                "slurm_job_id": None,
                "array_task_id": None,
                "manual_retry_marker": False,
            },
        ],
        "pipeline_events": [
            _manual_retry_marker_event(
                entity_id="job_cycle_gfs_2026070100_download",
                retry_count=5,
                previous_job_id="job_cycle_gfs_2026070100_download",
            )
        ],
    }

    evidence = _manual_retry_state_evidence(candidate, state, {})

    assert scheduler_module._manual_retry_requested(state) is True
    assert scheduler_module._manual_retry_new_attempt(state, previous_attempt=0) == 5
    assert scheduler_module._manual_retry_payload(state)["new_attempt"] == 5
    assert evidence["manual_retry"]["new_attempt"] == 5


def test_placeholder_cycle_scope_row_marker_does_not_pin() -> None:
    """The pin rule's live-failure domain must match the blocker scan's on BOTH sides.

    An unsubmitted auto-retry placeholder is already excluded when scanning candidate-scope
    rows; a cycle-scope placeholder is the same non-blocking shape, so it is a stale marker
    target too and must not hand its counter to this decision.
    """

    state = {
        "pipeline_status": "failed",
        "retry_count": 0,
        "failed_stage": "download",
        "pipeline_jobs": [
            {
                **_CYCLE_SCOPE_JOB,
                "job_id": "job_cycle_gfs_2026070100_download_retry_1",
                "status": "submission_failed",
                "retry_count": 1,
                "model_id": None,
                "slurm_job_id": None,
                "array_task_id": None,
            },
            {**_OWN_MODEL_JOB, "status": "succeeded"},
        ],
        "pipeline_events": [
            _manual_retry_marker_event(
                entity_id="job_cycle_gfs_2026070100_download_retry_1", retry_count=5
            )
        ],
    }

    assert scheduler_module._manual_retry_new_attempt(state, previous_attempt=0) == 1
    assert "new_attempt" not in scheduler_module._manual_retry_payload(state)


def test_repaired_cycle_scope_row_marker_does_not_pin() -> None:
    """A repaired cycle-scope row is a stale marker target: it pins nothing.

    Marker carries no ``previous_job_id`` so the historical-repair veto does not pre-empt the
    pin rule; the repaired cycle row must be rejected by the pin rule itself.
    """

    state = {
        "pipeline_status": "failed",
        "retry_count": 0,
        "failed_stage": "download",
        "pipeline_jobs": [
            {**_CYCLE_SCOPE_JOB, "retry_count": 4, "repair_status": "repaired"},
            {**_OWN_MODEL_JOB, "status": "succeeded"},
        ],
        "pipeline_events": [
            _manual_retry_marker_event(entity_id="job_cycle_gfs_2026070100_download", retry_count=5)
        ],
    }

    assert scheduler_module._manual_retry_requested(state) is True
    assert scheduler_module._manual_retry_new_attempt(state, previous_attempt=0) == 1
    assert "new_attempt" not in scheduler_module._manual_retry_payload(state)


def test_succeeded_same_stage_cycle_job_marker_does_not_pin() -> None:
    """Guard: the resolved-status arm outranks the same-stage arm.

    ``failed_stage`` names the marker job's own stage, but that job already succeeded — the
    stage name must not resurrect a stale counter.
    """

    state = {
        "pipeline_status": "failed",
        "retry_count": 0,
        "failed_stage": "download",
        "pipeline_jobs": [
            {**_CYCLE_SCOPE_JOB, "status": "succeeded", "retry_count": 5},
            dict(_OWN_MODEL_JOB),
        ],
        "pipeline_events": [
            _manual_retry_marker_event(
                entity_id="job_cycle_gfs_2026070100_download",
                retry_count=5,
                previous_job_id="job_cycle_gfs_2026070100_download",
            )
        ],
    }

    assert scheduler_module._manual_retry_requested(state) is True
    assert scheduler_module._manual_retry_new_attempt(state, previous_attempt=0) == 1
    assert "new_attempt" not in scheduler_module._manual_retry_payload(state)


def test_cohort_download_failure_shape_pins_marker_attempt() -> None:
    """Production projection: no explicit ``failed_stage`` and the cycle job is the only failure."""

    state = {
        "pipeline_status": "failed",
        "retry_count": 0,
        "stage": "forecast",
        "pipeline_jobs": [
            {**_CYCLE_SCOPE_JOB, "retry_count": 4},
            {**_OWN_MODEL_JOB, "status": "succeeded"},
        ],
        "pipeline_events": [
            _manual_retry_marker_event(
                entity_id="job_cycle_gfs_2026070100_download",
                retry_count=5,
                previous_job_id="job_cycle_gfs_2026070100_download",
            )
        ],
    }

    assert scheduler_module._manual_retry_requested(state) is True
    assert scheduler_module._manual_retry_new_attempt(state, previous_attempt=0) == 5
    assert scheduler_module._manual_retry_payload(state)["new_attempt"] == 5


def test_own_model_job_manual_marker_still_pins_candidate_new_attempt() -> None:
    candidate = types.SimpleNamespace(candidate_id="candidate_gfs", run_id="fcst_gfs_2026070100_model_a")
    state = {
        "pipeline_status": "failed",
        "retry_count": 0,
        "failed_stage": "forecast",
        "pipeline_jobs": [dict(_CYCLE_SCOPE_JOB), dict(_OWN_MODEL_JOB)],
        "pipeline_events": [_manual_retry_marker_event(entity_id="job_model_a_forecast", retry_count=5)],
    }

    evidence = _manual_retry_state_evidence(candidate, state, {})

    assert scheduler_module._manual_retry_requested(state) is True
    assert scheduler_module._manual_retry_new_attempt(state, previous_attempt=0) == 5
    assert scheduler_module._manual_retry_payload(state)["new_attempt"] == 5
    assert evidence["manual_retry"]["new_attempt"] == 5


def test_stale_own_marker_does_not_leak_attempt_when_newer_cycle_scope_marker_exists() -> None:
    """The newest retry_count-carrying adopted marker decides; a cycle-scope hit is terminal.

    Falling through to an older own-model marker would replay an attempt number the
    candidate already consumed (previous_attempt == 3 -> 3).
    """

    candidate = types.SimpleNamespace(candidate_id="candidate_gfs", run_id="fcst_gfs_2026070100_model_a")
    state = {
        "pipeline_status": "failed",
        "retry_count": 3,
        "failed_stage": "forecast",
        "pipeline_jobs": [dict(_CYCLE_SCOPE_JOB), dict(_OWN_MODEL_JOB)],
        "pipeline_events": [
            _manual_retry_marker_event(entity_id="job_model_a_forecast", retry_count=3, event_id=30),
            _manual_retry_marker_event(
                entity_id="job_cycle_gfs_2026070100_download", retry_count=5, event_id=31
            ),
        ],
    }

    evidence = _manual_retry_state_evidence(candidate, state, {})

    assert scheduler_module._manual_retry_requested(state) is True
    assert scheduler_module._manual_retry_new_attempt(state, previous_attempt=3) == 4
    assert evidence["manual_retry"]["new_attempt"] == 4


_NO_ATTEMPT_CLAIM_SHAPES = ["absent", "empty_string"]


def _no_attempt_claim_marker_event(
    *,
    entity_id: str,
    retry_count_shape: str,
    event_id: int = 31,
    created_at: str = "2026-07-01T02:00:00Z",
    **extra: Any,
) -> dict[str, Any]:
    """An adopted marker that makes NO operator attempt claim.

    The scan's terminal predicate is ``value in (None, "")``, so both empty shapes are built
    from one place: the field missing entirely (a marker written without it) and the empty
    string (what a writer persists for a blank operator field).  A mutant narrowing the
    terminal arm to ``is None`` must not survive either of them.
    """

    event = _manual_retry_marker_event(entity_id=entity_id, retry_count=0, event_id=event_id, **extra)
    event["created_at"] = created_at
    event["details"]["created_at"] = created_at
    if retry_count_shape == "absent":
        del event["details"]["retry_count"]
    else:
        event["details"]["retry_count"] = ""
    return event


def _older_own_model_pinning_marker() -> dict[str, Any]:
    """The candidate's own older marker, pinning the attempt number already consumed (3)."""

    return _manual_retry_marker_event(entity_id="job_model_a_forecast", retry_count=3, event_id=30)


def _consumed_attempt_marker_state(events: list[dict[str, Any]]) -> dict[str, Any]:
    """The #1289 shape: ``previous_attempt`` 3 is already spent, ``failed_stage`` resolves.

    ``failed_stage`` naming the canonical ``forecast`` keeps ``_fallback_previous_attempt``'s
    restarted-stage-family floor OUT of the picture, so the fallback here is exactly
    ``previous_attempt + 1`` and the assertions discriminate the scan's termination rule alone
    (the floor's own discrimination lives in ``test_production_scheduler.py``).
    """

    return {
        "pipeline_status": "failed",
        "retry_count": 3,
        "failed_stage": "forecast",
        "pipeline_jobs": [dict(_CYCLE_SCOPE_JOB), dict(_OWN_MODEL_JOB)],
        "pipeline_events": events,
    }


@pytest.mark.parametrize("retry_count_shape", _NO_ATTEMPT_CLAIM_SHAPES)
def test_newest_adopted_marker_without_retry_count_terminates_the_attempt_scan(retry_count_shape: str) -> None:
    """#1289: the newest adopted marker decides even when it carries no attempt claim.

    The older own-model marker's pin holds, so the walk-back handed back its ``retry_count`` 3 --
    exactly the attempt ``previous_attempt`` 3 records as already consumed, which the reservation
    boundary reads as a duplicate and silently skips instead of submitting.  The newest adopted
    marker makes no operator attempt claim, so the scan terminates there and the fallback (4)
    decides; the older marker is never consulted.
    """

    candidate = types.SimpleNamespace(candidate_id="candidate_gfs", run_id="fcst_gfs_2026070100_model_a")
    state = _consumed_attempt_marker_state(
        [
            _older_own_model_pinning_marker(),
            _no_attempt_claim_marker_event(
                entity_id="job_cycle_gfs_2026070100_download",
                retry_count_shape=retry_count_shape,
            ),
        ]
    )
    older, newest = state["pipeline_events"]

    # Premise: the older marker really would pin (its 3 is what the walk-back returned), and the
    # newer one really is adopted while carrying no attempt claim.
    assert scheduler_state_manual_retry_module._marker_event_pins_attempt(state, older) is True
    assert scheduler_state_manual_retry_module._event_is_adopted_manual_retry_marker(state, newest) is True
    assert newest["details"].get("retry_count") in (None, "")

    evidence = _manual_retry_state_evidence(candidate, state, {})
    payload = scheduler_module._manual_retry_payload(state)

    assert scheduler_module._manual_retry_new_attempt(state, previous_attempt=3) == 4
    # Both scanners terminate at the SAME marker: the payload reports the retry as requested
    # from the newest one and carries no ``new_attempt`` claim to explain a pinned value.
    assert payload["requested"] is True
    assert "new_attempt" not in payload
    assert evidence["manual_retry"]["new_attempt"] == 4


@pytest.mark.parametrize(
    "events",
    [
        pytest.param([], id="no_markers_at_all"),
        pytest.param(
            [
                _older_own_model_pinning_marker(),
                _no_attempt_claim_marker_event(
                    entity_id="job_cycle_gfs_2026070100_download",
                    retry_count_shape="absent",
                ),
            ],
            id="newest_marker_without_retry_count",
        ),
        pytest.param(
            [
                _older_own_model_pinning_marker(),
                _no_attempt_claim_marker_event(
                    entity_id="job_cycle_gfs_2026070100_download",
                    retry_count_shape="empty_string",
                ),
            ],
            id="newest_marker_with_empty_retry_count",
        ),
        pytest.param(
            [
                _older_own_model_pinning_marker(),
                _manual_retry_marker_event(
                    entity_id="job_cycle_gfs_2026070100_download",
                    retry_count=5,
                    event_id=31,
                ),
            ],
            id="newest_marker_whose_retry_count_does_not_pin",
        ),
    ],
)
def test_attempt_derivation_never_returns_a_consumed_attempt_without_a_pinning_claim(
    events: list[dict[str, Any]],
) -> None:
    """#1289 invariant: absent an operator pin claim, the derivation always moves forward.

    Domain: states carrying no top-level ``manual_retry`` attempt payload (that route
    short-circuits ahead of the event scan with no pin or adoption check and is out of this
    rule's scope).  The documented exemption is a newest adopted marker whose ``retry_count``
    pins -- an explicit operator claim may legitimately be at or below ``previous_attempt``,
    which the negative anchors below pin unchanged.
    """

    state = _consumed_attempt_marker_state(events)

    # Premise: the state-level short-circuit really is absent, so the event scan decides.
    assert "manual_retry" not in state
    assert "manual_retry_marker" not in state

    assert scheduler_module._manual_retry_new_attempt(state, previous_attempt=3) > 3


def test_newest_adopted_marker_with_pinning_retry_count_still_decides_the_attempt() -> None:
    """Negative anchor: the terminal arm must not swallow a real operator claim.

    Same two-marker sequence as the discriminating pair, except the newest marker carries a
    ``retry_count`` of its own (7, on the candidate's own model-scoped row, so the pin holds).
    That value is the operator's explicit claim and still decides -- terminality only changes
    what happens when the newest marker claims nothing.
    """

    state = _consumed_attempt_marker_state(
        [
            _older_own_model_pinning_marker(),
            _manual_retry_marker_event(entity_id="job_model_a_forecast", retry_count=7, event_id=31),
        ]
    )

    assert scheduler_module._manual_retry_new_attempt(state, previous_attempt=3) == 7
    assert scheduler_module._manual_retry_payload(state)["new_attempt"] == 7


def test_attempt_scan_falls_back_when_no_marker_is_adopted() -> None:
    """Negative anchor: with nothing adopted, the fallback semantics are unchanged.

    The only marker-shaped event names the whole forecast cycle and no model, so no candidate
    adopts it and the scan reaches its existing end-of-loop fallback.
    """

    state = _consumed_attempt_marker_state(
        [
            _manual_retry_marker_event(
                entity_id="gfs_2026070100",
                retry_count=5,
                entity_type="forecast_cycle",
                event_id=31,
            )
        ]
    )

    assert scheduler_module._manual_retry_requested(state) is False
    assert scheduler_module._manual_retry_new_attempt(state, previous_attempt=3) == 4
    assert "new_attempt" not in scheduler_module._manual_retry_payload(state)


def test_pin_refusal_on_the_newest_marker_still_returns_the_fallback() -> None:
    """Negative anchor: the pin-refusal arm keeps its own terminal fallback.

    The newest adopted marker carries ``retry_count`` 5 but targets the cycle download stage
    while this decision repairs ``forecast`` beside a live own failure, so neither pin arm
    holds.  That arm was already terminal before #1289 and must stay that way.
    """

    state = _consumed_attempt_marker_state(
        [
            _older_own_model_pinning_marker(),
            _manual_retry_marker_event(
                entity_id="job_cycle_gfs_2026070100_download",
                retry_count=5,
                event_id=31,
            ),
        ]
    )
    newest = state["pipeline_events"][1]

    # Premise: the newest marker really is adopted and its pin really is refused.
    assert scheduler_state_manual_retry_module._event_is_adopted_manual_retry_marker(state, newest) is True
    assert scheduler_state_manual_retry_module._marker_event_pins_attempt(state, newest) is False

    assert scheduler_module._manual_retry_new_attempt(state, previous_attempt=3) == 4
    assert "new_attempt" not in scheduler_module._manual_retry_payload(state)


@pytest.mark.parametrize("retry_count_shape", _NO_ATTEMPT_CLAIM_SHAPES)
def test_newer_unadopted_marker_shaped_event_does_not_terminate_the_attempt_scan(
    retry_count_shape: str,
) -> None:
    """Negative anchor: terminality is scoped to ADOPTED markers.

    The newer marker-shaped event is a cycle-granularity marker attributed to another model, so
    this candidate never adopts it (PR #1286's cross-model exclusion) and it carries no attempt
    claim either.  Widening the terminal arm past the adoption guard would hand this candidate
    the fallback (1) instead of the 5 its own adopted marker pins.
    """

    state = {
        "pipeline_status": "failed",
        "retry_count": 0,
        "failed_stage": "forecast",
        "pipeline_jobs": [dict(_CYCLE_SCOPE_JOB), dict(_OWN_MODEL_JOB)],
        "pipeline_events": [
            _manual_retry_marker_event(entity_id="job_model_a_forecast", retry_count=5, event_id=30),
            _no_attempt_claim_marker_event(
                entity_id="gfs_2026070100",
                retry_count_shape=retry_count_shape,
                entity_type="forecast_cycle",
                details_model_id="model_b",
                event_id=31,
            ),
        ],
    }
    own_marker, foreign_marker = state["pipeline_events"]

    # Premise: the foreign event really is marker-shaped, really is newer, and really is not
    # adopted -- so only the adoption guard keeps it out of the scan's termination.
    assert scheduler_state_manual_retry_module._manual_retry_marker_shape(foreign_marker) is True
    assert scheduler_state_manual_retry_module._event_is_adopted_manual_retry_marker(state, foreign_marker) is False
    assert scheduler_state_manual_retry_module._event_is_adopted_manual_retry_marker(state, own_marker) is True

    assert scheduler_module._manual_retry_new_attempt(state, previous_attempt=0) == 5
    assert scheduler_module._manual_retry_payload(state)["new_attempt"] == 5


def test_model_less_candidate_run_job_marker_still_pins_attempt_in_cohort_shape() -> None:
    """Multi-basin cohorts persist model-less job rows; only cycle run-id grammar is cycle scope.

    Both pin-rule arms are closed here on purpose — ``failed_stage`` names another stage and a
    live model-domain failure exists — so ONLY the run-id conjunct of the cycle-scope test can
    keep the operator-pinned attempt number.  Drop that conjunct and this state derives 1.
    """

    state = {
        "pipeline_status": "failed",
        "retry_count": 0,
        "failed_stage": "download",
        "pipeline_jobs": [
            {
                "job_id": "job_model_a_forecast",
                "run_id": "fcst_gfs_2026070100_model_a",
                "model_id": None,
                "stage": "forecast",
                "status": "failed",
                "retry_count": 0,
                "updated_at": "2026-07-01T00:00:00Z",
            },
            {
                "job_id": "job_model_a_download",
                "run_id": "fcst_gfs_2026070100_model_a",
                "model_id": "model_a",
                "stage": "download",
                "status": "failed",
                "retry_count": 0,
                "updated_at": "2026-07-01T00:00:00Z",
            },
        ],
        "pipeline_events": [_manual_retry_marker_event(entity_id="job_model_a_forecast", retry_count=5)],
    }

    assert scheduler_module._manual_retry_new_attempt(state, previous_attempt=0) == 5
    assert scheduler_module._manual_retry_payload(state)["new_attempt"] == 5


def test_cycle_granularity_manual_marker_naming_foreign_model_is_not_adopted() -> None:
    state = {
        "pipeline_status": "failed",
        "retry_count": 0,
        "failed_stage": "forecast",
        "pipeline_jobs": [dict(_OWN_MODEL_JOB)],
        "pipeline_events": [
            _manual_retry_marker_event(
                entity_id="gfs_2026070100",
                retry_count=5,
                entity_type="forecast_cycle",
                details_model_id="model_b",
            )
        ],
    }

    payload = scheduler_module._manual_retry_payload(state)

    assert scheduler_module._manual_retry_requested(state) is False
    assert "marker" not in payload
    assert "requested" not in payload
    assert "new_attempt" not in payload


def test_cycle_granularity_manual_marker_is_not_adopted_when_derived_model_set_is_empty() -> None:
    state = {
        "pipeline_status": "failed",
        "retry_count": 0,
        "failed_stage": "download",
        "pipeline_jobs": [dict(_CYCLE_SCOPE_JOB)],
        "pipeline_events": [
            _manual_retry_marker_event(
                entity_id="gfs_2026070100",
                retry_count=5,
                entity_type="forecast_cycle",
                details_model_id="model_a",
            )
        ],
    }

    payload = scheduler_module._manual_retry_payload(state)

    assert scheduler_module._manual_retry_requested(state) is False
    assert "marker" not in payload
    assert "requested" not in payload


def test_foreign_marker_shaped_event_does_not_block_own_manual_retry() -> None:
    state = {
        "pipeline_status": "failed",
        "retry_count": 0,
        "failed_stage": "forecast",
        "pipeline_jobs": [dict(_OWN_MODEL_JOB)],
        "pipeline_events": [
            _manual_retry_marker_event(
                entity_id="gfs_2026070100",
                retry_count=5,
                entity_type="forecast_cycle",
                event_id=20,
                status_to="pending",
            ),
            _manual_retry_marker_event(entity_id="job_model_a_forecast", retry_count=1, event_id=21),
        ],
    }

    assert scheduler_module._manual_retry_requested(state) is True


def test_terminal_job_row_shadows_stale_active_pipeline_event_for_retry_blocker() -> None:
    state = {
        "pipeline_jobs": [
            {
                "job_id": "job_model_a_forecast",
                "status": "failed",
                "retry_count": 0,
                "updated_at": "2026-07-01T00:00:00Z",
            }
        ],
        "pipeline_events": [
            {
                "event_id": 7,
                "entity_type": "pipeline_job",
                "entity_id": "job_model_a_forecast",
                "event_type": "submission",
                "status_to": "pending",
                "created_at": "2026-07-01T00:01:00Z",
                "details": {"stage": "forecast", "created_at": "2026-07-01T00:01:00Z"},
            }
        ],
    }

    blocker = scheduler_module._latest_manual_retry_blocker(state)

    assert blocker is not None
    assert blocker["source"] == "pipeline_job"
    assert blocker["status"] == "failed"
    assert blocker["active"] is False


def test_succeeded_job_row_clears_stale_active_pipeline_event_for_retry_blocker() -> None:
    state = {
        "pipeline_jobs": [
            {
                "job_id": "job_model_a_forecast",
                "status": "succeeded",
                "retry_count": 0,
                "updated_at": "2026-07-01T00:00:00Z",
            }
        ],
        "pipeline_events": [
            {
                "event_id": 7,
                "entity_type": "pipeline_job",
                "entity_id": "job_model_a_forecast",
                "event_type": "submission",
                "status_to": "pending",
                "created_at": "2026-07-01T00:01:00Z",
                "details": {"stage": "forecast", "created_at": "2026-07-01T00:01:00Z"},
            }
        ],
    }

    assert scheduler_module._latest_manual_retry_blocker(state) is None


def test_cold_start_manual_repair_restarts_from_forecast() -> None:
    candidate = types.SimpleNamespace(candidate_id="candidate_gfs", run_id="fcst_gfs_2026062700_model_a")
    state = {
        "pipeline_jobs": [
            {
                "job_id": "job_model_a_state_save_qc",
                "status": "failed",
                "stage": "state_save_qc",
                "error_code": "COLD_START_QUARANTINED",
                "retry_count": 0,
            }
        ],
        "pipeline_events": [
            {
                "event_type": "retry",
                "details": {
                    "trigger": "manual",
                    "manual_retry_marker": True,
                    "previous_error": "COLD_START_QUARANTINED",
                    "previous_job_id": "job_model_a_state_save_qc",
                    "retry_count": 1,
                },
            }
        ],
    }

    evidence = _manual_retry_state_evidence(candidate, state, {})

    assert evidence["restart_stage"] == "forecast"
    assert evidence["restart_from_stage"] == "forecast"
    assert evidence["native_shud_resubmitted"] is True
    assert evidence["force_native_shud_rerun"] is True


def test_cold_start_retry_failure_restarts_from_forecast() -> None:
    candidate = types.SimpleNamespace(candidate_id="candidate_gfs", run_id="fcst_gfs_2026062700_model_a")
    state = {
        "pipeline_jobs": [
            {
                "job_id": "job_model_a_state_save_qc",
                "status": "failed",
                "stage": "state_save_qc",
                "error_code": "COLD_START_QUARANTINED",
                "retry_count": 0,
            }
        ]
    }

    evidence = _retry_failure_evidence(candidate, state, {})

    assert evidence["restart_stage"] == "forecast"
    assert evidence["restart_from_stage"] == "forecast"


def test_cold_start_failure_does_not_reuse_durable_downstream_output() -> None:
    candidate = types.SimpleNamespace(candidate_id="candidate_gfs", run_id="fcst_gfs_2026062700_model_a")
    state = {
        "output_uri": "s3://nhms/runs/fcst_gfs_2026062700_model_a/output/",
        "pipeline_jobs": [
            {
                "job_id": "job_model_a_parse",
                "status": "failed",
                "stage": "parse",
                "error_code": "COLD_START_QUARANTINED",
                "retry_count": 0,
            }
        ],
    }

    assert _downstream_retry_evidence(candidate, state, {}) is None


def test_historical_event_ids_do_not_collide_across_models(tmp_path: Path) -> None:
    cycle_time = _dt("2026-06-28T00:00:00Z")
    rows = _historical_rows(cycle_time)
    rows["pipeline_events"] = [
        {**rows["pipeline_events"][0], "event_id": 3},
        {
            **rows["pipeline_events"][0],
            "event_id": 8,
            "entity_id": "job_model_b_completed",
            "status_from": "running",
            "status_to": "succeeded",
            "details": {"model_id": "model_b"},
        },
    ]
    journal_root = tmp_path / "journal"

    import_historical_scheduler_state(journal_root=journal_root, cutoff_time=cycle_time, **rows)
    repository = FileOrchestrationJournalRepository(journal_root)
    inserted = repository.insert_pipeline_event(
        entity_type="pipeline_job",
        entity_id="job_model_a_permanent",
        event_type="operator_note",
        status_from="permanently_failed",
        status_to="manual_repair_requested",
        details={"note": "post-migration event"},
    )
    cycle_rows = repository._cycle_rows(source_id="gfs", cycle_time=cycle_time, model_id=None)

    assert inserted["event_id"] > 8
    assert {event["event_id"] for event in cycle_rows.pipeline_events} >= {3, 8, inserted["event_id"]}


def test_historical_pipeline_event_runtime_roots_are_redacted_but_retry_recoverable(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cycle_time = _dt("2026-06-28T00:00:00Z")
    workspace_root = tmp_path / "historical-workspace"
    object_store_root = tmp_path / "historical-object-store"
    object_store_prefix = "s3://nhms-historical"
    for name in (
        "WORKSPACE_ROOT",
        "OBJECT_STORE_ROOT",
        "OBJECT_STORE_PREFIX",
        "NHMS_PUBLISHED_ARTIFACT_ROOT",
        "NHMS_PUBLISHED_ARTIFACT_URI_PREFIX",
    ):
        monkeypatch.delenv(name, raising=False)
    rows = _historical_rows(cycle_time)
    rows["pipeline_events"].append(
        {
            "event_id": 99,
            "entity_type": "pipeline_job",
            "entity_id": "job_download_failed",
            "event_type": "submission",
            "status_from": "reserved",
            "status_to": "submitted",
            "details": {
                "runtime_root_contract": {
                    "workspace_dir": str(workspace_root),
                    "object_store_root": str(object_store_root),
                    "object_store_prefix": object_store_prefix,
                }
            },
            "created_at": "2026-06-28T00:01:00Z",
        }
    )
    journal_root = tmp_path / "journal"

    import_historical_scheduler_state(journal_root=journal_root, cutoff_time=cycle_time, **rows)
    repository = FileOrchestrationJournalRepository(journal_root)
    raw_journal = (journal_root / "journal/gfs/2026062800.jsonl").read_text(encoding="utf-8")
    state = _candidate_state(repository, cycle_time=cycle_time, model_id="model_a")

    class Gateway:
        def __init__(self) -> None:
            self.requests: list[Any] = []

        def submit_job(self, request: Any) -> dict[str, Any]:
            self.requests.append(request)
            return {"job_id": "7010", "status": "submitted"}

    gateway = Gateway()
    service = FileJournalRetryService(repository, RetryConfig(max_retries=3, backoff_schedule=[0]))
    retried = service.attempt_manual_retry("cycle_gfs_2026062800", gateway, trusted_internal=True)

    assert str(workspace_root) not in raw_journal
    assert str(object_store_root) not in raw_journal
    assert object_store_prefix not in raw_journal
    assert "[local-path]" in raw_journal
    assert "[object-uri]" in raw_journal
    assert str(workspace_root) not in json.dumps(state, sort_keys=True)
    assert retried.status == "submitted"
    assert gateway.requests[0].manifest["workspace_dir"] == str(workspace_root)
    assert gateway.requests[0].manifest["object_store_root"] == str(object_store_root)
    assert gateway.requests[0].manifest["object_store_prefix"] == object_store_prefix


def test_write_migration_receipt_rejects_outside_root_and_symlink_target(tmp_path: Path) -> None:
    receipt = {"schema_version": MIGRATION_RECEIPT_SCHEMA_VERSION}
    journal_root = tmp_path / "journal"
    outside = tmp_path / "outside.json"

    with pytest.raises(ValueError, match="containment root"):
        write_migration_receipt(receipt, outside, containment_root=journal_root)

    journal_root.mkdir(exist_ok=True)
    symlink_path = journal_root / "receipt.json"
    symlink_path.symlink_to(outside)

    with pytest.raises(ValueError, match="symlink"):
        write_migration_receipt(receipt, "receipt.json", containment_root=journal_root)


def test_historical_scheduler_state_import_rejects_over_limit_iterable_before_writes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        migration_module,
        "HISTORICAL_MIGRATION_ROW_LIMITS",
        {
            "forecast_cycles": 2,
            "hydro_runs": 2,
            "pipeline_jobs": 2,
            "pipeline_events": 2,
        },
    )

    def oversized_cycles() -> Any:
        for index in range(3):
            yield {
                "cycle_id": f"gfs_202606280{index}",
                "source_id": "gfs",
                "cycle_time": _dt("2026-06-28T00:00:00Z"),
                "status": "forecast_running",
                "manifest_uri": f"s3://private-bucket/raw/{index}.json?token=secret-{index}",
            }

    journal_root = tmp_path / "journal"

    with pytest.raises(ValueError) as error:
        import_historical_scheduler_state(
            journal_root=journal_root,
            forecast_cycles=oversized_cycles(),
            cutoff_time=_dt("2026-06-28T00:10:00Z"),
        )

    message = str(error.value)
    assert "forecast_cycles" in message
    assert "2" in message
    assert "secret-2" not in message
    assert not journal_root.exists()


def test_export_scheduler_state_from_postgres_uses_node22_guard_and_stable_order(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured_sql: list[str] = []
    cycle_time = _dt("2026-06-28T00:00:00Z")
    exported_rows = [
        [
            {
                "cycle_id": cycle_id_for("gfs", cycle_time),
                "source_id": "gfs",
                "cycle_time": cycle_time,
                "status": "forecast_running",
                "created_at": "2026-06-28T00:00:00Z",
            }
        ],
        [
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
                "created_at": "2026-06-28T00:00:00Z",
                "updated_at": "2026-06-28T00:05:00Z",
            }
        ],
        [
            _job(
                job_id="job_export_model_a_failed",
                run_id="fcst_gfs_2026062800_model_a",
                cycle_time=cycle_time,
                model_id="model_a",
                status="permanently_failed",
                retry_count=3,
                error_code="INVALID_MANIFEST",
            ),
            _job(
                job_id="job_export_download_failed",
                run_id="cycle_gfs_2026062800",
                cycle_time=cycle_time,
                model_id=None,
                status="failed",
                job_type="download_source_cycle",
                retry_count=3,
                error_code="SOURCE_CYCLE_UNAVAILABLE",
            ),
        ],
        [
            {
                "event_id": 301,
                "entity_type": "pipeline_job",
                "entity_id": "job_export_model_a_failed",
                "event_type": "retry",
                "status_from": "permanently_failed",
                "status_to": "manual_repair_requested",
                "details": {
                    "trigger": "manual",
                    "manual_retry_marker": True,
                    "previous_job_id": "job_export_model_a_failed",
                    "previous_error": "INVALID_MANIFEST",
                    "prior_failure_reason": "INVALID_MANIFEST",
                },
                "created_at": "2026-06-28T00:06:00Z",
            },
            {
                "event_id": 302,
                "entity_type": "pipeline_job",
                "entity_id": "job_export_download_failed",
                "event_type": "retry",
                "status_from": "failed",
                "status_to": "manual_repair_requested",
                "details": {
                    "trigger": "manual",
                    "manual_retry_marker": True,
                    "previous_job_id": "job_export_download_failed",
                    "previous_error": "SOURCE_CYCLE_UNAVAILABLE",
                    "prior_failure_reason": "SOURCE_CYCLE_UNAVAILABLE",
                },
                "created_at": "2026-06-28T00:07:00Z",
            },
        ],
    ]

    class Cursor:
        def __enter__(self) -> "Cursor":
            return self

        def __exit__(self, *_args: object) -> None:
            return None

        def execute(self, sql: str, _params: dict[str, Any]) -> None:
            captured_sql.append(sql)

        def fetchall(self) -> list[dict[str, Any]]:
            return [dict(row) for row in exported_rows[(len(captured_sql) - 1) % 4]]

    class Connection:
        def __enter__(self) -> "Connection":
            return self

        def __exit__(self, *_args: object) -> None:
            return None

        def cursor(self) -> Cursor:
            return Cursor()

    fake_psycopg = types.SimpleNamespace(connect=lambda *_args, **_kwargs: Connection())
    fake_rows = types.SimpleNamespace(dict_row=object())
    monkeypatch.setitem(sys.modules, "psycopg", fake_psycopg)
    monkeypatch.setitem(sys.modules, "psycopg.rows", fake_rows)

    with pytest.raises(ValueError, match="node-22"):
        export_scheduler_state_from_postgres(
            database_url="postgresql://nwm@db.internal:55433/nhms",
            journal_root=tmp_path / "journal",
            allow_historical_node22=True,
        )

    receipt = export_scheduler_state_from_postgres(
        database_url="postgresql://nwm@localhost:55433/nhms",
        journal_root=tmp_path / "journal",
        allow_historical_node22=True,
        cutoff_time=_dt("2026-06-28T00:10:00Z"),
    )

    assert receipt["source"] == "localhost:55433"
    repository = FileOrchestrationJournalRepository(tmp_path / "journal")
    state = _candidate_state(repository, cycle_time=cycle_time, model_id="model_a")

    assert receipt["row_counts"] == {
        "forecast_cycles": 1,
        "hydro_runs": 1,
        "pipeline_jobs": 2,
        "pipeline_events": 2,
    }
    assert receipt["imported_row_counts"] == receipt["row_counts"]
    assert receipt["skipped_rows"]["count"] == 0
    assert all(value.startswith("sha256:") for value in receipt["checksums"].values())
    assert receipt["replay_status"]["status"] == "ok"
    assert receipt["stale_download_source_cycle_supersession"]["count"] == 1
    assert state["pipeline_status"] == "permanently_failed"
    assert [event["event_id"] for event in state["pipeline_events"]] == [301, 302]
    cycle_rows = repository._cycle_rows(source_id="gfs", cycle_time=cycle_time, model_id=None)
    assert [event["event_id"] for event in cycle_rows.pipeline_events] == [301, 302]

    documented_receipt = export_scheduler_state_from_postgres(
        database_url="postgresql://nwm@10.0.2.100:55433/nhms",
        journal_root=tmp_path / "journal-documented-host",
        allow_historical_node22=True,
        cutoff_time=_dt("2026-06-28T00:10:00Z"),
    )

    assert documented_receipt["source"] == "10.0.2.100:55433"
    assert len(captured_sql) == 8
    assert "ORDER BY created_at ASC NULLS FIRST, event_id ASC" in captured_sql[-1]


def test_export_scheduler_state_from_postgres_rejects_over_limit_fetchmany_rows(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        migration_module,
        "HISTORICAL_MIGRATION_ROW_LIMITS",
        {
            "forecast_cycles": 2,
            "hydro_runs": 2,
            "pipeline_jobs": 2,
            "pipeline_events": 2,
        },
    )
    cycle_time = _dt("2026-06-28T00:00:00Z")
    exported_rows = [
        {
            "cycle_id": cycle_id_for("gfs", cycle_time),
            "source_id": "gfs",
            "cycle_time": cycle_time,
            "status": "forecast_running",
            "manifest_uri": f"s3://private-bucket/raw/{index}.json?token=secret-{index}",
            "created_at": "2026-06-28T00:00:00Z",
        }
        for index in range(3)
    ]

    class Cursor:
        def __init__(self) -> None:
            self.offset = 0

        def __enter__(self) -> "Cursor":
            return self

        def __exit__(self, *_args: object) -> None:
            return None

        def execute(self, _sql: str, _params: dict[str, Any]) -> None:
            return None

        def fetchmany(self, size: int) -> list[dict[str, Any]]:
            batch = exported_rows[self.offset : self.offset + size]
            self.offset += len(batch)
            return [dict(row) for row in batch]

        def fetchall(self) -> list[dict[str, Any]]:
            raise AssertionError("export must not use unbounded fetchall when fetchmany is available")

    class Connection:
        def __enter__(self) -> "Connection":
            return self

        def __exit__(self, *_args: object) -> None:
            return None

        def cursor(self) -> Cursor:
            return Cursor()

    fake_psycopg = types.SimpleNamespace(connect=lambda *_args, **_kwargs: Connection())
    fake_rows = types.SimpleNamespace(dict_row=object())
    monkeypatch.setitem(sys.modules, "psycopg", fake_psycopg)
    monkeypatch.setitem(sys.modules, "psycopg.rows", fake_rows)

    with pytest.raises(ValueError) as error:
        export_scheduler_state_from_postgres(
            database_url="postgresql://nwm@localhost:55433/nhms",
            journal_root=tmp_path / "journal",
            allow_historical_node22=True,
            cutoff_time=_dt("2026-06-28T00:10:00Z"),
        )

    message = str(error.value)
    assert "forecast_cycles" in message
    assert "2" in message
    assert "secret-2" not in message


@pytest.mark.parametrize(
    "database_url",
    [
        "postgresql://nwm@localhost:55433/nhms?host=210.77.77.27",
        "postgresql://nwm@localhost:55433/nhms?hostaddr=210.77.77.27",
        "postgresql://nwm@localhost:55433/nhms?port=55432",
        "postgresql://nwm@localhost:55433/nhms?service=node27-primary",
    ],
)
def test_export_scheduler_state_from_postgres_rejects_libpq_query_overrides(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    database_url: str,
) -> None:
    def fail_connect(*_args: Any, **_kwargs: Any) -> None:
        raise AssertionError("unsafe historical DB URL must be rejected before psycopg.connect")

    fake_psycopg = types.SimpleNamespace(connect=fail_connect)
    fake_rows = types.SimpleNamespace(dict_row=object())
    monkeypatch.setitem(sys.modules, "psycopg", fake_psycopg)
    monkeypatch.setitem(sys.modules, "psycopg.rows", fake_rows)

    with pytest.raises(ValueError, match="query parameters"):
        export_scheduler_state_from_postgres(
            database_url=database_url,
            journal_root=tmp_path / "journal",
            allow_historical_node22=True,
        )


def test_migrate_scheduler_state_cli_writes_receipt_under_journal_root(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    receipt = {"schema_version": MIGRATION_RECEIPT_SCHEMA_VERSION, "row_counts": {}}

    def fake_export(**_kwargs: Any) -> dict[str, Any]:
        return dict(receipt)

    monkeypatch.setattr(cli_module, "export_scheduler_state_from_postgres", fake_export)
    journal_root = tmp_path / "journal"

    code = cli_module._argparse_main(
        [
            "migrate-scheduler-state",
            "--database-url",
            "postgresql://nwm@localhost:55433/nhms",
            "--journal-root",
            str(journal_root),
            "--receipt-path",
            "receipts/migration.json",
            "--allow-historical-node22",
        ]
    )

    assert code == 0
    assert json.loads((journal_root / "receipts/migration.json").read_text(encoding="utf-8")) == receipt


def test_migrate_scheduler_state_click_rejects_outside_receipt_path(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pytest.importorskip("click")

    def fake_export(**_kwargs: Any) -> dict[str, Any]:
        return {"schema_version": MIGRATION_RECEIPT_SCHEMA_VERSION}

    monkeypatch.setattr(cli_module, "export_scheduler_state_from_postgres", fake_export)

    with pytest.raises(SystemExit) as error:
        cli_module._click_main(
            [
                "migrate-scheduler-state",
                "--database-url",
                "postgresql://nwm@localhost:55433/nhms",
                "--journal-root",
                str(tmp_path / "journal"),
                "--receipt-path",
                str(tmp_path / "outside.json"),
                "--allow-historical-node22",
            ]
        )

    assert error.value.code == 2
