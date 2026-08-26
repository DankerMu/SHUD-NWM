"""Versioned-master write authority: accepted-submit evidence validation
and the immutable-field zero-write guards on every mutation surface.
"""

from __future__ import annotations

import copy
import json
from datetime import (
    UTC,
    datetime,
)
from typing import Any

import pytest

from tests.gateway_reconcile_helpers import _file_cohort_repository


@pytest.mark.parametrize(
    ("mutator", "field"),
    [
        (lambda row: row.update(submit_outcome="maybe"), "submit_outcome"),
        (lambda row: row.update(slurm_ownership_required="true"), "slurm_ownership_required"),
        (
            lambda row: row.update(
                reconciliation_source="slurm_exact_comment",
                reconciliation_decision="matched_bound",
                matched_slurm_job_id=None,
            ),
            "matched_slurm_job_id",
        ),
        (
            lambda row: row.update(
                candidate_projections=[
                    {
                        "candidate_id": "candidate",
                        "run_id": "run",
                        "model_id": "model",
                        "array_task_id": 0,
                        "array_task_outcome": "succeeded",
                        "restart_stage": "publish",
                        "native_shud_resubmitted": False,
                    }
                ]
            ),
            "candidate_projections.restart_stage",
        ),
    ],
)
def test_accepted_submit_evidence_validator_fails_closed(mutator: Any, field: str) -> None:
    from services.orchestrator.accepted_submit_identity import ACCEPTED_SUBMIT_CONTRACT_VERSION
    from services.orchestrator.file_orchestration_journal import (
        FileOrchestrationJournalError,
        _validate_accepted_submit_evidence,
    )

    row = {
        "accepted_submit_contract_version": ACCEPTED_SUBMIT_CONTRACT_VERSION,
        "stage": "forecast",
        "status": "submitted",
        "submit_outcome": "accepted",
        "restart_stage": "forecast",
        "submission_attempt": 1,
        "submission_attempt_started_at": datetime(2026, 7, 12, tzinfo=UTC),
        "slurm_ownership_required": False,
        "cohort_members": [{"array_task_id": 0}],
    }
    mutator(row)

    with pytest.raises(FileOrchestrationJournalError) as error:
        _validate_accepted_submit_evidence(row)

    assert error.value.field == field


@pytest.mark.parametrize("corruption", ["outcome", "digest", "projection_member", "master_model_id"])
def test_accepted_submit_evidence_validator_guards_all_file_surfaces(
    tmp_path: Any,
    corruption: str,
) -> None:
    from services.orchestrator.file_orchestration_journal import (
        FileOrchestrationJournalError,
        _CycleRows,
    )

    repository = _file_cohort_repository(tmp_path, member_count=2)
    job_id = "job_cycle_gfs_2026071200_forecast_fixture_forecast"
    direct_path = repository.root / "pipeline-jobs" / f"{job_id}.json"
    bad_record = json.loads(direct_path.read_text(encoding="utf-8"))
    if corruption == "outcome":
        bad_record["payload"]["submit_outcome"] = "invalid"
    elif corruption == "digest":
        bad_record["payload"]["cohort_digest"] = "0" * 64
    elif corruption == "projection_member":
        member = bad_record["payload"]["cohort_members"][0]
        bad_record["payload"]["candidate_projections"] = [
            {
                "candidate_id": "foreign-candidate",
                "run_id": member["run_id"],
                "model_id": member["model_id"],
                "array_task_id": 0,
                "array_task_outcome": "succeeded",
                "restart_stage": "state_save_qc",
                "native_shud_resubmitted": False,
            }
        ]
    else:
        bad_record["payload"]["model_id"] = "model_0"
    cycle_time = datetime(2026, 7, 12, tzinfo=UTC)

    with pytest.raises(FileOrchestrationJournalError):
        repository._validate_outgoing_record(
            bad_record,
            source_id="gfs",
            cycle_time=cycle_time,
            record_type="pipeline_job",
            model_id=None,
        )
    with pytest.raises(FileOrchestrationJournalError):
        repository._apply_journal_record(
            _CycleRows(),
            bad_record,
            source_id="gfs",
            cycle_time=cycle_time,
        )
    with pytest.raises(FileOrchestrationJournalError):
        repository._validated_direct_pipeline_job_record(bad_record, expected_job_id=job_id)

    latest_path = repository.root / "latest" / "gfs" / "2026071200" / "model_0.json"
    bad_latest = json.loads(latest_path.read_text(encoding="utf-8"))
    bad_latest["pipeline_jobs"].append(bad_record["payload"])
    with pytest.raises(FileOrchestrationJournalError):
        repository._apply_latest_view(
            _CycleRows(),
            bad_latest,
            source_id="gfs",
            cycle_time=cycle_time,
            expected_model_id="model_0",
        )


