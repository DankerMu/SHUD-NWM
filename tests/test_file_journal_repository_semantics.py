"""Repository-method semantics pinned on the file-journal path (#3259, R3-1).

Each test here is the file-journal twin of a SQL-repository test that was in
``tests/test_orchestration_chain.py`` (named in its docstring; R3, #3260, deleted the
SQL repository and those tests): the SAME
rows are written through the journal's write API
(``append_historical_pipeline_job`` / ``append_historical_pipeline_event`` /
``append_historical_hydro_run`` / ``append_historical_forecast_cycle``) and the
SAME outputs are asserted on the SAME ``FileOrchestrationJournalRepository``
method.  The SQL twins are deleted with the SQL repository in R3-2; these keep
the semantics pinned.

Write-boundary normalisation, applied identically to every seed:

* the journal requires ``job_type``; where the SQL row omitted it, the stage's
  canonical job type is supplied (``stage`` wins in every stage read);
* the journal stamps a missing ``created_at``/``updated_at`` with the clock.
  The clock is pinned to ``_UNDATED`` (older than every dated seed row), so an
  undated row keeps the SQL ordering rank it had at ``datetime.min``: oldest,
  tied with the other undated rows.
"""

from __future__ import annotations

import json
from collections.abc import Iterable, Iterator, Mapping, Sequence
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from services.orchestrator import file_orchestration_journal as journal_module
from services.orchestrator.file_orchestration_journal import FileOrchestrationJournalRepository

_UNDATED = datetime(2000, 1, 1, tzinfo=UTC)
_CYCLE_TIME = datetime(2026, 5, 1, tzinfo=UTC)

_ENV_VARS = (
    "NHMS_ORCHESTRATOR_TERMINAL_STAGE",
    "OBJECT_STORE_ROOT",
    "NHMS_OBJECT_STORE_COPYBACK_ROOT",
    "NHMS_SCHEDULER_REQUIRE_NFS_RAW_MANIFEST",
    "NHMS_SCHEDULER_NFS_RAW_MANIFEST_ROOT",
    "NHMS_SCHEDULER_NFS_RAW_MANIFEST_PREFIX",
)


@pytest.fixture(autouse=True)
def _isolated_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in _ENV_VARS:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(journal_module, "_utcnow", lambda: _UNDATED)


def _journal(
    tmp_path: Path,
    *,
    jobs: Iterable[Mapping[str, Any]] = (),
    events: Iterable[Mapping[str, Any]] = (),
    hydro_runs: Iterable[Mapping[str, Any]] = (),
    forecast_cycle: Mapping[str, Any] | None = None,
) -> FileOrchestrationJournalRepository:
    """Write every seed row through the journal's write API; every write must land."""

    root = tmp_path / "journal"
    repository = FileOrchestrationJournalRepository(root)
    if forecast_cycle is not None:
        assert repository.append_historical_forecast_cycle(dict(forecast_cycle)) is not None
    for hydro_run in hydro_runs:
        assert repository.append_historical_hydro_run(dict(hydro_run)) is not None
    job_ids = []
    for job in jobs:
        assert repository.append_historical_pipeline_job(dict(job)) is not None
        job_ids.append(str(job["job_id"]))
    event_count = 0
    for event in events:
        assert repository.append_historical_pipeline_event(dict(event)) is not None, event
        event_count += 1
    # Premise: a fresh reader sees every seeded row (no orphan event dropped, no job lost).
    reader = FileOrchestrationJournalRepository(root)
    for job_id in job_ids:
        assert reader.get_pipeline_job(job_id) is not None, job_id
    return reader


def _state(
    repository: FileOrchestrationJournalRepository,
    *,
    model_id: str,
    job_limit: int = 10,
    event_limit: int = 10,
    retry_limit: int | None = 3,
    forcing_version_id: str | None = None,
) -> dict[str, Any]:
    state = repository.candidate_state(
        source_id="gfs",
        cycle_time=_CYCLE_TIME,
        model_id=model_id,
        run_id=f"fcst_gfs_2026050100_{model_id}",
        forcing_version_id=forcing_version_id or f"forc_gfs_2026050100_{model_id}",
        candidate_id=f"gfs:2026-05-01T00:00:00Z:{model_id}:forecast_gfs_deterministic",
        retry_limit=retry_limit,
        job_limit=job_limit,
        event_limit=event_limit,
    )
    assert state is not None
    return state


def _cycle_job(**fields: Any) -> dict[str, Any]:
    """A model-less row of the bare ``cycle_gfs_2026050100`` run (the SQL fixtures' shape)."""

    payload: dict[str, Any] = {
        "run_id": "cycle_gfs_2026050100",
        "cycle_id": "gfs_2026050100",
        "model_id": None,
    }
    payload.update(fields)
    return payload


# --- task-results event family ---------------------------------------------------------


def test_file_journal_candidate_state_limits_jobs_and_reads_events_for_candidate_scope(tmp_path: Path) -> None:
    """Twin of ``test_psycopg_candidate_state_limits_jobs_and_reads_events_for_candidate_scope``."""

    repository = _journal(
        tmp_path,
        jobs=[
            _cycle_job(
                job_id="job_cycle_gfs_2026050100_forcing",
                job_type="produce_forcing_array",
                status="partially_failed",
                stage="forcing",
                error_code="NODE_FAILURE",
                retry_count=1,
            ),
            _cycle_job(
                job_id="job_cycle_gfs_2026050100_publish",
                job_type="publish_tiles",
                status="succeeded",
                stage="publish",
                retry_count=0,
            ),
            _cycle_job(job_id="overflow", job_type="publish_tiles", status="succeeded", stage="publish"),
        ],
        events=[
            {
                "event_id": 1,
                "entity_type": "pipeline_job",
                "entity_id": "job_cycle_gfs_2026050100_forcing",
                "event_type": "status_change",
                "status_from": "running",
                "status_to": "partially_failed",
                "created_at": "2026-05-01T00:02:00Z",
                "details": {
                    "stage": "forcing",
                    "task_results": [
                        {"task_id": 0, "model_id": "model_a", "status": "succeeded"},
                        {"task_id": 1, "model_id": "model_b", "status": "failed", "error_code": "NODE_FAILURE"},
                    ],
                },
            },
            {
                "event_id": 2,
                "entity_type": "pipeline_job",
                "entity_id": "job_cycle_gfs_2026050100_forcing",
                "event_type": "retry",
                "created_at": "2026-05-01T00:01:00Z",
                "details": {"trigger": "manual", "manual_retry_marker": True, "retry_count": 2},
            },
        ],
        hydro_runs=[
            {
                "run_id": "fcst_gfs_2026050100_model_b",
                "source_id": "gfs",
                "cycle_time": "2026-05-01T00:00:00Z",
                "model_id": "model_b",
                "status": "failed",
                "output_uri": "s3://nhms/runs/fcst_gfs_2026050100_model_b/output/",
            }
        ],
    )

    state = _state(repository, model_id="model_b", job_limit=2, event_limit=1)

    assert state["pipeline_status"] == "partially_failed"
    assert state["failed_stage"] == "forcing"
    assert state["array_task_id"] == 1
    assert state["successful_sibling_outputs_reused"] is True
    assert state["pipeline_jobs_total"] == 3
    assert state["state_truncated"] is True


# --- source-cycle download repair family ------------------------------------------------


def _source_cycle_journal_state(
    tmp_path: Path,
    *,
    jobs: Iterable[Mapping[str, Any]],
    events: Iterable[Mapping[str, Any]],
    manifest_uri: str | None = "raw/gfs/2026050100/manifest.json",
    forecast_status: str = "raw_complete",
    job_limit: int = 10,
    event_limit: int = 10,
) -> dict[str, Any]:
    """The journal seed of ``_source_cycle_retry_state`` (``tests/test_orchestration_chain.py``)."""

    repository = _journal(
        tmp_path,
        jobs=jobs,
        events=events,
        forecast_cycle={
            "cycle_id": "gfs_2026050100",
            "source_id": "gfs",
            "cycle_time": "2026-05-01T00:00:00Z",
            "status": forecast_status,
            "manifest_uri": manifest_uri,
        },
    )
    return _state(repository, model_id="model_b", job_limit=job_limit, event_limit=event_limit)


def _failed_source_cycle_download_job(**overrides: Any) -> dict[str, Any]:
    payload = _cycle_job(
        job_id="job_cycle_gfs_2026050100_download",
        job_type="download_source_cycle",
        slurm_job_id="6101",
        status="permanently_failed",
        stage="download",
        retry_count=1,
        error_code="SLURM_JOB_FAILED",
        error_message="download source cycle failed",
        submitted_at="2026-05-01T00:00:00Z",
        finished_at="2026-05-01T00:10:00Z",
        updated_at="2026-05-01T01:00:00Z",
    )
    payload.update(overrides)
    return payload


