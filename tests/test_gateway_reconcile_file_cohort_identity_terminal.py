"""File-cohort runtime identity: batch projection bounds and terminal identity.

Partition of ``tests/test_gateway_reconcile_file_cohort_identity.py`` (#3177):
batch projection bounds, the one-snapshot terminal runtime identity, non-forecast
cohorts, and the #1795 exact terminal reason tokens.
"""

from __future__ import annotations

import json
from datetime import (
    UTC,
    datetime,
)
from typing import Any

import pytest

from tests.gateway_reconcile_helpers import _bind_current_file_cohort, _file_cohort_repository


@pytest.mark.parametrize("member_count", [18, 64, 128, 256])
def test_file_cohort_batch_projection_bounds_lock_append_and_materialization(
    tmp_path: Any,
    monkeypatch: pytest.MonkeyPatch,
    member_count: int,
) -> None:
    repository = _file_cohort_repository(
        tmp_path / str(member_count), member_count=member_count, with_runtime_rows=False
    )
    key = "cycle_gfs_2026071200_forecast_fixture:forecast"
    _bind_current_file_cohort(repository, key, slurm_job_id="17667")
    calls = {
        "lock": 0,
        "append": 0,
        "materialize": 0,
        "event_scan": 0,
        "sequence_scan": 0,
        "read_jsonl": 0,
        "latest_enumerations": 0,
        "latest_paths_returned": 0,
    }
    original_lock = repository._locked_cycle_write
    original_append = repository._append_journal_records_unlocked
    original_materialize = repository._materialize_latest_unlocked
    original_event_scan = repository._next_event_id_unlocked
    original_sequence_scan = repository._next_sequence_unlocked
    original_read_jsonl = repository._read_jsonl
    original_latest_paths = repository._latest_paths

    def counted_lock(**kwargs: Any) -> Any:
        calls["lock"] += 1
        return original_lock(**kwargs)

    def counted_append(**kwargs: Any) -> Any:
        calls["append"] += 1
        return original_append(**kwargs)

    def counted_materialize(**kwargs: Any) -> Any:
        calls["materialize"] += 1
        return original_materialize(**kwargs)

    def counted_event_scan(**kwargs: Any) -> Any:
        calls["event_scan"] += 1
        return original_event_scan(**kwargs)

    def counted_sequence_scan(**kwargs: Any) -> Any:
        calls["sequence_scan"] += 1
        return original_sequence_scan(**kwargs)

    def counted_read_jsonl(path: Any, *, segment_index: int = 0) -> Any:
        calls["read_jsonl"] += 1
        return original_read_jsonl(path, segment_index=segment_index)

    def counted_latest_paths(*args: Any, **kwargs: Any) -> Any:
        calls["latest_enumerations"] += 1
        paths = original_latest_paths(*args, **kwargs)
        calls["latest_paths_returned"] += len(paths)
        return paths

    monkeypatch.setattr(repository, "_locked_cycle_write", counted_lock)
    monkeypatch.setattr(repository, "_append_journal_records_unlocked", counted_append)
    monkeypatch.setattr(repository, "_materialize_latest_unlocked", counted_materialize)
    monkeypatch.setattr(repository, "_next_event_id_unlocked", counted_event_scan)
    monkeypatch.setattr(repository, "_next_sequence_unlocked", counted_sequence_scan)
    monkeypatch.setattr(repository, "_read_jsonl", counted_read_jsonl)
    monkeypatch.setattr(repository, "_latest_paths", counted_latest_paths)
    projections = [
        {
            "candidate_id": f"gfs:2026-07-12T00:00:00Z:model_{index}:forecast_gfs_deterministic",
            "run_id": f"fcst_gfs_2026071200_model_{index}",
            "model_id": f"model_{index}",
            "array_task_id": index,
            "array_task_outcome": "succeeded",
            "task_slurm_job_id": f"17667_{index}",
            "restart_stage": "state_save_qc",
            "native_shud_resubmitted": False,
        }
        for index in range(member_count)
    ]

    result = repository.project_forecast_cohort_tasks(
        "job_cycle_gfs_2026071200_forecast_fixture_forecast",
        master_slurm_job_id="17667",
        projections=projections,
        complete=True,
        master_status="succeeded",
        master_error_code=None,
        reconciliation_decision="matched_bound",
    )

    assert result["total"] == (2 * member_count) + 2
    assert calls["lock"] == 1
    assert calls["append"] == 1
    assert calls["materialize"] == member_count
    # Accepted-submit projection must not fall back to the generic event scan.
    assert calls["event_scan"] == 0
    assert calls["sequence_scan"] == 2
    # Filesystem implementations may perform a small platform-specific number
    # of descriptor reads, but work stays constant as the cohort scales.
    assert calls["read_jsonl"] <= 16
    assert calls["latest_enumerations"] <= 2
    assert calls["latest_paths_returned"] == 0

    latest_files = sorted((repository.root / "latest" / "gfs" / "2026071200").glob("*.json"))
    assert len(latest_files) == member_count
    assert sum(path.stat().st_size for path in latest_files) < member_count * 8_000
    master_job_id = "job_cycle_gfs_2026071200_forecast_fixture_forecast"
    assert all(
        master_job_id not in {job["job_id"] for job in json.loads(path.read_text())["pipeline_jobs"]}
        for path in latest_files
    )

    from services.orchestrator.file_orchestration_journal import FileOrchestrationJournalRepository

    root = repository.root
    journal_records = [
        json.loads(line)
        for path in sorted(root.rglob("*.jsonl"))
        for line in path.read_text(encoding="utf-8").splitlines()
    ]
    event_ids = [
        int(record["payload"]["event_id"])
        for record in journal_records
        if record["record_type"] == "pipeline_event"
    ]
    assert len(event_ids) == member_count + 1
    assert len(event_ids) == len(set(event_ids))
    assert event_ids == list(range(event_ids[0], event_ids[0] + member_count + 1))

    reopened = FileOrchestrationJournalRepository(root)
    for index in (0, member_count - 1):
        candidate_job_id = f"job_fcst_gfs_2026071200_model_{index}_forecast_reconciled_17667_{index}"
        direct_path = (
            root
            / "pipeline-jobs"
            / "by-cycle"
            / "gfs"
            / "2026071200"
            / f"{candidate_job_id}.json"
        )
        direct_payload = json.loads(direct_path.read_text(encoding="utf-8"))["payload"]
        replayed = reopened.get_pipeline_job(candidate_job_id)
        latest_path = root / "latest" / "gfs" / "2026071200" / f"model_{index}.json"
        latest = json.loads(latest_path.read_text(encoding="utf-8"))
        latest_payload = next(job for job in latest["pipeline_jobs"] if job["job_id"] == candidate_job_id)
        assert direct_payload == replayed == latest_payload
        assert direct_payload["accepted_submit_contract_version"] == "nhms.accepted_submit.v1"

    if member_count == 256:
        partition = root / "pipeline-jobs" / "by-cycle" / "gfs" / "2026071200"
        assert len(tuple(partition.glob("*.json"))) == 256
        assert len(tuple((root / "pipeline-jobs").glob("*.json"))) == 1
        seed = next(partition.glob("*.json")).read_bytes()
        for history_index in range(300):
            history = root / "pipeline-jobs" / "by-cycle" / "gfs" / f"2025{history_index:06d}"
            history.mkdir(parents=True)
            (history / "historical.json").write_bytes(seed)
        bounded = FileOrchestrationJournalRepository(root, max_files=512)
        assert len(list(bounded._iter_direct_pipeline_job_records())) == 1
        assert bounded.get_pipeline_job(
            "job_fcst_gfs_2026071200_model_255_forecast_reconciled_17667_255"
        )["status"] == "succeeded"
        current = list(
            bounded._iter_direct_pipeline_job_records_for_cycle(
                source_id="gfs",
                cycle_time=datetime(2026, 7, 12, tzinfo=UTC),
                model_id=None,
            )
        )
        assert len(current) == 256
        assert all(job.get("model_id") not in (None, "") for job in current)
        queried = bounded.query_pipeline_jobs_by_cycle("gfs_2026071200")
        assert len(queried) == 257
        assert all(job["job_id"] != "file_journal_read_blocked" for job in queried)
        assert bounded.query_inflight_jobs() == []

    before_replay = {
        str(path.relative_to(root)): path.read_bytes()
        for path in root.rglob("*")
        if path.is_file() and path.suffix in {".json", ".jsonl"}
    }
    second = reopened.project_forecast_cohort_tasks(
        "job_cycle_gfs_2026071200_forecast_fixture_forecast",
        master_slurm_job_id="17667",
        projections=projections,
        complete=True,
        master_status="succeeded",
        master_error_code=None,
        reconciliation_decision="matched_bound",
    )
    after_replay = {
        str(path.relative_to(root)): path.read_bytes()
        for path in root.rglob("*")
        if path.is_file() and path.suffix in {".json", ".jsonl"}
    }
    assert second == {"total": 0, "pipeline_status": 0, "pipeline_event": 0}
    assert after_replay == before_replay