@pytest.mark.parametrize("source_id", ["gfs", "ifs"])
@pytest.mark.parametrize("anchor_case", ["missing", "naive"])
def test_versioned_master_reserve_and_replay_require_valid_attempt_anchor(
    tmp_path: Any,
    source_id: str,
    anchor_case: str,
) -> None:
    from services.orchestrator.file_orchestration_journal import (
        FileOrchestrationJournalError,
        FileOrchestrationJournalRepository,
        _CycleRows,
    )

    template = _file_cohort_repository(
        tmp_path / "template",
        member_count=1,
        source_id=source_id,
    )
    job_id = f"job_cycle_{source_id}_2026071200_forecast_fixture_forecast"
    row = template.get_accepted_submit_pipeline_job(job_id)
    if anchor_case == "missing":
        row.pop("submission_attempt_started_at")
    else:
        row["submission_attempt_started_at"] = datetime(2026, 7, 12)
    target = FileOrchestrationJournalRepository(tmp_path / "target")
    with pytest.raises(FileOrchestrationJournalError) as reserve_error:
        target.reserve_pipeline_job(row)
    assert reserve_error.value.field == "submission_attempt_started_at"

    direct_path = template.root / "pipeline-jobs" / f"{job_id}.json"
    record = json.loads(direct_path.read_text(encoding="utf-8"))
    if anchor_case == "missing":
        record["payload"].pop("submission_attempt_started_at")
    else:
        record["payload"]["submission_attempt_started_at"] = "2026-07-12T00:00:00"
    with pytest.raises(FileOrchestrationJournalError) as replay_error:
        template._apply_journal_record(
            _CycleRows(),
            record,
            source_id=source_id,
            cycle_time=datetime(2026, 7, 12, tzinfo=UTC),
        )
    assert replay_error.value.field == "submission_attempt_started_at"


