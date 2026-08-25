"""File-cohort reconcile-by-comment: fail-closed decision branches,
reclaim anchors, and the legacy/current reconciliation recorder contracts.
"""

from __future__ import annotations

import copy
from concurrent.futures import ThreadPoolExecutor
from datetime import (
    UTC,
    datetime,
    timedelta,
)
from typing import Any

import pytest

from services.orchestrator.reconcile import SacctRecord
from tests.gateway_reconcile_helpers import (
    _append_cohort_placeholders,
    _authoritative_absence_query,
    _bind_current_file_cohort,
    _file_cohort_repository,
)


def test_file_cohort_exact_comment_reconcile_distinguishes_all_fail_closed_branches(
    tmp_path: Any,
) -> None:
    from services.orchestrator.file_orchestration_journal import FileOrchestrationJournalRepository
    from services.orchestrator.reconcile import (
        ReconcileQueryUnavailable,
        reconcile_reserved_unbound_jobs,
    )

    key = "cycle_gfs_2026071200_forecast_fixture:forecast"
    exact = SacctRecord(
        slurm_job_id="17667",
        raw_state="RUNNING",
        job_name="nhms_forecast",
        comment=f"nhms_idem:{key}",
        run_id="cycle_gfs_2026071200_forecast_fixture",
        stage="forecast",
        pipeline_job_id="job_cycle_gfs_2026071200_forecast_fixture_forecast",
    )

    def assert_reopen_tuple(repository: Any, outcome: Any, *, submit_outcome: str) -> None:
        persisted = repository.get_pipeline_job(outcome.job_id)
        reopened = FileOrchestrationJournalRepository(repository.root).get_pipeline_job(outcome.job_id)
        expected = (
            submit_outcome,
            outcome.reconciliation_source,
            outcome.reconciliation_decision,
            outcome.matched_slurm_job_id,
        )
        fields = (
            "submit_outcome",
            "reconciliation_source",
            "reconciliation_decision",
            "matched_slurm_job_id",
        )
        assert tuple(persisted[field] for field in fields) == expected
        assert tuple(reopened[field] for field in fields) == expected

    unique = _file_cohort_repository(tmp_path / "unique")
    outcome = reconcile_reserved_unbound_jobs(unique, comment_query=lambda _key: exact)[0]
    assert (outcome.reconciliation_source, outcome.reconciliation_decision, outcome.matched_slurm_job_id) == (
        "slurm_exact_comment",
        "matched_bound",
        "17667",
    )
    assert unique.get_pipeline_job(outcome.job_id)["slurm_job_id"] == "17667"
    assert_reopen_tuple(unique, outcome, submit_outcome="accepted")

    multiple = _file_cohort_repository(tmp_path / "multiple")
    outcome = reconcile_reserved_unbound_jobs(
        multiple,
        comment_query=lambda _key: tuple(
            SacctRecord(**{**exact.__dict__, "slurm_job_id": str(17703 + index)})
            for index in range(10)
        ),
    )[0]
    assert (outcome.reconciliation_source, outcome.reconciliation_decision, outcome.matched_slurm_job_id) == (
        "slurm_exact_comment",
        "multiple_matches_blocked",
        None,
    )
    assert outcome.match_count == 2
    assert multiple.get_pipeline_job(outcome.job_id)["slurm_job_id"] is None
    assert_reopen_tuple(multiple, outcome, submit_outcome="submit_result_ambiguous")

    mismatch = _file_cohort_repository(tmp_path / "mismatch")
    wrong = SacctRecord(**{**exact.__dict__, "stage": "forcing"})
    outcome = reconcile_reserved_unbound_jobs(mismatch, comment_query=lambda _key: wrong)[0]
    assert (outcome.reconciliation_source, outcome.reconciliation_decision, outcome.matched_slurm_job_id) == (
        "slurm_exact_comment",
        "identity_mismatch_blocked",
        None,
    )
    assert mismatch.get_pipeline_job(outcome.job_id)["slurm_job_id"] is None
    assert_reopen_tuple(mismatch, outcome, submit_outcome="submit_result_ambiguous")

    unavailable = _file_cohort_repository(tmp_path / "unavailable")

    def unavailable_query(_key: str) -> None:
        raise ReconcileQueryUnavailable("sacct unavailable at /private/runtime")

    outcome = reconcile_reserved_unbound_jobs(unavailable, comment_query=unavailable_query)[0]
    assert (outcome.reconciliation_source, outcome.reconciliation_decision, outcome.matched_slurm_job_id) == (
        "slurm_exact_comment",
        "accounting_unavailable",
        None,
    )
    persisted = unavailable.get_pipeline_job(outcome.job_id)
    assert persisted["reconciliation_decision"] == "accounting_unavailable"
    assert persisted["reconciliation_reason_class"] == "process_unavailable"
    assert outcome.reconciliation_reason_class == "process_unavailable"
    assert persisted["matched_slurm_job_id"] is None
    assert "/private/runtime" not in str(persisted)
    assert_reopen_tuple(unavailable, outcome, submit_outcome="submit_result_ambiguous")

    wrong_comment = _file_cohort_repository(tmp_path / "wrong-comment")
    wrong = SacctRecord(**{**exact.__dict__, "comment": "nhms_idem:another-reservation"})
    outcome = reconcile_reserved_unbound_jobs(wrong_comment, comment_query=lambda _key: wrong)[0]
    assert (outcome.reconciliation_source, outcome.reconciliation_decision, outcome.matched_slurm_job_id) == (
        "slurm_exact_comment",
        "identity_mismatch_blocked",
        None,
    )
    assert wrong_comment.get_pipeline_job(outcome.job_id)["slurm_job_id"] is None
    assert_reopen_tuple(wrong_comment, outcome, submit_outcome="submit_result_ambiguous")

    from datetime import timedelta

    started_at = datetime(2026, 7, 12, tzinfo=UTC)
    deferred = _file_cohort_repository(tmp_path / "deferred", created_at=started_at)
    outcome = reconcile_reserved_unbound_jobs(
        deferred,
        comment_query=_authoritative_absence_query,
        now=lambda: started_at + timedelta(seconds=1),
    )[0]
    assert (outcome.reconciliation_source, outcome.reconciliation_decision, outcome.matched_slurm_job_id) == (
        "slurm_exact_comment",
        "absence_deferred",
        None,
    )
    assert_reopen_tuple(deferred, outcome, submit_outcome="submit_result_ambiguous")

    expired = _file_cohort_repository(tmp_path / "expired", created_at=started_at)
    outcome = reconcile_reserved_unbound_jobs(
        expired,
        comment_query=_authoritative_absence_query,
        now=lambda: started_at + timedelta(seconds=121),
    )[0]
    assert (outcome.reconciliation_source, outcome.reconciliation_decision, outcome.matched_slurm_job_id) == (
        "slurm_exact_comment",
        "absence_retry_permitted",
        None,
    )
    assert_reopen_tuple(expired, outcome, submit_outcome="submit_result_ambiguous")