@pytest.mark.parametrize("member_count", [18, 256])
def test_terminal_runtime_identity_uses_one_cycle_snapshot_from_reconcile_entry(
    tmp_path: Any,
    monkeypatch: pytest.MonkeyPatch,
    member_count: int,
) -> None:
    from services.orchestrator.file_orchestration_journal import _CycleRows
    from services.orchestrator.reconcile import SacctRecord, reconcile_inflight_jobs

    repository = _file_cohort_repository(
        tmp_path / str(member_count), member_count=member_count, with_runtime_rows=False
    )
    key = "cycle_gfs_2026071200_forecast_fixture:forecast"
    _bind_current_file_cohort(repository, key, slurm_job_id="17667")
    identity = repository.get_pipeline_job("job_cycle_gfs_2026071200_forecast_fixture_forecast")
    calls = {"read_jsonl": 0, "latest_enumerations": 0, "batch_snapshots": 0}
    original_read_jsonl = repository._read_jsonl
    original_latest_paths = repository._latest_paths

    def counted_read_jsonl(path: Any, *, segment_index: int = 0) -> Any:
        calls["read_jsonl"] += 1
        return original_read_jsonl(path, segment_index=segment_index)

    def counted_latest_paths(*args: Any, **kwargs: Any) -> Any:
        calls["latest_enumerations"] += 1
        return original_latest_paths(*args, **kwargs)

    def batch_rows(
        *,
        source_id: str,
        cycle_time: datetime,
        model_ids: Any,
    ) -> dict[str, Any]:
        calls["batch_snapshots"] += 1
        assert source_id == "gfs"
        assert cycle_time == datetime(2026, 7, 12, tzinfo=UTC)
        requested = list(model_ids)
        assert len(requested) == member_count
        members = {str(member["model_id"]): member for member in identity["cohort_members"]}
        return {
            model_id: _CycleRows(
                hydro_run={
                    **members[model_id],
                    "source_id": "gfs",
                    "cycle_time": "2026-07-12T00:00:00Z",
                    "submission_attempt": 1,
                }
            )
            for model_id in requested
        }

    monkeypatch.setattr(repository, "_read_jsonl", counted_read_jsonl)
    monkeypatch.setattr(repository, "_latest_paths", counted_latest_paths)
    monkeypatch.setattr(repository, "_cycle_rows_by_model_unlocked", batch_rows)
    monkeypatch.setattr(
        repository,
        "_hydro_run_for",
        lambda *_args, **_kwargs: pytest.fail("runtime identity must not scan one member at a time"),
    )
    record = SacctRecord("17667", "RUNNING", "nhms_forecast", comment=f"nhms_idem:{key}")

    outcome = reconcile_inflight_jobs(repository, sacct_query=lambda _job_id: record)[0]

    assert outcome.action == "still_running"
    assert calls["read_jsonl"] <= 8
    assert calls["latest_enumerations"] == 0
    assert calls["batch_snapshots"] == 1