def _successful_source_cycle_retry_job(**overrides: Any) -> dict[str, Any]:
    payload = _cycle_job(
        job_id="job_cycle_gfs_2026050100_retry_active",
        job_type="download_source_cycle",
        slurm_job_id="6102",
        status="succeeded",
        stage="download",
        retry_count=2,
        manual_retry_marker=True,
        submitted_at="2026-05-01T00:20:00Z",
        finished_at="2026-05-01T00:35:00Z",
        updated_at="2026-05-01T00:35:00Z",
    )
    payload.update(overrides)
    return payload


def _manual_retry_event(**overrides: Any) -> dict[str, Any]:
    details = {
        "trigger": "manual",
        "manual_retry_marker": True,
        "retry_count": 2,
        "previous_job_id": "job_cycle_gfs_2026050100_download",
        "stage": "download",
        "job_type": "download_source_cycle",
    }
    details.update(overrides.pop("details", {}))
    payload = {
        "event_id": 20,
        "entity_type": "pipeline_job",
        "entity_id": "job_cycle_gfs_2026050100_retry_active",
        "event_type": "retry",
        "status_from": "permanently_failed",
        "status_to": "pending",
        "created_at": "2026-05-01T00:20:00Z",
        "details": details,
    }
    payload.update(overrides)
    return payload


def test_file_journal_candidate_state_source_cycle_retry_success_repairs_stale_failed_download(
    tmp_path: Path,
) -> None:
    """Twin of ``test_psycopg_candidate_state_source_cycle_retry_success_repairs_stale_failed_download``."""

    state = _source_cycle_journal_state(
        tmp_path,
        jobs=[_failed_source_cycle_download_job(), _successful_source_cycle_retry_job()],
        events=[_manual_retry_event()],
    )

    assert state["pipeline_status"] is None
    assert state["failed_stage"] is None
    assert state["error_code"] is None
    assert state["stage"] is None
    assert state["repaired_stage_evidence"] == {
        "status": "repaired",
        "repair_status": "repaired",
        "stage": "download",
        "job_type": "download_source_cycle",
        "original_failed_job_id": "job_cycle_gfs_2026050100_download",
        "repairing_retry_job_id": "job_cycle_gfs_2026050100_retry_active",
        "manual_retry_event_id": 20,
        "manual_retry_marker": True,
        "manifest_uri": "raw/gfs/2026050100/manifest.json",
        "forecast_cycle_status": "raw_complete",
        "source_id": "gfs",
        "cycle_id": "gfs_2026050100",
        "cycle_time": "2026-05-01T00:00:00Z",
    }
    failed_job = next(job for job in state["pipeline_jobs"] if job["job_id"] == "job_cycle_gfs_2026050100_download")
    retry_job = next(job for job in state["pipeline_jobs"] if job["job_id"] == "job_cycle_gfs_2026050100_retry_active")
    assert failed_job["status"] == "permanently_failed"
    assert retry_job["status"] == "succeeded"


def _own_succeeded_forecast_job(**overrides: Any) -> dict[str, Any]:
    payload = {
        "job_id": "job_model_b_forecast",
        "run_id": "fcst_gfs_2026050100_model_b",
        "cycle_id": "gfs_2026050100",
        "model_id": "model_b",
        "stage": "forecast",
        "job_type": "run_shud_forecast_array",
        "status": "succeeded",
        "retry_count": 0,
        "submitted_at": "2026-05-01T00:40:00Z",
        "finished_at": "2026-05-01T00:55:00Z",
        "updated_at": "2026-05-01T00:55:00Z",
    }
    payload.update(overrides)
    return payload


def _own_candidate_job(
    *,
    job_id: str,
    stage: str,
    job_type: str,
    status: str,
    updated_at: str,
    **overrides: Any,
) -> dict[str, Any]:
    payload = {
        "job_id": job_id,
        "run_id": "fcst_gfs_2026050100_model_b",
        "cycle_id": "gfs_2026050100",
        "model_id": "model_b",
        "stage": stage,
        "job_type": job_type,
        "status": status,
        "retry_count": 0,
        "updated_at": updated_at,
    }
    payload.update(overrides)
    return payload


def _own_manual_repair_event(*, entity_id: str, previous_job_id: str, event_id: int, created_at: str) -> dict[str, Any]:
    return {
        "event_id": event_id,
        "entity_type": "pipeline_job",
        "entity_id": entity_id,
        "event_type": "retry",
        "created_at": created_at,
        "details": {
            "trigger": "manual",
            "manual_retry_marker": True,
            "retry_count": 1,
            "previous_job_id": previous_job_id,
        },
    }


def test_file_journal_candidate_state_source_cycle_repair_keeps_the_candidates_completed_forecast(
    tmp_path: Path,
) -> None:
    """Twin of ``test_psycopg_candidate_state_source_cycle_repair_keeps_the_candidates_completed_forecast``.

    #1461 E5: repaired evidence without a restart stage must not suppress the completed-stage scan.
    """

    state = _source_cycle_journal_state(
        tmp_path,
        jobs=[
            _failed_source_cycle_download_job(),
            _successful_source_cycle_retry_job(),
            _own_succeeded_forecast_job(),
        ],
        events=[_manual_retry_event()],
    )

    repaired = state["repaired_stage_evidence"]
    assert repaired["original_failed_job_id"] == "job_cycle_gfs_2026050100_download"
    assert repaired["repairing_retry_job_id"] == "job_cycle_gfs_2026050100_retry_active"
    assert repaired["stage"] == "download"
    assert "restart_stage" not in repaired
    assert state["completed_stage_evidence"]["stage"] == "forecast"
    assert state["completed_stage_evidence"]["job_id"] == "job_model_b_forecast"
    assert state["restart_stage"] == "parse"
    assert state["restart_from_stage"] == "parse"


def test_file_journal_candidate_state_repaired_evidence_with_restart_stage_still_wins_the_projection(
    tmp_path: Path,
) -> None:
    """Twin of ``test_psycopg_candidate_state_repaired_evidence_with_restart_stage_still_wins_the_projection``."""

    state = _source_cycle_journal_state(
        tmp_path,
        jobs=[
            _own_candidate_job(
                job_id="job_model_b_convert",
                stage="convert",
                job_type="convert_canonical",
                status="succeeded",
                updated_at="2026-05-01T00:30:00Z",
            ),
            _own_candidate_job(
                job_id="job_model_b_forcing_retry_1",
                stage="forcing",
                job_type="produce_forcing_array",
                status="succeeded",
                retry_count=1,
                manual_retry_marker=True,
                updated_at="2026-05-01T00:50:00Z",
            ),
            _own_candidate_job(
                job_id="job_model_b_forcing",
                stage="forcing",
                job_type="produce_forcing_array",
                status="failed",
                error_code="NODE_FAILURE",
                error_message="forcing failed",
                updated_at="2026-05-01T01:00:00Z",
            ),
        ],
        events=[
            _own_manual_repair_event(
                entity_id="job_model_b_forcing_retry_1",
                previous_job_id="job_model_b_forcing",
                event_id=30,
                created_at="2026-05-01T00:50:00Z",
            )
        ],
    )

    repaired = state["repaired_stage_evidence"]
    assert repaired["stage"] == "forcing"
    assert repaired["restart_stage"] == "forecast"
    assert state["completed_stage_evidence"] == repaired
    assert state["restart_stage"] == "forecast"
    assert state["restart_from_stage"] == "forecast"
    for key in ("pipeline_status", "stage", "failed_stage", "error_code", "error_message"):
        assert state[key] is None


def test_file_journal_candidate_state_terminal_completion_still_suppresses_the_scan_under_repaired_evidence(
    tmp_path: Path,
) -> None:
    """Twin of the SQL test of the same suffix.

    ``test_psycopg_candidate_state_terminal_completion_still_suppresses_the_scan_under_repaired_evidence``
    """

    from services.orchestrator.chain_repository_state import _has_terminal_completion_stage_success

    state = _source_cycle_journal_state(
        tmp_path,
        jobs=[
            _failed_source_cycle_download_job(),
            _successful_source_cycle_retry_job(),
            _own_succeeded_forecast_job(),
            _own_candidate_job(
                job_id="job_model_b_publish",
                stage="publish",
                job_type="publish_tiles",
                status="succeeded",
                updated_at="2026-05-01T01:05:00Z",
            ),
        ],
        events=[_manual_retry_event()],
    )

    assert "restart_stage" not in state["repaired_stage_evidence"]
    assert _has_terminal_completion_stage_success(state["pipeline_jobs"]) is True
    assert "completed_stage_evidence" not in state
    assert "restart_stage" not in state
    assert "restart_from_stage" not in state


