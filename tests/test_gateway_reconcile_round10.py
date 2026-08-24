"""Round-10 accepted-submit guards: typed commit validation, retry CAS,
migration marker exactness, and cancellation receipts.
"""

from __future__ import annotations

import copy
import json
from typing import Any

import pytest

from tests.gateway_reconcile_helpers import (
    _bind_current_file_cohort,
    _file_cohort_repository,
)


@pytest.mark.parametrize("invalid_status", [None, "unknown"])
def test_round10_typed_submit_commit_rejects_unknown_status_without_write(
    tmp_path: Any,
    invalid_status: Any,
) -> None:
    from services.orchestrator.accepted_submit_identity import AcceptedSubmitTransition
    from services.orchestrator.file_orchestration_journal import FileOrchestrationJournalError

    repository = _file_cohort_repository(tmp_path, member_count=1, submit_outcome=None)
    job_id = "job_cycle_gfs_2026071200_forecast_fixture_forecast"
    key = "cycle_gfs_2026071200_forecast_fixture:forecast"
    before = copy.deepcopy(repository.get_pipeline_job(job_id))
    journal_before = tuple(
        (path, path.read_bytes()) for path in sorted(repository.root.glob("journal/**/*.jsonl"))
    )
    with pytest.raises(FileOrchestrationJournalError) as exc_info:
        repository.commit_pipeline_job_submit_attempt(
            key,
            pipeline_job_id=job_id,
            expected_submission_attempt=1,
            slurm_job_id="88101",
            transition=AcceptedSubmitTransition.accepted(status=invalid_status),
        )
    assert exc_info.value.field == "status"
    assert repository.get_pipeline_job(job_id) == before
    assert tuple(
        (path, path.read_bytes()) for path in sorted(repository.root.glob("journal/**/*.jsonl"))
    ) == journal_before


def test_round10_timeout_unverified_cancel_receipt_is_persistent_and_once_only(tmp_path: Any) -> None:
    from services.orchestrator.chain_forecast_control import cancel_active_cycle_jobs

    repository = _file_cohort_repository(tmp_path, member_count=1, submit_outcome=None)
    key = "cycle_gfs_2026071200_forecast_fixture:forecast"
    job_id = "job_cycle_gfs_2026071200_forecast_fixture_forecast"
    _bind_current_file_cohort(repository, key, slurm_job_id="99110")
    repository.transition_pipeline_job_runtime_status(
        job_id,
        "reconcile_unverified",
        expected_statuses=("submitted",),
        error_code="SLURM_JOB_TIMEOUT",
        error_message="accounting timed out",
    )
    calls: list[str] = []

    class Client:
        def cancel_job(self, slurm_job_id: str) -> dict[str, Any]:
            calls.append(slurm_job_id)
            return {"status": "cancelled", "finished_at": "2026-07-12T00:02:00Z"}

    class Harness:
        def __init__(self, current: Any) -> None:
            self.repository = current
            self.slurm_client = Client()

        def _query_pipeline_jobs_by_cycle(self, cycle_id: str) -> list[dict[str, Any]]:
            return self.repository.query_pipeline_jobs_by_cycle(cycle_id)

    reopened = type(repository)(repository.root)
    assert len(cancel_active_cycle_jobs(Harness(reopened), "gfs_2026071200")) == 1
    persisted = type(repository)(repository.root).get_pipeline_job(job_id)
    assert persisted["status"] == "reconcile_unverified"
    assert persisted["cancellation_receipt_recorded"] is True
    assert cancel_active_cycle_jobs(Harness(type(repository)(repository.root)), "gfs_2026071200") == []
    assert calls == ["99110"]


