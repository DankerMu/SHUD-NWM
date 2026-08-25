"""File-cohort terminal projection: task-to-outcome mapping, master-status
derivation, and digest/identity invariants at projection time.
"""

from __future__ import annotations

import copy
from dataclasses import replace
from datetime import (
    UTC,
    datetime,
)
from typing import Any

import pytest

from services.orchestrator.reconcile import (
    RECONCILE_UNVERIFIED_STATUS,
    SacctRecord,
    reconcile_inflight_jobs,
)
from tests.gateway_reconcile_helpers import (
    _bind_current_file_cohort,
    _file_cohort_repository,
)


def _seed_cancelled_master(repository: Any, job_id: str) -> None:
    """Append a schema-valid ``cancelled`` status through the journal write seam.

    No current typed API produces a ``cancelled`` master (the cancellation flow
    parks masters on ``reconcile_unverified`` and lets task accounting decide),
    but ``cancelled`` is a legal persisted status a historical accepted-submit
    master may carry.  The row is written through the journal's own outgoing
    validator + direct-row seam, so an impossible shape fails here rather than
    faking a passing test.  The seed starts from a bound ``submitted`` master
    with NO candidate projections, so the restart reconcile is responsible for
    the whole first complete projection.
    """

    from services.orchestrator import file_orchestration_journal as journal_module

    source_id = journal_module._source_id_from_job(repository.get_pipeline_job(job_id))
    cycle_time = journal_module._cycle_time_from_job(repository.get_pipeline_job(job_id))
    with repository._locked_cycle_write(source_id=source_id, cycle_time=cycle_time):
        existing = repository._accepted_submit_job_for_id_unlocked(
            job_id,
            source_id=source_id,
            cycle_time=cycle_time,
        )
        assert existing is not None
        assert not existing.get("candidate_projections")
        row = {**existing, "status": "cancelled"}
        journal_record = journal_module._journal_record_for_write(
            "pipeline_job",
            row,
            source_id=source_id,
            cycle_time=cycle_time,
            model_id=None,
            sequence=repository._next_sequence_unlocked(source_id=source_id, cycle_time=cycle_time),
        )
        repository._validate_outgoing_record(
            journal_record,
            source_id=source_id,
            cycle_time=cycle_time,
            record_type="pipeline_job",
            model_id=None,
        )
        repository._append_journal_records_unlocked(
            source_id=source_id,
            cycle_time=cycle_time,
            records=[journal_record],
        )
        repository._write_pipeline_job_direct_unlocked(row, journal_record)
    assert repository.get_pipeline_job(job_id)["status"] == "cancelled"