def test_candidate_submit_outcome_enum_fails_closed_on_every_file_surface(tmp_path: Any) -> None:
    from services.orchestrator.accepted_submit_identity import ACCEPTED_SUBMIT_CONTRACT_VERSION
    from services.orchestrator.file_orchestration_journal import (
        FileOrchestrationJournalError,
        FileOrchestrationJournalRepository,
        _CycleRows,
    )

    repository = FileOrchestrationJournalRepository(tmp_path / "journal")
    cycle_time = datetime(2026, 7, 12, tzinfo=UTC)
    candidate = {
        "job_id": "job_fcst_gfs_2026071200_model_0_forecast_candidate_0",
        "run_id": "fcst_gfs_2026071200_model_0",
        "cycle_id": "gfs_2026071200",
        "job_type": "run_shud_forecast_array",
        "slurm_job_id": "17667_0",
        "array_task_id": 0,
        "model_id": "model_0",
        "status": "succeeded",
        "stage": "forecast",
        "candidate_id": "gfs:2026-07-12T00:00:00Z:model_0:forecast_gfs_deterministic",
        "submit_outcome": "accepted",
        "restart_stage": "forecast",
        "native_shud_resubmitted": False,
        "accepted_submit_contract_version": ACCEPTED_SUBMIT_CONTRACT_VERSION,
    }
    repository.upsert_pipeline_job(candidate)
    invalid_candidate = {**candidate, "submit_outcome": "invalid"}
    with pytest.raises(FileOrchestrationJournalError) as upsert_error:
        repository.upsert_pipeline_job(invalid_candidate)
    assert upsert_error.value.field == "submit_outcome"

    direct_path = (
        repository.root
        / "pipeline-jobs"
        / "by-cycle"
        / "gfs"
        / "2026071200"
        / f"{candidate['job_id']}.json"
    )
    bad_record = json.loads(direct_path.read_text(encoding="utf-8"))
    bad_record["payload"]["submit_outcome"] = "invalid"
    with pytest.raises(FileOrchestrationJournalError):
        repository._validate_outgoing_record(
            bad_record,
            source_id="gfs",
            cycle_time=cycle_time,
            record_type="pipeline_job",
            model_id="model_0",
        )
    with pytest.raises(FileOrchestrationJournalError):
        repository._apply_journal_record(
            _CycleRows(),
            bad_record,
            source_id="gfs",
            cycle_time=cycle_time,
        )
    with pytest.raises(FileOrchestrationJournalError):
        repository._validated_direct_pipeline_job_record(
            bad_record,
            expected_job_id=str(candidate["job_id"]),
        )

    latest_path = repository.root / "latest" / "gfs" / "2026071200" / "model_0.json"
    bad_latest = json.loads(latest_path.read_text(encoding="utf-8"))
    latest_candidate = next(
        job for job in bad_latest["pipeline_jobs"] if job.get("job_id") == candidate["job_id"]
    )
    latest_candidate["submit_outcome"] = "invalid"
    with pytest.raises(FileOrchestrationJournalError):
        repository._apply_latest_view(
            _CycleRows(),
            bad_latest,
            source_id="gfs",
            cycle_time=cycle_time,
            expected_model_id="model_0",
        )

    journal_path = repository.root / "journal" / "gfs" / "2026071200.jsonl"
    journal_records = [json.loads(line) for line in journal_path.read_text(encoding="utf-8").splitlines()]
    for record in journal_records:
        if record.get("record_type") == "pipeline_job" and record["payload"].get("job_id") == candidate["job_id"]:
            record["payload"]["submit_outcome"] = "invalid"
    journal_path.write_text(
        "".join(f"{json.dumps(record, sort_keys=True)}\n" for record in journal_records),
        encoding="utf-8",
    )
    direct_path.write_text(json.dumps(bad_record), encoding="utf-8")
    latest_path.write_text(json.dumps(bad_latest), encoding="utf-8")

    reopened = FileOrchestrationJournalRepository(repository.root)
    blocked = reopened.get_pipeline_job(str(candidate["job_id"]))
    assert blocked["file_journal"]["status"] == "blocked"
    assert blocked["file_journal"]["field"] == "submit_outcome"
    queried = reopened.query_pipeline_jobs_by_cycle("gfs_2026071200")
    assert any(
        job.get("error_code") == "file_journal_evidence_enum_invalid"
        and job.get("file_journal", {}).get("field") == "submit_outcome"
        for job in queried
    )