def test_file_journal_candidate_state_terminal_stage_manual_repair_keeps_its_evidence(tmp_path: Path) -> None:
    """Twin of ``test_psycopg_candidate_state_terminal_stage_manual_repair_keeps_its_evidence`` (#1461 E10)."""

    from services.orchestrator.chain_repository_state import _has_terminal_completion_stage_success

    state = _source_cycle_journal_state(
        tmp_path,
        jobs=[
            _own_candidate_job(
                job_id="job_model_b_state_save_qc_retry_1",
                stage="state_save_qc",
                job_type="save_state_snapshot",
                status="succeeded",
                retry_count=1,
                manual_retry_marker=True,
                updated_at="2026-05-01T00:50:00Z",
            ),
            _own_candidate_job(
                job_id="job_model_b_state_save_qc",
                stage="state_save_qc",
                job_type="save_state_snapshot",
                status="failed",
                error_code="NODE_FAILURE",
                error_message="state save qc failed",
                updated_at="2026-05-01T01:00:00Z",
            ),
            _own_succeeded_forecast_job(),
        ],
        events=[
            _own_manual_repair_event(
                entity_id="job_model_b_state_save_qc_retry_1",
                previous_job_id="job_model_b_state_save_qc",
                event_id=31,
                created_at="2026-05-01T00:50:00Z",
            )
        ],
    )

    repaired = state["repaired_stage_evidence"]
    assert repaired["stage"] == "state_save_qc"
    assert "restart_stage" not in repaired
    assert repaired["original_failed_job_id"] == "job_model_b_state_save_qc"
    assert state["pipeline_status"] == "failed"
    assert state["stage"] == "state_save_qc"
    assert state["error_code"] == "NODE_FAILURE"
    forecast_row = next(job for job in state["pipeline_jobs"] if job["job_id"] == "job_model_b_forecast")
    assert forecast_row["status"] == "succeeded"
    assert _has_terminal_completion_stage_success(state["pipeline_jobs"]) is True
    assert "completed_stage_evidence" not in state
    assert "restart_stage" not in state


def test_file_journal_candidate_state_linked_retry_repairs_slurm_local_time_skew(tmp_path: Path) -> None:
    """Twin of ``test_psycopg_candidate_state_linked_retry_repairs_slurm_local_time_skew``."""

    state = _source_cycle_journal_state(
        tmp_path,
        jobs=[
            _failed_source_cycle_download_job(
                finished_at="2026-05-01T14:10:00Z",
                started_at="2026-05-01T14:00:00Z",
                updated_at="2026-05-01T00:10:00Z",
                retry_count=0,
            ),
            _successful_source_cycle_retry_job(
                submitted_at="2026-05-01T00:20:00Z",
                finished_at=None,
                updated_at="2026-05-01T00:40:00Z",
                retry_count=1,
            ),
        ],
        events=[
            _manual_retry_event(
                details={"previous_job_id": "job_cycle_gfs_2026050100_download", "retry_count": 1},
            )
        ],
    )

    assert state["pipeline_status"] is None
    assert state["failed_stage"] is None
    assert state["repaired_stage_evidence"]["original_failed_job_id"] == "job_cycle_gfs_2026050100_download"
    assert state["repaired_stage_evidence"]["repairing_retry_job_id"] == "job_cycle_gfs_2026050100_retry_active"


def test_file_journal_candidate_state_source_cycle_multihop_retry_repairs_failed_ancestors(tmp_path: Path) -> None:
    """Twin of ``test_psycopg_candidate_state_source_cycle_multihop_retry_repairs_failed_ancestors``."""

    original_job_id = "job_cycle_gfs_2026050100_download"
    failed_retry_job_id = "job_cycle_gfs_2026050100_download_retry_1"
    successful_retry_job_id = "job_cycle_gfs_2026050100_download_retry_2"

    state = _source_cycle_journal_state(
        tmp_path,
        jobs=[
            _failed_source_cycle_download_job(
                job_id=original_job_id,
                slurm_job_id="6101",
                retry_count=0,
                error_code="ORIGINAL_FAILED",
                submitted_at="2026-05-01T00:00:00Z",
                finished_at="2026-05-01T00:10:00Z",
                updated_at="2026-05-01T00:10:00Z",
            ),
            _failed_source_cycle_download_job(
                job_id=failed_retry_job_id,
                slurm_job_id="6102",
                retry_count=1,
                error_code="FAILED_RETRY",
                submitted_at="2026-05-01T00:20:00Z",
                finished_at="2026-05-01T00:30:00Z",
                created_at="2026-05-01T00:20:00Z",
                updated_at="2026-05-01T00:30:00Z",
            ),
            _successful_source_cycle_retry_job(
                job_id=successful_retry_job_id,
                slurm_job_id="6103",
                retry_count=2,
                submitted_at="2026-05-01T00:40:00Z",
                finished_at="2026-05-01T00:50:00Z",
                created_at="2026-05-01T00:40:00Z",
                updated_at="2026-05-01T00:50:00Z",
            ),
        ],
        events=[
            _manual_retry_event(
                event_id=21,
                entity_id=failed_retry_job_id,
                created_at="2026-05-01T00:20:00Z",
                details={"previous_job_id": original_job_id, "retry_count": 1},
            ),
            _manual_retry_event(
                event_id=22,
                entity_id=successful_retry_job_id,
                created_at="2026-05-01T00:40:00Z",
                details={"previous_job_id": failed_retry_job_id, "retry_count": 2},
            ),
        ],
    )

    assert state["pipeline_status"] is None
    assert state["failed_stage"] is None
    assert state["error_code"] is None
    assert state["repaired_stage_evidence"]["original_failed_job_id"] == original_job_id
    assert state["repaired_stage_evidence"]["repairing_retry_job_id"] == successful_retry_job_id
    assert state["repaired_stage_evidence"]["manual_retry_event_id"] == 22
    original_job = next(job for job in state["pipeline_jobs"] if job["job_id"] == original_job_id)
    failed_retry_job = next(job for job in state["pipeline_jobs"] if job["job_id"] == failed_retry_job_id)
    successful_retry_job = next(job for job in state["pipeline_jobs"] if job["job_id"] == successful_retry_job_id)
    for job in (original_job, failed_retry_job):
        assert job["repair_status"] == "repaired"
        assert job["repaired_by_job_id"] == successful_retry_job_id
        assert job["active_blocker"] is False
    assert successful_retry_job["repair_status"] == "repair_succeeded"
    assert successful_retry_job["repairs_job_id"] == original_job_id
    assert successful_retry_job["repairs_job_ids"] == [original_job_id, failed_retry_job_id]


def test_file_journal_candidate_state_prefixed_s3_manifest_repairs_stale_failed_source_cycle(tmp_path: Path) -> None:
    """Twin of ``test_psycopg_candidate_state_prefixed_s3_manifest_repairs_stale_failed_source_cycle``."""

    state = _source_cycle_journal_state(
        tmp_path,
        jobs=[_failed_source_cycle_download_job(), _successful_source_cycle_retry_job()],
        events=[_manual_retry_event()],
        manifest_uri="s3://nhms-prod/qhh/raw/gfs/2026050100/manifest.json",
    )

    assert state["pipeline_status"] is None
    assert state["failed_stage"] is None
    assert state["error_code"] is None
    # The SQL twin echoes the raw ``s3://`` URI here.  The journal's public candidate-state
    # surface redacts object URIs (``_public_candidate_state``, ``file_journal_candidates.py:505``),
    # so the binding is pinned by the repair itself (an unbound URI keeps the failure active,
    # see the unsupported-scheme test below) and the echo by its redacted public form.
    assert state["repaired_stage_evidence"]["manifest_uri"] == "[object-uri]"
    failed_job = next(job for job in state["pipeline_jobs"] if job["job_id"] == "job_cycle_gfs_2026050100_download")
    assert failed_job["repair_status"] == "repaired"
    assert failed_job["repaired_by_job_id"] == "job_cycle_gfs_2026050100_retry_active"
    assert failed_job["active_blocker"] is False


def test_file_journal_candidate_state_failed_download_manifest_allows_linked_retry_repair(tmp_path: Path) -> None:
    """Twin of ``test_psycopg_candidate_state_failed_download_manifest_allows_linked_retry_repair``."""

    state = _source_cycle_journal_state(
        tmp_path,
        jobs=[_failed_source_cycle_download_job(), _successful_source_cycle_retry_job()],
        events=[_manual_retry_event()],
        manifest_uri="s3://nhms/raw/gfs/2026050100/manifest.json",
        forecast_status="failed_download",
    )

    assert state["pipeline_status"] is None
    assert state["failed_stage"] is None
    assert state["error_code"] is None
    assert state["repaired_stage_evidence"]["forecast_cycle_status"] == "failed_download"
    assert state["repaired_stage_evidence"]["repairing_retry_job_id"] == "job_cycle_gfs_2026050100_retry_active"
    failed_job = next(job for job in state["pipeline_jobs"] if job["job_id"] == "job_cycle_gfs_2026050100_download")
    assert failed_job["repair_status"] == "repaired"
    assert failed_job["active_blocker"] is False