def test_non_forecast_file_cohort_terminal_reconcile_never_projects_forecast_success(
    tmp_path: Any,
) -> None:
    from services.orchestrator.file_orchestration_journal import FileOrchestrationJournalRepository
    from services.orchestrator.reconcile import SacctRecord, reconcile_inflight_jobs

    repository = FileOrchestrationJournalRepository(tmp_path / "journal")
    key = "cycle_gfs_2026071200_forcing_fixture:forcing"
    repository.reserve_pipeline_job(
        {
            "job_id": "job_cycle_gfs_2026071200_forcing_fixture_forcing",
            "run_id": "cycle_gfs_2026071200_forcing_fixture",
            "cycle_id": "gfs_2026071200",
            "job_type": "produce_forcing_array",
            "stage": "forcing",
            "idempotency_key": key,
            # Simulate a stale/pre-fix row carrying fields that #1112 must
            # ignore outside the canonical forecast family.
            "cohort_members": [
                {
                    "array_task_id": 0,
                    "candidate_id": "gfs:2026-07-12T00:00:00Z:model_0:forecast_gfs_deterministic",
                    "run_id": "fcst_gfs_2026071200_model_0",
                    "model_id": "model_0",
                    "basin_id": "basin_0",
                    "restart_stage": "forcing",
                }
            ],
        }
    )
    repository.bind_pipeline_job_reservation(key, slurm_job_id="18001")
    record = SacctRecord(
        slurm_job_id="18001",
        raw_state="COMPLETED",
        job_name="nhms_forcing",
        array_member_job_ids=("18001_0",),
        array_task_records=(
            SacctRecord("18001_0", "COMPLETED", "nhms_forcing", array_task_id=0),
        ),
    )

    outcomes = reconcile_inflight_jobs(repository, sacct_query=lambda _job_id: record)

    assert outcomes[0].status == "succeeded"
    forcing = repository.get_pipeline_job("job_cycle_gfs_2026071200_forcing_fixture_forcing")
    assert forcing["candidate_projections"] == []
    assert forcing["restart_stage"] is None
    jobs = repository.query_pipeline_jobs_by_cycle("gfs_2026071200")
    assert all(job["job_type"] != "run_shud_forecast_array" for job in jobs)
    assert all(job.get("restart_stage") != "state_save_qc" for job in jobs)