def test_round10_timeout_unverified_cancel_gateway_failure_retries_persisted_intent(
    tmp_path: Any,
) -> None:
    from services.orchestrator.chain import SlurmClientError
    from services.orchestrator.chain_forecast_control import cancel_active_cycle_jobs

    repository = _file_cohort_repository(tmp_path, member_count=1, submit_outcome=None)
    key = "cycle_gfs_2026071200_forecast_fixture:forecast"
    job_id = "job_cycle_gfs_2026071200_forecast_fixture_forecast"
    _bind_current_file_cohort(repository, key, slurm_job_id="99111")
    repository.transition_pipeline_job_runtime_status(
        job_id, "reconcile_unverified", expected_statuses=("submitted",)
    )
    attempts = 0

    class Client:
        def cancel_job(self, _slurm_job_id: str) -> dict[str, Any]:
            nonlocal attempts
            attempts += 1
            if attempts == 1:
                raise SlurmClientError("SLURM_GATEWAY_UNAVAILABLE", "cancel failed")
            return {"status": "cancelled", "finished_at": "2026-07-12T00:02:00Z"}

    class Harness:
        def __init__(self, current: Any) -> None:
            self.repository = current
            self.slurm_client = Client()

        def _query_pipeline_jobs_by_cycle(self, cycle_id: str) -> list[dict[str, Any]]:
            return self.repository.query_pipeline_jobs_by_cycle(cycle_id)

    with pytest.raises(SlurmClientError):
        cancel_active_cycle_jobs(Harness(type(repository)(repository.root)), "gfs_2026071200")
    assert type(repository)(repository.root).get_pipeline_job(job_id)["status"] == "cancellation_pending"
    assert len(cancel_active_cycle_jobs(Harness(type(repository)(repository.root)), "gfs_2026071200")) == 1
    assert attempts == 2


@pytest.mark.parametrize("invalid_attempt", [None, 0, -1, True, "1"])
def test_round10_versioned_retry_requires_exact_positive_integer_attempt_zero_write(
    tmp_path: Any,
    invalid_attempt: Any,
) -> None:
    from services.orchestrator.accepted_submit_identity import ACCEPTED_SUBMIT_CONTRACT_VERSION
    from services.orchestrator.file_orchestration_journal import FileOrchestrationJournalError

    repository = _file_cohort_repository(tmp_path, member_count=1, submit_outcome=None)
    job_id = "job_cycle_gfs_2026071200_forecast_fixture_forecast"
    current = repository.get_pipeline_job(job_id)
    before_files = {
        path.relative_to(repository.root): path.read_bytes()
        for path in repository.root.rglob("*")
        if path.is_file()
    }
    with pytest.raises(FileOrchestrationJournalError) as exc_info:
        repository.permit_pipeline_job_retry(
            job_id,
            accepted_submit_contract_version=ACCEPTED_SUBMIT_CONTRACT_VERSION,
            expected_submission_attempt=invalid_attempt,
            expected_submission_attempt_started_at=current["submission_attempt_started_at"],
        )
    assert exc_info.value.field == "expected_submission_attempt"
    assert {
        path.relative_to(repository.root): path.read_bytes()
        for path in repository.root.rglob("*")
        if path.is_file()
    } == before_files


def test_round10_versioned_retry_exact_tuple_is_once_only(tmp_path: Any) -> None:
    from services.orchestrator.accepted_submit_identity import ACCEPTED_SUBMIT_CONTRACT_VERSION

    repository = _file_cohort_repository(tmp_path, member_count=1, submit_outcome=None)
    job_id = "job_cycle_gfs_2026071200_forecast_fixture_forecast"
    current = repository.get_pipeline_job(job_id)
    kwargs = {
        "accepted_submit_contract_version": ACCEPTED_SUBMIT_CONTRACT_VERSION,
        "expected_submission_attempt_started_at": current["submission_attempt_started_at"],
    }
    assert repository.permit_pipeline_job_retry(job_id, expected_submission_attempt=2, **kwargs) == 0
    assert repository.permit_pipeline_job_retry(job_id, expected_submission_attempt=1, **kwargs) > 0
    assert repository.permit_pipeline_job_retry(job_id, expected_submission_attempt=1, **kwargs) == 0