def test_file_journal_candidate_state_unrelated_success_does_not_repair_source_cycle_failure(tmp_path: Path) -> None:
    """Twin of ``test_psycopg_candidate_state_unrelated_success_does_not_repair_source_cycle_failure``."""

    state = _source_cycle_journal_state(
        tmp_path,
        jobs=[
            _failed_source_cycle_download_job(),
            _successful_source_cycle_retry_job(
                job_id="job_cycle_gfs_2026050100_publish",
                job_type="publish_results",
                stage="publish",
                retry_count=0,
                manual_retry_marker=False,
            ),
        ],
        events=[],
    )

    assert state["pipeline_status"] == "permanently_failed"
    assert state["failed_stage"] == "download"
    assert state["error_code"] == "SLURM_JOB_FAILED"
    assert "repaired_stage_evidence" not in state


def test_file_journal_candidate_state_unlinked_source_cycle_success_does_not_repair_failure(tmp_path: Path) -> None:
    """Twin of ``test_psycopg_candidate_state_unlinked_source_cycle_success_does_not_repair_failure``."""

    state = _source_cycle_journal_state(
        tmp_path,
        jobs=[_failed_source_cycle_download_job(), _successful_source_cycle_retry_job()],
        events=[_manual_retry_event(details={"previous_job_id": "job_cycle_gfs_2026050100_other_failed"})],
    )

    assert state["pipeline_status"] == "permanently_failed"
    assert state["failed_stage"] == "download"
    assert state["error_code"] == "SLURM_JOB_FAILED"
    assert "repaired_stage_evidence" not in state


@pytest.mark.parametrize("retry_status", ["pending", "failed"])
def test_file_journal_candidate_state_stale_or_non_succeeded_retry_does_not_repair_source_cycle_failure(
    tmp_path: Path,
    retry_status: str,
) -> None:
    """Twin of ``test_psycopg_candidate_state_stale_or_non_succeeded_retry_does_not_repair_source_cycle_failure``."""

    state = _source_cycle_journal_state(
        tmp_path,
        jobs=[_failed_source_cycle_download_job(), _successful_source_cycle_retry_job(status=retry_status)],
        events=[
            _manual_retry_event(created_at="2026-05-01T00:01:00Z")
            if retry_status == "pending"
            else _manual_retry_event()
        ],
    )

    assert state["pipeline_status"] == "permanently_failed"
    assert state["failed_stage"] == "download"
    assert state["error_code"] == "SLURM_JOB_FAILED"
    assert "repaired_stage_evidence" not in state


@pytest.mark.parametrize(
    "manifest_uri",
    [
        None,
        "raw/gfs/2026050106/manifest.json",
        "raw/ifs/2026050100/manifest.json",
        "raw/gfs/2026050100/index.json",
        "s3://nhms-prod/qhh/raw/gfs/2026050106/manifest.json",
        "s3://nhms-prod/qhh/raw/ifs/2026050100/manifest.json",
        "s3://nhms-prod/qhh/raw/gfs/2026050100/index.json",
        "https://host/raw/gfs/2026050100/manifest.json",
        "file:///raw/gfs/2026050100/manifest.json",
        "s3://nhms-prod/qhh/raw/gfs/../2026050100/manifest.json",
    ],
)
def test_file_journal_candidate_state_unsupported_scheme_or_mismatched_manifest_keeps_source_cycle_failure_active(
    tmp_path: Path,
    manifest_uri: str | None,
) -> None:
    """Twin of the SQL test of the same suffix.

    ``test_psycopg_candidate_state_unsupported_scheme_or_mismatched_manifest_keeps_source_cycle_failure_active``
    """

    state = _source_cycle_journal_state(
        tmp_path,
        jobs=[_failed_source_cycle_download_job(), _successful_source_cycle_retry_job()],
        events=[_manual_retry_event()],
        manifest_uri=manifest_uri,
    )

    assert state["pipeline_status"] == "permanently_failed"
    assert state["failed_stage"] == "download"
    assert state["error_code"] == "SLURM_JOB_FAILED"
    assert "repaired_stage_evidence" not in state


def test_file_journal_candidate_state_repaired_source_cycle_then_later_unrepaired_failure_stays_active(
    tmp_path: Path,
) -> None:
    """Twin of ``test_psycopg_candidate_state_repaired_source_cycle_then_later_unrepaired_failure_stays_active``."""

    later_failed_job_id = "job_cycle_gfs_2026050100_download_retry_3"
    state = _source_cycle_journal_state(
        tmp_path,
        jobs=[
            _failed_source_cycle_download_job(),
            _successful_source_cycle_retry_job(),
            _failed_source_cycle_download_job(
                job_id=later_failed_job_id,
                slurm_job_id="6103",
                status="permanently_failed",
                retry_count=3,
                error_code="LATER_UNREPAIRED",
                error_message="later source cycle retry failed",
                submitted_at="2026-05-01T01:50:00Z",
                finished_at="2026-05-01T02:00:00Z",
                updated_at="2026-05-01T02:00:00Z",
            ),
        ],
        events=[_manual_retry_event()],
    )

    assert state["pipeline_status"] == "permanently_failed"
    assert state["failed_stage"] == "download"
    assert state["error_code"] == "LATER_UNREPAIRED"
    assert state["pipeline_truth_timestamp"] == "2026-05-01T02:00:00Z"
    assert "repaired_stage_evidence" not in state
    older_failed_job = next(
        job for job in state["pipeline_jobs"] if job["job_id"] == "job_cycle_gfs_2026050100_download"
    )
    later_failed_job = next(job for job in state["pipeline_jobs"] if job["job_id"] == later_failed_job_id)
    assert older_failed_job["repair_status"] == "repaired"
    assert older_failed_job["repaired_by_job_id"] == "job_cycle_gfs_2026050100_retry_active"
    assert older_failed_job["active_blocker"] is False
    assert "repair_status" not in later_failed_job


def test_file_journal_candidate_state_truncated_repair_window_keeps_latest_unrepaired_failure_active(
    tmp_path: Path,
) -> None:
    """Twin of ``test_psycopg_candidate_state_truncated_repair_window_keeps_latest_unrepaired_failure_active``."""

    later_failed_job_id = "job_cycle_gfs_2026050100_download_retry_3"
    state = _source_cycle_journal_state(
        tmp_path,
        jobs=[
            _failed_source_cycle_download_job(
                job_id=later_failed_job_id,
                slurm_job_id="6103",
                status="permanently_failed",
                retry_count=3,
                error_code="LATER_UNREPAIRED",
                error_message="later source cycle retry failed",
                submitted_at="2026-05-01T01:50:00Z",
                finished_at="2026-05-01T02:00:00Z",
                updated_at="2026-05-01T02:00:00Z",
            ),
            _successful_source_cycle_retry_job(),
            _failed_source_cycle_download_job(),
            _failed_source_cycle_download_job(
                job_id="job_cycle_gfs_2026050100_omitted_old_failure",
                slurm_job_id="6099",
                retry_count=0,
                error_code="OMITTED_OLD_FAILURE",
                submitted_at="2026-04-30T23:30:00Z",
                finished_at="2026-04-30T23:40:00Z",
                updated_at="2026-04-30T23:40:00Z",
            ),
        ],
        events=[_manual_retry_event()],
        job_limit=3,
    )

    assert state["state_truncated"] is True
    assert state["pipeline_status"] == "permanently_failed"
    assert state["failed_stage"] == "download"
    assert state["error_code"] == "LATER_UNREPAIRED"
    assert state["pipeline_truth_timestamp"] == "2026-05-01T02:00:00Z"
    assert "source_cycle_repair_evidence" not in state
    assert "repaired_stage_evidence" not in state
    older_failed_job = next(
        job for job in state["pipeline_jobs"] if job["job_id"] == "job_cycle_gfs_2026050100_download"
    )
    successful_retry_job = next(
        job for job in state["pipeline_jobs"] if job["job_id"] == "job_cycle_gfs_2026050100_retry_active"
    )
    later_failed_job = next(job for job in state["pipeline_jobs"] if job["job_id"] == later_failed_job_id)
    assert older_failed_job["repair_status"] == "repaired"
    assert older_failed_job["repaired_by_job_id"] == "job_cycle_gfs_2026050100_retry_active"
    assert older_failed_job["active_blocker"] is False
    assert successful_retry_job["repair_status"] == "repair_succeeded"
    assert successful_retry_job["repairs_job_id"] == "job_cycle_gfs_2026050100_download"
    assert "repair_status" not in later_failed_job