@pytest.mark.parametrize("with_runtime_rows", [False, True])
def test_file_cohort_pre_outcome_restart_classifies_ambiguous_before_decision(
    tmp_path: Any,
    with_runtime_rows: bool,
) -> None:

    from services.orchestrator.file_orchestration_journal import FileOrchestrationJournalRepository
    from services.orchestrator.reconcile import reconcile_reserved_unbound_jobs

    started_at = datetime(2026, 7, 12, tzinfo=UTC)
    repository = _file_cohort_repository(
        tmp_path,
        created_at=started_at,
        member_count=2,
        submit_outcome=None,
        with_runtime_rows=with_runtime_rows,
    )
    job_id = "job_cycle_gfs_2026071200_forecast_fixture_forecast"

    outcome = reconcile_reserved_unbound_jobs(
        repository,
        comment_query=_authoritative_absence_query,
        now=lambda: started_at + timedelta(seconds=1),
    )[0]

    # #1850 Fix 1: a pre-outcome row resolves the capability/runtime lane
    # exactly like an already-timeout row. With runtime rows present the
    # exact-comment absence path defers; without them the runtime mismatch
    # takes the ordinary exact-comment streak lane (identity_mismatch_blocked)
    # instead of being admitted past the runtime gate.
    if with_runtime_rows:
        assert outcome.reconciliation_decision == "absence_deferred"
        expected_decision = "absence_deferred"
    else:
        assert outcome.reconciliation_decision == "identity_mismatch_blocked"
        expected_decision = "identity_mismatch_blocked"
    persisted = repository.get_pipeline_job(job_id)
    assert persisted["submit_outcome"] == "submit_result_ambiguous"
    assert persisted["reconciliation_decision"] == expected_decision
    reopened = FileOrchestrationJournalRepository(repository.root).get_pipeline_job(job_id)
    assert reopened == persisted