# ---------------------------------------------------------------------------
# #1795: terminal file-cohort identity blocks carry one stable clause-level
# reason token while preserving action, status, and zero durable writes.
# ---------------------------------------------------------------------------


def _terminal_validator_direct(identity: dict[str, Any], record: Any) -> tuple[bool, str | None]:
    """Call the terminal validator with a fake store whose runtime identity gate
    is always true, isolating the durable-unreachable reason sites."""
    from types import SimpleNamespace

    from services.orchestrator.reconcile import _terminal_file_cohort_identity_matches

    store = SimpleNamespace(
        forecast_cohort_runtime_identity_matches=lambda _identity: True,
    )
    job = SimpleNamespace(**identity)
    return _terminal_file_cohort_identity_matches(store, record, job)


@pytest.mark.parametrize(
    ("mutate", "expected_reason"),
    [
        # Split folded validity/runtime predicates: distinct tokens.
        ({"identity_invalid": True}, "cohort_identity_invalid"),
        ({"break_runtime_rows": True}, "runtime_identity_mismatch"),
        # Reconcile-side gates.
        ({"master_id": "99999"}, "master_id_mismatch"),
        ({"comment": "nhms_idem:wrong"}, "comment_mismatch"),
        ({"stage": "forcing"}, "stage_family_mismatch"),
        ({"user": None, "account": None}, "ownership_unproven"),
        ({"user": "wrong-user"}, "ownership_user_mismatch"),
        ({"account": "wrong-account"}, "ownership_account_mismatch"),
        # Task-accounting gates.
        ({"member_ids": "unparsable"}, "cohort_members_unparsable"),
        ({"task_array_comment": "nhms_idem:wrong"}, "task_comment_mismatch"),
        ({"task_job_name": "not_nhms"}, "task_job_name_mismatch"),
        ({"task_mapping": "foreign"}, "task_mapping_mismatch"),
        ({"task_id": "unparsable"}, "task_id_unparsable"),
        ({"task_identity_mismatch": True}, "task_identity_values_mismatch"),
        ({"task_identity_unparsable": True}, "task_identity_values_unparsable"),
    ],
    ids=[
        "cohort_identity_invalid",
        "runtime_identity_mismatch",
        "master_id_mismatch",
        "comment_mismatch",
        "stage_family_mismatch",
        "ownership_unproven",
        "ownership_user_mismatch",
        "ownership_account_mismatch",
        "cohort_members_unparsable",
        "task_comment_mismatch",
        "task_job_name_mismatch",
        "task_mapping_mismatch",
        "task_id_unparsable",
        "task_identity_values_mismatch",
        "task_identity_values_unparsable",
    ],
)
def test_file_cohort_terminal_identity_reason_tokens_are_exact_and_zero_write(
    tmp_path: Any,
    monkeypatch: pytest.MonkeyPatch,
    mutate: dict[str, Any],
    expected_reason: str,
) -> None:
    from services.orchestrator.reconcile import SacctRecord, reconcile_inflight_jobs

    if mutate.get("identity_invalid") or mutate.get("member_ids") == "unparsable":
        # These two sites are unreachable through a durable journal (the write
        # boundary refuses a corrupt digest / malformed member map, and the
        # validity gate already proves member parseability), so they are
        # exercised directly at the validator seam with a validly-shaped record.
        from tests.gateway_reconcile_helpers import _versioned_master_reservation_record

        record_template = _versioned_master_reservation_record(
            member_count=18,
            expected_user="scheduler-user",
            expected_account="scheduler-account",
        )
        if mutate.get("identity_invalid"):
            record_template["cohort_digest"] = "0" * 64
        else:
            # Malformed member map: validity (patched below) passes but the
            # member_ids comprehension raises, hitting the defensive
            # ``cohort_members_unparsable`` site.
            record_template["cohort_members"] = [{"array_task_id": "not-an-int"}]
            monkeypatch.setattr(
                "services.orchestrator.reconcile_inflight.forecast_cohort_identity_is_valid",
                lambda _identity: True,
            )
        record_template["slurm_job_id"] = "17667"
        tasks = tuple(
            SacctRecord(f"17667_{index}", "COMPLETED", "nhms_forecast", array_task_id=index)
            for index in range(18)
        )
        sacct = SacctRecord(
            "17667",
            "COMPLETED",
            "nhms_forecast",
            comment=f"nhms_idem:{record_template['idempotency_key']}",
            user="scheduler-user",
            account="scheduler-account",
            array_member_job_ids=tuple(task.slurm_job_id for task in tasks),
            array_task_records=tasks,
        )
        verdict, reason = _terminal_validator_direct(record_template, sacct)
        assert verdict is False
        assert reason == expected_reason
        return
    repository = _file_cohort_repository(
        tmp_path / expected_reason,
        expected_user="scheduler-user",
        expected_account="scheduler-account",
    )
    key = "cycle_gfs_2026071200_forecast_fixture:forecast"
    job_id = "job_cycle_gfs_2026071200_forecast_fixture_forecast"
    _bind_current_file_cohort(repository, key, slurm_job_id="17667")
    before = repository.get_pipeline_job(job_id)

    tasks = tuple(
        SacctRecord(
            f"17667_{index}",
            "COMPLETED",
            "nhms_forecast",
            comment=f"nhms_idem:{key}",
            array_task_id=index,
        )
        for index in range(18)
    )
    record = SacctRecord(
        "17667",
        "COMPLETED",
        "nhms_forecast",
        comment=f"nhms_idem:{key}",
        user="scheduler-user",
        account="scheduler-account",
        array_member_job_ids=tuple(task.slurm_job_id for task in tasks),
        array_task_records=tasks,
    )

    if mutate.get("break_runtime_rows"):
        monkeypatch.setattr(
            repository,
            "forecast_cohort_runtime_identity_matches",
            lambda _identity: False,
        )
    if mutate.get("master_id"):
        record = SacctRecord(**{**record.__dict__, "slurm_job_id": mutate["master_id"]})
    if mutate.get("comment"):
        record = SacctRecord(**{**record.__dict__, "comment": mutate["comment"]})
    if mutate.get("stage"):
        record = SacctRecord(**{**record.__dict__, "stage": mutate["stage"]})
    if "user" in mutate or "account" in mutate:
        record = SacctRecord(
            **{
                **record.__dict__,
                "user": mutate.get("user", record.user),
                "account": mutate.get("account", record.account),
            }
        )
    if mutate.get("task_array_comment"):
        broken = list(record.array_task_records)
        broken[0] = SacctRecord(
            broken[0].slurm_job_id,
            "COMPLETED",
            "nhms_forecast",
            comment=mutate["task_array_comment"],
            array_task_id=0,
        )
        record = SacctRecord(**{**record.__dict__, "array_task_records": tuple(broken)})
    if mutate.get("task_job_name"):
        broken = list(record.array_task_records)
        broken[0] = SacctRecord(
            broken[0].slurm_job_id,
            "COMPLETED",
            "not_nhms",
            comment=f"nhms_idem:{key}",
            array_task_id=0,
        )
        record = SacctRecord(**{**record.__dict__, "array_task_records": tuple(broken)})
    if mutate.get("task_mapping") == "foreign":
        broken = list(record.array_task_records)
        broken[0] = SacctRecord(
            "17667_99",
            "COMPLETED",
            "nhms_forecast",
            comment=f"nhms_idem:{key}",
            array_task_id=99,
        )
        record = SacctRecord(**{**record.__dict__, "array_task_records": tuple(broken)})
    if mutate.get("task_id") == "unparsable":
        broken = list(record.array_task_records)
        broken[0] = SacctRecord(
            "17667_0",
            "COMPLETED",
            "nhms_forecast",
            comment=f"nhms_idem:{key}",
            array_task_id="unparsable",
        )
        record = SacctRecord(**{**record.__dict__, "array_task_records": tuple(broken)})
    if mutate.get("task_identity_mismatch"):
        broken = list(record.array_task_records)
        broken[0] = SacctRecord(
            "17667_0",
            "COMPLETED",
            "nhms_forecast",
            comment=f"nhms_idem:{key}",
            array_task_id=0,
            task_id=1,
        )
        record = SacctRecord(**{**record.__dict__, "array_task_records": tuple(broken)})
    if mutate.get("task_identity_unparsable"):
        broken = list(record.array_task_records)
        broken[0] = SacctRecord(
            "17667_0",
            "COMPLETED",
            "nhms_forecast",
            comment=f"nhms_idem:{key}",
            array_task_id=0,
            task_id="bad",
        )
        record = SacctRecord(**{**record.__dict__, "array_task_records": tuple(broken)})

    outcome = reconcile_inflight_jobs(repository, sacct_query=lambda _job_id: record)[0]

    assert outcome.action == "identity_mismatch_blocked"
    assert outcome.status == "submitted"
    assert outcome.reconciliation_reason_class == expected_reason
    assert outcome.durable_write_count == 0
    assert repository.get_pipeline_job(job_id) == before
    assert len(repository.query_pipeline_jobs_by_cycle("gfs_2026071200")) == 1


