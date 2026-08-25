"""File-cohort runtime identity: projection identity gates, stale-layout
tolerance (#1749), and batch projection bounds.
"""

from __future__ import annotations

import json
from datetime import (
    UTC,
    datetime,
)
from typing import Any

import pytest

from services.orchestrator.reconcile import (
    SacctRecord,
    reconcile_inflight_jobs,
)
from tests.gateway_reconcile_helpers import (
    _append_cohort_placeholders,
    _bind_current_file_cohort,
    _file_cohort_repository,
    _versioned_master_reservation_record,
)


def test_file_cohort_task_identity_errors_block_every_projection(tmp_path: Any) -> None:
    from services.orchestrator.reconcile import SacctRecord, reconcile_inflight_jobs

    repository = _file_cohort_repository(tmp_path)
    key = "cycle_gfs_2026071200_forecast_fixture:forecast"
    job_id = "job_cycle_gfs_2026071200_forecast_fixture_forecast"
    _bind_current_file_cohort(repository, key, slurm_job_id="17667")
    tasks = [
        SacctRecord(
            f"17667_{index}",
            "RUNNING" if index == 7 else ("FAILED" if index % 2 else "COMPLETED"),
            "nhms_forecast",
            comment=f"nhms_idem:{key}",
            array_task_id=index,
        )
        for index in range(18)
        if index != 5
    ]
    tasks.append(
        SacctRecord("17667_6.batch", "COMPLETED", "nhms_forecast", array_task_id=6)
    )
    tasks.append(
        SacctRecord("17667_99", "COMPLETED", "nhms_forecast", array_task_id=99)
    )
    master = SacctRecord(
        "17667",
        "COMPLETED",
        "nhms_forecast",
        comment=f"nhms_idem:{key}",
        array_member_job_ids=tuple(task.slurm_job_id for task in tasks),
        array_task_records=tuple(tasks),
    )

    outcome = reconcile_inflight_jobs(repository, sacct_query=lambda _job_id: master)[0]
    assert outcome.action == "identity_mismatch_blocked"
    assert outcome.durable_write_count == 0
    assert repository.get_pipeline_job(job_id)["candidate_projections"] == []