def test_file_cohort_accounting_proof_separates_owner_and_global_scope(tmp_path: Any) -> None:

    from services.orchestrator.reconcile import (
        ReconcileQueryUnavailable,
        SacctRecord,
        reconcile_reserved_unbound_jobs,
    )

    started_at = datetime(2026, 7, 12, tzinfo=UTC)
    key = "cycle_gfs_2026071200_forecast_fixture:forecast"
    owned = SacctRecord(
        "17667",
        "RUNNING",
        "nhms_forecast",
        comment=f"nhms_idem:{key}",
        user="scheduler",
        account="account",
    )
    foreign = SacctRecord(
        "17668",
        "RUNNING",
        "nhms_forecast",
        comment=f"nhms_idem:{key}",
        user="foreign",
        account="other",
    )

    repository = _file_cohort_repository(
        tmp_path / "owner-match",
        created_at=started_at,
        expected_user="scheduler",
        expected_account="account",
    )
    calls: list[tuple[str | None, str | None]] = []

    def owner_match(
        _key: str,
        *,
        expected_user: str | None = None,
        expected_account: str | None = None,
    ) -> list[Any]:
        calls.append((expected_user, expected_account))
        return [owned] if expected_user else [owned, foreign]

    assert (
        reconcile_reserved_unbound_jobs(repository, comment_query=owner_match)[0].action
        == "identity_mismatch_blocked"
    )
    assert calls == [("scheduler", "account"), (None, None)]

    repository = _file_cohort_repository(
        tmp_path / "globally-unique-owner",
        created_at=started_at,
        expected_user="scheduler",
        expected_account="account",
    )
    calls = []

    def globally_unique_owner(
        _key: str,
        *,
        expected_user: str | None = None,
        expected_account: str | None = None,
    ) -> list[Any]:
        calls.append((expected_user, expected_account))
        return [owned]

    assert (
        reconcile_reserved_unbound_jobs(repository, comment_query=globally_unique_owner)[0].action
        == "bound"
    )
    assert calls == [("scheduler", "account"), (None, None)]

    repository = _file_cohort_repository(
        tmp_path / "foreign-only",
        created_at=started_at,
        expected_user="scheduler",
        expected_account="account",
    )
    outcome = reconcile_reserved_unbound_jobs(repository, comment_query=lambda _key: [foreign])[0]
    assert outcome.action == "identity_mismatch_blocked"

    repository = _file_cohort_repository(
        tmp_path / "two-owned",
        created_at=started_at,
        expected_user="scheduler",
        expected_account="account",
    )
    second_owned = SacctRecord(**{**owned.__dict__, "slurm_job_id": "17669"})
    assert (
        reconcile_reserved_unbound_jobs(repository, comment_query=lambda _key: [owned, second_owned])[0].action
        == "multiple_matches_blocked"
    )

    repository = _file_cohort_repository(
        tmp_path / "global-zero",
        created_at=started_at,
        expected_user="scheduler",
        expected_account="account",
    )
    scopes: list[tuple[str | None, str | None]] = []

    def global_zero(
        _key: str,
        *,
        expected_user: str | None = None,
        expected_account: str | None = None,
    ) -> Any:
        from services.orchestrator.reconcile import CommentAccountingResult

        scopes.append((expected_user, expected_account))
        return CommentAccountingResult(
            (),
            scope="global" if expected_user is None and expected_account is None else "owner",
            coverage_start=started_at,
            coverage_end=started_at + timedelta(seconds=121),
            coverage_complete=True,
        )

    outcome = reconcile_reserved_unbound_jobs(
        repository,
        comment_query=global_zero,
        now=lambda: started_at + timedelta(seconds=121),
    )[0]
    assert outcome.action == "absence_retry_permitted"
    assert scopes == [("scheduler", "account"), (None, None)]

    repository = _file_cohort_repository(
        tmp_path / "global-unavailable",
        created_at=started_at,
        expected_user="scheduler",
        expected_account="account",
    )

    def global_unavailable(
        _key: str,
        *,
        expected_user: str | None = None,
        expected_account: str | None = None,
    ) -> list[Any]:
        if expected_user:
            return [owned]
        raise ReconcileQueryUnavailable("global accounting unavailable")

    assert (
        reconcile_reserved_unbound_jobs(repository, comment_query=global_unavailable)[0].action
        == "query_unavailable"
    )