def test_terminal_genuine_runtime_mismatch_reason_without_monkeypatched_validator(
    tmp_path: Any,
) -> None:
    """#1850: a genuine runtime identity mismatch surfaces through the real
    runtime validator (no monkeypatched validator may be the sole token
    oracle). A durable cohort whose per-model ``hydro_run`` rows are absent
    reports ``runtime_identity_mismatch`` on the terminal lane, action
    ``identity_mismatch_blocked``, zero durable writes."""
    from services.orchestrator.reconcile import SacctRecord, reconcile_inflight_jobs

    repository = _file_cohort_repository(
        tmp_path,
        member_count=1,
        with_runtime_rows=False,
        expected_user="scheduler-user",
        expected_account="scheduler-account",
    )
    key = "cycle_gfs_2026071200_forecast_fixture:forecast"
    job_id = "job_cycle_gfs_2026071200_forecast_fixture_forecast"
    _bind_current_file_cohort(repository, key, slurm_job_id="17667")
    before = repository.get_pipeline_job(job_id)

    tasks = (
        SacctRecord(
            "17667_0",
            "COMPLETED",
            "nhms_forecast",
            comment=f"nhms_idem:{key}",
            array_task_id=0,
        ),
    )
    record = SacctRecord(
        "17667",
        "COMPLETED",
        "nhms_forecast",
        comment=f"nhms_idem:{key}",
        user="scheduler-user",
        account="scheduler-account",
        array_member_job_ids=("17667_0",),
        array_task_records=tasks,
    )

    outcome = reconcile_inflight_jobs(repository, sacct_query=lambda _job_id: record)[0]

    assert outcome.action == "identity_mismatch_blocked"
    assert outcome.status == "submitted"
    assert outcome.reconciliation_reason_class == "runtime_identity_mismatch"
    assert outcome.durable_write_count == 0
    assert repository.get_pipeline_job(job_id) == before