@pytest.mark.parametrize(
    "updates",
    [
        {"comment": "nhms_idem:wrong"},
        {"slurm_job_id": "99999"},
        {"stage": "forcing"},
        {"user": "wrong-user"},
        {"account": "wrong-account"},
        {"comment": None, "user": "wrong-user"},
        {"comment": None, "account": "wrong-account"},
    ],
)
def test_file_cohort_terminal_identity_mismatch_never_projects(
    tmp_path: Any,
    updates: dict[str, Any],
) -> None:
    from services.orchestrator.reconcile import SacctRecord, reconcile_inflight_jobs

    repository = _file_cohort_repository(
        tmp_path,
        expected_user="scheduler-user",
        expected_account="scheduler-account",
    )
    key = "cycle_gfs_2026071200_forecast_fixture:forecast"
    job_id = "job_cycle_gfs_2026071200_forecast_fixture_forecast"
    _bind_current_file_cohort(repository, key, slurm_job_id="17667")
    before = repository.get_pipeline_job(job_id)
    tasks = tuple(
        SacctRecord(f"17667_{index}", "COMPLETED", "nhms_forecast", array_task_id=index)
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
    mismatch = SacctRecord(**{**record.__dict__, **updates})

    outcome = reconcile_inflight_jobs(repository, sacct_query=lambda _job_id: mismatch)[0]

    assert outcome.action == "identity_mismatch_blocked"
    assert outcome.durable_write_count == 0
    assert repository.get_pipeline_job(job_id) == before
    assert len(repository.query_pipeline_jobs_by_cycle("gfs_2026071200")) == 1


def test_file_cohort_terminal_projects_when_accounting_stores_no_comment(
    tmp_path: Any,
) -> None:
    """Clusters without ``AccountingStoreFlags=job_comment`` report an empty
    sacct Comment; ownership + master-id + task-bijection identity must still
    reconcile the crashed cohort master instead of blocking forever."""
    from services.orchestrator.reconcile import SacctRecord, reconcile_inflight_jobs

    repository = _file_cohort_repository(
        tmp_path,
        expected_user="scheduler-user",
        expected_account="scheduler-account",
    )
    key = "cycle_gfs_2026071200_forecast_fixture:forecast"
    job_id = "job_cycle_gfs_2026071200_forecast_fixture_forecast"
    _bind_current_file_cohort(repository, key, slurm_job_id="17667")
    tasks = tuple(
        SacctRecord(f"17667_{index}", "COMPLETED", "nhms_forecast", array_task_id=index)
        for index in range(18)
    )
    record = SacctRecord(
        "17667",
        "COMPLETED",
        "nhms_forecast",
        comment=None,
        user="scheduler-user",
        account="scheduler-account",
        array_member_job_ids=tuple(task.slurm_job_id for task in tasks),
        array_task_records=tasks,
    )

    outcome = reconcile_inflight_jobs(repository, sacct_query=lambda _job_id: record)[0]

    assert outcome.action == "terminal"
    assert outcome.status == "succeeded"
    cohort = repository.get_pipeline_job(job_id)
    assert cohort["status"] == "succeeded"
    assert all(
        projection["array_task_outcome"] == "succeeded"
        for projection in cohort["candidate_projections"]
    )


def test_file_cohort_terminal_projects_when_hydro_run_rows_lack_planning_identity(
    tmp_path: Any,
) -> None:
    """The chain per-model trigger writes ``hydro_run`` rows without
    ``candidate_id``/``basin_id``/``array_task_id`` (all ``None`` — the run
    manifest and run context carry none of them). Absent means "not stored",
    not "different job": a fully proven sacct terminal must project
    ``matched_bound`` instead of wedging on ``identity_mismatch_blocked``
    forever (IFS 2026071100 incident, node-22, 2026-08-03)."""
    from services.orchestrator.reconcile import SacctRecord, reconcile_inflight_jobs

    repository = _file_cohort_repository(tmp_path, with_runtime_rows=False)
    _append_cohort_placeholders(
        repository,
        common_updates={"candidate_id": None, "basin_id": None, "array_task_id": None},
    )
    key = "cycle_gfs_2026071200_forecast_fixture:forecast"
    job_id = "job_cycle_gfs_2026071200_forecast_fixture_forecast"
    _bind_current_file_cohort(repository, key, slurm_job_id="17667")
    tasks = tuple(
        SacctRecord(f"17667_{index}", "COMPLETED", "nhms_forecast", array_task_id=index)
        for index in range(18)
    )
    record = SacctRecord(
        "17667",
        "COMPLETED",
        "nhms_forecast",
        comment=f"nhms_idem:{key}",
        array_member_job_ids=tuple(task.slurm_job_id for task in tasks),
        array_task_records=tasks,
    )

    outcome = reconcile_inflight_jobs(repository, sacct_query=lambda _job_id: record)[0]

    assert outcome.action == "terminal"
    assert outcome.status == "succeeded"
    cohort = repository.get_pipeline_job(job_id)
    assert cohort["status"] == "succeeded"
    assert cohort["reconciliation_decision"] == "matched_bound"
    assert all(
        projection["array_task_outcome"] == "succeeded"
        for projection in cohort["candidate_projections"]
    )


@pytest.mark.parametrize(
    "field_updates",
    [
        {"candidate_id": "foreign-candidate"},
        {"basin_id": "foreign-basin"},
    ],
)
def test_file_cohort_present_but_different_runtime_identity_still_blocks(
    tmp_path: Any,
    field_updates: dict[str, Any],
) -> None:
    """Absent-field degrade must not weaken the present case: a ``hydro_run``
    row that *claims* a planning identity differing from the cohort member
    stays ``identity_mismatch_blocked`` with zero durable writes."""
    from services.orchestrator.reconcile import SacctRecord, reconcile_inflight_jobs

    repository = _file_cohort_repository(tmp_path, with_runtime_rows=False)
    _append_cohort_placeholders(repository, updates_by_index={0: field_updates})
    key = "cycle_gfs_2026071200_forecast_fixture:forecast"
    job_id = "job_cycle_gfs_2026071200_forecast_fixture_forecast"
    _bind_current_file_cohort(repository, key, slurm_job_id="17667")
    before = repository.get_pipeline_job(job_id)
    tasks = tuple(
        SacctRecord(f"17667_{index}", "COMPLETED", "nhms_forecast", array_task_id=index)
        for index in range(18)
    )
    record = SacctRecord(
        "17667",
        "COMPLETED",
        "nhms_forecast",
        comment=f"nhms_idem:{key}",
        array_member_job_ids=tuple(task.slurm_job_id for task in tasks),
        array_task_records=tasks,
    )

    outcome = reconcile_inflight_jobs(repository, sacct_query=lambda _job_id: record)[0]

    assert outcome.action == "identity_mismatch_blocked"
    assert outcome.durable_write_count == 0
    assert repository.get_pipeline_job(job_id) == before


# ---------------------------------------------------------------------------
# #1749: cohort runtime identity is scoped off the array layout.
#
# ``hydro_run.array_task_id`` is the index a member occupied in the array
# submission that *created* its row, and ``_write_hydro_run`` freezes that row
# unless it is ``failed``/``cancelled``. Whenever a cohort's member set changes
# the indices are renumbered, so surviving rows carry an index from the old
# layout. Measured on node-22 (``gfs_2026080712``, issue #1749 triage): the
# same ``run_id`` was task 10 in a 17-member cohort, 8 in a 15-member one, and
# 12 in the 22-member one; 20 of 22 members disagreed with the new layout.
# ---------------------------------------------------------------------------

# Earlier-submission layouts for the 22-member cohort below, keyed by the
# member's index in the *new* layout. Only the members that already existed in
# the earlier submission carry a stale index; members 17.. (resp. 15..) were
# first written by the current submission and so carry the current index.
_STALE_LAYOUTS = {
    # 17-member submission: new task 12 was old task 10 (triage datapoint).
    17: {index: (index + 15) % 17 for index in range(17)},
    # 15-member submission: new task 12 was old task 8.
    15: {index: (index + 11) % 15 for index in range(15)},
}


def _stale_layout_updates(stale_layout: dict[int, int]) -> dict[int, dict[str, Any]]:
    return {index: {"array_task_id": task_id} for index, task_id in stale_layout.items()}


def test_file_cohort_renumbered_member_set_no_longer_fails_identity(
    tmp_path: Any,
) -> None:
    """Delta scenario 1: a member set change renumbers the array, so the
    surviving ``hydro_run`` rows carry the previous submission's task ids while
    every other identity field agrees. That must reconcile, not wedge the whole
    cohort on ``identity_mismatch_*`` (#1749, first link of the #1748 stall)."""
    from services.orchestrator.reconcile import SacctRecord, reconcile_inflight_jobs

    member_count = 22
    repository = _file_cohort_repository(
        tmp_path,
        member_count=member_count,
        with_runtime_rows=False,
    )
    _append_cohort_placeholders(
        repository,
        member_count,
        updates_by_index=_stale_layout_updates(_STALE_LAYOUTS[17]),
    )
    key = "cycle_gfs_2026071200_forecast_fixture:forecast"
    job_id = "job_cycle_gfs_2026071200_forecast_fixture_forecast"
    _bind_current_file_cohort(repository, key, slurm_job_id="17667")
    tasks = tuple(
        SacctRecord(f"17667_{index}", "COMPLETED", "nhms_forecast", array_task_id=index)
        for index in range(member_count)
    )
    record = SacctRecord(
        "17667",
        "COMPLETED",
        "nhms_forecast",
        comment=f"nhms_idem:{key}",
        array_member_job_ids=tuple(task.slurm_job_id for task in tasks),
        array_task_records=tasks,
    )

    outcome = reconcile_inflight_jobs(repository, sacct_query=lambda _job_id: record)[0]

    assert outcome.action == "terminal"
    assert outcome.status == "succeeded"
    cohort = repository.get_pipeline_job(job_id)
    assert cohort["reconciliation_decision"] == "matched_bound"
    assert all(
        projection["array_task_outcome"] == "succeeded"
        for projection in cohort["candidate_projections"]
    )


@pytest.mark.parametrize("earlier_member_count", [17, 15])
def test_file_cohort_runtime_identity_tolerates_stale_task_id_not_only_absent(
    tmp_path: Any,
    earlier_member_count: int,
) -> None:
    """Delta scenario 1, second half, at the gate itself.

    The donor change (``fix-cohort-runtime-identity-absent-fields``)
    only covers ``array_task_id is None``. The production failure is
    present-but-**stale**, and on production data the absent branch is dead:
    ``create_hydro_run_from_basin`` persists a real index on every row. This
    test asserts the stored index is non-``None`` *and* different before
    asserting the gate passes, so it cannot silently degrade into a re-test of
    the absent branch.
    """
    member_count = 22
    repository = _file_cohort_repository(
        tmp_path,
        member_count=member_count,
        with_runtime_rows=False,
    )
    stale_layout = _STALE_LAYOUTS[earlier_member_count]
    rows = _append_cohort_placeholders(
        repository,
        member_count,
        updates_by_index=_stale_layout_updates(stale_layout),
    )
    identity = _versioned_master_reservation_record(member_count=member_count)

    stale_members = 0
    for member in identity["cohort_members"]:
        row = rows[str(member["run_id"])]
        assert row["array_task_id"] is not None, "row must exercise the present branch"
        if int(row["array_task_id"]) != int(member["array_task_id"]):
            stale_members += 1
    assert stale_members == len(stale_layout), (
        "every member carried over from the earlier submission must disagree; "
        f"only {stale_members} of {len(stale_layout)} differ"
    )

    assert repository.forecast_cohort_runtime_identity_matches(identity) is True


@pytest.mark.parametrize("field", ["candidate_id", "basin_id"])
def test_file_cohort_runtime_identity_sibling_fields_stay_fatal_when_present(
    tmp_path: Any,
    field: str,
) -> None:
    """Delta scenario 2 at the gate: dropping ``array_task_id`` must not drag
    its siblings with it. ``candidate_id``/``basin_id`` are derived from
    model/basin identity (not from the layout), so a present-but-different
    value stays fatal; ``None`` still means "not stored" because some
    ``create_hydro_run`` paths persist neither."""
    member_count = 4
    repository = _file_cohort_repository(
        tmp_path,
        member_count=member_count,
        with_runtime_rows=False,
    )
    _append_cohort_placeholders(
        repository,
        member_count,
        updates_by_index={2: {field: f"foreign-{field}"}},
    )
    identity = _versioned_master_reservation_record(member_count=member_count)

    assert repository.forecast_cohort_runtime_identity_matches(identity) is False


@pytest.mark.parametrize(
    ("row_updates", "member_updates"),
    [
        # The journal ties ``run_id`` to ``model_id`` structurally, so a row
        # cannot carry a foreign ``run_id`` and stay discoverable; the member
        # side is mutated instead, which reaches the same comparison.
        ({}, {"run_id": "fcst_gfs_2026071200_model_foreign"}),
        ({"scenario_id": "foreign_scenario"}, {}),
    ],
    ids=["run_id", "scenario_id"],
)
def test_file_cohort_runtime_identity_retained_strict_fields_still_bite(
    tmp_path: Any,
    row_updates: dict[str, Any],
    member_updates: dict[str, Any],
) -> None:
    """Delta scenario 3: the fields kept strict must still fail the gate.

    The mutated rows stay discoverable (same ``model_id``/``source_id``/
    ``cycle_time``) so the failure comes from the strict comparison, not from
    the row-missing branch above it.
    """
    member_count = 4
    repository = _file_cohort_repository(
        tmp_path,
        member_count=member_count,
        with_runtime_rows=False,
    )
    _append_cohort_placeholders(
        repository,
        member_count,
        updates_by_index={2: row_updates} if row_updates else None,
    )
    identity = _versioned_master_reservation_record(member_count=member_count)
    identity["cohort_members"][2].update(member_updates)

    assert repository.forecast_cohort_runtime_identity_matches(identity) is False


def test_file_cohort_reclaimed_attempt_two_accepts_frozen_attempt_one_runtime_rows(
    tmp_path: Any,
) -> None:
    """#1792: reclaim advances the master to attempt 2 while every successful
    per-model ``hydro_run`` row stays frozen at the attempt that wrote it (1).
    ``submission_attempt`` is immutable lineage, not cross-submission equality
    identity — the runtime cross-check must pass (this assertion was RED before
    #1792 removed the equality comparison).
    """
    member_count = 4
    repository = _file_cohort_repository(
        tmp_path,
        member_count=member_count,
        with_runtime_rows=False,
    )
    _append_cohort_placeholders(repository, member_count)
    identity = _versioned_master_reservation_record(member_count=member_count)

    # Prove the fixture geometry is the reclaim shape before asserting: the
    # durable per-model rows are frozen at attempt 1 while the master advances
    # to attempt 2. ``forecast_cohort_runtime_identity_matches`` must not read
    # the attempt as cross-submission identity.
    assert all(
        repository._hydro_run_for(member["run_id"])["submission_attempt"] == 1
        for member in identity["cohort_members"]
    )
    identity["submission_attempt"] = 2

    assert repository.forecast_cohort_runtime_identity_matches(identity) is True


@pytest.mark.parametrize("member_count", [2, 256])
@pytest.mark.parametrize("corruption", ["swapped", "malformed", "duplicate"])
def test_file_cohort_physical_task_identity_mismatch_is_zero_mutation(
    tmp_path: Any,
    member_count: int,
    corruption: str,
) -> None:
    from services.orchestrator.reconcile import SacctRecord, reconcile_inflight_jobs

    repository = _file_cohort_repository(
        tmp_path / f"{member_count}-{corruption}",
        member_count=member_count,
    )
    key = "cycle_gfs_2026071200_forecast_fixture:forecast"
    job_id = "job_cycle_gfs_2026071200_forecast_fixture_forecast"
    _bind_current_file_cohort(repository, key, slurm_job_id="17667")
    before = repository.get_pipeline_job(job_id)
    tasks = [
        SacctRecord(
            f"17667_{index}",
            "COMPLETED",
            "nhms_forecast",
            array_task_id=index,
        )
        for index in range(member_count)
    ]
    if corruption == "swapped":
        tasks[0] = SacctRecord("17667_1", "COMPLETED", "nhms_forecast", array_task_id=0)
    elif corruption == "malformed":
        tasks[0] = SacctRecord("17667_bad", "COMPLETED", "nhms_forecast", array_task_id=0)
    else:
        tasks[1] = SacctRecord("17667_0", "COMPLETED", "nhms_forecast", array_task_id=0)
    record = SacctRecord(
        "17667",
        "COMPLETED",
        "nhms_forecast",
        comment=f"nhms_idem:{key}",
        array_member_job_ids=tuple(task.slurm_job_id for task in tasks),
        array_task_records=tuple(tasks),
    )

    outcome = reconcile_inflight_jobs(repository, sacct_query=lambda _job_id: record)[0]

    assert outcome.action == "identity_mismatch_blocked"
    assert outcome.durable_write_count == 0
    assert repository.get_pipeline_job(job_id) == before
    assert len(repository.query_pipeline_jobs_by_cycle("gfs_2026071200")) == 1


def test_file_cohort_exact_accounting_match_without_runtime_rows_stays_identity_blocked(
    tmp_path: Any,
) -> None:
    repository = _file_cohort_repository(tmp_path, member_count=1, with_runtime_rows=False)
    key = "cycle_gfs_2026071200_forecast_fixture:forecast"
    job_id = "job_cycle_gfs_2026071200_forecast_fixture_forecast"
    _bind_current_file_cohort(repository, key, slurm_job_id="17667")
    exact = SacctRecord(
        "17667",
        "RUNNING",
        "nhms_forecast",
        comment=f"nhms_idem:{key}",
    )

    outcome = reconcile_inflight_jobs(repository, sacct_query=lambda _job_id: exact)[0]

    assert outcome.action == "identity_mismatch_blocked"
    durable = repository.get_pipeline_job(job_id)
    assert durable["status"] == "submitted"
    assert durable["reconciliation_decision"] is None
    assert durable["candidate_projections"] == []


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
        include_direct_jobs: bool = True,
    ) -> dict[str, Any]:
        calls["batch_snapshots"] += 1
        assert source_id == "gfs"
        assert cycle_time == datetime(2026, 7, 12, tzinfo=UTC)
        assert include_direct_jobs is False
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
            from services.orchestrator import reconcile as reconcile_module

            monkeypatch.setattr(
                reconcile_module,
                "forecast_cohort_identity_is_valid",
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
