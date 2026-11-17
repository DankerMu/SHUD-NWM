"""File-cohort runtime identity: projection identity gates and stale-layout
tolerance (#1749).

#3177 moved batch projection bounds and the terminal identity cases to
``tests/test_gateway_reconcile_file_cohort_identity_terminal.py``.
"""

from __future__ import annotations

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