@pytest.mark.parametrize("source_id", ["gfs", "ifs"])
@pytest.mark.parametrize(
    "mutation",
    [
        "contract_version",
        "job_id",
        "run_id",
        "cycle_id",
        "source_id",
        "cycle_time",
        "job_type",
        "stage",
        "model_id",
        "array_task_id",
        "candidate_id",
        "idempotency_key",
        "slurm_comment",
        "cohort_members",
        "cohort_digest",
        "restart_stage",
        "native_shud_resubmitted",
        "expected_slurm_user",
        "expected_slurm_account",
        "slurm_ownership_required",
        "submission_attempt",
        "submission_attempt_started_at",
    ],
)
def test_versioned_master_ordinary_upsert_rejects_every_immutable_authority_group(
    tmp_path: Any,
    source_id: str,
    mutation: str,
) -> None:
    from services.orchestrator.file_orchestration_journal import (
        FileOrchestrationJournalError,
        FileOrchestrationJournalRepository,
    )

    repository = _file_cohort_repository(tmp_path, member_count=2, source_id=source_id)
    job_id = f"job_cycle_{source_id}_2026071200_forecast_fixture_forecast"
    before = repository.get_pipeline_job(job_id)
    changed = copy.deepcopy(before)
    replacements = {
        "contract_version": ("accepted_submit_contract_version", "nhms.accepted_submit.v2"),
        "job_id": ("job_id", f"{before['job_id']}_foreign"),
        "run_id": ("run_id", f"{before['run_id']}_foreign"),
        "cycle_id": ("cycle_id", f"{source_id}_2026071300"),
        "source_id": ("source_id", "IFS" if source_id == "gfs" else "gfs"),
        "cycle_time": ("cycle_time", "2026-07-13T00:00:00Z"),
        "job_type": ("job_type", "forecast"),
        "stage": ("stage", "run_shud_forecast"),
        "model_id": ("model_id", "model_0"),
        "array_task_id": ("array_task_id", 0),
        "candidate_id": ("candidate_id", before["cohort_members"][0]["candidate_id"]),
        "idempotency_key": ("idempotency_key", f"{before['idempotency_key']}:foreign"),
        "slurm_comment": ("slurm_comment", "nhms_idem:foreign"),
        "cohort_digest": ("cohort_digest", "0" * 64),
        "restart_stage": ("restart_stage", "state_save_qc"),
        "native_shud_resubmitted": ("native_shud_resubmitted", True),
        "expected_slurm_user": ("expected_slurm_user", "foreign-user"),
        "expected_slurm_account": ("expected_slurm_account", "foreign-account"),
        "slurm_ownership_required": ("slurm_ownership_required", True),
        "submission_attempt": ("submission_attempt", 2),
        "submission_attempt_started_at": (
            "submission_attempt_started_at",
            datetime(2026, 7, 12, 0, 0, 1, tzinfo=UTC),
        ),
    }
    if mutation == "cohort_members":
        changed["cohort_members"][0]["basin_id"] = "foreign-basin"
    else:
        field, value = replacements[mutation]
        changed[field] = value

    with pytest.raises(FileOrchestrationJournalError):
        repository.upsert_pipeline_job(changed)

    assert repository.get_pipeline_job(job_id) == before
    reopened = FileOrchestrationJournalRepository(repository.root)
    assert reopened.get_pipeline_job(job_id) == before


@pytest.mark.parametrize("source_id", ["gfs", "ifs"])
def test_versioned_master_classification_detour_fails_on_first_step_and_remains_sticky(
    tmp_path: Any,
    source_id: str,
) -> None:
    from services.orchestrator.accepted_submit_identity import AcceptedSubmitTransition
    from services.orchestrator.file_orchestration_journal import FileOrchestrationJournalError

    repository = _file_cohort_repository(tmp_path, member_count=1, source_id=source_id)
    job_id = f"job_cycle_{source_id}_2026071200_forecast_fixture_forecast"
    before = repository.get_pipeline_job(job_id)

    with pytest.raises(FileOrchestrationJournalError) as stage_error:
        repository.upsert_pipeline_job({**before, "stage": "forcing"})
    assert stage_error.value.field == "stage"

    with pytest.raises(FileOrchestrationJournalError) as attempt_error:
        repository.upsert_pipeline_job(
            {
                **before,
                "submission_attempt": 2,
                "submission_attempt_started_at": datetime(2026, 7, 12, 0, 1, tzinfo=UTC),
            }
        )
    assert attempt_error.value.field == "submission_attempt"

    journal_line_count = sum(
        len(path.read_text(encoding="utf-8").splitlines())
        for path in repository.root.glob("journal/**/*.jsonl")
    )
    replayed = repository.upsert_pipeline_job(
        {
            **before,
            "submission_attempt_started_at": "2026-07-11T20:00:00-04:00",
        }
    )
    assert replayed == before
    assert sum(
        len(path.read_text(encoding="utf-8").splitlines())
        for path in repository.root.glob("journal/**/*.jsonl")
    ) == journal_line_count

    key = f"cycle_{source_id}_2026071200_forecast_fixture:forecast"
    committed = repository.commit_pipeline_job_submit_attempt(
        key,
        pipeline_job_id=job_id,
        expected_submission_attempt=1,
        slurm_job_id="17667",
        transition=AcceptedSubmitTransition.accounting(
            "matched_bound",
            submit_outcome="accepted",
            matched_slurm_job_id="17667",
            status="submitted",
        ),
    )
    assert committed.outcome == "applied"
    accepted = repository.get_pipeline_job(job_id)
    assert accepted["status"] == "submitted"
    assert accepted["slurm_job_id"] == "17667"
    assert accepted["submit_outcome"] == "accepted"
    assert accepted["reconciliation_decision"] == "matched_bound"
    assert accepted["stage"] == "forecast"
    assert accepted["submission_attempt"] == 1
    assert accepted["submission_attempt_started_at"] == "2026-07-12T00:00:00Z"
    reopened = type(repository)(repository.root)
    assert reopened.get_pipeline_job(job_id) == accepted