def test_file_journal_candidate_state_equal_truth_timestamp_selects_later_source_cycle_failure(tmp_path: Path) -> None:
    """Twin of ``test_psycopg_candidate_state_equal_truth_timestamp_selects_later_source_cycle_failure``."""

    older_failed_job_id = "job_cycle_gfs_2026050100_download_retry_2"
    later_failed_job_id = "job_cycle_gfs_2026050100_download_retry_3"
    state = _source_cycle_journal_state(
        tmp_path,
        jobs=[
            _failed_source_cycle_download_job(
                job_id=older_failed_job_id,
                retry_count=2,
                error_code="OLDER_UNREPAIRED",
                submitted_at="2026-05-01T01:20:00Z",
                finished_at="2026-05-01T01:30:00Z",
                created_at="2026-05-01T01:20:00Z",
                updated_at="2026-05-01T02:00:00Z",
            ),
            _failed_source_cycle_download_job(
                job_id=later_failed_job_id,
                retry_count=3,
                error_code="LATER_UNREPAIRED",
                submitted_at="2026-05-01T01:50:00Z",
                finished_at="2026-05-01T02:00:00Z",
                created_at="2026-05-01T01:50:00Z",
                updated_at="2026-05-01T02:00:00Z",
            ),
        ],
        events=[],
    )

    assert state["pipeline_status"] == "permanently_failed"
    assert state["failed_stage"] == "download"
    assert state["error_code"] == "LATER_UNREPAIRED"
    assert state["pipeline_truth_timestamp"] == "2026-05-01T02:00:00Z"
    active_job = next(job for job in state["pipeline_jobs"] if job["job_id"] == later_failed_job_id)
    assert active_job["retry_count"] == 3


def test_file_journal_candidate_state_truncated_source_cycle_repair_window_is_inconclusive(tmp_path: Path) -> None:
    """Twin of ``test_psycopg_candidate_state_truncated_source_cycle_repair_window_is_inconclusive``."""

    state = _source_cycle_journal_state(
        tmp_path,
        jobs=[
            _successful_source_cycle_retry_job(),
            _failed_source_cycle_download_job(),
            _failed_source_cycle_download_job(
                job_id="job_cycle_gfs_2026050100_omitted_old_failure",
                slurm_job_id="6099",
                retry_count=0,
                error_code="OMITTED_OLD_FAILURE",
                submitted_at="2026-04-30T23:30:00Z",
                finished_at="2026-04-30T23:40:00Z",
                updated_at="2026-04-30T23:40:00Z",
            ),
        ],
        events=[],
        job_limit=2,
        event_limit=10,
    )

    assert state["state_truncated"] is True
    assert state["pipeline_status"] is None
    assert state["failed_stage"] is None
    assert state["error_code"] is None
    assert "repaired_stage_evidence" not in state
    assert state["source_cycle_repair_evidence"] == {
        "status": "inconclusive_truncated",
        "truncated": True,
        "reason": "source_cycle_repair_window_truncated",
        "unresolved_failed_job_ids": ["job_cycle_gfs_2026050100_download"],
    }


# --- task-results / shared-cycle family --------------------------------------------------


def test_file_journal_candidate_state_latest_truth_timestamp_selects_terminal_success(tmp_path: Path) -> None:
    """Twin of ``test_psycopg_candidate_state_latest_truth_timestamp_selects_terminal_success``."""

    own = {"run_id": "fcst_gfs_2026050100_model_b", "cycle_id": "gfs_2026050100", "model_id": "model_b"}
    repository = _journal(
        tmp_path,
        jobs=[
            {
                **own,
                "job_id": "job_success",
                "job_type": "publish_tiles",
                "status": "succeeded",
                "stage": "publish",
                "submitted_at": "2026-05-01T06:00:00Z",
                "finished_at": "2026-05-01T06:40:00Z",
                "updated_at": "2026-05-01T06:40:00Z",
            },
            {
                **own,
                "job_id": "job_failed",
                "job_type": "parse_output",
                "status": "failed",
                "stage": "parse",
                "error_code": "FAILED_PARSE",
                "submitted_at": "2026-05-01T06:10:00Z",
                "finished_at": "2026-05-01T06:20:00Z",
                "updated_at": "2026-05-01T06:20:00Z",
            },
            {
                **own,
                "job_id": "old_overflow",
                "job_type": "parse_output",
                "status": "failed",
                "stage": "parse",
                "error_code": "FAILED_PARSE",
                "updated_at": "2026-05-01T05:00:00Z",
            },
        ],
    )

    state = _state(repository, model_id="model_b", job_limit=2, event_limit=10)

    assert state["pipeline_status"] == "succeeded"
    assert state["failed_stage"] is None
    assert state["pipeline_truth_timestamp"] == "2026-05-01T06:40:00Z"
    assert state["pipeline_jobs_total"] == 3
    assert state["state_truncated"] is True


def test_file_journal_candidate_state_rejects_ambiguous_array_task_identity(tmp_path: Path) -> None:
    """Twin of ``test_psycopg_candidate_state_rejects_ambiguous_array_task_identity``."""

    repository = _journal(
        tmp_path,
        jobs=[
            _cycle_job(
                job_id="job_cycle_gfs_2026050100_forcing",
                job_type="produce_forcing_array",
                status="partially_failed",
                stage="forcing",
                error_code="NODE_FAILURE",
                retry_count=1,
            ),
            _cycle_job(
                job_id="job_cycle_gfs_2026050100_publish",
                job_type="publish_tiles",
                status="succeeded",
                stage="publish",
                retry_count=0,
            ),
        ],
        events=[
            {
                "event_id": 1,
                "entity_type": "pipeline_job",
                "entity_id": "job_cycle_gfs_2026050100_forcing",
                "event_type": "status_change",
                "status_from": "running",
                "status_to": "partially_failed",
                "details": {
                    "stage": "forcing",
                    "task_results": [
                        {"task_id": 0, "status": "succeeded"},
                        {"task_id": 1, "status": "failed", "error_code": "NODE_FAILURE"},
                    ],
                },
            }
        ],
    )

    state = _state(repository, model_id="model_b")

    assert state["shared_cycle_aggregate"] is True
    assert state["pipeline_status"] is None
    assert state["array_task_id"] is None
    assert state["original_task_id"] is None
    assert state["successful_sibling_outputs_reused"] is False
    assert state["successful_sibling_task_count"] == 0


def _forecast_task_event(task_results: list[dict[str, Any]], **details: Any) -> dict[str, Any]:
    return {
        "event_id": 1,
        "entity_type": "pipeline_job",
        "entity_id": "job_cycle_gfs_2026050100_forecast",
        "event_type": "status_change",
        "status_to": "partially_failed",
        "created_at": "2026-05-01T00:05:00Z",
        "details": {"stage": "forecast", "task_results": task_results, **details},
    }


def _partially_failed_cycle_forecast_job(**fields: Any) -> dict[str, Any]:
    return _cycle_job(
        job_id="job_cycle_gfs_2026050100_forecast",
        job_type="run_shud_forecast_array",
        status="partially_failed",
        stage="forecast",
        **fields,
    )


def test_file_journal_candidate_state_ignores_sibling_candidate_only_task_failure(tmp_path: Path) -> None:
    """Twin of ``test_psycopg_candidate_state_ignores_sibling_candidate_only_task_failure``."""

    repository = _journal(
        tmp_path,
        jobs=[_partially_failed_cycle_forecast_job()],
        events=[
            _forecast_task_event(
                [
                    {
                        "task_id": 1,
                        "original_task_id": 1,
                        "candidate_id": "gfs:2026-05-01T00:00:00Z:model_b:forecast_gfs_deterministic",
                        "status": "failed",
                        "error_code": "NODE_FAILURE",
                    }
                ]
            )
        ],
    )

    model_a = _state(repository, model_id="model_a")
    model_b = _state(repository, model_id="model_b")

    assert model_a["pipeline_status"] is None
    assert model_a["array_task_id"] is None
    assert model_b["pipeline_status"] == "partially_failed"
    assert model_b["failed_stage"] == "forecast"
    assert model_b["original_task_id"] == 1


def test_file_journal_candidate_state_ignores_conflicting_candidate_and_model_task_payload(tmp_path: Path) -> None:
    """Twin of ``test_psycopg_candidate_state_ignores_conflicting_candidate_and_model_task_payload``."""

    repository = _journal(
        tmp_path,
        jobs=[_partially_failed_cycle_forecast_job()],
        events=[
            _forecast_task_event(
                [
                    {
                        "task_id": 1,
                        "original_task_id": 1,
                        "candidate_id": "gfs:2026-05-01T00:00:00Z:model_b:forecast_gfs_deterministic",
                        "model_id": "model_a",
                        "status": "failed",
                        "error_code": "NODE_FAILURE",
                    },
                    {
                        "task_id": 2,
                        "original_task_id": 2,
                        "candidate_id": "gfs:2026-05-01T00:00:00Z:model_a:forecast_gfs_deterministic",
                        "model_id": "model_a",
                        "status": "failed",
                        "error_code": "OUT_OF_MEMORY",
                    },
                ]
            )
        ],
    )

    state = _state(repository, model_id="model_a")

    assert state["pipeline_status"] == "partially_failed"
    assert state["original_task_id"] == 2
    assert state["error_code"] == "OUT_OF_MEMORY"