@pytest.mark.parametrize(
    "marker",
    [
        {},
        {"schema_version": "nhms.scheduler.reconcile_inventory_migration.v1"},
        {
            "schema_version": "nhms.scheduler.reconcile_inventory_migration.v1",
            "completed_at": None,
        },
        {
            "schema_version": "nhms.scheduler.reconcile_inventory_migration.v1",
            "completed_at": "2026-07-12T00:00:00",
        },
        {
            "schema_version": "nhms.scheduler.reconcile_inventory_migration.v1",
            "completed_at": "2026-07-12T00:00:00+00:00",
        },
        {
            "schema_version": "nhms.scheduler.reconcile_inventory_migration.v1",
            "completed_at": "not-a-time",
        },
        {
            "schema_version": "nhms.scheduler.reconcile_inventory_migration.v1",
            "completed_at": "2026-07-12T00:00:00Z",
            "extra": True,
        },
    ],
)
def test_round10_migration_marker_is_exact_and_repairable(tmp_path: Any, marker: Any) -> None:

    from services.orchestrator.file_orchestration_journal import FileOrchestrationJournalError

    repository = _file_cohort_repository(tmp_path, member_count=1, with_runtime_rows=False)
    job_id = "job_cycle_gfs_2026071200_forecast_fixture_forecast"
    assert [job.job_id for job in repository.query_reserved_unbound_jobs()] == [job_id]
    marker_path = repository.root / "reconcile-inventory-migration-v1.json"
    marker_path.write_text(json.dumps(marker), encoding="utf-8")
    reopened = type(repository)(repository.root)
    with pytest.raises(FileOrchestrationJournalError):
        reopened.query_reserved_unbound_jobs()
    assert reopened._reconcile_inventory_migration_checked is False

    marker_path.unlink()
    repaired = type(repository)(repository.root)
    assert [job.job_id for job in repaired.query_reserved_unbound_jobs()] == [job_id]
    assert marker_path.is_file()


def test_round10_migration_unsafe_flat_json_fails_closed_then_recovers_after_repair(
    tmp_path: Any,
) -> None:
    from services.orchestrator.file_orchestration_journal import FileOrchestrationJournalError

    repository = _file_cohort_repository(tmp_path, member_count=1, with_runtime_rows=False)
    job_id = "job_cycle_gfs_2026071200_forecast_fixture_forecast"
    repository.query_reserved_unbound_jobs()
    marker = repository.root / "reconcile-inventory-migration-v1.json"
    marker.unlink()
    unsafe = repository.root / "pipeline-jobs" / "unsafe name.json"
    unsafe.write_text("{}", encoding="utf-8")
    with pytest.raises(FileOrchestrationJournalError):
        type(repository)(repository.root).query_reserved_unbound_jobs()
    assert not marker.exists()

    unsafe.rename(repository.root / "quarantine-unsafe-name.json")
    repaired = type(repository)(repository.root)
    assert [job.job_id for job in repaired.query_reserved_unbound_jobs()] == [job_id]
    assert marker.is_file()


@pytest.mark.parametrize("boundary", ["stat", "read"])
def test_round10_migration_disappearance_fails_closed_without_marker(
    tmp_path: Any,
    monkeypatch: pytest.MonkeyPatch,
    boundary: str,
) -> None:
    from services.orchestrator import file_orchestration_journal as journal_module
    from services.orchestrator.file_orchestration_journal import FileOrchestrationJournalError

    repository = _file_cohort_repository(tmp_path, member_count=1, with_runtime_rows=False)
    repository.query_reserved_unbound_jobs()
    marker = repository.root / "reconcile-inventory-migration-v1.json"
    marker.unlink()
    reopened = type(repository)(repository.root)
    direct = next((repository.root / "pipeline-jobs").glob("*.json"))
    if boundary == "stat":
        original = journal_module.stat_no_follow

        def disappear(path: Any, **kwargs: Any) -> Any:
            if path == direct:
                raise FileNotFoundError(path)
            return original(path, **kwargs)

        monkeypatch.setattr(journal_module, "stat_no_follow", disappear)
    else:
        original_read = reopened._read_optional_json

        def missing_read(path: Any) -> Any:
            if path == direct:
                return None
            return original_read(path)

        monkeypatch.setattr(reopened, "_read_optional_json", missing_read)
    with pytest.raises(FileOrchestrationJournalError):
        reopened.query_reserved_unbound_jobs()
    assert reopened._reconcile_inventory_migration_checked is False
    assert not marker.exists()