@pytest.mark.parametrize("source_id", ["gfs", "ifs"])
def test_bound_master_generic_retry_forgery_is_zero_write_and_typed_retry_stays_blocked(
    tmp_path: Any,
    source_id: str,
) -> None:
    from services.orchestrator.accepted_submit_identity import (
        ACCEPTED_SUBMIT_CONTRACT_VERSION,
        AcceptedSubmitTransition,
    )
    from services.orchestrator.file_orchestration_journal import (
        FileOrchestrationJournalError,
        FileOrchestrationJournalRepository,
    )

    repository = _file_cohort_repository(tmp_path, member_count=1, source_id=source_id)
    job_id = f"job_cycle_{source_id}_2026071200_forecast_fixture_forecast"
    key = f"cycle_{source_id}_2026071200_forecast_fixture:forecast"
    committed = repository.commit_pipeline_job_submit_attempt(
        key,
        pipeline_job_id=job_id,
        expected_submission_attempt=1,
        slurm_job_id="17667",
        transition=AcceptedSubmitTransition.accepted(status="submitted"),
    )
    assert committed.outcome == "applied"
    bound = repository.get_pipeline_job(job_id)
    before_lines = sum(
        len(path.read_text(encoding="utf-8").splitlines())
        for path in repository.root.glob("journal/**/*.jsonl")
    )
    forged = {
        **bound,
        "slurm_job_id": None,
        "status": "reservation_lost",
        "submit_outcome": "submit_result_ambiguous",
        "reconciliation_source": "slurm_exact_comment",
        "reconciliation_decision": "absence_retry_permitted",
        "reconciliation_reason_class": None,
        "matched_slurm_job_id": None,
        # Binding provenance is impossible without a numeric Slurm id (minted
        # only by a successful bind); the forged retry-permission row drops it
        # so the ordinary-upsert guard is the one under test, not the
        # provenance/slurm-id closure.
        "slurm_binding_source": None,
        "slurm_accounting_submitted_at": None,
    }

    with pytest.raises(FileOrchestrationJournalError) as error:
        repository.upsert_pipeline_job(forged)
    assert error.value.field == "slurm_job_id"
    assert repository.get_pipeline_job(job_id) == bound
    assert sum(
        len(path.read_text(encoding="utf-8").splitlines())
        for path in repository.root.glob("journal/**/*.jsonl")
    ) == before_lines
    reopened = FileOrchestrationJournalRepository(repository.root)
    assert reopened.get_pipeline_job(job_id) == bound
    assert repository.reclaim_pipeline_job_reservation(forged) is None
    assert (
        repository.permit_pipeline_job_retry(
            job_id,
            accepted_submit_contract_version=ACCEPTED_SUBMIT_CONTRACT_VERSION,
            expected_submission_attempt=1,
            expected_submission_attempt_started_at=bound["submission_attempt_started_at"],
            expected_status="submitted",
        )
        == 0
    )
    assert repository.get_pipeline_job(job_id) == bound


def test_master_ordinary_upsert_guard_covers_every_mutable_merge_field() -> None:
    from services.orchestrator.accepted_submit_identity import (
        ACCEPTED_SUBMIT_MASTER_ORDINARY_UPSERT_FIELDS,
    )
    from services.orchestrator.file_orchestration_journal import (
        _PIPELINE_JOB_UPSERT_MUTABLE_FIELDS,
    )

    assert set(_PIPELINE_JOB_UPSERT_MUTABLE_FIELDS) <= set(
        ACCEPTED_SUBMIT_MASTER_ORDINARY_UPSERT_FIELDS
    )