def test_file_journal_candidate_state_later_retry_success_supersedes_older_failed_task(tmp_path: Path) -> None:
    """Twin of ``test_psycopg_candidate_state_later_retry_success_supersedes_older_failed_task``.

    The SQL fixture returns an event for ``..._forcing_retry_1`` with no job row; the SQL
    event read (``entity_id IN (SELECT pj.job_id ...)``) and the journal write
    (``append_historical_pipeline_event`` resolves the entity row) both refuse such an
    orphan, so the retry's job row is seeded.  It is kept OLDER than the failed row
    (``retry_count`` 0 < 1, both undated) so the failed row stays the latest job and the
    verdict is still made from the events' task supersession, as in the SQL twin.
    """

    repository = _journal(
        tmp_path,
        jobs=[
            _cycle_job(
                job_id="job_cycle_gfs_2026050100_forcing",
                job_type="produce_forcing_array",
                status="partially_failed",
                stage="forcing",
                error_code="NODE_FAILURE",
                retry_count=1,
            ),
            _cycle_job(
                job_id="job_cycle_gfs_2026050100_forcing_retry_1",
                job_type="produce_forcing_array",
                status="succeeded",
                stage="forcing",
                retry_count=0,
            ),
        ],
        events=[
            {
                "event_id": 1,
                "entity_type": "pipeline_job",
                "entity_id": "job_cycle_gfs_2026050100_forcing",
                "event_type": "status_change",
                "status_to": "partially_failed",
                "created_at": "2026-05-01T00:01:00Z",
                "details": {
                    "stage": "forcing",
                    "task_results": [
                        {
                            "task_id": 1,
                            "original_task_id": 1,
                            "model_id": "model_b",
                            "status": "failed",
                            "error_code": "NODE_FAILURE",
                        }
                    ],
                },
            },
            {
                "event_id": 2,
                "entity_type": "pipeline_job",
                "entity_id": "job_cycle_gfs_2026050100_forcing_retry_1",
                "event_type": "status_change",
                "status_to": "succeeded",
                "created_at": "2026-05-01T00:05:00Z",
                "details": {
                    "stage": "forcing",
                    "task_results": [
                        {"task_id": 0, "original_task_id": 1, "model_id": "model_b", "status": "succeeded"}
                    ],
                },
            },
        ],
        hydro_runs=[
            {
                "run_id": "fcst_gfs_2026050100_model_b",
                "source_id": "gfs",
                "cycle_time": "2026-05-01T00:00:00Z",
                "model_id": "model_b",
                "status": "failed",
                "output_uri": "s3://nhms/runs/fcst_gfs_2026050100_model_b/output/",
            }
        ],
    )

    state = _state(repository, model_id="model_b")

    # Premise: the failed row is still the latest job, so supersession is what decides.
    assert state["pipeline_jobs"][-1]["job_id"] == "job_cycle_gfs_2026050100_forcing"
    assert state["pipeline_status"] is None
    assert state["failed_stage"] is None
    assert state["array_task_id"] is None
    assert state["original_task_id"] is None


def test_file_journal_candidate_state_bounds_nested_task_results_before_state_decision(tmp_path: Path) -> None:
    """Twin of ``test_psycopg_candidate_state_bounds_nested_task_results_before_state_decision``."""

    task_results = [
        {"task_id": index, "original_task_id": index, "model_id": "model_a", "status": "succeeded"}
        for index in range(17)
    ]
    task_results[-1] = {**task_results[-1], "status": "failed", "error_code": "NODE_FAILURE"}
    repository = _journal(
        tmp_path,
        jobs=[_partially_failed_cycle_forecast_job(error_code="NODE_FAILURE")],
        events=[_forecast_task_event(task_results)],
    )

    state = _state(repository, model_id="model_a")

    details = state["pipeline_events"][0]["details"]
    assert state["pipeline_status"] is None
    assert state["array_task_id"] is None
    assert len(details["task_results"]) == 16
    assert details["task_results_total"] == 17
    assert details["task_results_included"] == 16
    assert details["task_results_overflow"] is True
    assert details["task_results_omitted"] == 1


def test_file_journal_candidate_state_reports_task_results_overflow_past_sentinel(tmp_path: Path) -> None:
    """Twin of ``test_psycopg_candidate_state_does_not_scan_task_results_past_overflow_sentinel`` (output half).

    The read-count half (``task_results.read_count == 17``) needs a lazy sequence, which a
    journal write serialises to a list; it is pinned on the shared projection below.
    """

    repository = _journal(
        tmp_path,
        jobs=[_partially_failed_cycle_forecast_job(error_code="NODE_FAILURE")],
        events=[
            _forecast_task_event(
                [
                    {"task_id": index, "original_task_id": index, "model_id": "model_b", "status": "succeeded"}
                    for index in range(24)
                ],
                task_results_total=120,
            )
        ],
    )

    state = _state(repository, model_id="model_b")

    details = state["pipeline_events"][0]["details"]
    assert state["pipeline_status"] is None
    assert state["array_task_id"] is None
    assert len(details["task_results"]) == 16
    assert details["task_results_total"] == 120
    assert details["task_results_included"] == 16
    assert details["task_results_limit"] == 16
    assert details["task_results_overflow"] is True
    assert details["task_results_omitted"] == 104


class _BoundedReadSequence(Sequence[Any]):
    """A ``task_results`` sequence that fails if read past ``allowed_reads`` items."""

    def __init__(self, items: list[Any], *, allowed_reads: int) -> None:
        self.items = items
        self.allowed_reads = allowed_reads
        self.read_count = 0

    def __iter__(self) -> Iterator[Any]:
        for index, item in enumerate(self.items):
            if index >= self.allowed_reads:
                raise AssertionError("task_results scanned past overflow sentinel")
            self.read_count = index + 1
            yield item

    def __getitem__(self, index: Any) -> Any:
        if isinstance(index, slice):
            raise AssertionError("task_results must not be sliced")
        if index >= self.allowed_reads:
            raise AssertionError("task_results scanned past overflow sentinel")
        self.read_count = max(self.read_count, index + 1)
        return self.items[index]

    def __len__(self) -> int:
        raise AssertionError("task_results length must not be required")


def test_candidate_state_projection_does_not_scan_task_results_past_overflow_sentinel() -> None:
    """Read-count half of ``test_psycopg_candidate_state_does_not_scan_task_results_past_overflow_sentinel``.

    Pinned on the projection the journal repository calls
    (``chain_repository_state.candidate_state_from_rows``; until R3, #3260, the SQL repository
    called it too), with the rows the deleted SQL twin's stubbed fetches returned: a lazy
    ``task_results`` is never read past the 17th item.
    """

    from services.orchestrator.chain_repository_state import candidate_state_from_rows

    task_results = _BoundedReadSequence(
        [
            {"task_id": index, "original_task_id": index, "model_id": "model_b", "status": "succeeded"}
            for index in range(24)
        ],
        allowed_reads=17,
    )

    state = candidate_state_from_rows(
        source_id="gfs",
        cycle_time=_CYCLE_TIME,
        model_id="model_b",
        run_id="fcst_gfs_2026050100_model_b",
        forcing_version_id="forc_gfs_2026050100_model_b",
        candidate_id="gfs:2026-05-01T00:00:00Z:model_b:forecast_gfs_deterministic",
        hydro_run=None,
        pipeline_jobs=[_partially_failed_cycle_forecast_job(error_code="NODE_FAILURE")],
        pipeline_events=[_forecast_task_event(task_results, task_results_total=120)],
        forcing_version=None,
        forecast_cycle=None,
        retry_limit=3,
        job_limit=10,
        event_limit=10,
    )

    assert state is not None
    details = state["pipeline_events"][0]["details"]
    assert len(details["task_results"]) == 16
    assert details["task_results_omitted"] == 104
    assert state["pipeline_status"] is None
    assert task_results.read_count == 17


# --- has_active_pipeline / active_slurm_jobs --------------------------------------------


@pytest.mark.parametrize("status", ["queued", "submitted", "running"])
def test_file_journal_has_active_pipeline_counts_non_terminal_pipeline_rows(tmp_path: Path, status: str) -> None:
    """Rule half of ``test_psycopg_has_active_pipeline_includes_queued_pipeline_rows``.

    The SQL twin pins that ``queued``/``submitted``/``running`` are outside the probe's
    terminal ``NOT IN`` list; here a row at each status answers active.
    """

    repository = _journal(
        tmp_path,
        jobs=[
            {
                "job_id": "job_model_a_forcing",
                "run_id": "fcst_gfs_2026050100_model_a",
                "cycle_id": "gfs_2026050100",
                "model_id": "model_a",
                "job_type": "produce_forcing_array",
                "stage": "forcing",
                "status": status,
                "slurm_job_id": "3001",
            }
        ],
    )

    assert repository.has_active_pipeline(source_id="gfs", cycle_time=_CYCLE_TIME, model_id="model_a") is True


