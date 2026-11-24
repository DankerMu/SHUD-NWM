"""A model's latest view carries only rows its own reader accepts (#3308).

Model B is the candidate, model A the other model.  A row naming A with the
exact cycle run id ``cycle_<source>_<stamp>`` is admitted into B's row set by
the deliberately wide candidate predicate (#1288 / #1303 D1); the latest
materializer must not copy it into B's latest view, because
``_apply_latest_view`` validates every job row against the view's model.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from packages.common.contracts.time_normalization import format_cycle_time
from services.orchestrator.file_orchestration_journal import (
    FILE_ORCHESTRATION_JOURNAL_SCHEMA_VERSION,
    FileOrchestrationJournalRepository,
)

CYCLE_TIME = datetime(2026, 5, 1, tzinfo=UTC)
STAMP = format_cycle_time(CYCLE_TIME)
CYCLE_ID = f"gfs_{STAMP}"
CYCLE_RUN_ID = f"cycle_gfs_{STAMP}"
RUN_ID_B = f"fcst_gfs_{STAMP}_model_b"
RUN_ID_A = f"fcst_gfs_{STAMP}_model_a"
NOW = datetime(2026, 5, 1, 1, tzinfo=UTC)
BLOCKED_SENTINEL = "file_journal_read_blocked"

_BASE = {
    "cycle_id": CYCLE_ID,
    "job_type": "produce_forcing_array",
    "stage": "forcing",
    "status": "succeeded",
    "submitted_at": NOW,
    "started_at": NOW,
    "finished_at": NOW,
}


def _own_b(**overrides: Any) -> dict[str, Any]:
    return {**_BASE, "job_id": "own_b", "run_id": RUN_ID_B, "model_id": "model_b", **overrides}


def _foreign_a(**overrides: Any) -> dict[str, Any]:
    return {**_BASE, "job_id": "foreign_a", "run_id": CYCLE_RUN_ID, "model_id": "model_a", **overrides}


_ACTIVE = {"status": "running", "finished_at": None, "slurm_job_id": "4242"}
_COMPLETION = {"stage": "state_save_qc", "job_type": "state_save_qc"}


def _latest_path(root: Path, model_id: str) -> Path:
    return root / "latest" / "gfs" / STAMP / f"{model_id}.json"


def _read_latest(root: Path, model_id: str = "model_b") -> dict[str, Any]:
    return json.loads(_latest_path(root, model_id).read_text(encoding="utf-8"))


def _latest_job(root: Path, model_id: str, job_id: str) -> dict[str, Any]:
    (job,) = [job for job in _read_latest(root, model_id)["pipeline_jobs"] if job["job_id"] == job_id]
    return job


def _candidate_state(repository: FileOrchestrationJournalRepository, model_id: str = "model_b") -> dict[str, Any]:
    state = repository.candidate_state(
        source_id="gfs",
        cycle_time=CYCLE_TIME,
        model_id=model_id,
        run_id=f"fcst_gfs_{STAMP}_{model_id}",
        forcing_version_id=f"forc_gfs_{STAMP}_{model_id}",
        candidate_id=f"cand_{model_id}",
    )
    assert state is not None
    return state


def _gates(repository: FileOrchestrationJournalRepository, model_id: str = "model_b") -> dict[str, Any]:
    scope = {"source_id": "gfs", "cycle_time": CYCLE_TIME, "model_id": model_id}
    return {
        "active": repository.has_active_pipeline(**scope),
        "completed": repository.has_completed_pipeline(**scope),
        "slurm": [job.get("job_id") for job in repository.active_slurm_jobs(**scope)],
    }


def _cycle_job_ids(repository: FileOrchestrationJournalRepository) -> list[str]:
    return sorted(str(job.get("job_id")) for job in repository.query_pipeline_jobs_by_cycle(CYCLE_ID))


def _append_model_less_marker(root: Path, *, entity_id: str) -> None:
    """The #1288 manual-retry marker: a model-less ``pipeline_event`` envelope."""

    record = {
        "schema_version": FILE_ORCHESTRATION_JOURNAL_SCHEMA_VERSION,
        "sequence": 90,
        "record_type": "pipeline_event",
        "source_id": "gfs",
        "cycle_time": CYCLE_TIME.isoformat(),
        "payload": {
            "event_id": 90,
            "entity_type": "pipeline_job",
            "entity_id": entity_id,
            "event_type": "retry",
            "created_at": "2026-05-01T00:05:00Z",
            "details": {"trigger": "manual", "manual_retry_marker": True, "retry_count": 1},
        },
    }
    segment = root / "pipeline-events" / "gfs" / f"{STAMP}.jsonl"
    segment.parent.mkdir(parents=True, exist_ok=True)
    with segment.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(record) + "\n")