@pytest.mark.parametrize("source_id", ["gfs", "ifs"])
@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("slurm_job_id", None),
        ("array_task_id", 0),
        ("status", "reserved"),
        ("status", "reservation_lost"),
        ("status", "submission_failed"),
        ("status", "succeeded"),
        ("submit_outcome", "submit_result_ambiguous"),
        ("matched_slurm_job_id", "17668"),
        ("submitted_at", None),
        ("started_at", "2026-07-12T00:01:00Z"),
        ("finished_at", "2026-07-12T00:02:00Z"),
        ("exit_code", 1),
        ("error_code", "FORGED"),
        ("error_message", "forged master evidence"),
        ("log_uri", "s3://forged/log"),
        ("retry_count", 9),
        ("manual_retry_marker", True),
        ("previous_job_id", "job_foreign_previous"),
    ],
)
def test_bound_master_ordinary_upsert_rejects_every_authority_state_field(
    tmp_path: Any,
    source_id: str,
    field: str,
    value: Any,
) -> None:
    from services.orchestrator.accepted_submit_identity import AcceptedSubmitTransition
    from services.orchestrator.file_orchestration_journal import (
        FileOrchestrationJournalError,
        FileOrchestrationJournalRepository,
    )

    repository = _file_cohort_repository(tmp_path, member_count=1, source_id=source_id)
    job_id = f"job_cycle_{source_id}_2026071200_forecast_fixture_forecast"
    key = f"cycle_{source_id}_2026071200_forecast_fixture:forecast"
    assert repository.commit_pipeline_job_submit_attempt(
        key,
        pipeline_job_id=job_id,
        expected_submission_attempt=1,
        slurm_job_id="17667",
        transition=AcceptedSubmitTransition.accounting(
            "matched_bound",
            submit_outcome="accepted",
            matched_slurm_job_id="17667",
            status="submitted",
        ),
    ).outcome == "applied"
    before = repository.get_pipeline_job(job_id)
    before_lines = sum(
        len(path.read_text(encoding="utf-8").splitlines())
        for path in repository.root.glob("journal/**/*.jsonl")
    )

    with pytest.raises(FileOrchestrationJournalError) as error:
        repository.upsert_pipeline_job({**before, field: value})
    assert error.value.field == field
    assert repository.get_pipeline_job(job_id) == before
    assert sum(
        len(path.read_text(encoding="utf-8").splitlines())
        for path in repository.root.glob("journal/**/*.jsonl")
    ) == before_lines
    reopened = FileOrchestrationJournalRepository(repository.root)
    assert reopened.get_pipeline_job(job_id) == before


@pytest.mark.parametrize("source_id", ["gfs", "ifs"])
def test_bound_master_ordinary_upsert_rejects_reconciliation_and_projection_state(
    tmp_path: Any,
    source_id: str,
) -> None:
    from services.orchestrator.accepted_submit_identity import AcceptedSubmitTransition
    from services.orchestrator.file_orchestration_journal import FileOrchestrationJournalError

    repository = _file_cohort_repository(tmp_path, member_count=1, source_id=source_id)
    job_id = f"job_cycle_{source_id}_2026071200_forecast_fixture_forecast"
    key = f"cycle_{source_id}_2026071200_forecast_fixture:forecast"
    assert repository.commit_pipeline_job_submit_attempt(
        key,
        pipeline_job_id=job_id,
        expected_submission_attempt=1,
        slurm_job_id="17667",
        transition=AcceptedSubmitTransition.accepted(status="submitted"),
    ).outcome == "applied"
    before = repository.get_pipeline_job(job_id)
    member = before["cohort_members"][0]
    mutations = (
        {
            "reconciliation_source": "slurm_exact_comment",
            "reconciliation_decision": "accounting_unavailable",
            "reconciliation_reason_class": "coverage_incomplete",
            "matched_slurm_job_id": None,
        },
        {
            "candidate_projections": [
                {
                    "candidate_id": member["candidate_id"],
                    "run_id": member["run_id"],
                    "model_id": member["model_id"],
                    "array_task_id": member["array_task_id"],
                    "array_task_outcome": "unverified",
                    "restart_stage": "forecast",
                    "native_shud_resubmitted": False,
                }
            ]
        },
    )

    for mutation in mutations:
        with pytest.raises(FileOrchestrationJournalError):
            repository.upsert_pipeline_job({**before, **mutation})
        assert repository.get_pipeline_job(job_id) == before