@pytest.mark.parametrize(
    ("hydro_status", "active"),
    [
        ("created", True),
        ("staged", True),
        ("pending", True),
        ("submitted", True),
        ("running", True),
        ("failed", False),
        ("parsed", False),
        ("complete", False),
    ],
)
def test_file_journal_has_active_pipeline_hydro_arm_is_the_active_hydro_status_set(
    tmp_path: Path,
    hydro_status: str,
    active: bool,
) -> None:
    """Rule half of ``test_psycopg_has_active_pipeline_includes_queued_pipeline_rows`` (hydro set).

    The SQL twin pins the hydro arm's bound set to exactly
    ``{created, staged, pending, submitted, running}``; also the rule
    ``test_real_active_pipeline_probe_counts_a_pending_hydro_run_with_no_pipeline_job``
    proves on real Postgres.  A hydro run with NO pipeline row answers from the hydro arm alone.
    """

    repository = _journal(
        tmp_path,
        hydro_runs=[
            {
                "run_id": "fcst_gfs_2026050100_model_a",
                "source_id": "gfs",
                "cycle_time": "2026-05-01T00:00:00Z",
                "model_id": "model_a",
                "status": hydro_status,
            }
        ],
    )

    assert repository.has_active_pipeline(source_id="gfs", cycle_time=_CYCLE_TIME, model_id="model_a") is active


@pytest.mark.parametrize(
    ("run_id", "model_id", "active"),
    [
        ("fcst_gfs_2026050100_model_a", None, True),
        ("cycle_gfs_2026050100", None, True),
        # The journal write admits only candidate/cycle/cycle-prefixed run ids, so the model-id
        # arm is exercised on a cycle-prefixed run id that only the model id can match.
        ("cycle_gfs_2026050100_forcing_cohort", "model_a", True),
        ("fcst_gfs_2026050100_model_b", "model_b", False),
    ],
    ids=["candidate_run", "cycle_run", "model_id", "foreign_model_run"],
)
def test_file_journal_has_active_pipeline_row_visibility(
    tmp_path: Path,
    run_id: str,
    model_id: str | None,
    active: bool,
) -> None:
    """Visibility half of ``test_psycopg_has_active_pipeline_includes_queued_pipeline_rows`` (job-arm row visibility).

    The SQL job arm matches the candidate run id, the cycle run id, or the model id; another
    model's own run is not this candidate's pipeline.
    """

    repository = _journal(
        tmp_path,
        jobs=[
            {
                "job_id": "job_forcing",
                "run_id": run_id,
                "cycle_id": "gfs_2026050100",
                "model_id": model_id,
                "job_type": "produce_forcing_array",
                "stage": "forcing",
                "status": "queued",
            }
        ],
    )

    assert repository.has_active_pipeline(source_id="gfs", cycle_time=_CYCLE_TIME, model_id="model_a") is active


def test_file_journal_active_slurm_jobs_includes_cycle_run_array_job_for_filtered_model(tmp_path: Path) -> None:
    """Rule half of ``test_psycopg_active_slurm_jobs_includes_cycle_run_array_job_for_filtered_model``.

    A row on the exact cycle run id stays visible to another model's active-Slurm scan even
    when it names a different model.
    """

    repository = _journal(
        tmp_path,
        jobs=[
            {
                "job_id": "job_cycle_gfs_2026050100_forecast",
                "run_id": "cycle_gfs_2026050100",
                "cycle_id": "gfs_2026050100",
                "job_type": "run_shud_forecast_array",
                "slurm_job_id": "3001",
                "model_id": "model_a",
                "status": "running",
                "stage": "forecast",
            }
        ],
    )

    jobs = repository.active_slurm_jobs(source_id="gfs", cycle_time=_CYCLE_TIME, model_id="model_b")

    assert [job["job_id"] for job in jobs] == ["job_cycle_gfs_2026050100_forecast"]
    assert jobs[0]["run_id"] == "cycle_gfs_2026050100"
    assert jobs[0]["model_id"] == "model_a"


@pytest.mark.parametrize("status", ["queued", "submitted", "running"])
def test_file_journal_active_slurm_jobs_includes_non_terminal_pipeline_rows(tmp_path: Path, status: str) -> None:
    """Rule half of ``test_psycopg_active_slurm_jobs_includes_queued_pipeline_rows``."""

    repository = _journal(
        tmp_path,
        jobs=[
            {
                "job_id": "job_cycle_gfs_2026050100_forcing",
                "run_id": "cycle_gfs_2026050100",
                "cycle_id": "gfs_2026050100",
                "job_type": "run_shud_forecast_array",
                "slurm_job_id": "3001",
                "model_id": "model_a",
                "status": status,
                "stage": "forcing",
            }
        ],
    )

    jobs = repository.active_slurm_jobs(source_id="gfs", cycle_time=_CYCLE_TIME, model_id="model_a")

    assert [job["status"] for job in jobs] == [status]
    assert jobs[0]["slurm_job_id"] == "3001"


# --- NFS raw-manifest readiness ----------------------------------------------------------


def _write_nfs_manifest(root: Path) -> None:
    """The NFS raw manifest of the SQL-path NFS raw-manifest tests (R3-1 rows 41-42; deleted by R3, #3260)."""

    local_key = "raw/gfs/2026062612/gfs.t12z.f000.bundle.grib2"
    raw_file = root / local_key
    raw_file.parent.mkdir(parents=True, exist_ok=True)
    raw_file.write_bytes(b"grib-bytes")
    manifest = {
        "source_id": "gfs",
        "cycle_time": "2026-06-26T12:00:00+00:00",
        "manifest_uri": "s3://nhms/raw/gfs/2026062612/manifest.json",
        "entries": [
            {
                "remote_url": "https://example.invalid/gfs",
                "local_key": local_key,
                "variable": "prcp_rate_or_amount",
                "forecast_hour": 0,
            }
        ],
    }
    (root / "raw/gfs/2026062612/manifest.json").write_text(json.dumps(manifest), encoding="utf-8")


def _nfs_candidate_state(journal_root: Path) -> dict[str, Any] | None:
    return FileOrchestrationJournalRepository(journal_root).candidate_state(
        source_id="gfs",
        cycle_time=datetime(2026, 6, 26, 12, tzinfo=UTC),
        model_id="basins_qhh_shud",
        run_id="fcst_gfs_2026062612_basins_qhh_shud",
        forcing_version_id="forc_gfs_2026062612_basins_qhh_shud",
        candidate_id="gfs:2026-06-26T12:00:00Z:basins_qhh_shud:forecast_gfs_deterministic",
    )