def test_restart_reconcile_receipt_reports_durable_cancelled_without_resubmit(
    tmp_path: Any,
) -> None:
    """#1629 receipt closure: complete restart accounting reports ``cancelled``.

    A historical accepted-submit master persisted as ``cancelled`` (with
    incomplete task projections) is admitted to restart reconcile, and complete
    sacct accounting projects it.  The projection preserves the sticky
    ``cancelled`` status while refreshing task/observational evidence, so the
    operator receipt's status -- which reports the terminal status READ BACK
    from the durable master -- must say ``cancelled`` too, not the
    projection-derived value.  The durable master stays ``cancelled`` and the
    job leaves the inflight inventory (no resubmission / re-reservation).
    """

    repository = _file_cohort_repository(tmp_path, member_count=2)
    key = "cycle_gfs_2026071200_forecast_fixture:forecast"
    job_id = "job_cycle_gfs_2026071200_forecast_fixture_forecast"
    _bind_current_file_cohort(repository, key, slurm_job_id="17667")
    _seed_cancelled_master(repository, job_id)
    assert [job.job_id for job in repository.query_inflight_jobs()] == [job_id]

    task_records = (
        SacctRecord("17667_0", "COMPLETED", "nhms_forecast", exit_code="0:0", array_task_id=0),
        SacctRecord("17667_1", "COMPLETED", "nhms_forecast", exit_code="0:0", array_task_id=1),
    )
    master = SacctRecord(
        slurm_job_id="17667",
        raw_state="COMPLETED",
        job_name="nhms_forecast",
        comment=f"nhms_idem:{key}",
        array_member_job_ids=("17667_0", "17667_1"),
        array_task_records=task_records,
    )

    outcomes = reconcile_inflight_jobs(repository, sacct_query=lambda _job_id: master)

    # Complete accounting over the sticky cancelled master: the receipt must
    # report the durable truth, not the projection-derived ``succeeded``.
    assert len(outcomes) == 1
    outcome = outcomes[0]
    assert outcome.action == "terminal"
    assert outcome.status == "cancelled"
    assert outcome.durable_write_kind == "forecast_cohort_projection"
    # First complete projection over a bound cancelled master with no candidate
    # projections: 2 candidate pipeline_jobs + 2 succeeded hydro refreshes +
    # the master pipeline_job = 5 pipeline_status writes, plus 2
    # array_task_reconciled events + 1 status_change event.
    assert outcome.durable_write_count == 8
    assert outcome.pipeline_status_write_count == 5
    assert outcome.pipeline_event_write_count == 3

    # Durable/public master and the outcome agree on ``cancelled``.
    master_row = repository.get_pipeline_job(job_id)
    assert master_row is not None and master_row["status"] == "cancelled"
    # The task projection still refreshed the observational evidence and the
    # candidate rows, exactly as for any complete matched-bound projection.
    assert len(master_row["candidate_projections"]) == 2
    assert all(
        projection["array_task_outcome"] == "succeeded"
        for projection in master_row["candidate_projections"]
    )

    # Terminal + projections complete => no longer needs restart reconcile and
    # the bound row is not reserved-unbound, so a later pass resubmits nothing.
    assert repository.query_inflight_jobs() == []
    assert repository.query_reserved_unbound_jobs() == []
    assert reconcile_inflight_jobs(repository, sacct_query=lambda _job_id: master) == []