@pytest.mark.parametrize("source_id", ["gfs", "ifs"])
def test_rejected_master_cannot_reclaim_without_typed_absence_retry_proof(
    tmp_path: Any,
    source_id: str,
) -> None:
    from services.orchestrator.file_orchestration_journal import (
        FileOrchestrationJournalError,
        FileOrchestrationJournalRepository,
    )

    repository = _file_cohort_repository(tmp_path, member_count=1, source_id=source_id)
    job_id = f"job_cycle_{source_id}_2026071200_forecast_fixture_forecast"
    key = f"cycle_{source_id}_2026071200_forecast_fixture:forecast"
    rejected = repository.reject_pipeline_job_submit_attempt(
        key,
        pipeline_job_id=job_id,
        expected_submission_attempt=1,
        finished_at=datetime(2026, 7, 12, 0, 1, tzinfo=UTC),
        error_code="SBATCH_REJECTED",
        error_message="scheduler rejected request",
        stage="forecast",
        job_type="run_shud_forecast_array",
    )
    assert rejected.outcome == "applied"
    before = repository.get_pipeline_job(job_id)
    with pytest.raises(FileOrchestrationJournalError):
        repository.upsert_pipeline_job(
            {
                **before,
                "status": "reserved",
                "submit_outcome": None,
                "finished_at": None,
                "error_code": None,
                "error_message": None,
            }
        )
    assert FileOrchestrationJournalRepository(repository.root).get_pipeline_job(job_id) == before
    reclaimed = repository.reclaim_pipeline_job_reservation(
        {
            **before,
            "status": "reserved",
            "submission_attempt": 2,
            "submit_outcome": None,
            "reconciliation_source": None,
            "reconciliation_decision": None,
            "reconciliation_reason_class": None,
            "matched_slurm_job_id": None,
        }
    )
    assert reclaimed is None
    assert repository.get_pipeline_job(job_id) == before


def test_current_version_candidate_master_cross_classification_and_unclassified_rows_fail_closed(
    tmp_path: Any,
) -> None:
    from services.orchestrator.accepted_submit_identity import ACCEPTED_SUBMIT_CONTRACT_VERSION
    from services.orchestrator.file_orchestration_journal import (
        FileOrchestrationJournalError,
        FileOrchestrationJournalRepository,
    )

    repository = FileOrchestrationJournalRepository(tmp_path / "journal")
    candidate = {
        "job_id": "job_fcst_gfs_2026071200_model_0_forecast_candidate_0",
        "run_id": "fcst_gfs_2026071200_model_0",
        "cycle_id": "gfs_2026071200",
        "job_type": "run_shud_forecast_array",
        "array_task_id": 0,
        "model_id": "model_0",
        "status": "succeeded",
        "stage": "forecast",
        "candidate_id": "gfs:2026-07-12T00:00:00Z:model_0:forecast_gfs_deterministic",
        "submit_outcome": "accepted",
        "restart_stage": "forecast",
        "native_shud_resubmitted": False,
        "accepted_submit_contract_version": ACCEPTED_SUBMIT_CONTRACT_VERSION,
    }
    repository.upsert_pipeline_job(candidate)
    candidate = repository.upsert_pipeline_job(
        {**candidate, "status": "failed", "error_code": "SLURM_TASK_FAILED"}
    )
    assert candidate["status"] == "failed"
    before_candidate = repository.get_pipeline_job(candidate["job_id"])

    for mutation in (
        {"stage": "forcing"},
        {"model_id": None},
        {"slurm_ownership_required": True},
        {"cohort_members": [{"array_task_id": 0}], "cohort_digest": "0" * 64},
    ):
        with pytest.raises(FileOrchestrationJournalError):
            repository.upsert_pipeline_job({**candidate, **mutation})
        assert repository.get_pipeline_job(candidate["job_id"]) == before_candidate

    unclassified = {
        **candidate,
        "job_id": "job_cycle_gfs_2026071200_unclassified_forecast",
        "run_id": "cycle_gfs_2026071200_unclassified",
        "model_id": None,
        "array_task_id": None,
        "candidate_id": None,
    }
    with pytest.raises(FileOrchestrationJournalError) as error:
        repository.upsert_pipeline_job(unclassified)
    assert error.value.field == "accepted_submit_row_kind"

    master_repository = _file_cohort_repository(tmp_path / "master", member_count=1)
    master_job_id = "job_cycle_gfs_2026071200_forecast_fixture_forecast"
    before_master = master_repository.get_pipeline_job(master_job_id)
    with pytest.raises(FileOrchestrationJournalError):
        master_repository.upsert_pipeline_job(
            {**before_master, "model_id": "model_0", "array_task_id": 0}
        )
    assert master_repository.get_pipeline_job(master_job_id) == before_master