@pytest.mark.parametrize("surface", ["direct", "journal"])
def test_round10_invalid_current_master_status_blocks_migration_without_marker_or_anchor_loss(
    tmp_path: Any,
    surface: str,
) -> None:

    from services.orchestrator.file_orchestration_journal import FileOrchestrationJournalError

    repository = _file_cohort_repository(tmp_path, member_count=1, with_runtime_rows=False)
    job_id = "job_cycle_gfs_2026071200_forecast_fixture_forecast"
    assert [job.job_id for job in repository.query_reserved_unbound_jobs()] == [job_id]
    marker = repository.root / "reconcile-inventory-migration-v1.json"
    anchor = repository.root / "reconcile-inventory" / f"{job_id}.json"
    marker.unlink()
    if surface == "direct":
        path = repository.root / "pipeline-jobs" / f"{job_id}.json"
        record = json.loads(path.read_text(encoding="utf-8"))
        record["payload"]["status"] = "invented"
        path.write_text(json.dumps(record), encoding="utf-8")
    else:
        path = repository.root / "journal" / "gfs" / "2026071200.jsonl"
        records = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
        master_records = [
            record
            for record in records
            if record.get("record_type") == "pipeline_job"
            and record.get("payload", {}).get("job_id") == job_id
        ]
        assert master_records
        master_records[-1]["payload"]["status"] = "invented"
        path.write_text("\n".join(json.dumps(record) for record in records) + "\n", encoding="utf-8")

    reopened = type(repository)(repository.root)
    with pytest.raises(FileOrchestrationJournalError) as exc_info:
        reopened.query_reserved_unbound_jobs()
    assert exc_info.value.field == "status"
    assert reopened._reconcile_inventory_migration_checked is False
    assert not marker.exists()
    assert anchor.is_file()


def test_round10_cancellation_receipt_is_clean_false_attempt_authority(tmp_path: Any) -> None:
    from services.orchestrator.file_orchestration_journal import (
        FileOrchestrationJournalError,
        FileOrchestrationJournalRepository,
    )

    template = _file_cohort_repository(tmp_path / "template", member_count=1, submit_outcome=None)
    job_id = "job_cycle_gfs_2026071200_forecast_fixture_forecast"
    clean = dict(template.get_pipeline_job(job_id))
    assert clean["cancellation_receipt_recorded"] is False
    dirty = {**clean, "cancellation_receipt_recorded": True}
    target = FileOrchestrationJournalRepository(tmp_path / "target" / "journal")
    with pytest.raises(FileOrchestrationJournalError) as exc_info:
        target.reserve_pipeline_job(dirty)
    assert exc_info.value.field == "cancellation_receipt_recorded"
    assert target.get_pipeline_job(job_id) is None
    assert not tuple(target.root.glob("journal/**/*.jsonl"))

    before = copy.deepcopy(template.get_pipeline_job(job_id))
    with pytest.raises(FileOrchestrationJournalError) as upsert_error:
        template.upsert_pipeline_job({**before, "cancellation_receipt_recorded": True})
    assert upsert_error.value.field == "cancellation_receipt_recorded"
    assert template.get_pipeline_job(job_id) == before


def test_round10_master_status_closed_set_does_not_constrain_candidate_or_legacy(tmp_path: Any) -> None:
    from services.orchestrator.accepted_submit_identity import (
        ACCEPTED_SUBMIT_CONTRACT_VERSION,
        ACCEPTED_SUBMIT_MASTER_STATUSES,
        normalize_accepted_submit_evidence,
    )

    repository = _file_cohort_repository(tmp_path, member_count=1, submit_outcome=None)
    job_id = "job_cycle_gfs_2026071200_forecast_fixture_forecast"
    master = repository.get_pipeline_job(job_id)
    for status in ACCEPTED_SUBMIT_MASTER_STATUSES:
        assert normalize_accepted_submit_evidence({**master, "status": status})["status"] == status

    candidate = {
        "accepted_submit_contract_version": ACCEPTED_SUBMIT_CONTRACT_VERSION,
        "job_id": "job_fcst_gfs_2026071200_model_0_forecast_candidate_0",
        "run_id": "fcst_gfs_2026071200_model_0",
        "cycle_id": "gfs_2026071200",
        "job_type": "run_shud_forecast_array",
        "stage": "forecast",
        "status": "candidate_private_state",
        "model_id": "model_0",
        "array_task_id": 0,
        "candidate_id": "gfs:2026-07-12T00:00:00Z:model_0:forecast_gfs_deterministic",
        "restart_stage": "forecast",
        "native_shud_resubmitted": False,
        "submit_outcome": "accepted",
    }
    assert normalize_accepted_submit_evidence(candidate)["status"] == "candidate_private_state"
    legacy = {"status": "legacy_private_state"}
    assert normalize_accepted_submit_evidence(legacy) == legacy