def test_file_cohort_authoritative_absence_allows_one_atomic_retry(tmp_path: Any) -> None:
    from datetime import timedelta

    from services.orchestrator.reconcile import reconcile_reserved_unbound_jobs

    created_at = datetime(2026, 7, 12, tzinfo=UTC)
    repository = _file_cohort_repository(tmp_path, created_at=created_at)
    _append_cohort_placeholders(repository)

    def reconcile() -> Any:
        return reconcile_reserved_unbound_jobs(
            repository,
            comment_query=_authoritative_absence_query,
            grace=timedelta(seconds=120),
            now=lambda: created_at + timedelta(seconds=121),
        )

    with ThreadPoolExecutor(max_workers=2) as pool:
        outcomes = [item for batch in pool.map(lambda _index: reconcile(), range(2)) for item in batch]

    assert sum(item.status == "reservation_lost" for item in outcomes) == 1
    row = repository.get_pipeline_job("job_cycle_gfs_2026071200_forecast_fixture_forecast")
    assert row["reconciliation_decision"] == "absence_retry_permitted"
    assert row["matched_slurm_job_id"] is None
    assert all(
        repository._hydro_run_for(f"fcst_gfs_2026071200_model_{index}")["status"] == "failed"
        for index in range(18)
    )


@pytest.mark.parametrize("source_id", ["gfs", "ifs"])
def test_file_cohort_reclaim_begins_attempt_with_fresh_locked_anchor_and_cas(
    tmp_path: Any,
    monkeypatch: pytest.MonkeyPatch,
    source_id: str,
) -> None:

    from services.orchestrator import file_orchestration_journal as journal_module
    from services.orchestrator.accepted_submit_identity import (
        AcceptedSubmitTransition,
        forecast_cohort_digest,
    )
    from services.orchestrator.file_orchestration_journal import FileOrchestrationJournalRepository
    from services.orchestrator.reconcile import reconcile_reserved_unbound_jobs

    attempt_one_started_at = datetime(2026, 7, 12, tzinfo=UTC)
    repository = _file_cohort_repository(
        tmp_path,
        created_at=attempt_one_started_at,
        member_count=1,
        source_id=source_id,
    )
    outcome = reconcile_reserved_unbound_jobs(
        repository,
        comment_query=_authoritative_absence_query,
        grace=timedelta(seconds=120),
        now=lambda: attempt_one_started_at + timedelta(seconds=121),
    )[0]
    assert outcome.action == "absence_retry_permitted"
    job_id = f"job_cycle_{source_id}_2026071200_forecast_fixture_forecast"
    attempt_one = repository.get_pipeline_job(job_id)
    assert attempt_one["submission_attempt"] == 1
    assert attempt_one["cancellation_receipt_recorded"] is False
    assert attempt_one["submit_outcome"] == "submit_result_ambiguous"
    assert attempt_one["reconciliation_decision"] == "absence_retry_permitted"

    request_anchor = attempt_one_started_at + timedelta(seconds=122)
    locked_anchor = attempt_one_started_at + timedelta(seconds=123)
    request = {
        **attempt_one,
        "expected_submission_attempt": attempt_one["submission_attempt"],
        "expected_submission_attempt_started_at": attempt_one["submission_attempt_started_at"],
        "status": "reserved",
        "submission_attempt": 2,
        "submission_attempt_started_at": request_anchor,
        "submit_outcome": None,
        "reconciliation_source": None,
        "reconciliation_decision": None,
        "matched_slurm_job_id": None,
    }
    changed_identity = copy.deepcopy(request)
    changed_identity["cohort_members"][0]["basin_id"] = "foreign-basin"
    changed_identity["cohort_digest"] = forecast_cohort_digest(changed_identity)
    assert repository.reclaim_pipeline_job_reservation(changed_identity) is None
    assert repository.get_pipeline_job(job_id) == attempt_one

    monkeypatch.setattr(journal_module, "_utcnow", lambda: locked_anchor)
    reclaimed = repository.reclaim_pipeline_job_reservation(request)

    assert reclaimed is not None
    assert reclaimed["cancellation_receipt_recorded"] is False
    fields = (
        "submission_attempt",
        "status",
        "submit_outcome",
        "reconciliation_source",
        "reconciliation_decision",
        "matched_slurm_job_id",
    )
    expected = (2, "reserved", None, None, None, None)
    assert tuple(reclaimed[field] for field in fields) == expected
    assert reclaimed["submission_attempt_started_at"] == locked_anchor.isoformat().replace("+00:00", "Z")
    assert reclaimed["submission_attempt_started_at"] != request_anchor.isoformat().replace("+00:00", "Z")
    assert tuple(repository.get_pipeline_job(job_id)[field] for field in fields) == expected
    assert repository.get_pipeline_job(job_id)["cancellation_receipt_recorded"] is False
    reopened = FileOrchestrationJournalRepository(repository.root)
    assert tuple(reopened.get_pipeline_job(job_id)[field] for field in fields) == expected
    assert reopened.get_pipeline_job(job_id)["cancellation_receipt_recorded"] is False

    with pytest.raises(journal_module.FileOrchestrationJournalError) as immutable:
        repository.upsert_pipeline_job(
            {
                **reclaimed,
                "submission_attempt_started_at": locked_anchor + timedelta(seconds=1),
            }
        )
    assert immutable.value.field == "submission_attempt_started_at"

    key = str(reclaimed["idempotency_key"])
    stale = repository.commit_pipeline_job_submit_attempt(
        key,
        pipeline_job_id=job_id,
        expected_submission_attempt=1,
        slurm_job_id="17667",
        transition=AcceptedSubmitTransition.accepted(status="submitted"),
    )
    assert stale.outcome == "stale"
    assert repository.get_pipeline_job(job_id)["slurm_job_id"] is None
    committed = repository.commit_pipeline_job_submit_attempt(
        key,
        pipeline_job_id=job_id,
        expected_submission_attempt=2,
        slurm_job_id="17667",
        transition=AcceptedSubmitTransition.accepted(status="submitted"),
    )
    assert committed.outcome == "applied"