def test_file_cohort_terminal_tasks_project_exact_success_failure_and_restart(
    tmp_path: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from services.orchestrator import scheduler as scheduler_module
    from services.orchestrator.file_orchestration_journal import FileOrchestrationJournalError
    from services.orchestrator.reconcile import SacctRecord, reconcile_inflight_jobs

    repository = _file_cohort_repository(tmp_path, member_count=2)
    key = "cycle_gfs_2026071200_forecast_fixture:forecast"
    _bind_current_file_cohort(repository, key, slurm_job_id="17667")
    for index in range(2):
        repository.append_historical_hydro_run(
            {
                "run_id": f"fcst_gfs_2026071200_model_{index}",
                "run_type": "forecast",
                "scenario_id": "operational",
                "model_id": f"model_{index}",
                "basin_version_id": f"basin_v{index}",
                "forcing_version_id": f"forc_gfs_2026071200_model_{index}",
                "init_state_id": f"state_{index}",
                "source_id": "gfs",
                "cycle_time": "2026-07-12T00:00:00Z",
                "start_time": "2026-07-12T00:00:00Z",
                "end_time": "2026-07-12T18:00:00Z",
                "status": "failed",
                "run_manifest_uri": f"s3://nhms/runs/model_{index}/run-manifest.json",
                "output_uri": f"s3://nhms/runs/model_{index}/output",
                "log_uri": f"s3://nhms/runs/model_{index}/logs",
                "error_code": "SLURM_GATEWAY_UNAVAILABLE",
                "error_message": "transport timeout",
                "created_at": "2026-07-12T00:00:00Z",
                "updated_at": "2026-07-12T00:01:00Z",
            }
        )
    task_records = (
        SacctRecord("17667_0", "COMPLETED", "nhms_forecast", exit_code="0:0", array_task_id=0),
        SacctRecord("17667_1", "TIMEOUT", "nhms_forecast", exit_code="1:0", array_task_id=1),
    )
    master = SacctRecord(
        slurm_job_id="17667",
        raw_state="COMPLETED",
        job_name="nhms_forecast",
        comment=f"nhms_idem:{key}",
        array_member_job_ids=("17667_0", "17667_1"),
        array_task_records=task_records,
    )
    before_success = repository._hydro_run_for("fcst_gfs_2026071200_model_0")

    outcomes = reconcile_inflight_jobs(repository, sacct_query=lambda _job_id: master)

    assert outcomes[0].action == "terminal"
    assert outcomes[0].status == "partially_failed"
    cohort = repository.get_pipeline_job("job_cycle_gfs_2026071200_forecast_fixture_forecast")
    assert cohort["status"] == "partially_failed"
    projections = cohort["candidate_projections"]
    assert projections[0]["array_task_outcome"] == "succeeded"
    assert projections[0]["restart_stage"] == "state_save_qc"
    assert projections[0]["native_shud_resubmitted"] is False
    assert projections[1]["array_task_outcome"] == "failed"
    with pytest.raises(FileOrchestrationJournalError):
        repository.upsert_pipeline_job(
            {
                **cohort,
                "status": "reserved",
                "slurm_job_id": None,
                "candidate_projections": [],
            }
        )
    assert repository.get_pipeline_job(cohort["job_id"]) == cohort
    succeeded = repository._hydro_run_for("fcst_gfs_2026071200_model_0")
    failed = repository._hydro_run_for("fcst_gfs_2026071200_model_1")
    assert succeeded["status"] == "succeeded"
    assert succeeded["error_code"] is None
    assert succeeded["init_state_id"] == "state_0"
    assert succeeded["run_manifest_uri"] == before_success["run_manifest_uri"]
    assert succeeded["output_uri"] == before_success["output_uri"]
    assert failed["status"] == "failed"
    assert failed["slurm_job_id"] == "17667_1"
    assert failed["error_code"] == "SLURM_TIMEOUT"

    cycle_time = datetime(2026, 7, 12, tzinfo=UTC)
    monkeypatch.setenv("NHMS_ORCHESTRATOR_TERMINAL_STAGE", "forecast_state_save_qc")
    assert repository.has_completed_pipeline(
        source_id="gfs",
        cycle_time=cycle_time,
        model_id="model_0",
    ) is False
    state = repository.candidate_state(
        source_id="gfs",
        cycle_time=cycle_time,
        model_id="model_0",
        run_id="fcst_gfs_2026071200_model_0",
        forcing_version_id="forc_gfs_2026071200_model_0",
        candidate_id="gfs:2026-07-12T00:00:00Z:model_0:forecast_gfs_deterministic",
    )
    candidate = scheduler_module.SchedulerCandidate(
        candidate_id="gfs:2026-07-12T00:00:00Z:model_0:forecast_gfs_deterministic",
        source_id="gfs",
        cycle_id="gfs_2026071200",
        cycle_time_utc=cycle_time,
        model_id="model_0",
        basin_id="basin_0",
        basin_version_id="basin_v0",
        river_network_version_id="river_v0",
        segment_count=1,
        output_segment_count=1,
        model_package_uri="s3://nhms/models/model_0.tar",
        resource_profile={},
        display_capabilities={},
        horizon={},
        scenario_id="forecast_gfs_deterministic",
        run_id="fcst_gfs_2026071200_model_0",
        forcing_version_id="forc_gfs_2026071200_model_0",
        status="ready",
    )
    decision = scheduler_module._candidate_state_decision(candidate, state)
    assert decision is not None
    assert decision.action == "retry"
    assert decision.reason == "resume_after_completed_stage"
    assert decision.evidence["restart_stage"] == "state_save_qc"
    assert decision.evidence["native_shud_resubmitted"] is False

    failed_state = repository.candidate_state(
        source_id="gfs",
        cycle_time=cycle_time,
        model_id="model_1",
        run_id="fcst_gfs_2026071200_model_1",
        forcing_version_id="forc_gfs_2026071200_model_1",
        candidate_id="gfs:2026-07-12T00:00:00Z:model_1:forecast_gfs_deterministic",
    )
    failed_candidate = replace(
        candidate,
        candidate_id="gfs:2026-07-12T00:00:00Z:model_1:forecast_gfs_deterministic",
        model_id="model_1",
        basin_id="basin_1",
        basin_version_id="basin_v1",
        river_network_version_id="river_v1",
        model_package_uri="s3://nhms/models/model_1.tar",
        run_id="fcst_gfs_2026071200_model_1",
        forcing_version_id="forc_gfs_2026071200_model_1",
    )
    failed_decision = scheduler_module._candidate_state_decision(failed_candidate, failed_state)
    assert failed_decision is not None
    # Since #1160 this URI-absent geometry (the journal records no forcing
    # provenance for the failed member) is demoted to the stable repair-eligible
    # missing-forcing blocker instead of re-issuing a forecast retry that would
    # spin.  Since #1203 no provenance tier witnesses a package here (no journal
    # row, no journal direct file, and ``resource_profile={}`` leaves the sidecar
    # tier unconfigured), so the blocker carries the distinct "cannot determine"
    # reason.  The projection assertions above -- this test's actual subject --
    # are unchanged, as is the fail-closed block direction.
    assert (failed_decision.action, failed_decision.reason) == ("blocked", "forcing_version_row_absent")
    assert failed_decision.evidence["error_code"] == "FORCING_VERSION_ROW_ABSENT"
    assert failed_decision.evidence["artifact_guard"]["stable_classifier"] == "FORCING_VERSION_ROW_ABSENT"
    assert failed_decision.evidence["classifier"] == "missing_upstream_artifact"
    assert failed_decision.evidence["restart_stage"] == "forecast"
    assert failed_decision.evidence["artifact_guard"]["artifact_exists"] is False


def test_file_cohort_boot_fail_task_projects_failed_with_node_failure(tmp_path: Any) -> None:
    # BOOT_FAIL is a Slurm terminal state (slurm_validation TERMINAL_SLURM_STATES has
    # always enumerated it), but until it entered SLURM_STATE_MAP the cohort
    # projection could not read it as terminal: the task stalled on
    # ``unverified`` and the whole cohort reported task_accounting_incomplete.
    repository = _file_cohort_repository(tmp_path, member_count=2)
    key = "cycle_gfs_2026071200_forecast_fixture:forecast"
    _bind_current_file_cohort(repository, key, slurm_job_id="17667")
    for index in range(2):
        repository.append_historical_hydro_run(
            {
                "run_id": f"fcst_gfs_2026071200_model_{index}",
                "run_type": "forecast",
                "scenario_id": "operational",
                "model_id": f"model_{index}",
                "basin_version_id": f"basin_v{index}",
                "forcing_version_id": f"forc_gfs_2026071200_model_{index}",
                "init_state_id": f"state_{index}",
                "source_id": "gfs",
                "cycle_time": "2026-07-12T00:00:00Z",
                "start_time": "2026-07-12T00:00:00Z",
                "end_time": "2026-07-12T18:00:00Z",
                "status": "failed",
                "run_manifest_uri": f"s3://nhms/runs/model_{index}/run-manifest.json",
                "output_uri": f"s3://nhms/runs/model_{index}/output",
                "log_uri": f"s3://nhms/runs/model_{index}/logs",
                "error_code": "SLURM_GATEWAY_UNAVAILABLE",
                "error_message": "transport timeout",
                "created_at": "2026-07-12T00:00:00Z",
                "updated_at": "2026-07-12T00:01:00Z",
            }
        )
    task_records = (
        SacctRecord("17667_0", "COMPLETED", "nhms_forecast", exit_code="0:0", array_task_id=0),
        SacctRecord("17667_1", "BOOT_FAIL", "nhms_forecast", exit_code="1:0", array_task_id=1),
    )
    master = SacctRecord(
        slurm_job_id="17667",
        raw_state="COMPLETED",
        job_name="nhms_forecast",
        comment=f"nhms_idem:{key}",
        array_member_job_ids=("17667_0", "17667_1"),
        array_task_records=task_records,
    )

    outcomes = reconcile_inflight_jobs(repository, sacct_query=lambda _job_id: master)

    assert outcomes[0].action == "terminal"
    assert outcomes[0].status == "partially_failed"
    cohort = repository.get_pipeline_job("job_cycle_gfs_2026071200_forecast_fixture_forecast")
    projections = cohort["candidate_projections"]
    assert projections[0]["array_task_outcome"] == "succeeded"
    assert projections[1]["array_task_outcome"] == "failed"
    # The durable cohort projection is bounded to identity keys, so the mapped code
    # is read back off the projected hydro run.
    failed = repository._hydro_run_for("fcst_gfs_2026071200_model_1")
    assert failed["status"] == "failed"
    assert failed["error_code"] == "NODE_FAILURE"


@pytest.mark.parametrize("raw_state", ["REVOKED", "SPECIAL_EXIT"])
def test_file_cohort_unmapped_terminal_task_projects_failed(tmp_path: Any, raw_state: str) -> None:
    # Same shape as the BOOT_FAIL case above: both states are enumerated by
    # slurm_validation TERMINAL_SLURM_STATES, so a cohort carrying one of them must
    # project a terminal task rather than stalling the whole cohort on
    # task_accounting_incomplete.  The error code stays the generic unknown one --
    # SLURM_STATE_MAP registration is orthogonal to map_slurm_error_code.
    repository = _file_cohort_repository(tmp_path, member_count=2)
    key = "cycle_gfs_2026071200_forecast_fixture:forecast"
    _bind_current_file_cohort(repository, key, slurm_job_id="17667")
    for index in range(2):
        repository.append_historical_hydro_run(
            {
                "run_id": f"fcst_gfs_2026071200_model_{index}",
                "run_type": "forecast",
                "scenario_id": "operational",
                "model_id": f"model_{index}",
                "basin_version_id": f"basin_v{index}",
                "forcing_version_id": f"forc_gfs_2026071200_model_{index}",
                "init_state_id": f"state_{index}",
                "source_id": "gfs",
                "cycle_time": "2026-07-12T00:00:00Z",
                "start_time": "2026-07-12T00:00:00Z",
                "end_time": "2026-07-12T18:00:00Z",
                "status": "failed",
                "run_manifest_uri": f"s3://nhms/runs/model_{index}/run-manifest.json",
                "output_uri": f"s3://nhms/runs/model_{index}/output",
                "log_uri": f"s3://nhms/runs/model_{index}/logs",
                "error_code": "SLURM_GATEWAY_UNAVAILABLE",
                "error_message": "transport timeout",
                "created_at": "2026-07-12T00:00:00Z",
                "updated_at": "2026-07-12T00:01:00Z",
            }
        )
    task_records = (
        SacctRecord("17667_0", "COMPLETED", "nhms_forecast", exit_code="0:0", array_task_id=0),
        SacctRecord("17667_1", raw_state, "nhms_forecast", exit_code="1:0", array_task_id=1),
    )
    master = SacctRecord(
        slurm_job_id="17667",
        raw_state="COMPLETED",
        job_name="nhms_forecast",
        comment=f"nhms_idem:{key}",
        array_member_job_ids=("17667_0", "17667_1"),
        array_task_records=task_records,
    )

    outcomes = reconcile_inflight_jobs(repository, sacct_query=lambda _job_id: master)

    assert outcomes[0].action == "terminal"
    assert outcomes[0].status == "partially_failed"
    assert outcomes[0].status != RECONCILE_UNVERIFIED_STATUS
    cohort = repository.get_pipeline_job("job_cycle_gfs_2026071200_forecast_fixture_forecast")
    projections = cohort["candidate_projections"]
    assert projections[0]["array_task_outcome"] == "succeeded"
    assert projections[1]["array_task_outcome"] == "failed"
    failed = repository._hydro_run_for("fcst_gfs_2026071200_model_1")
    assert failed["status"] == "failed"
    assert failed["error_code"] == "SLURM_JOB_FAILED"


@pytest.mark.parametrize(
    ("task_outcomes", "raw_master_status", "expected_status"),
    [
        pytest.param(("succeeded", "succeeded"), "failed", "succeeded", id="all-success"),
        pytest.param(("succeeded", "failed"), "succeeded", "partially_failed", id="mixed"),
        pytest.param(("failed", "failed"), "succeeded", "failed", id="all-failed"),
    ],
)
def test_file_cohort_complete_projection_derives_master_status_only_from_tasks(
    tmp_path: Any,
    task_outcomes: tuple[str, str],
    raw_master_status: str,
    expected_status: str,
) -> None:
    repository = _file_cohort_repository(tmp_path, member_count=2)
    job_id = "job_cycle_gfs_2026071200_forecast_fixture_forecast"
    key = "cycle_gfs_2026071200_forecast_fixture:forecast"
    _bind_current_file_cohort(repository, key, slurm_job_id="17667")
    members = repository.get_pipeline_job(job_id)["cohort_members"]
    projections = [
        {
            **member,
            "array_task_outcome": task_outcomes[index],
            "task_slurm_job_id": f"17667_{index}",
            "error_code": "SLURM_TIMEOUT" if task_outcomes[index] == "failed" else None,
            "restart_stage": (
                "state_save_qc" if task_outcomes[index] == "succeeded" else "forecast"
            ),
            "native_shud_resubmitted": False,
        }
        for index, member in enumerate(members)
    ]

    repository.project_forecast_cohort_tasks(
        job_id,
        master_slurm_job_id="17667",
        projections=projections,
        complete=True,
        master_status=raw_master_status,
        master_error_code="RAW_MASTER_STATUS_MUST_NOT_WIN",
        reconciliation_decision="matched_bound",
    )

    durable = repository.get_pipeline_job(job_id)
    assert durable["status"] == expected_status
    assert durable["error_code"] == (
        None if expected_status == "succeeded" else "SLURM_TIMEOUT"
    )


def test_file_cohort_18_member_partial_then_complete_is_monotonic_and_idempotent(
    tmp_path: Any,
) -> None:
    from services.orchestrator.reconcile import SacctRecord, reconcile_inflight_jobs

    repository = _file_cohort_repository(tmp_path)
    key = "cycle_gfs_2026071200_forecast_fixture:forecast"
    job_id = "job_cycle_gfs_2026071200_forecast_fixture_forecast"
    _bind_current_file_cohort(repository, key, slurm_job_id="17667")

    def terminal(task_count: int) -> SacctRecord:
        tasks = tuple(
            SacctRecord(
                f"17667_{index}",
                "COMPLETED",
                "nhms_forecast",
                comment=f"nhms_idem:{key}",
                array_task_id=index,
            )
            for index in range(task_count)
        )
        return SacctRecord(
            "17667",
            "COMPLETED",
            "nhms_forecast",
            comment=f"nhms_idem:{key}",
            array_member_job_ids=tuple(task.slurm_job_id for task in tasks),
            array_task_records=tasks,
        )

    partial = reconcile_inflight_jobs(repository, sacct_query=lambda _job_id: terminal(17))[0]
    partial_row = repository.get_pipeline_job(job_id)
    complete = reconcile_inflight_jobs(repository, sacct_query=lambda _job_id: terminal(18))[0]
    complete_row = repository.get_pipeline_job(job_id)
    line_count = sum(
        len(path.read_text(encoding="utf-8").splitlines())
        for path in repository.root.rglob("*.jsonl")
    )

    assert partial.action == "task_accounting_incomplete"
    assert partial.pipeline_status_write_count == 1
    assert partial.pipeline_event_write_count == 0
    assert partial_row["status"] == "reconcile_unverified"
    assert partial_row["candidate_projections"] == []
    assert complete.action == "terminal"
    assert complete.pipeline_event_write_count == 19
    assert complete_row["status"] == "succeeded"
    assert len(complete_row["candidate_projections"]) == 18
    assert reconcile_inflight_jobs(repository, sacct_query=lambda _job_id: terminal(18)) == []
    assert line_count == sum(
        len(path.read_text(encoding="utf-8").splitlines())
        for path in repository.root.rglob("*.jsonl")
    )


def test_file_cohort_corrupt_digest_blocks_initial_bind(tmp_path: Any) -> None:
    from services.orchestrator.file_orchestration_journal import FileOrchestrationJournalError

    with pytest.raises(FileOrchestrationJournalError, match="file_journal_evidence_invariant_invalid"):
        _file_cohort_repository(tmp_path, corrupt_digest=True)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("array_task_id", 9),
        ("candidate_id", "gfs:2026-07-13T00:00:00Z:model_0:forecast_gfs_deterministic"),
        ("run_id", "fcst_gfs_2026071300_model_0"),
        ("model_id", "wrong-model"),
        ("scenario_id", "wrong-scenario"),
        ("restart_stage", "parse"),
    ],
)
def test_file_cohort_recomputed_digest_cannot_override_canonical_member_shape(
    tmp_path: Any,
    field: str,
    value: Any,
) -> None:
    from services.orchestrator.accepted_submit_identity import (
        forecast_cohort_digest,
        forecast_cohort_identity_is_valid,
    )

    repository = _file_cohort_repository(tmp_path, member_count=2)
    identity = copy.deepcopy(
        repository.get_pipeline_job("job_cycle_gfs_2026071200_forecast_fixture_forecast")
    )
    identity["cohort_members"][0][field] = value
    identity["cohort_digest"] = forecast_cohort_digest(identity)

    assert forecast_cohort_identity_is_valid(identity) is False