def _write_b_latest_with(root: Path, extra_job: dict[str, Any]) -> None:
    """Hand-write B's latest view: its current content plus ``extra_job``."""

    latest = _read_latest(root)
    latest["pipeline_jobs"].append(extra_job)
    latest["replay"]["job_count"] = len(latest["pipeline_jobs"])
    _latest_path(root, "model_b").write_text(json.dumps(latest), encoding="utf-8")


def _assert_latest_self_consistent(latest: dict[str, Any]) -> None:
    assert latest["replay"]["job_count"] == len(latest["pipeline_jobs"])
    assert latest["replay"]["event_count"] == len(latest["pipeline_events"])


@pytest.fixture
def root(tmp_path: Path) -> Path:
    return tmp_path.resolve() / "journal"


# 2.1
def test_rematerializing_beside_a_foreign_cycle_run_row_keeps_the_model_readable(root: Path) -> None:
    repository = FileOrchestrationJournalRepository(root)
    repository.upsert_pipeline_job(_own_b())
    repository.upsert_pipeline_job(_foreign_a())
    repository.upsert_pipeline_job(_own_b())

    latest = _read_latest(root)
    assert [job["job_id"] for job in latest["pipeline_jobs"]] == ["own_b"]
    _assert_latest_self_consistent(latest)

    fresh = FileOrchestrationJournalRepository(root)
    assert _candidate_state(fresh).get("file_journal") is None
    assert _cycle_job_ids(fresh) == ["foreign_a", "own_b"]
    fresh.upsert_pipeline_job(_own_b(job_id="own_b2"))
    fresh.insert_pipeline_event(
        entity_type="pipeline_job",
        entity_id="own_b",
        event_type="status_change",
        status_from="running",
        status_to="succeeded",
        message="own event",
        details={},
    )
    assert "foreign_a" not in {job["job_id"] for job in _read_latest(root)["pipeline_jobs"]}
    state_a = _candidate_state(FileOrchestrationJournalRepository(root), "model_a")
    assert state_a.get("file_journal") is None
    assert "foreign_a" in {job.get("job_id") for job in state_a["pipeline_jobs"]}


# 2.2
def test_foreign_rows_and_their_pipeline_job_events_leave_the_view_together(root: Path) -> None:
    repository = FileOrchestrationJournalRepository(root)
    repository.upsert_pipeline_job(_own_b())
    repository.upsert_pipeline_job(_foreign_a())
    _append_model_less_marker(root, entity_id="foreign_a")
    FileOrchestrationJournalRepository(root).upsert_pipeline_job(_own_b())

    latest = _read_latest(root)
    assert [job["job_id"] for job in latest["pipeline_jobs"]] == ["own_b"]
    assert "foreign_a" not in {event.get("entity_id") for event in latest["pipeline_events"]}
    assert latest["replay"]["job_count"] == len(latest["pipeline_jobs"]) == 1
    assert latest["replay"]["event_count"] == len(latest["pipeline_events"])
    assert _candidate_state(FileOrchestrationJournalRepository(root)).get("file_journal") is None


# 2.3 first bullet
def test_active_foreign_cycle_run_row_stays_visible_to_the_gates_after_rematerialization(root: Path) -> None:
    repository = FileOrchestrationJournalRepository(root)
    repository.upsert_pipeline_job(_own_b())
    repository.upsert_pipeline_job(_foreign_a(**_ACTIVE))
    repository.upsert_pipeline_job(_own_b())

    fresh = FileOrchestrationJournalRepository(root)
    gates = _gates(fresh)
    assert gates["active"] is True
    assert gates["slurm"] == ["foreign_a"]
    assert _candidate_state(fresh).get("file_journal") is None


# 2.3 second bullet
def test_foreign_completion_row_does_not_complete_or_activate_after_rematerialization(root: Path) -> None:
    repository = FileOrchestrationJournalRepository(root)
    repository.upsert_pipeline_job(_own_b())
    repository.upsert_pipeline_job(_foreign_a(**_COMPLETION))
    repository.upsert_pipeline_job(_own_b())

    gates = _gates(FileOrchestrationJournalRepository(root))
    assert gates["completed"] is False
    assert gates["active"] is False