def _reclaim_init_state_identity(index: int, *, generation: str) -> dict[str, Any]:
    """One well-formed per-model init-state identity for the reclaim cohort."""

    return {
        "array_task_id": index,
        "model_id": f"model_{index}",
        "init_state_id": f"state_gfs_model_{index}_2026071200_{generation}",
        "init_state_checksum": f"sha256:{index}" + generation[-3:] + "a" * 59,
        "init_state_uri": f"s3://nhms/states/gfs/model_{index}/2026071200/{generation}.cfg.ic",
        "init_state_valid_time": "2026-07-12T00:00:00Z",
    }


def test_reclaimed_reservation_keeps_the_first_attempts_init_state_mapping(
    tmp_path: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """#1188 J13/J14/J15: reclaim is keep-first for the init-state mapping.

    A reclaim opens a new submission attempt, so "stable from reservation" is
    ambiguous at exactly this boundary. The adjudication is keep-first: the
    reclaim request's freshly recomputed mapping is dropped and the first
    attempt's mapping stays authoritative, all the way through to the terminal
    per-model rows projected after the new attempt binds.
    """


    from services.orchestrator import file_orchestration_journal as journal_module
    from services.orchestrator.reconcile import reconcile_reserved_unbound_jobs
    from tests.test_file_orchestration_journal import _durable_pipeline_job_payloads

    member_count = 2
    attempt_one_started_at = datetime(2026, 7, 12, tzinfo=UTC)
    mapping_a = [
        _reclaim_init_state_identity(index, generation="attempt_one")
        for index in range(member_count)
    ]
    mapping_b = [
        _reclaim_init_state_identity(index, generation="attempt_two")
        for index in range(member_count)
    ]
    assert mapping_a != mapping_b

    repository = _file_cohort_repository(
        tmp_path / "keep-first",
        created_at=attempt_one_started_at,
        member_count=member_count,
        init_state_identities=mapping_a,
    )
    journal_root = tmp_path / "keep-first" / "journal"
    job_id = "job_cycle_gfs_2026071200_forecast_fixture_forecast"

    outcome = reconcile_reserved_unbound_jobs(
        repository,
        comment_query=_authoritative_absence_query,
        grace=timedelta(seconds=120),
        now=lambda: attempt_one_started_at + timedelta(seconds=121),
    )[0]
    assert outcome.action == "absence_retry_permitted"
    attempt_one = repository.get_pipeline_job(job_id)
    assert attempt_one["status"] == "reservation_lost"
    assert _durable_pipeline_job_payloads(journal_root, job_id)[-1]["init_state_identities"] == (
        mapping_a
    )

    locked_anchor = attempt_one_started_at + timedelta(seconds=123)
    monkeypatch.setattr(journal_module, "_utcnow", lambda: locked_anchor)
    reclaimed = repository.reclaim_pipeline_job_reservation(
        {
            **attempt_one,
            "expected_submission_attempt": attempt_one["submission_attempt"],
            "expected_submission_attempt_started_at": attempt_one["submission_attempt_started_at"],
            "status": "reserved",
            "submission_attempt": 2,
            "submission_attempt_started_at": attempt_one_started_at + timedelta(seconds=122),
            "submit_outcome": None,
            "reconciliation_source": None,
            "reconciliation_decision": None,
            "matched_slurm_job_id": None,
            "init_state_identities": mapping_b,
        }
    )
    monkeypatch.undo()

    assert reclaimed is not None
    # J14: the reclaim really took the success path — a request rejected by an
    # identity gate would leave every one of these unchanged and make the
    # keep-first assertion below vacuous.
    assert reclaimed["submission_attempt"] == attempt_one["submission_attempt"] + 1
    assert reclaimed["status"] == "reserved"
    assert reclaimed["submission_attempt_started_at"] == locked_anchor.isoformat().replace(
        "+00:00", "Z"
    )
    assert reclaimed["submission_attempt_started_at"] != attempt_one["submission_attempt_started_at"]

    # J13: the new attempt kept the FIRST attempt's mapping, at the durable layer.
    assert _durable_pipeline_job_payloads(journal_root, job_id)[-1]["init_state_identities"] == (
        mapping_a
    )

    # J15: and the adjudication reaches the lineage evidence, not just the master.
    _bind_current_file_cohort(
        repository,
        str(reclaimed["idempotency_key"]),
        slurm_job_id="17667",
    )
    members = repository.get_pipeline_job(job_id)["cohort_members"]
    repository.project_forecast_cohort_tasks(
        job_id,
        master_slurm_job_id="17667",
        projections=[
            {
                **member,
                "array_task_outcome": "succeeded",
                "task_slurm_job_id": f"17667_{index}",
                "restart_stage": "forecast",
                "native_shud_resubmitted": False,
            }
            for index, member in enumerate(members)
        ],
        complete=True,
        master_status="succeeded",
        master_error_code=None,
        reconciliation_decision="matched_bound",
    )

    for index in range(member_count):
        terminal_job_id = (
            f"job_fcst_gfs_2026071200_model_{index}_forecast_reconciled_17667_{index}"
        )
        terminal = _durable_pipeline_job_payloads(journal_root, terminal_job_id)[-1]
        assert terminal["init_state_identities"] == [mapping_a[index]]


@pytest.mark.parametrize(
    "decision",
    [
        "matched_bound",
        "absence_deferred",
        "absence_retry_permitted",
        "multiple_matches_blocked",
        "identity_mismatch_blocked",
        "accounting_unavailable",
    ],
)
def test_legacy_file_cohort_reconciliation_recorder_contract(tmp_path: Any, decision: str) -> None:
    from services.orchestrator.file_orchestration_journal import FileOrchestrationJournalRepository

    repository = _file_cohort_repository(tmp_path / decision, member_count=2, versioned=False)
    job_id = "job_cycle_gfs_2026071200_forecast_fixture_forecast"
    matched = "17667" if decision == "matched_bound" else None

    emitted = repository.record_pipeline_job_reconciliation(
        job_id,
        submit_outcome="accepted" if decision == "matched_bound" else "submit_result_ambiguous",
        reconciliation_decision=decision,
        matched_slurm_job_id=matched,
    )
    reopened = FileOrchestrationJournalRepository(repository.root).get_pipeline_job(job_id)

    assert emitted is not None
    fields = (
        "submit_outcome",
        "reconciliation_source",
        "reconciliation_decision",
        "matched_slurm_job_id",
    )
    assert tuple(emitted[field] for field in fields) == tuple(reopened[field] for field in fields)


def test_current_master_generic_mutation_apis_are_zero_write_but_legacy_stays_compatible(
    tmp_path: Any,
) -> None:
    from services.orchestrator.file_orchestration_journal import (
        FileOrchestrationJournalError,
        FileOrchestrationJournalRepository,
    )

    repository = _file_cohort_repository(tmp_path / "current", member_count=1)
    job_id = "job_cycle_gfs_2026071200_forecast_fixture_forecast"
    before = repository.get_pipeline_job(job_id)
    before_files = {
        str(path.relative_to(repository.root)): path.read_bytes()
        for path in repository.root.rglob("*")
        if path.is_file()
    }
    mutations = (
        lambda: repository.record_pipeline_job_reconciliation(
            job_id,
            reconciliation_decision="absence_retry_permitted",
            submit_outcome="submit_result_ambiguous",
            status="reservation_lost",
        ),
        lambda: repository.update_pipeline_job_status(job_id, "reservation_lost"),
        lambda: repository.update_job_status(job_id, "reservation_lost"),
    )
    for mutate in mutations:
        with pytest.raises(FileOrchestrationJournalError):
            mutate()
        assert repository.get_pipeline_job(job_id) == before
        assert {
            str(path.relative_to(repository.root)): path.read_bytes()
            for path in repository.root.rglob("*")
            if path.is_file()
        } == before_files
        assert FileOrchestrationJournalRepository(repository.root).get_pipeline_job(job_id) == before

    legacy = _file_cohort_repository(
        tmp_path / "legacy",
        member_count=1,
        versioned=False,
    )
    legacy.update_job_status(job_id, "running")
    updated = legacy.record_pipeline_job_reconciliation(
        job_id,
        submit_outcome="submit_result_ambiguous",
        reconciliation_decision="absence_deferred",
        status="running",
    )
    assert updated is not None
    assert updated["status"] == "running"
    assert updated["reconciliation_decision"] == "absence_deferred"


@pytest.mark.parametrize(
    ("decision", "reason_class"),
    [
        ("accounting_unavailable", "process_unavailable"),
        ("identity_mismatch_blocked", None),
        ("multiple_matches_blocked", None),
        ("absence_deferred", None),
    ],
)
def test_identical_typed_reconciliation_transition_is_true_zero_write(
    tmp_path: Any,
    decision: str,
    reason_class: str | None,
) -> None:
    from services.orchestrator.accepted_submit_identity import (
        ACCEPTED_SUBMIT_CONTRACT_VERSION,
        AcceptedSubmitTransition,
    )
    from services.orchestrator.file_orchestration_journal import FileOrchestrationJournalRepository

    repository = _file_cohort_repository(tmp_path / decision, member_count=1)
    job_id = "job_cycle_gfs_2026071200_forecast_fixture_forecast"
    transition = AcceptedSubmitTransition.accounting(
        decision,
        submit_outcome="submit_result_ambiguous",
        reconciliation_reason_class=reason_class,
        status="reserved",
    )
    first = repository.transition_pipeline_job_submit_evidence(
        job_id,
        transition,
        accepted_submit_contract_version=ACCEPTED_SUBMIT_CONTRACT_VERSION,
        expected_submission_attempt=1,
        expected_statuses=("reserved",),
        require_unbound=True,
    )
    before = repository.get_pipeline_job(job_id)
    before_files = {
        str(path.relative_to(repository.root)): path.read_bytes()
        for path in repository.root.rglob("*")
        if path.is_file()
    }
    second = repository.transition_pipeline_job_submit_evidence(
        job_id,
        transition,
        accepted_submit_contract_version=ACCEPTED_SUBMIT_CONTRACT_VERSION,
        expected_submission_attempt=1,
        expected_statuses=("reserved",),
        require_unbound=True,
    )

    assert first.outcome in {"applied", "idempotent"}
    assert second.outcome == "idempotent"
    assert repository.get_pipeline_job(job_id) == before
    assert repository.get_pipeline_job(job_id)["updated_at"] == before["updated_at"]
    assert {
        str(path.relative_to(repository.root)): path.read_bytes()
        for path in repository.root.rglob("*")
        if path.is_file()
    } == before_files
    assert FileOrchestrationJournalRepository(repository.root).get_pipeline_job(job_id) == before


def test_changed_typed_reconciliation_transition_writes_once(tmp_path: Any) -> None:
    from services.orchestrator.accepted_submit_identity import (
        ACCEPTED_SUBMIT_CONTRACT_VERSION,
        AcceptedSubmitTransition,
    )

    repository = _file_cohort_repository(tmp_path, member_count=1)
    job_id = "job_cycle_gfs_2026071200_forecast_fixture_forecast"
    first = repository.transition_pipeline_job_submit_evidence(
        job_id,
        AcceptedSubmitTransition.accounting(
            "accounting_unavailable",
            submit_outcome="submit_result_ambiguous",
            reconciliation_reason_class="process_unavailable",
            status="reserved",
        ),
        accepted_submit_contract_version=ACCEPTED_SUBMIT_CONTRACT_VERSION,
        expected_submission_attempt=1,
        expected_statuses=("reserved",),
        require_unbound=True,
    )
    changed = repository.transition_pipeline_job_submit_evidence(
        job_id,
        AcceptedSubmitTransition.accounting(
            "identity_mismatch_blocked",
            submit_outcome="submit_result_ambiguous",
            status="reserved",
        ),
        accepted_submit_contract_version=ACCEPTED_SUBMIT_CONTRACT_VERSION,
        expected_submission_attempt=1,
        expected_statuses=("reserved",),
        require_unbound=True,
    )
    assert first.outcome == "applied"
    assert changed.outcome == "applied"
    assert repository.get_pipeline_job(job_id)["reconciliation_decision"] == "identity_mismatch_blocked"


def test_file_cohort_absence_uses_immutable_attempt_anchor_and_configured_window(
    tmp_path: Any,
) -> None:

    from services.orchestrator.reconcile import reconcile_reserved_unbound_jobs

    started_at = datetime(2026, 7, 12, tzinfo=UTC)
    repository = _file_cohort_repository(tmp_path, created_at=started_at)
    job_id = "job_cycle_gfs_2026071200_forecast_fixture_forecast"
    at_121 = reconcile_reserved_unbound_jobs(
        repository,
        comment_query=_authoritative_absence_query,
        grace=timedelta(seconds=300),
        now=lambda: started_at + timedelta(seconds=121),
    )[0]
    at_301 = reconcile_reserved_unbound_jobs(
        repository,
        comment_query=_authoritative_absence_query,
        grace=timedelta(seconds=300),
        now=lambda: started_at + timedelta(seconds=301),
    )[0]

    assert at_121.action == "absence_unconfirmed"
    assert at_301.action == "absence_retry_permitted"
    row = repository.get_pipeline_job(job_id)
    assert row["submission_attempt_started_at"] == "2026-07-12T00:00:00Z"