def test_marker_free_nonforecast_rows_keep_legacy_classification_compatibility() -> None:
    from services.orchestrator.accepted_submit_identity import (
        accepted_submit_row_kind,
        normalize_accepted_submit_evidence,
    )

    legacy = {
        "stage": "forcing",
        "job_type": "produce_forcing_array",
        "cohort_members": [{"array_task_id": 0}],
        "cohort_digest": "historical-unversioned-value",
    }

    assert accepted_submit_row_kind(legacy) is None
    assert normalize_accepted_submit_evidence(legacy) == legacy


def test_master_model_id_corruption_blocks_query_instead_of_becoming_candidate(tmp_path: Any) -> None:
    from services.orchestrator.file_orchestration_journal import FileOrchestrationJournalRepository

    repository = _file_cohort_repository(tmp_path, member_count=2)
    job_id = "job_cycle_gfs_2026071200_forecast_fixture_forecast"
    journal_path = repository.root / "journal" / "gfs" / "2026071200.jsonl"
    records = [json.loads(line) for line in journal_path.read_text(encoding="utf-8").splitlines()]
    for record in records:
        if record.get("record_type") == "pipeline_job" and record["payload"].get("job_id") == job_id:
            record["payload"]["model_id"] = "model_0"
    journal_path.write_text(
        "".join(f"{json.dumps(record, sort_keys=True)}\n" for record in records),
        encoding="utf-8",
    )
    direct_path = repository.root / "pipeline-jobs" / f"{job_id}.json"
    direct = json.loads(direct_path.read_text(encoding="utf-8"))
    direct["payload"]["model_id"] = "model_0"
    direct_path.write_text(json.dumps(direct), encoding="utf-8")

    blocked = FileOrchestrationJournalRepository(repository.root).get_pipeline_job(job_id)

    assert blocked["file_journal"]["status"] == "blocked"
    assert blocked["file_journal"]["field"] == "model_id"
    assert blocked["error_code"] == "file_journal_evidence_invariant_invalid"


@pytest.mark.parametrize("failure", ["too_many", "extra_field", "wrong_member", "duplicate_task"])
def test_reconciliation_projection_api_fails_closed_before_persistence(
    tmp_path: Any,
    failure: str,
) -> None:
    from services.orchestrator.file_orchestration_journal import FileOrchestrationJournalError

    repository = _file_cohort_repository(tmp_path, member_count=2)
    job_id = "job_cycle_gfs_2026071200_forecast_fixture_forecast"
    member = repository.get_pipeline_job(job_id)["cohort_members"][0]
    projection = {
        "candidate_id": member["candidate_id"],
        "run_id": member["run_id"],
        "model_id": member["model_id"],
        "array_task_id": member["array_task_id"],
        "array_task_outcome": "succeeded",
        "restart_stage": "state_save_qc",
        "native_shud_resubmitted": False,
    }
    if failure == "too_many":
        projections = [projection] * 257
    elif failure == "extra_field":
        projections = [{**projection, "credential": "must-not-persist"}]
    elif failure == "wrong_member":
        projections = [{**projection, "candidate_id": "foreign-candidate"}]
    else:
        projections = [projection, projection]
    before = repository.get_pipeline_job(job_id)

    with pytest.raises(FileOrchestrationJournalError):
        repository.record_pipeline_job_reconciliation(job_id, candidate_projections=projections)

    assert repository.get_pipeline_job(job_id) == before