@pytest.mark.parametrize("phase", ["initial_bind", "terminal_projection"])
@pytest.mark.parametrize("mutation", ["candidate_cycle", "scenario", "basin", "model", "order"])
def test_file_cohort_runtime_manifest_identity_blocks_joint_member_and_digest_mutation(
    tmp_path: Any,
    phase: str,
    mutation: str,
) -> None:
    from services.orchestrator.accepted_submit_identity import forecast_cohort_digest
    from services.orchestrator.file_orchestration_journal import FileOrchestrationJournalError
    from services.orchestrator.reconcile import SacctRecord, reconcile_reserved_unbound_jobs

    repository = _file_cohort_repository(tmp_path, member_count=2)
    job_id = "job_cycle_gfs_2026071200_forecast_fixture_forecast"
    key = "cycle_gfs_2026071200_forecast_fixture:forecast"
    exact = SacctRecord(
        "17667",
        "RUNNING",
        "nhms_forecast",
        comment=f"nhms_idem:{key}",
        run_id="cycle_gfs_2026071200_forecast_fixture",
        stage="forecast",
        pipeline_job_id=job_id,
    )
    if phase == "terminal_projection":
        assert reconcile_reserved_unbound_jobs(repository, comment_query=lambda _key: exact)[0].action == "bound"

    before = repository.get_pipeline_job(job_id)
    identity = copy.deepcopy(before)
    members = identity["cohort_members"]
    if mutation == "candidate_cycle":
        members[0]["candidate_id"] = members[0]["candidate_id"].replace("2026-07-12", "2026-07-13")
    elif mutation == "scenario":
        members[0]["scenario_id"] = "forecast_ifs_deterministic"
        members[0]["candidate_id"] = members[0]["candidate_id"].replace(
            "forecast_gfs_deterministic", "forecast_ifs_deterministic"
        )
    elif mutation == "basin":
        members[0]["basin_id"] = "foreign_basin"
    elif mutation == "model":
        members[0]["model_id"] = "foreign_model"
        members[0]["run_id"] = "fcst_gfs_2026071200_foreign_model"
        members[0]["candidate_id"] = (
            "gfs:2026-07-12T00:00:00Z:foreign_model:forecast_gfs_deterministic"
        )
    else:
        members.reverse()
        for index, member in enumerate(members):
            member["array_task_id"] = index
    identity["cohort_digest"] = forecast_cohort_digest(identity)
    with pytest.raises(FileOrchestrationJournalError, match="file_journal_evidence_invariant_invalid"):
        repository.upsert_pipeline_job(identity)
    assert repository.get_pipeline_job(job_id) == before


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("source_id", "IFS"),
        ("cycle_id", "gfs_2026071300"),
        ("run_id", "cycle_gfs_2026071300_forecast_fixture"),
        ("job_id", "job_cycle_gfs_2026071200_forecast_fixture_forecast_wrong"),
        ("idempotency_key", "cycle_gfs_2026071200_forecast_fixture:forecast:wrong"),
        ("slurm_comment", "nhms_idem:wrong"),
    ],
)
def test_file_cohort_recomputed_digest_cannot_override_canonical_tuple(
    tmp_path: Any,
    field: str,
    value: Any,
) -> None:
    from services.orchestrator.accepted_submit_identity import (
        forecast_cohort_digest,
        forecast_cohort_identity_is_valid,
    )

    repository = _file_cohort_repository(tmp_path, member_count=2)
    identity = copy.deepcopy(
        repository.get_pipeline_job("job_cycle_gfs_2026071200_forecast_fixture_forecast")
    )
    identity[field] = value
    identity["cohort_digest"] = forecast_cohort_digest(identity)

    assert forecast_cohort_identity_is_valid(identity) is False