def test_file_journal_candidate_state_materializes_nfs_raw_manifest_without_journal_row(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Twin of ``test_candidate_state_materializes_nfs_raw_manifest_without_local_db_row``."""

    nfs_root = tmp_path / "nfs"
    _write_nfs_manifest(nfs_root)
    monkeypatch.setenv("NHMS_SCHEDULER_REQUIRE_NFS_RAW_MANIFEST", "true")
    monkeypatch.setenv("NHMS_SCHEDULER_NFS_RAW_MANIFEST_ROOT", str(nfs_root))
    monkeypatch.setenv("NHMS_SCHEDULER_NFS_RAW_MANIFEST_PREFIX", "s3://nhms")

    state = _nfs_candidate_state(tmp_path / "journal")

    assert state is not None
    assert state["forecast_cycle"]["status"] == "raw_complete"
    # The SQL twin echoes the raw ``s3://`` URI; the journal's public candidate-state surface
    # redacts object URIs (``_public_candidate_state``, ``file_journal_candidates.py:505``).
    assert state["forecast_cycle"]["manifest_uri"] == "[object-uri]"
    assert state["forecast_cycle"]["source_cycle_truth"] == "node27_nfs_raw_manifest"
    assert state["nfs_raw_manifest"]["status"] == "ready"


def test_file_journal_candidate_state_reports_required_nfs_manifest_gap_without_fallback_row(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Twin of ``test_candidate_state_reports_required_nfs_manifest_gap_without_fallback_db_row``."""

    nfs_root = tmp_path / "nfs"
    nfs_root.mkdir()
    monkeypatch.setenv("NHMS_SCHEDULER_REQUIRE_NFS_RAW_MANIFEST", "true")
    monkeypatch.setenv("NHMS_SCHEDULER_NFS_RAW_MANIFEST_ROOT", str(nfs_root))

    state = _nfs_candidate_state(tmp_path / "journal")

    assert state is not None
    assert state["forecast_cycle"] is None
    assert state["nfs_raw_manifest"]["status"] == "missing"
    assert state["nfs_raw_manifest"]["required"] is True


# --- freshness window / truncation (the #1572 geometry) ----------------------------------


def _truncation_rows(*, job_limit: int, friendly: bool) -> list[dict[str, Any]]:
    """``_integration_candidate_rows`` of ``tests/test_orchestration_chain.py`` (cycle 2026-05-03)."""

    cycle_time = datetime(2026, 5, 3, tzinfo=UTC)
    cycle_id = "gfs_2026050300"
    cycle_run_id = "cycle_gfs_2026050300"

    def retry_row(at: datetime) -> dict[str, Any]:
        return {
            "job_id": f"job_{cycle_run_id}_forecast_retry_87",
            "run_id": cycle_run_id,
            "cycle_id": cycle_id,
            "job_type": "run_shud_forecast_array",
            "status": "failed",
            "stage": "forecast",
            "retry_count": 0,
            "model_id": None,
            "created_at": at,
            "updated_at": at,
            "finished_at": at,
        }

    rows: list[dict[str, Any]] = []
    if not friendly:
        rows.append(retry_row(cycle_time - timedelta(days=1)))
    rows.extend(
        {
            "job_id": f"job_{cycle_run_id}_publish_{index}",
            "run_id": cycle_run_id,
            "cycle_id": cycle_id,
            "job_type": "publish_tiles",
            "status": "succeeded",
            "stage": "publish",
            "retry_count": 0,
            "model_id": None,
            "created_at": cycle_time + timedelta(minutes=index),
            "updated_at": cycle_time + timedelta(minutes=index),
            "finished_at": cycle_time + timedelta(minutes=index),
        }
        for index in range(job_limit + 1)
    )
    if friendly:
        rows.append(retry_row(cycle_time + timedelta(hours=1)))
    return rows


@pytest.mark.parametrize("friendly", [False, True], ids=["reverse", "friendly"])
def test_file_journal_candidate_state_truncation_keeps_true_total_and_stage_attempt_floor(
    tmp_path: Path,
    friendly: bool,
) -> None:
    """Journal half of ``test_integration_candidate_state_reverse_truncation_matches_file_journal``.

    That oracle is ``integration``-marked (node-27 only) and constructs the SQL repository, so
    its journal-side expectations get a plain home here.  Reverse: the max-attempt row sits
    behind ``job_limit`` fresher rows; friendly: it is the newest row.  Either way the
    stage-scoped forecast attempt is 87, the total is the true admitted count, and the
    returned rows are the pure-freshness top ``job_limit``.
    """

    from services.orchestrator.scheduler_state_rows import _state_retry_attempt

    job_limit = 5
    rows = _truncation_rows(job_limit=job_limit, friendly=friendly)
    repository = _journal(tmp_path, jobs=rows)

    state = repository.candidate_state(
        source_id="gfs",
        cycle_time=datetime(2026, 5, 3, tzinfo=UTC),
        model_id="model_a",
        run_id="fcst_gfs_2026050300_model_a",
        forcing_version_id="forc_gfs_2026050300_model_a",
        candidate_id="gfs:2026-05-03T00:00:00Z:model_a:forecast_gfs_deterministic",
        job_limit=job_limit,
        event_limit=job_limit,
    )

    assert state is not None
    assert len(state["pipeline_jobs"]) == job_limit
    assert state["pipeline_jobs_total"] == job_limit + 2
    assert state["state_truncated"] is True
    assert _state_retry_attempt(state, stage="forecast") == 87
    freshest = sorted(rows, key=lambda row: row["updated_at"])[-job_limit:]
    assert [job["job_id"] for job in state["pipeline_jobs"]] == [row["job_id"] for row in freshest]


# --- reserve_pipeline_job conflict absorption --------------------------------------------


@pytest.mark.parametrize("conflict", ["job_id", "idempotency_key"])
def test_file_journal_reserve_pipeline_job_absorbs_every_unique_conflict_as_a_clean_loss(
    tmp_path: Path,
    conflict: str,
) -> None:
    """Rule half of ``test_reserve_pipeline_job_sql_absorbs_all_unique_conflicts`` and
    ``test_real_reserve_pipeline_job_absorbs_job_id_pk_conflict``.

    The SQL repository pins an UNTARGETED ``ON CONFLICT DO NOTHING``: a reserve that collides on
    the job id (a legacy row with no idempotency key) or on the idempotency key is a clean loss
    (``None``), never an exception and never an overwrite.
    """

    existing = {
        "job_id": "job_fcst_gfs_2026050100_model_1_forcing",
        "run_id": "fcst_gfs_2026050100_model_1",
        "cycle_id": "gfs_2026050100",
        "job_type": "forcing",
        "model_id": "model_1",
        "stage": "forcing",
        "status": "running",
        "idempotency_key": None if conflict == "job_id" else "gfs:gfs_2026050100:model_1:forcing",
    }
    repository = _journal(tmp_path, jobs=[existing])
    reservation = {
        **existing,
        "job_id": existing["job_id"] if conflict == "job_id" else "job_fcst_gfs_2026050100_model_1_forcing_b",
        "status": "reserved",
        "idempotency_key": "gfs:gfs_2026050100:model_1:forcing",
        "candidate_id": "fcst_gfs_2026050100_model_1",
    }

    assert repository.reserve_pipeline_job(reservation) is None
    kept = repository.get_pipeline_job(existing["job_id"])
    assert kept is not None
    assert kept["status"] == "running"
    if conflict == "idempotency_key":
        assert repository.get_pipeline_job(reservation["job_id"]) is None


# --- candidate_state row scope -----------------------------------------------------------


def test_file_journal_candidate_state_scope_admits_the_candidates_rows_and_their_events(tmp_path: Path) -> None:
    """Rule half of ``test_psycopg_candidate_state_limits_jobs_and_reads_events_for_candidate_scope``.

    The SQL job read and its event subquery admit a row when its run id is the candidate's,
    when it is a model-less row of the exact cycle run, or when it names the candidate's model
    anywhere in the cycle; another model's own run is not admitted, and neither are its events.
    (Another model's row on the exact cycle run id is pinned by
    ``test_candidate_state_excludes_foreign_model_cycle_run_row_and_its_marker``.)
    """

    jobs = [
        {"job_id": "job_own_run", "run_id": "fcst_gfs_2026050100_model_b", "model_id": "model_b"},
        {"job_id": "job_cycle_run_model_less", "run_id": "cycle_gfs_2026050100", "model_id": None},
        {"job_id": "job_own_model_cohort_run", "run_id": "cycle_gfs_2026050100_forcing_b", "model_id": "model_b"},
        {"job_id": "job_foreign_own_run", "run_id": "fcst_gfs_2026050100_model_a", "model_id": "model_a"},
    ]
    repository = _journal(
        tmp_path,
        jobs=[
            {
                **job,
                "cycle_id": "gfs_2026050100",
                "job_type": "produce_forcing_array",
                "stage": "forcing",
                "status": "succeeded",
                "updated_at": f"2026-05-01T00:0{index}:00Z",
            }
            for index, job in enumerate(jobs)
        ],
        events=[
            {
                "event_id": index + 1,
                "entity_type": "pipeline_job",
                "entity_id": job["job_id"],
                "event_type": "status_change",
                "status_to": "succeeded",
                "created_at": f"2026-05-01T00:0{index}:00Z",
                "details": {},
            }
            for index, job in enumerate(jobs)
        ],
    )

    state = _state(repository, model_id="model_b", job_limit=50, event_limit=50)

    admitted = ["job_own_run", "job_cycle_run_model_less", "job_own_model_cohort_run"]
    assert [job["job_id"] for job in state["pipeline_jobs"]] == admitted
    assert state["pipeline_jobs_total"] == 3
    assert [event["entity_id"] for event in state["pipeline_events"]] == admitted
    assert state["pipeline_events_total"] == 3


def test_file_journal_candidate_state_admits_a_model_less_suffixed_cohort_row(tmp_path: Path) -> None:
    """The journal's answer where it departs from the SQL scope predicate, by design.

    The SQL candidate-state read admits a model-less row only on the EXACT cycle run id.  The
    journal attributes a model-less row of a suffixed cohort run
    (``cycle_<source>_<stamp>_<suffix>``) to every candidate unless its recorded membership
    provably excludes the candidate (#2543/#2603); a row recording no member list is
    ``unwitnessed`` and stays, with its events.
    """

    repository = _journal(
        tmp_path,
        jobs=[
            _cycle_job(
                job_id="job_cohort_state_save_qc",
                run_id="cycle_gfs_2026050100_state_save_qc_cohort",
                job_type="save_state_snapshot_array",
                stage="state_save_qc",
                status="succeeded",
            )
        ],
        events=[
            {
                "event_id": 1,
                "entity_type": "pipeline_job",
                "entity_id": "job_cohort_state_save_qc",
                "event_type": "status_change",
                "status_to": "succeeded",
                "created_at": "2026-05-01T00:01:00Z",
                "details": {},
            }
        ],
    )

    state = _state(repository, model_id="model_b")

    assert [(job["job_id"], job["cohort_membership"]) for job in state["pipeline_jobs"]] == [
        ("job_cohort_state_save_qc", "unwitnessed")
    ]
    assert [event["entity_id"] for event in state["pipeline_events"]] == ["job_cohort_state_save_qc"]