# 2.4
def test_model_less_and_own_cycle_run_rows_stay_in_the_view(root: Path) -> None:
    repository = FileOrchestrationJournalRepository(root)
    kept = {
        "cohort_exact": {"run_id": CYCLE_RUN_ID, "model_id": None},
        "cohort_suffixed": {"run_id": f"{CYCLE_RUN_ID}_cohortx", "model_id": None},
        "own_b_cycle": {"run_id": CYCLE_RUN_ID, "model_id": "model_b"},
    }
    repository.upsert_pipeline_job(_own_b())
    for job_id, identity in kept.items():
        repository.upsert_pipeline_job({**_BASE, "job_id": job_id, **identity})
    repository.upsert_pipeline_job(_own_b())

    latest = _read_latest(root)
    assert {job["job_id"] for job in latest["pipeline_jobs"]} >= set(kept)
    _assert_latest_self_consistent(latest)
    state = _candidate_state(FileOrchestrationJournalRepository(root))
    assert state.get("file_journal") is None
    assert {job.get("job_id") for job in state["pipeline_jobs"]} >= set(kept)


def _polluted(root: Path, *, direct: dict[str, Any], copy: dict[str, Any]) -> None:
    """B's latest view as the pre-#3308 writer left it, A's copy taken from a write of ``copy``."""

    scratch = root.parent / "copy-source"
    scratch_repository = FileOrchestrationJournalRepository(scratch)
    scratch_repository.upsert_pipeline_job(_foreign_a(**copy))
    foreign_copy = _latest_job(scratch, "model_a", "foreign_a")

    repository = FileOrchestrationJournalRepository(root)
    repository.upsert_pipeline_job(_own_b())
    repository.upsert_pipeline_job(_foreign_a(**direct))
    _write_b_latest_with(root, foreign_copy)


# 2.5
def test_polluted_view_is_read_and_repaired(root: Path) -> None:
    _polluted(root, direct={}, copy={})
    assert "foreign_a" in {job["job_id"] for job in _read_latest(root)["pipeline_jobs"]}

    fresh = FileOrchestrationJournalRepository(root)
    assert _candidate_state(fresh).get("file_journal") is None
    assert _cycle_job_ids(fresh) == ["foreign_a", "own_b"]
    assert _gates(fresh)["active"] is False
    fresh.upsert_pipeline_job(_own_b(job_id="own_b2"))
    assert "foreign_a" not in {job["job_id"] for job in _read_latest(root)["pipeline_jobs"]}


# 2.5b
@pytest.mark.parametrize(
    ("copy", "direct", "expected_active", "expected_slurm"),
    [
        pytest.param(_ACTIVE, {}, False, [], id="copy-running-direct-succeeded"),
        pytest.param({}, _ACTIVE, True, ["foreign_a"], id="copy-succeeded-direct-running"),
    ],
)
def test_polluted_copy_is_ignored_not_trusted(
    root: Path,
    copy: dict[str, Any],
    direct: dict[str, Any],
    expected_active: bool,
    expected_slurm: list[str],
) -> None:
    _polluted(root, direct=direct, copy=copy)

    fresh = FileOrchestrationJournalRepository(root)
    gates = _gates(fresh)
    assert gates["active"] is expected_active
    assert gates["slurm"] == expected_slurm
    assert _candidate_state(fresh).get("file_journal") is None


# 2.6
@pytest.mark.parametrize(
    ("overrides", "expected_reason", "expected_field"),
    [
        pytest.param({"run_id": f"{CYCLE_RUN_ID}_cohortx"}, "file_journal_model_mismatch", "model_id", id="suffixed"),
        pytest.param({"run_id": RUN_ID_A}, "file_journal_model_mismatch", "model_id", id="a-forecast-run"),
        pytest.param({"run_id": RUN_ID_B}, "file_journal_model_mismatch", "model_id", id="b-forecast-run"),
        pytest.param(
            {"cycle_id": "gfs_2026050106"}, "file_journal_cycle_id_mismatch", "cycle_id", id="exact-wrong-cycle-id"
        ),
    ],
)
def test_reader_stays_strict_for_every_other_foreign_row(
    root: Path,
    overrides: dict[str, Any],
    expected_reason: str,
    expected_field: str,
) -> None:
    repository = FileOrchestrationJournalRepository(root)
    repository.upsert_pipeline_job(_own_b())
    repository.upsert_pipeline_job(_foreign_a())
    _write_b_latest_with(root, {**_latest_job(root, "model_a", "foreign_a"), **overrides})

    fresh = FileOrchestrationJournalRepository(root)
    blocker = _candidate_state(fresh).get("file_journal")
    assert blocker is not None
    assert (blocker["reason"], blocker["field"]) == (expected_reason, expected_field)
    assert _cycle_job_ids(fresh) == [BLOCKED_SENTINEL]
