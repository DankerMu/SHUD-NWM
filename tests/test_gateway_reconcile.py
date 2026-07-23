"""Tests for restart reconcile-by-identity of in-flight Slurm jobs.

Reconcile MUST read job ids from the durable ``pipeline_job`` table (not gateway
memory), verify candidate identity via ``sacct``, and never resubmit a
still-running or already-terminal candidate.
"""

from __future__ import annotations

import copy
import json
import os
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from sqlalchemy import create_engine, event
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from services.orchestrator.persistence import Base, PipelineJob, PipelineStore
from services.orchestrator.reconcile import (
    RECONCILE_UNVERIFIED_STATUS,
    SacctRecord,
    reconcile_inflight_jobs,
)


def _authoritative_absence_query(
    _key: str,
    **kwargs: Any,
) -> Any:
    from services.orchestrator.reconcile import CommentAccountingResult

    anchor = kwargs.get("submission_attempt_started_at") or datetime(2026, 7, 12, tzinfo=UTC)
    return CommentAccountingResult(
        (),
        scope="global",
        coverage_start=anchor,
        coverage_end=anchor,
        coverage_complete=True,
    )


class _StoreRepo:
    """Repository-shaped wrapper over PipelineStore for reservation tests.

    Exposes the ``reserve_pipeline_job``/``bind_pipeline_job_reservation``/
    ``query_candidate_state`` surface the chain repository implements, backed by
    the in-memory store, so the durable two-phase protocol is exercised exactly
    as production would.
    """

    def __init__(self, store: PipelineStore) -> None:
        self.store = store

    def query_candidate_state(self, idempotency_key: str):
        job = self.store.query_candidate_state(idempotency_key)
        return _job_dict(job) if job is not None else None

    def reserve_pipeline_job(self, record: dict[str, Any]) -> dict[str, Any] | None:
        # Mirror the production contract: INSERT ... ON CONFLICT DO NOTHING
        # RETURNING. A returned row == this caller won; None == a row already
        # existed. The unique idempotency_key index is the race backstop.
        job = self.store.reserve_job(
            job_id=record["job_id"],
            run_id=record.get("run_id"),
            cycle_id=record.get("cycle_id"),
            job_type=record["job_type"],
            model_id=record.get("model_id"),
            stage=record.get("stage"),
            status=record.get("status", "reserved"),
            idempotency_key=record["idempotency_key"],
            candidate_id=record.get("candidate_id"),
        )
        return _job_dict(job) if job is not None else None

    def reclaim_pipeline_job_reservation(self, record: dict[str, Any]) -> dict[str, Any] | None:
        # Mirror the production conditional UPDATE: only a DEAD reservation
        # (slurm_job_id IS NULL AND status IN submission_failed/reservation_lost)
        # is re-claimed back to 'reserved'; a live row never matches.
        job = self.store.reclaim_reservation(
            record["idempotency_key"],
            run_id=record.get("run_id"),
            cycle_id=record.get("cycle_id"),
            model_id=record.get("model_id"),
            stage=record.get("stage"),
            candidate_id=record.get("candidate_id"),
        )
        return _job_dict(job) if job is not None else None

    def bind_pipeline_job_reservation(
        self,
        idempotency_key: str,
        *,
        slurm_job_id: str,
        status: str = "submitted",
        array_task_id: int | None = None,
    ):
        job = self.store.bind_reservation(
            idempotency_key,
            slurm_job_id=slurm_job_id,
            status=status,
            array_task_id=array_task_id,
        )
        return _job_dict(job) if job is not None else None


def _job_dict(job: PipelineJob) -> dict[str, Any]:
    return {
        "job_id": job.job_id,
        "run_id": job.run_id,
        "cycle_id": job.cycle_id,
        "job_type": job.job_type,
        "slurm_job_id": job.slurm_job_id,
        "model_id": job.model_id,
        "status": job.status,
        "stage": job.stage,
        "idempotency_key": job.idempotency_key,
        "candidate_id": job.candidate_id,
    }


def _store_repo() -> _StoreRepo:
    return _StoreRepo(_store())


def _store() -> PipelineStore:
    engine = create_engine(
        "sqlite://",
        future=True,
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )

    @event.listens_for(engine, "connect")
    def _attach_schemas(dbapi_connection: Any, _record: Any) -> None:
        dbapi_connection.execute("ATTACH DATABASE ':memory:' AS ops")

    Base.metadata.create_all(engine)
    return PipelineStore(Session(engine))


def _file_cohort_repository(
    tmp_path: Any,
    *,
    created_at: datetime | None = None,
    member_count: int = 18,
    expected_user: str | None = None,
    expected_account: str | None = None,
    corrupt_digest: bool = False,
    with_runtime_rows: bool = True,
    submit_outcome: str | None = "submit_result_ambiguous",
    versioned: bool = True,
    source_id: str = "gfs",
) -> Any:
    from packages.common.source_identity import normalize_source_id
    from services.orchestrator.accepted_submit_identity import (
        ACCEPTED_SUBMIT_CONTRACT_VERSION,
        forecast_cohort_digest,
    )
    from services.orchestrator.chain_config import scenario_for_source
    from services.orchestrator.file_orchestration_journal import FileOrchestrationJournalRepository

    repository = FileOrchestrationJournalRepository(tmp_path / "journal")
    cycle_time = datetime(2026, 7, 12, tzinfo=UTC)
    canonical_source_id = normalize_source_id(source_id)
    source_id = canonical_source_id.lower()
    scenario_id = scenario_for_source(canonical_source_id)
    record = {
            "accepted_submit_contract_version": ACCEPTED_SUBMIT_CONTRACT_VERSION,
            "job_id": f"job_cycle_{source_id}_2026071200_forecast_fixture_forecast",
            "run_id": f"cycle_{source_id}_2026071200_forecast_fixture",
            "source_id": canonical_source_id,
            "cycle_id": f"{source_id}_2026071200",
            "job_type": "run_shud_forecast_array",
            "model_id": None,
            "stage": "forecast",
            "idempotency_key": f"cycle_{source_id}_2026071200_forecast_fixture:forecast",
            "slurm_comment": f"nhms_idem:cycle_{source_id}_2026071200_forecast_fixture:forecast",
            "submit_outcome": None if versioned else submit_outcome,
            "restart_stage": "forecast",
            "cohort_members": [
                {
                    "array_task_id": index,
                    "candidate_id": f"{canonical_source_id}:2026-07-12T00:00:00Z:model_{index}:{scenario_id}",
                    "run_id": f"fcst_{source_id}_2026071200_model_{index}",
                    "model_id": f"model_{index}",
                    "basin_id": f"basin_{index}",
                    "scenario_id": scenario_id,
                    "restart_stage": "forecast",
                }
                for index in range(member_count)
            ],
            "submission_attempt": 1,
            "submission_attempt_started_at": created_at or cycle_time,
            "expected_slurm_user": expected_user,
            "expected_slurm_account": expected_account,
            "slurm_ownership_required": bool(expected_user and expected_account),
            "created_at": created_at or cycle_time,
            "updated_at": created_at or cycle_time,
        }
    record["cohort_digest"] = forecast_cohort_digest(record)
    if not versioned:
        record.pop("accepted_submit_contract_version")
    if corrupt_digest:
        record["cohort_digest"] = "0" * 64
    repository.reserve_pipeline_job(record)
    if versioned and submit_outcome == "submit_result_ambiguous":
        from services.orchestrator.accepted_submit_identity import AcceptedSubmitTransition

        repository.transition_pipeline_job_submit_evidence(
            record["job_id"],
            AcceptedSubmitTransition.timeout(),
            accepted_submit_contract_version=ACCEPTED_SUBMIT_CONTRACT_VERSION,
            expected_submission_attempt=1,
            expected_statuses=("reserved",),
            require_unbound=True,
        )
    if with_runtime_rows:
        _append_cohort_placeholders(repository, member_count, source_id=source_id)
    return repository


def _bind_current_file_cohort(
    repository: Any,
    idempotency_key: str,
    *,
    slurm_job_id: str,
    status: str = "submitted",
) -> None:
    from services.orchestrator.accepted_submit_identity import AcceptedSubmitTransition

    current = repository.query_candidate_state(idempotency_key)
    assert current is not None
    result = repository.commit_pipeline_job_submit_attempt(
        idempotency_key,
        pipeline_job_id=str(current["job_id"]),
        expected_submission_attempt=int(current.get("submission_attempt") or 1),
        slurm_job_id=slurm_job_id,
        transition=AcceptedSubmitTransition.accepted(status=status),
    )
    assert result.committed


def _append_cohort_placeholders(repository: Any, count: int = 18, *, source_id: str = "gfs") -> None:
    from packages.common.source_identity import normalize_source_id
    from services.orchestrator.chain_config import scenario_for_source

    canonical_source_id = normalize_source_id(source_id)
    source_id = canonical_source_id.lower()
    scenario_id = scenario_for_source(canonical_source_id)
    for index in range(count):
        repository.append_historical_hydro_run(
            {
                "run_id": f"fcst_{source_id}_2026071200_model_{index}",
                "candidate_id": f"{canonical_source_id}:2026-07-12T00:00:00Z:model_{index}:{scenario_id}",
                "run_type": "forecast",
                "scenario_id": scenario_id,
                "model_id": f"model_{index}",
                "basin_id": f"basin_{index}",
                "array_task_id": index,
                "basin_version_id": f"basin_v{index}",
                "forcing_version_id": f"forc_{source_id}_2026071200_model_{index}",
                "init_state_id": f"state_{index}",
                "source_id": canonical_source_id,
                "cycle_time": "2026-07-12T00:00:00Z",
                "start_time": "2026-07-12T00:00:00Z",
                "end_time": "2026-07-12T18:00:00Z",
                "status": "failed",
                "submission_attempt": 1,
                "run_manifest_uri": f"s3://nhms/runs/model_{index}/run-manifest.json",
                "output_uri": f"s3://nhms/runs/model_{index}/output",
                "log_uri": f"s3://nhms/runs/model_{index}/logs",
                "error_code": "SLURM_GATEWAY_UNAVAILABLE",
                "error_message": "transport timeout",
            }
        )


def _seed_unrelated_history(repository: Any, *, count: int = 10) -> None:
    for index in range(count):
        cycle_time = datetime(2026, 6, 1, tzinfo=UTC) + timedelta(hours=index * 6)
        stamp = cycle_time.strftime("%Y%m%d%H")
        repository.append_historical_pipeline_job(
            {
                "job_id": f"job_fcst_gfs_{stamp}_history_model_{index}",
                "run_id": f"fcst_gfs_{stamp}_history_model_{index}",
                "cycle_id": f"gfs_{stamp}",
                "job_type": "run_shud_forecast_array",
                "model_id": f"history_model_{index}",
                "status": "succeeded",
                "stage": "forecast",
                "candidate_id": f"history_{index}",
            }
        )
    malformed_direct = repository.root / "pipeline-jobs" / "job_unrelated_malformed.json"
    malformed_direct.parent.mkdir(parents=True, exist_ok=True)
    malformed_direct.write_text("{not-json", encoding="utf-8")
    malformed_latest = repository.root / "latest" / "gfs" / "2025010100" / "bad.json"
    malformed_latest.parent.mkdir(parents=True, exist_ok=True)
    malformed_latest.write_text("[]", encoding="utf-8")
    malformed_journal = repository.root / "journal" / "gfs" / "2025010100.jsonl"
    malformed_journal.parent.mkdir(parents=True, exist_ok=True)
    malformed_journal.write_text("{not-json\n", encoding="utf-8")


def test_file_cohort_exact_comment_reconcile_distinguishes_all_fail_closed_branches(
    tmp_path: Any,
) -> None:
    from services.orchestrator.file_orchestration_journal import FileOrchestrationJournalRepository
    from services.orchestrator.reconcile import (
        ReconcileQueryUnavailable,
        SacctRecord,
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
    from datetime import timedelta

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

    assert outcome.reconciliation_decision == "absence_deferred"
    persisted = repository.get_pipeline_job(job_id)
    assert persisted["submit_outcome"] == "submit_result_ambiguous"
    assert persisted["reconciliation_decision"] == "absence_deferred"
    reopened = FileOrchestrationJournalRepository(repository.root).get_pipeline_job(job_id)
    assert reopened == persisted


def test_file_cohort_accounting_proof_separates_owner_and_global_scope(tmp_path: Any) -> None:
    from datetime import timedelta

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
    from concurrent.futures import ThreadPoolExecutor
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
    from datetime import timedelta

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
    from datetime import timedelta

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
    assert (failed_decision.action, failed_decision.reason) == ("retry", "retry_failed_candidate")


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

    def counted_read_jsonl(path: Any) -> Any:
        calls["read_jsonl"] += 1
        return original_read_jsonl(path)

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

    def counted_read_jsonl(path: Any) -> Any:
        calls["read_jsonl"] += 1
        return original_read_jsonl(path)

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


def _make_inflight_job(
    store: PipelineStore,
    *,
    job_id: str,
    slurm_job_id: str,
    stage: str = "run_shud_forecast_array",
    status: str = "running",
    run_id: str = "run_1",
    model_id: str = "model_1",
    array_task_id: int | None = None,
) -> None:
    job = store.create_job(
        job_id=job_id,
        run_id=run_id,
        cycle_id="cycle_1",
        job_type=stage,
        slurm_job_id=slurm_job_id,
        model_id=model_id,
        stage=stage,
        status=status,
    )
    if array_task_id is not None:
        job.array_task_id = array_task_id
        store.session.add(job)
        store.session.commit()


def _fake_sacct(records: dict[str, SacctRecord | None]):
    """Fake sacct querier backed by a dict; ``None`` => unknown to accounting."""

    def _query(slurm_job_id: str) -> SacctRecord | None:
        return records.get(str(slurm_job_id))

    return _query


def _past_grace_now(store: _StoreRepo, grace: Any) -> Any:
    """A tz-aware ``now`` just past ``grace`` for the sole reserved-unbound row.

    The reconcile grace guard anchors on ``updated_at`` (refreshed by reserve,
    reclaim, and bind), so the clock must be driven past grace relative to that
    anchor. SQLite returns naive timestamps; normalize to UTC so the injected
    clock is comparable with the reconcile guard's tz-aware arithmetic.
    """

    from datetime import UTC, timedelta

    anchor = store.store.query_reserved_unbound_jobs()[0].updated_at
    if anchor.tzinfo is None:
        anchor = anchor.replace(tzinfo=UTC)
    return anchor + grace + timedelta(seconds=1)


def test_restart_reconcile_reads_pipeline_job_not_memory() -> None:
    # Durable in-flight job exists; gateway memory (_jobs) is irrelevant/empty.
    store = _store()
    _make_inflight_job(store, job_id="job_a", slurm_job_id="99001")

    sacct = _fake_sacct(
        {
            "99001": SacctRecord(
                slurm_job_id="99001",
                raw_state="COMPLETED",
                job_name="nhms_run_shud_forecast_array",
                exit_code="0:0",
            )
        }
    )

    outcomes = reconcile_inflight_jobs(store, sacct_query=sacct)

    assert len(outcomes) == 1
    assert outcomes[0].slurm_job_id == "99001"
    # State came from durable DB row + sacct, not any in-memory gateway map.
    assert store.get_job("job_a").status == "succeeded"


def test_reconcile_verifies_candidate_identity_via_sacct() -> None:
    store = _store()
    # Matching identity: sacct JobName carries the recorded stage token.
    _make_inflight_job(store, job_id="job_match", slurm_job_id="2001")
    # Mismatched identity: sacct returns a job for an unrelated stage.
    _make_inflight_job(store, job_id="job_mismatch", slurm_job_id="2002")

    sacct = _fake_sacct(
        {
            "2001": SacctRecord(
                slurm_job_id="2001",
                raw_state="COMPLETED",
                job_name="nhms_run_shud_forecast_array",
            ),
            "2002": SacctRecord(
                slurm_job_id="2002",
                raw_state="COMPLETED",
                job_name="nhms_some_other_basin_job",
            ),
        }
    )

    reconcile_inflight_jobs(store, sacct_query=sacct)

    assert store.get_job("job_match").status == "succeeded"
    # Mismatch is not accepted: typed unverified, NOT a terminal success.
    mismatch = store.get_job("job_mismatch")
    assert mismatch.status == RECONCILE_UNVERIFIED_STATUS
    assert mismatch.error_code == "SLURM_RECONCILE_UNVERIFIED"


def test_reconcile_generic_array_job_name_requires_manifest_task_identity() -> None:
    store = _store()
    _make_inflight_job(
        store,
        job_id="job_forecast_task_3",
        slurm_job_id="2103",
        stage="forecast",
        run_id="fcst_gfs_2026062912_model_a",
        model_id="model_a",
        array_task_id=3,
    )
    _make_inflight_job(
        store,
        job_id="job_forecast_no_identity",
        slurm_job_id="2104",
        stage="forecast",
        run_id="fcst_gfs_2026062912_model_b",
        model_id="model_b",
        array_task_id=4,
    )

    sacct = _fake_sacct(
        {
            "2103_3": SacctRecord(
                slurm_job_id="2103_3",
                raw_state="COMPLETED",
                job_name="nhms_forecast",
                exit_code="0:0",
                submitted_manifest={
                    "pipeline_job_id": "job_forecast_task_3",
                    "run_id": "fcst_gfs_2026062912_model_a",
                    "model_id": "model_a",
                    "stage": "run_shud_forecast_array",
                    "array_task_id": 3,
                },
                stdout_identity={
                    "run_id": "fcst_gfs_2026062912_model_a",
                    "model_id": "model_a",
                    "stage": "forecast",
                    "task_id": 3,
                },
            ),
            "2104_4": SacctRecord(
                slurm_job_id="2104_4",
                raw_state="COMPLETED",
                job_name="nhms_forecast",
                exit_code="0:0",
            ),
        }
    )

    outcomes = reconcile_inflight_jobs(store, sacct_query=sacct)

    assert {outcome.job_id: outcome.action for outcome in outcomes} == {
        "job_forecast_task_3": "terminal",
        "job_forecast_no_identity": "unverified",
    }
    assert store.get_job("job_forecast_task_3").status == "succeeded"
    assert store.get_job("job_forecast_no_identity").status == RECONCILE_UNVERIFIED_STATUS


def test_reconcile_generic_terminal_comment_only_is_unverified() -> None:
    from services.orchestrator.reservation import slurm_comment_for

    store = _store()
    _make_inflight_job(
        store,
        job_id="job_forecast_comment_only",
        slurm_job_id="2105",
        stage="forecast",
        run_id="fcst_gfs_2026062912_model_a",
        model_id="model_a",
        array_task_id=3,
    )
    job = store.get_job("job_forecast_comment_only")
    job.idempotency_key = "gfs:gfs_2026062912:basin_a:forecast"
    store.session.add(job)
    store.session.commit()
    sacct = _fake_sacct(
        {
            "2105_3": SacctRecord(
                slurm_job_id="2105_3",
                raw_state="COMPLETED",
                job_name="nhms_forecast",
                exit_code="0:0",
                comment=slurm_comment_for(job.idempotency_key),
            ),
        }
    )

    outcomes = reconcile_inflight_jobs(store, sacct_query=sacct)

    assert outcomes[0].action == "unverified"
    assert store.get_job("job_forecast_comment_only").status == RECONCILE_UNVERIFIED_STATUS


def test_reconcile_queries_array_task_when_durable_row_has_task_id() -> None:
    store = _store()
    _make_inflight_job(
        store,
        job_id="job_precise_task_3",
        slurm_job_id="12345",
        stage="run_shud_forecast_array",
        array_task_id=3,
    )
    queried: list[str] = []

    def sacct(slurm_job_id: str) -> SacctRecord | None:
        queried.append(slurm_job_id)
        if slurm_job_id == "12345_3":
            return SacctRecord(
                slurm_job_id="12345_3",
                raw_state="COMPLETED",
                job_name="nhms_run_shud_forecast_array",
                exit_code="0:0",
                task_id=3,
                array_task_id=3,
            )
        return None

    outcomes = reconcile_inflight_jobs(store, sacct_query=sacct)

    assert queried == ["12345_3"]
    assert outcomes[0].action == "terminal"
    assert store.get_job("job_precise_task_3").status == "succeeded"


def test_reconcile_generic_array_task_row_accepts_exact_task_identity() -> None:
    store = _store()
    _make_inflight_job(
        store,
        job_id="job_generic_task_3",
        slurm_job_id="12346",
        stage="forecast",
        run_id="fcst_gfs_2026062912_model_a",
        model_id="model_a",
        array_task_id=3,
    )
    queried: list[str] = []

    def sacct(slurm_job_id: str) -> SacctRecord | None:
        queried.append(slurm_job_id)
        if slurm_job_id == "12346_3":
            return SacctRecord(
                slurm_job_id="12346_3",
                raw_state="COMPLETED",
                job_name="nhms_forecast",
                exit_code="0:0",
                task_id=3,
                array_task_id=3,
            )
        return None

    outcomes = reconcile_inflight_jobs(store, sacct_query=sacct)

    assert queried == ["12346_3"]
    assert outcomes[0].action == "terminal"
    assert store.get_job("job_generic_task_3").status == "succeeded"


def test_reconcile_legacy_non_db_free_precise_job_name_remains_compatible() -> None:
    store = _store()
    _make_inflight_job(
        store,
        job_id="job_legacy_non_db_free",
        slurm_job_id="2110",
        stage="run_shud_forecast_array",
        run_id="legacy_run_1",
        model_id="legacy_model",
    )
    sacct = _fake_sacct(
        {
            "2110": SacctRecord(
                slurm_job_id="2110",
                raw_state="COMPLETED",
                job_name="nhms_run_shud_forecast_array",
                exit_code="0:0",
            ),
        }
    )

    outcomes = reconcile_inflight_jobs(store, sacct_query=sacct)

    assert outcomes[0].action == "terminal"
    assert store.get_job("job_legacy_non_db_free").status == "succeeded"


def test_reconcile_unknown_to_accounting_is_unverified_not_resubmitted() -> None:
    store = _store()
    _make_inflight_job(store, job_id="job_unknown", slurm_job_id="3003")

    # sacct knows nothing about this job id.
    sacct = _fake_sacct({"3003": None})

    outcomes = reconcile_inflight_jobs(store, sacct_query=sacct)

    assert outcomes[0].action == "unverified"
    assert store.get_job("job_unknown").status == RECONCILE_UNVERIFIED_STATUS


def test_pipeline_store_success_status_clears_previous_unverified_error() -> None:
    store = _store()
    _make_inflight_job(
        store,
        job_id="job_recovered",
        slurm_job_id="3004",
        status="running",
    )
    store.update_job_status(
        "job_recovered",
        RECONCILE_UNVERIFIED_STATUS,
        error_code="SLURM_RECONCILE_UNVERIFIED",
        error_message="sacct could not verify the candidate identity.",
    )
    store.update_job_status("job_recovered", "succeeded", exit_code=0)
    recovered = store.get_job("job_recovered")
    assert recovered.status == "succeeded"
    assert recovered.error_code is None
    assert recovered.error_message is None


def test_reconcile_no_duplicate_resubmit_for_running_or_terminal() -> None:
    store = _store()
    _make_inflight_job(store, job_id="job_running", slurm_job_id="4001")
    _make_inflight_job(store, job_id="job_done", slurm_job_id="4002")

    submit_calls: list[str] = []

    class _GuardStore:
        """Wrap the real store and trap any unexpected submit/create call."""

        def __init__(self, inner: PipelineStore) -> None:
            self._inner = inner

        def query_inflight_jobs(self):
            return self._inner.query_inflight_jobs()

        def update_job_status(self, *args: Any, **kwargs: Any):
            return self._inner.update_job_status(*args, **kwargs)

        def create_job(self, *args: Any, **kwargs: Any):
            submit_calls.append(kwargs.get("job_id", "?"))
            raise AssertionError("reconcile must not create/resubmit jobs")

    sacct = _fake_sacct(
        {
            "4001": SacctRecord(
                slurm_job_id="4001",
                raw_state="RUNNING",
                job_name="nhms_run_shud_forecast_array",
            ),
            "4002": SacctRecord(
                slurm_job_id="4002",
                raw_state="COMPLETED",
                job_name="nhms_run_shud_forecast_array",
            ),
        }
    )

    outcomes = reconcile_inflight_jobs(_GuardStore(store), sacct_query=sacct)

    assert submit_calls == []
    actions = {o.job_id: o.action for o in outcomes}
    assert actions["job_running"] == "still_running"
    assert actions["job_done"] == "terminal"
    assert store.get_job("job_running").status == "running"
    assert store.get_job("job_done").status == "succeeded"


def test_reconcile_failed_job_records_error_code() -> None:
    store = _store()
    _make_inflight_job(store, job_id="job_fail", slurm_job_id="5005")

    sacct = _fake_sacct(
        {
            "5005": SacctRecord(
                slurm_job_id="5005",
                raw_state="TIMEOUT",
                job_name="nhms_run_shud_forecast_array",
                exit_code="0:1",
            )
        }
    )

    reconcile_inflight_jobs(store, sacct_query=sacct)

    job = store.get_job("job_fail")
    assert job.status == "failed"
    assert job.error_code == "SLURM_TIMEOUT"


# --- M24 §3A: durable two-phase reservation + crash-window reconcile ---------


def test_idempotency_key_unique_constraint() -> None:
    """Reserving the same idempotency_key twice does NOT create a second row."""

    from services.orchestrator.reservation import reserve_candidate

    store = _store_repo()
    common = dict(
        run_id="run_1",
        cycle_id="cycle_1",
        job_type="forcing",
        model_id="model_1",
        stage="forcing",
    )

    first = reserve_candidate(store, idempotency_key="gfs:cyc:basin:forcing", job_id="job_a", **common)
    second = reserve_candidate(store, idempotency_key="gfs:cyc:basin:forcing", job_id="job_b", **common)

    assert first.created is True
    assert second.created is False  # reused, not a new row.
    assert second.job_id == "job_a"
    # Exactly one durable row carries that key.
    rows = [j for j in store.store.session.query(PipelineJob).all() if j.idempotency_key == "gfs:cyc:basin:forcing"]
    assert len(rows) == 1


def test_idempotency_key_unique_constraint_concurrent(tmp_path: Any) -> None:
    """Concurrent reserve of the SAME key (each thread its own session against a
    shared SQLite file + unique index): exactly one wins (created=True), exactly
    one durable row exists.

    Counterfactual: if reserve_pipeline_job returned the existing row instead of
    None on conflict (losing the DB RETURNING win/lose signal), >1 pass would
    report created=True and the ``exactly one created`` assertion goes red.
    """

    import threading

    from services.orchestrator.reservation import reserve_candidate

    # File-backed engine so each thread holds an independent connection/session
    # contending on the SAME physical unique idempotency_key index.
    db_path = tmp_path / "reserve_race.sqlite"
    engine = create_engine(f"sqlite:///{db_path}", future=True)

    @event.listens_for(engine, "connect")
    def _attach_schemas(dbapi_connection: Any, _record: Any) -> None:
        dbapi_connection.execute(f"ATTACH DATABASE '{db_path}' AS ops")

    Base.metadata.create_all(engine)

    key = "gfs:cyc:basin:forcing"
    common = dict(
        run_id="run_1",
        cycle_id="cycle_1",
        job_type="forcing",
        model_id="model_1",
        stage="forcing",
    )

    n = 8
    barrier = threading.Barrier(n)
    results: list[Any] = [None] * n

    def _attempt(index: int) -> None:
        repo = _StoreRepo(PipelineStore(Session(engine)))
        barrier.wait()  # release all threads into reserve at once.
        try:
            results[index] = reserve_candidate(
                repo, idempotency_key=key, job_id=f"job_{index}", **common
            )
        finally:
            repo.store.session.close()

    threads = [threading.Thread(target=_attempt, args=(i,)) for i in range(n)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    created = [r for r in results if r is not None and r.created]
    assert len(created) == 1, f"exactly one creator expected, got {len(created)}"
    # And every loser observes the same winning row id.
    winner_job_id = created[0].job_id
    losers = [r for r in results if r is not None and not r.created]
    assert len(losers) == n - 1
    assert all(r.job_id == winner_job_id for r in losers)
    # Exactly one durable row carries that key (unique constraint held).
    verify = PipelineStore(Session(engine))
    rows = [
        j
        for j in verify.session.query(PipelineJob).all()
        if j.idempotency_key == key
    ]
    assert len(rows) == 1


def test_array_stage_kill_before_bind_reconciles_by_comment() -> None:
    """Array-stage crash after sbatch (array master accepted, comment recorded)
    but before bind: reconcile recovers the array master slurm_job_id by the
    idempotency comment and binds it — no array resubmission.

    Counterfactual: if the array submit path did NOT thread ``--comment`` (item 2
    BLOCKER), accounting could not be matched back by idempotency_key, the guard
    would mark the reservation reservation_lost, and the ``action == 'bound'``
    assertion goes red.
    """

    from services.orchestrator.reconcile import (
        SacctRecord,
        reconcile_reserved_unbound_jobs,
    )
    from services.orchestrator.reservation import reserve_candidate, slurm_comment_for

    store = _store_repo()
    key = "gfs:cyc:basin:run_shud_forecast_array"
    reserve_candidate(
        store,
        idempotency_key=key,
        job_id="job_array_crash",
        run_id="run_1",
        cycle_id="cycle_1",
        job_type="run_shud_forecast_array",
        model_id="model_1",
        stage="run_shud_forecast_array",
    )
    assert store.query_candidate_state(key)["slurm_job_id"] is None

    # The array master sbatch accepted (it recorded our comment). Array job ids
    # take the ``<master>`` form in sacct for the master record.
    def _comment_query(idem: str) -> SacctRecord | None:
        if idem == key:
            return SacctRecord(
                slurm_job_id="77042",
                raw_state="RUNNING",
                job_name="nhms_run_shud_forecast_array",
                comment=slurm_comment_for(key),
            )
        return None

    outcomes = reconcile_reserved_unbound_jobs(store.store, comment_query=_comment_query)

    assert len(outcomes) == 1
    assert outcomes[0].action == "bound"
    assert outcomes[0].slurm_job_id == "77042"
    bound = store.query_candidate_state(key)
    assert bound["slurm_job_id"] == "77042"
    assert bound["status"] == "submitted"


def test_reservation_written_before_submit_and_queryable() -> None:
    """Phase 1 reserve writes status=reserved, queryable via candidate_state."""

    from services.orchestrator.persistence import RESERVED_STATUS
    from services.orchestrator.reservation import reserve_candidate

    store = _store_repo()
    key = "gfs:cyc:basin:forecast"
    result = reserve_candidate(
        store,
        idempotency_key=key,
        job_id="job_resv",
        run_id="run_1",
        cycle_id="cycle_1",
        job_type="forecast",
        model_id="model_1",
        stage="forecast",
    )

    assert result.created is True
    state = store.query_candidate_state(key)
    assert state is not None
    assert state["status"] == RESERVED_STATUS
    assert state["slurm_job_id"] is None  # not yet bound.


def test_overlapping_pass_does_not_double_submit() -> None:
    """An overlapping pass sees the reservation and skips, even before sacct."""

    from services.orchestrator.reservation import reservation_is_active, reserve_candidate

    store = _store_repo()
    key = "gfs:cyc:basin:forcing"
    common = dict(
        run_id="run_1",
        cycle_id="cycle_1",
        job_type="forcing",
        model_id="model_1",
        stage="forcing",
    )

    first = reserve_candidate(store, idempotency_key=key, job_id="job_pass1", **common)
    assert first.created is True

    # Pass 2: query candidate_state BEFORE any sacct row exists.
    state = store.query_candidate_state(key)
    assert reservation_is_active(state["status"]) is True

    # If pass 2 still calls reserve (race), it reuses the existing row.
    second = reserve_candidate(store, idempotency_key=key, job_id="job_pass2", **common)
    assert second.already_inflight is True
    assert second.job_id == "job_pass1"
    rows = [j for j in store.store.session.query(PipelineJob).all() if j.idempotency_key == key]
    assert len(rows) == 1


def test_kill_after_submit_before_bind_reconciles_by_idempotency() -> None:
    """Crash after sbatch, before bind: reconcile binds via the comment key."""

    from services.orchestrator.reconcile import (
        SacctRecord,
        reconcile_reserved_unbound_jobs,
    )
    from services.orchestrator.reservation import reserve_candidate, slurm_comment_for

    store = _store_repo()
    key = "gfs:cyc:basin:forecast"
    reserve_candidate(
        store,
        idempotency_key=key,
        job_id="job_crash",
        run_id="run_1",
        cycle_id="cycle_1",
        job_type="forecast",
        model_id="model_1",
        stage="forecast",
    )
    # Reservation row exists, slurm_job_id is NULL (bind never ran).
    assert store.query_candidate_state(key)["slurm_job_id"] is None

    # sbatch DID accept the job (it recorded our comment).
    def _comment_query(idem: str) -> SacctRecord | None:
        if idem == key:
            return SacctRecord(
                slurm_job_id="88001",
                raw_state="RUNNING",
                job_name="nhms_forecast",
                comment=slurm_comment_for(key),
            )
        return None

    outcomes = reconcile_reserved_unbound_jobs(store.store, comment_query=_comment_query)

    assert len(outcomes) == 1
    assert outcomes[0].action == "bound"
    assert outcomes[0].slurm_job_id == "88001"
    bound = store.query_candidate_state(key)
    assert bound["slurm_job_id"] == "88001"
    assert bound["status"] == "submitted"


def test_submit_timeout_unknown_result_not_blindly_resubmitted() -> None:
    """HTTP submit timeout: reservation stays; recovery reconciles, no double-run."""

    from services.orchestrator.reconcile import (
        RESERVATION_LOST_STATUS,
        reconcile_reserved_unbound_jobs,
    )
    from services.orchestrator.reservation import reserve_candidate

    store = _store_repo()
    key = "gfs:cyc:basin:forcing"
    reserve_candidate(
        store,
        idempotency_key=key,
        job_id="job_timeout",
        run_id="run_1",
        cycle_id="cycle_1",
        job_type="forcing",
        model_id="model_1",
        stage="forcing",
    )

    submit_attempts: list[str] = []

    # sbatch never actually took (accounting has no job for this comment).
    def _comment_query(idem: str) -> Any:
        submit_attempts.append(idem)
        return None

    # Drive a clock past the absence grace so the confirmed-absent verdict is
    # authoritative (not deferred as slurmdbd propagation lag).
    from services.orchestrator.reconcile import RESERVATION_ABSENCE_GRACE

    past_grace = _past_grace_now(store, RESERVATION_ABSENCE_GRACE)
    outcomes = reconcile_reserved_unbound_jobs(
        store.store,
        comment_query=_comment_query,
        now=lambda: past_grace,
    )

    # Reconcile queried accounting (did not re-submit) and marked it typed.
    assert submit_attempts == [key]
    assert outcomes[0].action == "reservation_lost"
    state = store.query_candidate_state(key)
    assert state["status"] == RESERVATION_LOST_STATUS
    assert state["slurm_job_id"] is None
    # At most one row for this key ever existed.
    rows = [j for j in store.store.session.query(PipelineJob).all() if j.idempotency_key == key]
    assert len(rows) == 1


def test_transient_sacct_failure_does_not_mark_reservation_lost() -> None:
    """A transient sacct failure during crash-recovery reconcile must NOT be read
    as 'job is gone'. The reservation stays ``reserved`` (not bound, not
    reservation_lost) so a later pass cannot re-reserve+re-sbatch an in-flight
    candidate — the double-submit BLOCKER.

    Counterfactual: drop the ReconcileQueryUnavailable try/except in
    reconcile_reserved_unbound_jobs (let the exception bubble, or treat it as a
    None confirmed-absent) → the row is marked reservation_lost → this assertion
    goes red.
    """

    from services.orchestrator.reconcile import (
        RESERVATION_LOST_STATUS,
        ReconcileQueryUnavailable,
        reconcile_reserved_unbound_jobs,
    )
    from services.orchestrator.reservation import reserve_candidate

    store = _store_repo()
    key = "gfs:cyc:basin:forcing"
    reserve_candidate(
        store,
        idempotency_key=key,
        job_id="job_transient",
        run_id="run_1",
        cycle_id="cycle_1",
        job_type="forcing",
        model_id="model_1",
        stage="forcing",
    )
    assert store.query_candidate_state(key)["slurm_job_id"] is None

    def _comment_query(_idem: str) -> Any:
        raise ReconcileQueryUnavailable("sacct timed out")

    outcomes = reconcile_reserved_unbound_jobs(store.store, comment_query=_comment_query)

    assert len(outcomes) == 1
    assert outcomes[0].action == "query_unavailable"
    state = store.query_candidate_state(key)
    # Still reserved: NOT bound, NOT reservation_lost.
    assert state["status"] == "reserved"
    assert state["status"] != RESERVATION_LOST_STATUS
    assert state["slurm_job_id"] is None


def test_confirmed_absent_marks_reservation_lost() -> None:
    """The complementary side of the tri-state: a query that *succeeds* and
    confirms accounting has no such job (comment_query returns None) is the only
    case that may mark reservation_lost. Pins the confirmed-absent path so the
    transient-failure fix above does not accidentally swallow real losses.
    """

    from services.orchestrator.reconcile import (
        RESERVATION_LOST_STATUS,
        reconcile_reserved_unbound_jobs,
    )
    from services.orchestrator.reservation import reserve_candidate

    store = _store_repo()
    key = "gfs:cyc:basin:forcing"
    reserve_candidate(
        store,
        idempotency_key=key,
        job_id="job_absent",
        run_id="run_1",
        cycle_id="cycle_1",
        job_type="forcing",
        model_id="model_1",
        stage="forcing",
    )

    def _comment_query(_idem: str) -> Any:
        return None  # query succeeded; accounting confirms no such job.

    # Past the absence grace, a confirmed-absent answer is authoritative.
    from services.orchestrator.reconcile import RESERVATION_ABSENCE_GRACE

    past_grace = _past_grace_now(store, RESERVATION_ABSENCE_GRACE)
    outcomes = reconcile_reserved_unbound_jobs(
        store.store,
        comment_query=_comment_query,
        now=lambda: past_grace,
    )

    assert len(outcomes) == 1
    assert outcomes[0].action == "reservation_lost"
    state = store.query_candidate_state(key)
    assert state["status"] == RESERVATION_LOST_STATUS
    assert state["slurm_job_id"] is None


def test_reserve_pipeline_job_sql_absorbs_all_unique_conflicts() -> None:
    """The production reserve SQL must absorb ANY unique conflict (idempotency_key
    unique index OR job_id primary key) via an untargeted ``ON CONFLICT DO
    NOTHING``. A narrow ``ON CONFLICT (idempotency_key)`` would let a pre-existing
    job_id row with a NULL idempotency_key slip past the partial index and raise
    on the job_id PK, aborting the whole pass.

    This guards the Postgres-only semantics deterministically at the SQL-text
    level (real concurrency is covered by the node-22 integration run).

    Counterfactual: revert FIX2 to the narrow target → this assertion goes red.
    """

    import ast
    import inspect
    import textwrap

    from services.orchestrator.chain import PsycopgOrchestratorRepository

    source = inspect.getsource(PsycopgOrchestratorRepository.reserve_pipeline_job)
    # Assert against the executable SQL only, not the docstring (which legitimately
    # names the narrow form to explain why it was rejected). Collect every string
    # literal in the function body except the leading docstring.
    func = ast.parse(textwrap.dedent(source)).body[0]
    # The first body statement is the docstring expression; drop it so we assert
    # only against executable string literals (the SQL).
    body = func.body
    if body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant):
        body = body[1:]
    sql_text = "\n".join(
        node.value
        for stmt in body
        for node in ast.walk(stmt)
        if isinstance(node, ast.Constant) and isinstance(node.value, str)
    )
    assert "ON CONFLICT DO NOTHING" in sql_text
    assert "ON CONFLICT (idempotency_key)" not in sql_text


def test_reserve_candidate_does_not_raise_on_job_id_pk_conflict(tmp_path: Any) -> None:
    """Behavior-level guard for FIX2: a pre-existing row with the SAME job_id but
    a NULL idempotency_key must make reserve_candidate report a clean loss
    (created=False) WITHOUT raising — even though the idempotency_key partial
    index does not cover it and the job_id primary key does.

    Backed by a file-backed SQLite repository with job_id PRIMARY KEY + a partial
    UNIQUE idempotency_key index, mirroring migration 000029.
    """

    import sqlite3

    from services.orchestrator.reservation import reserve_candidate

    db_path = tmp_path / "pk_conflict.sqlite"
    conn = sqlite3.connect(db_path)
    conn.execute(
        """
        CREATE TABLE pipeline_job (
            job_id TEXT PRIMARY KEY,
            run_id TEXT,
            cycle_id TEXT,
            job_type TEXT,
            model_id TEXT,
            stage TEXT,
            status TEXT,
            slurm_job_id TEXT,
            idempotency_key TEXT,
            candidate_id TEXT
        )
        """
    )
    conn.execute(
        "CREATE UNIQUE INDEX idem_uidx ON pipeline_job (idempotency_key) "
        "WHERE idempotency_key IS NOT NULL"
    )
    # Legacy / non-reserve row: same job_id, NULL idempotency_key.
    conn.execute(
        "INSERT INTO pipeline_job (job_id, status, idempotency_key) "
        "VALUES (?, ?, NULL)",
        ("job_dup", "running"),
    )
    conn.commit()

    class _SqliteRepo:
        """Repository implementing the production reserve contract over SQLite:
        untargeted ON CONFLICT DO NOTHING RETURNING (FIX2 shape).
        """

        def reserve_pipeline_job(self, record: dict[str, Any]) -> dict[str, Any] | None:
            cur = conn.execute(
                """
                INSERT INTO pipeline_job (
                    job_id, run_id, cycle_id, job_type, model_id, stage,
                    status, idempotency_key, candidate_id
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT DO NOTHING
                RETURNING *
                """,
                (
                    record["job_id"],
                    record.get("run_id"),
                    record.get("cycle_id"),
                    record["job_type"],
                    record.get("model_id"),
                    record.get("stage"),
                    record.get("status", "reserved"),
                    record["idempotency_key"],
                    record.get("candidate_id"),
                ),
            )
            row = cur.fetchone()
            conn.commit()
            if row is None:
                return None
            cols = [c[0] for c in cur.description]
            return dict(zip(cols, row, strict=True))

        def query_candidate_state(self, idempotency_key: str) -> dict[str, Any] | None:
            cur = conn.execute(
                "SELECT * FROM pipeline_job WHERE idempotency_key = ?",
                (idempotency_key,),
            )
            row = cur.fetchone()
            if row is None:
                return None
            cols = [c[0] for c in cur.description]
            return dict(zip(cols, row, strict=True))

    repo = _SqliteRepo()
    # Same job_id as the legacy NULL-idem row, new idempotency_key. The
    # idempotency_key partial index does not cover the existing NULL row, so the
    # job_id PRIMARY KEY is what conflicts. Must be a clean loss, never a raise.
    result = reserve_candidate(
        repo,
        idempotency_key="gfs:cyc:basin:forcing",
        job_id="job_dup",
        run_id="run_1",
        cycle_id="cycle_1",
        job_type="forcing",
        model_id="model_1",
        stage="forcing",
    )

    assert result.created is False
    conn.close()


def _reserve_then_set_status(store: _StoreRepo, *, key: str, job_id: str, status: str) -> None:
    """Reserve a candidate then force its row into ``status`` (with no slurm bind).

    Models a DEAD reservation: a row that was reserved but never bound, then
    demoted (``submission_failed`` by a rejected sbatch, or ``reservation_lost``
    by crash-recovery reconcile). The idempotency_key still occupies the partial
    unique index, so a plain reserve loses to it.
    """

    from services.orchestrator.reservation import reserve_candidate

    reserve_candidate(
        store,
        idempotency_key=key,
        job_id=job_id,
        run_id="run_1",
        cycle_id="cycle_1",
        job_type="forcing",
        model_id="model_1",
        stage="forcing",
    )
    job = store.store.query_candidate_state(key)
    assert job is not None and job.slurm_job_id is None
    job.status = status
    store.store.session.add(job)
    store.store.session.commit()


def test_reserve_candidate_reclaims_submission_failed_dead_reservation() -> None:
    """A DEAD reservation in ``submission_failed`` (reserved-but-never-bound) is
    atomically taken over by a later reserve_candidate: created=True and the row
    returns to ``reserved`` so THIS pass re-submits.

    This positively covers the previously-missing stale-reclaim (GAP-3): without
    ``reclaim_pipeline_job_reservation`` the plain INSERT keeps losing to the
    idempotency_key index forever and the candidate is permanently stuck.
    """

    store = _store_repo()
    key = "gfs:cyc:basin:forcing"
    _reserve_then_set_status(store, key=key, job_id="job_dead", status="submission_failed")
    assert store.query_candidate_state(key)["status"] == "submission_failed"

    from services.orchestrator.reservation import reserve_candidate

    result = reserve_candidate(
        store,
        idempotency_key=key,
        job_id="job_dead",
        run_id="run_1",
        cycle_id="cycle_1",
        job_type="forcing",
        model_id="model_1",
        stage="forcing",
    )

    assert result.created is True
    state = store.query_candidate_state(key)
    assert state["status"] == "reserved"
    assert state["slurm_job_id"] is None
    # Take-over is in place, not a second row.
    rows = [j for j in store.store.session.query(PipelineJob).all() if j.idempotency_key == key]
    assert len(rows) == 1


def test_reserve_candidate_reclaims_reservation_lost_dead_reservation() -> None:
    """A DEAD reservation demoted to ``reservation_lost`` by crash-recovery
    reconcile is likewise atomically reclaimed back to ``reserved`` (created=True).
    """

    store = _store_repo()
    key = "gfs:cyc:basin:forecast"
    _reserve_then_set_status(store, key=key, job_id="job_lost", status="reservation_lost")
    assert store.query_candidate_state(key)["status"] == "reservation_lost"

    from services.orchestrator.reservation import reserve_candidate

    result = reserve_candidate(
        store,
        idempotency_key=key,
        job_id="job_lost",
        run_id="run_1",
        cycle_id="cycle_1",
        job_type="forcing",
        model_id="model_1",
        stage="forcing",
    )

    assert result.created is True
    assert store.query_candidate_state(key)["status"] == "reserved"


def test_reserve_candidate_never_reclaims_a_live_reservation() -> None:
    """The take-over predicate matches ONLY dead statuses, so a LIVE row
    (``reserved`` / ``submitted`` / ``running``) is never stolen: reserve_candidate
    reports created=False and leaves the row byte-for-byte unchanged. This is the
    race-safety guarantee against double-submit.
    """

    from services.orchestrator.reservation import reserve_candidate

    for live_status in ("reserved", "submitted", "running"):
        store = _store_repo()
        key = f"gfs:cyc:basin:{live_status}"
        _reserve_then_set_status(store, key=key, job_id=f"job_{live_status}", status=live_status)
        before = store.query_candidate_state(key)
        assert before["status"] == live_status

        result = reserve_candidate(
            store,
            idempotency_key=key,
            job_id="job_intruder",
            run_id="run_2",
            cycle_id="cycle_2",
            job_type="forcing",
            model_id="model_2",
            stage="forcing",
        )

        assert result.created is False, live_status
        after = store.query_candidate_state(key)
        # Untouched: same job_id, same live status, still unbound.
        assert after["job_id"] == before["job_id"]
        assert after["status"] == live_status
        assert after["slurm_job_id"] is None


def test_repeated_transient_reconcile_keeps_reservation_reserved() -> None:
    """GAP-4: two consecutive crash-recovery reconcile passes that both hit a
    transient query failure (``ReconcileQueryUnavailable``) must leave the row
    ``reserved`` — never marked ``reservation_lost`` — across BOTH passes, so a
    transient outage that spans multiple ticks can never free an in-flight
    reservation for double-submit.
    """

    from services.orchestrator.reconcile import (
        RESERVATION_LOST_STATUS,
        ReconcileQueryUnavailable,
        reconcile_reserved_unbound_jobs,
    )
    from services.orchestrator.reservation import reserve_candidate

    store = _store_repo()
    key = "gfs:cyc:basin:forcing"
    reserve_candidate(
        store,
        idempotency_key=key,
        job_id="job_transient2",
        run_id="run_1",
        cycle_id="cycle_1",
        job_type="forcing",
        model_id="model_1",
        stage="forcing",
    )

    def _comment_query(_idem: str) -> Any:
        raise ReconcileQueryUnavailable("sacct timed out")

    for _ in range(2):
        outcomes = reconcile_reserved_unbound_jobs(store.store, comment_query=_comment_query)
        assert len(outcomes) == 1
        assert outcomes[0].action == "query_unavailable"
        state = store.query_candidate_state(key)
        assert state["status"] == "reserved"
        assert state["status"] != RESERVATION_LOST_STATUS
        assert state["slurm_job_id"] is None


# --- Grace guard for confirmed-but-young absence (slurmdbd propagation lag) ----


def _reserved_row(store: _StoreRepo, key: str, *, job_id: str) -> PipelineJob:
    """Reserve a candidate and return its durable PipelineJob row."""

    from services.orchestrator.reservation import reserve_candidate

    reserve_candidate(
        store,
        idempotency_key=key,
        job_id=job_id,
        run_id="run_1",
        cycle_id="cycle_1",
        job_type="forcing",
        model_id="model_1",
        stage="forcing",
    )
    row = (
        store.store.session.query(PipelineJob)
        .filter(PipelineJob.idempotency_key == key)
        .one()
    )
    assert row.slurm_job_id is None
    return row


def test_young_confirmed_absence_defers_not_reservation_lost() -> None:
    """A reserved-unbound row younger than the absence grace whose comment query
    confirms absence (returncode 0, no matching row) must NOT be demoted to
    reservation_lost — it may merely be slurmdbd propagation lag for a job
    sbatch just accepted. It is emitted ``absence_unconfirmed``, stays
    ``reserved``, and store.update_job_status is never called for it (so the
    reserve gate cannot reclaim+re-sbatch an in-flight job → no double submit).
    """

    from datetime import UTC, datetime

    from services.orchestrator.reconcile import reconcile_reserved_unbound_jobs

    store = _store_repo()
    key = "gfs:cyc:basin:forcing"
    fixed_now = datetime(2026, 6, 4, 12, 0, 0, tzinfo=UTC)
    row = _reserved_row(store, key, job_id="job_young")
    # Anchor age on updated_at (== created_at at first submit), set near `now`.
    row.created_at = fixed_now
    row.updated_at = fixed_now  # last sbatch attempt exactly at `now` → young.
    store.store.session.flush()

    update_calls: list[tuple[str, str]] = []
    original_update = store.store.update_job_status

    def _spy_update(job_id: str, status: str, **kwargs: Any) -> Any:
        update_calls.append((job_id, status))
        return original_update(job_id, status, **kwargs)

    store.store.update_job_status = _spy_update  # type: ignore[method-assign]

    def _comment_query(_idem: str) -> Any:
        return None  # query succeeded; accounting confirms no such job (yet).

    outcomes = reconcile_reserved_unbound_jobs(
        store.store,
        comment_query=_comment_query,
        now=lambda: fixed_now,
    )

    assert len(outcomes) == 1
    assert outcomes[0].action == "absence_unconfirmed"
    assert outcomes[0].status == "reserved"
    assert update_calls == []  # never demoted → no reclaim → no double submit.
    state = store.query_candidate_state(key)
    assert state["status"] == "reserved"
    assert state["slurm_job_id"] is None


def test_configured_accepted_absence_window_does_not_extend_legacy_forcing() -> None:
    from datetime import timedelta

    from services.orchestrator.reconcile import reconcile_reserved_unbound_jobs

    store = _store_repo()
    key = "gfs:cyc:basin:forcing-window"
    row = _reserved_row(store, key, job_id="job_legacy_forcing_window")
    started_at = datetime(2026, 7, 12, tzinfo=UTC)
    row.created_at = started_at
    row.updated_at = started_at
    store.store.session.commit()

    outcome = reconcile_reserved_unbound_jobs(
        store.store,
        comment_query=lambda _key: None,
        accepted_submit_grace=timedelta(seconds=300),
        now=lambda: started_at + timedelta(seconds=121),
    )[0]

    assert outcome.action == "reservation_lost"
    assert outcome.status == "reservation_lost"


def test_old_confirmed_absence_marks_reservation_lost() -> None:
    """A reserved-unbound row OLDER than the grace whose comment query confirms
    absence keeps the legacy behavior: demote to reservation_lost. Past the
    propagation window, an empty answer is authoritative — sbatch did not take.
    """

    from datetime import UTC, datetime, timedelta

    from services.orchestrator.reconcile import (
        RESERVATION_LOST_STATUS,
        reconcile_reserved_unbound_jobs,
    )

    store = _store_repo()
    key = "gfs:cyc:basin:forcing"
    fixed_now = datetime(2026, 6, 4, 12, 0, 0, tzinfo=UTC)
    row = _reserved_row(store, key, job_id="job_old")
    # Age is driven by updated_at (the last sbatch attempt); well past grace.
    row.created_at = fixed_now - timedelta(minutes=10)
    row.updated_at = fixed_now - timedelta(minutes=10)
    store.store.session.flush()

    def _comment_query(_idem: str) -> Any:
        return None

    outcomes = reconcile_reserved_unbound_jobs(
        store.store,
        comment_query=_comment_query,
        now=lambda: fixed_now,
    )

    assert len(outcomes) == 1
    assert outcomes[0].action == "reservation_lost"
    state = store.query_candidate_state(key)
    assert state["status"] == RESERVATION_LOST_STATUS
    assert state["slurm_job_id"] is None


def test_absent_with_no_created_at_marks_reservation_lost() -> None:
    """A reserved-unbound row that cannot prove its youth (both ``updated_at`` and
    the legacy ``created_at`` fallback are None) keeps the demote-to-
    reservation_lost behavior. Liveness must never regress: an un-aged absence is
    treated as authoritative rather than indefinitely deferred.
    """

    from datetime import UTC, datetime

    from services.orchestrator.reconcile import (
        RESERVATION_LOST_STATUS,
        reconcile_reserved_unbound_jobs,
    )

    class _NoCreatedAtJob:
        job_id = "job_no_created"
        idempotency_key = "gfs:cyc:basin:forcing"
        status = "reserved"
        slurm_job_id = None
        updated_at = None  # primary anchor absent.
        created_at = None  # legacy fallback also absent.

    demoted: list[tuple[str, str]] = []

    class _FakeStore:
        def query_reserved_unbound_jobs(self) -> list[Any]:
            return [_NoCreatedAtJob()]

        def update_job_status(self, job_id: str, status: str, **_kwargs: Any) -> None:
            demoted.append((job_id, status))

    def _comment_query(_idem: str) -> Any:
        return None

    outcomes = reconcile_reserved_unbound_jobs(
        _FakeStore(),
        comment_query=_comment_query,
        now=lambda: datetime(2026, 6, 4, 12, 0, 0, tzinfo=UTC),
    )

    assert len(outcomes) == 1
    assert outcomes[0].action == "reservation_lost"
    assert demoted == [("job_no_created", RESERVATION_LOST_STATUS)]


def test_young_with_valid_record_still_binds() -> None:
    """Regression: the grace guard only gates the *absence* branch. A young
    reserved-unbound row whose comment query returns a valid matching record is
    still bound (action == "bound"); grace must not interfere with success.
    """

    from datetime import UTC, datetime

    from services.orchestrator.reconcile import (
        SacctRecord,
        reconcile_reserved_unbound_jobs,
    )
    from services.orchestrator.reservation import slurm_comment_for

    store = _store_repo()
    key = "gfs:cyc:basin:forcing"
    fixed_now = datetime(2026, 6, 4, 12, 0, 0, tzinfo=UTC)
    row = _reserved_row(store, key, job_id="job_young_bound")
    row.created_at = fixed_now
    row.updated_at = fixed_now  # young by last sbatch attempt.
    store.store.session.flush()

    def _comment_query(idem: str) -> SacctRecord | None:
        if idem == key:
            return SacctRecord(
                slurm_job_id="99123",
                raw_state="RUNNING",
                job_name="nhms_forcing",
                comment=slurm_comment_for(key),
            )
        return None

    outcomes = reconcile_reserved_unbound_jobs(
        store.store,
        comment_query=_comment_query,
        now=lambda: fixed_now,
    )

    assert len(outcomes) == 1
    assert outcomes[0].action == "bound"
    assert outcomes[0].slurm_job_id == "99123"
    bound = store.query_candidate_state(key)
    assert bound["slurm_job_id"] == "99123"
    assert bound["status"] == "submitted"


def test_young_with_query_unavailable_still_query_unavailable() -> None:
    """Regression: a young reserved-unbound row whose comment query raises
    ReconcileQueryUnavailable yields action == "query_unavailable" (the
    transient path), unaffected by the absence grace guard. The row stays
    ``reserved``; the grace branch is never reached on a transient failure.
    """

    from datetime import UTC, datetime

    from services.orchestrator.reconcile import (
        ReconcileQueryUnavailable,
        reconcile_reserved_unbound_jobs,
    )

    store = _store_repo()
    key = "gfs:cyc:basin:forcing"
    fixed_now = datetime(2026, 6, 4, 12, 0, 0, tzinfo=UTC)
    row = _reserved_row(store, key, job_id="job_young_transient")
    row.created_at = fixed_now
    row.updated_at = fixed_now  # young by last sbatch attempt.
    store.store.session.flush()

    def _comment_query(_idem: str) -> Any:
        raise ReconcileQueryUnavailable("sacct timed out")

    outcomes = reconcile_reserved_unbound_jobs(
        store.store,
        comment_query=_comment_query,
        now=lambda: fixed_now,
    )

    assert len(outcomes) == 1
    assert outcomes[0].action == "query_unavailable"
    state = store.query_candidate_state(key)
    assert state["status"] == "reserved"
    assert state["slurm_job_id"] is None


def test_reclaimed_reservation_young_by_updated_at_defers_despite_stale_created_at() -> None:
    """Direct regression for the double-submit hole this fix closes. A reservation
    reclaimed → re-sbatched → crashed-before-bind has a STALE ``created_at`` (the
    original reserve moment, hours ago) but a FRESH ``updated_at`` (the reclaim
    takeover / last sbatch attempt, seconds ago). Anchoring on updated_at keeps
    grace coverage: the confirmed-but-young absence is deferred (not demoted),
    store.update_job_status is never called, so the reserve gate cannot
    reclaim+re-sbatch an in-flight job → no double submit.

    Counterfactual: anchor on created_at (the pre-fix behavior) → the hours-old
    created_at falls outside grace → reservation_lost → reclaim → re-sbatch =
    double submit; this assertion goes red.
    """

    from datetime import UTC, datetime, timedelta

    from services.orchestrator.reconcile import (
        ABSENCE_UNCONFIRMED_ACTION,
        reconcile_reserved_unbound_jobs,
    )

    store = _store_repo()
    key = "gfs:cyc:basin:forcing"
    fixed_now = datetime(2026, 6, 4, 12, 0, 0, tzinfo=UTC)
    row = _reserved_row(store, key, job_id="job_reclaimed")
    # Reclaim leaves created_at stale (original reserve, an hour ago) but
    # refreshes updated_at to the takeover/re-sbatch moment (10s ago < grace).
    row.created_at = fixed_now - timedelta(hours=1)
    row.updated_at = fixed_now - timedelta(seconds=10)
    store.store.session.flush()

    update_calls: list[tuple[str, str]] = []
    original_update = store.store.update_job_status

    def _spy_update(job_id: str, status: str, **kwargs: Any) -> Any:
        update_calls.append((job_id, status))
        return original_update(job_id, status, **kwargs)

    store.store.update_job_status = _spy_update  # type: ignore[method-assign]

    def _comment_query(_idem: str) -> Any:
        return None  # confirmed-absent (sbatch not yet visible in accounting).

    outcomes = reconcile_reserved_unbound_jobs(
        store.store,
        comment_query=_comment_query,
        now=lambda: fixed_now,
    )

    assert len(outcomes) == 1
    assert outcomes[0].action == ABSENCE_UNCONFIRMED_ACTION
    assert outcomes[0].status == "reserved"
    assert update_calls == []  # not demoted → no reclaim → no double submit.
    state = store.query_candidate_state(key)
    assert state["status"] == "reserved"
    assert state["slurm_job_id"] is None


def test_absence_exactly_at_grace_boundary_marks_reservation_lost() -> None:
    """Boundary: an age exactly EQUAL to the grace must demote (the guard is a
    strict ``<``). At ``updated_at == now - grace`` the propagation window has
    fully elapsed, so a confirmed-absent answer is authoritative.
    """

    from datetime import UTC, datetime

    from services.orchestrator.reconcile import (
        RESERVATION_ABSENCE_GRACE,
        RESERVATION_LOST_STATUS,
        reconcile_reserved_unbound_jobs,
    )

    store = _store_repo()
    key = "gfs:cyc:basin:forcing"
    fixed_now = datetime(2026, 6, 4, 12, 0, 0, tzinfo=UTC)
    row = _reserved_row(store, key, job_id="job_boundary")
    row.created_at = fixed_now - RESERVATION_ABSENCE_GRACE
    row.updated_at = fixed_now - RESERVATION_ABSENCE_GRACE  # age == grace exactly.
    store.store.session.flush()

    def _comment_query(_idem: str) -> Any:
        return None

    outcomes = reconcile_reserved_unbound_jobs(
        store.store,
        comment_query=_comment_query,
        now=lambda: fixed_now,
    )

    assert len(outcomes) == 1
    assert outcomes[0].action == "reservation_lost"
    state = store.query_candidate_state(key)
    assert state["status"] == RESERVATION_LOST_STATUS
    assert state["slurm_job_id"] is None


def test_malformed_record_young_defers() -> None:
    """A young reserved-unbound row whose comment query returns a record with a
    malformed slurm_job_id (fails the ``\\d+``/``\\d+_\\d+`` shape) falls into the
    same confirmed-absent branch — but, being young, must DEFER (absence_unconfirmed),
    not demote. Locks the young-defer guard for the malformed-record path so a
    garbage accounting row can never trigger an immediate reclaim+re-sbatch.
    """

    from datetime import UTC, datetime

    from services.orchestrator.reconcile import (
        ABSENCE_UNCONFIRMED_ACTION,
        SacctRecord,
        reconcile_reserved_unbound_jobs,
    )
    from services.orchestrator.reservation import slurm_comment_for

    store = _store_repo()
    key = "gfs:cyc:basin:forcing"
    fixed_now = datetime(2026, 6, 4, 12, 0, 0, tzinfo=UTC)
    row = _reserved_row(store, key, job_id="job_malformed_young")
    row.created_at = fixed_now
    row.updated_at = fixed_now  # young by last sbatch attempt.
    store.store.session.flush()

    update_calls: list[tuple[str, str]] = []
    original_update = store.store.update_job_status

    def _spy_update(job_id: str, status: str, **kwargs: Any) -> Any:
        update_calls.append((job_id, status))
        return original_update(job_id, status, **kwargs)

    store.store.update_job_status = _spy_update  # type: ignore[method-assign]

    def _comment_query(idem: str) -> SacctRecord | None:
        if idem == key:
            # Correct comment, but the JobID shape is illegal → fails the bind guard.
            return SacctRecord(
                slurm_job_id="not-a-number",
                raw_state="RUNNING",
                job_name="nhms_forcing",
                comment=slurm_comment_for(key),
            )
        return None

    outcomes = reconcile_reserved_unbound_jobs(
        store.store,
        comment_query=_comment_query,
        now=lambda: fixed_now,
    )

    assert len(outcomes) == 1
    assert outcomes[0].action == ABSENCE_UNCONFIRMED_ACTION
    assert outcomes[0].status == "reserved"
    assert update_calls == []  # young → deferred, not demoted.
    state = store.query_candidate_state(key)
    assert state["status"] == "reserved"
    assert state["slurm_job_id"] is None


# --- FINDING-2: real comment-row parsing + array-master normalization ----------


@pytest.mark.parametrize("source_id", ["gfs", "ifs"])
@pytest.mark.parametrize("operation", ["reserve", "commit", "accounting_bind", "reject"])
def test_versioned_accepted_submit_mutations_never_enumerate_unrelated_history(
    tmp_path: Any,
    monkeypatch: pytest.MonkeyPatch,
    source_id: str,
    operation: str,
) -> None:
    from services.orchestrator.accepted_submit_identity import AcceptedSubmitTransition
    from services.orchestrator.file_orchestration_journal import FileOrchestrationJournalRepository
    from services.orchestrator.reconcile import SacctRecord, reconcile_reserved_unbound_jobs

    if operation == "reserve":
        template = _file_cohort_repository(
            tmp_path / "template",
            member_count=1,
            with_runtime_rows=False,
            source_id=source_id,
        )
        pipeline_job_id = f"job_cycle_{source_id}_2026071200_forecast_fixture_forecast"
        template_row = template.get_accepted_submit_pipeline_job(pipeline_job_id)
        repository = FileOrchestrationJournalRepository(tmp_path / "target")
        assert repository.query_reserved_unbound_jobs() == []
        _seed_unrelated_history(repository)
    else:
        repository = _file_cohort_repository(
            tmp_path / "target",
            member_count=1,
            source_id=source_id,
        )
        pipeline_job_id = f"job_cycle_{source_id}_2026071200_forecast_fixture_forecast"
        template_row = None
        assert len(repository.query_reserved_unbound_jobs()) == 1
        _seed_unrelated_history(repository)
    repository.max_records = 8

    def global_iteration_forbidden(*_args: Any, **_kwargs: Any) -> Any:
        pytest.fail("versioned accepted-submit mutation called the global history iterator")

    monkeypatch.setattr(repository, "_iter_pipeline_job_records", global_iteration_forbidden)

    if operation == "reserve":
        assert template_row is not None
        clean_template = {
            **template_row,
            "status": "reserved",
            "slurm_job_id": None,
            "submit_outcome": None,
            "reconciliation_source": None,
            "reconciliation_decision": None,
            "reconciliation_reason_class": None,
            "matched_slurm_job_id": None,
            "candidate_projections": [],
        }
        created = repository.reserve_pipeline_job(dict(clean_template))
        assert created is not None
        assert repository.reserve_pipeline_job(dict(clean_template)) is None
        return

    current = repository.get_accepted_submit_pipeline_job(pipeline_job_id)
    assert current is not None
    idempotency_key = str(current["idempotency_key"])
    if operation == "commit":
        before = dict(current)
        stale = repository.commit_pipeline_job_submit_attempt(
            idempotency_key,
            pipeline_job_id=pipeline_job_id,
            expected_submission_attempt=2,
            slurm_job_id="71001",
            transition=AcceptedSubmitTransition.accepted(status="submitted"),
        )
        assert stale.outcome == "stale"
        assert repository.get_accepted_submit_pipeline_job(pipeline_job_id) == before
        applied = repository.commit_pipeline_job_submit_attempt(
            idempotency_key,
            pipeline_job_id=pipeline_job_id,
            expected_submission_attempt=1,
            slurm_job_id="71001",
            transition=AcceptedSubmitTransition.accepted(status="submitted"),
        )
        assert applied.outcome == "applied"
        assert applied.row["slurm_job_id"] == "71001"
        return

    if operation == "accounting_bind":
        exact = SacctRecord(
            slurm_job_id="71002",
            raw_state="RUNNING",
            job_name="nhms_forecast",
            comment=str(current["slurm_comment"]),
        )
        outcome = reconcile_reserved_unbound_jobs(repository, comment_query=lambda _key: exact)[0]
        assert outcome.action == "bound"
        assert repository.get_accepted_submit_pipeline_job(pipeline_job_id)["slurm_job_id"] == "71002"
        return

    rejected = repository.reject_pipeline_job_submit_attempt(
        idempotency_key,
        pipeline_job_id=pipeline_job_id,
        expected_submission_attempt=1,
        finished_at=datetime(2026, 7, 12, 0, 1, tzinfo=UTC),
        error_code="VALIDATION_ERROR",
        error_message="request rejected before acceptance",
        stage="forecast",
        job_type="run_shud_forecast_array",
    )
    assert rejected.outcome == "applied"
    reopened = FileOrchestrationJournalRepository(repository.root, max_records=8)
    row = reopened.get_accepted_submit_pipeline_job(pipeline_job_id)
    assert row["submit_outcome"] == "rejected"
    with reopened._locked_cycle_write(
        source_id=source_id,
        cycle_time=datetime(2026, 7, 12, tzinfo=UTC),
    ):
        rows = reopened._cycle_rows_by_model_unlocked(
            source_id=source_id,
            cycle_time=datetime(2026, 7, 12, tzinfo=UTC),
            model_ids=("model_0",),
            include_direct_jobs=False,
        )
    assert rows["model_0"].hydro_run["status"] == "failed"


@pytest.mark.parametrize("source_id", ["gfs", "ifs"])
def test_default_comment_accounting_requires_full_attempt_coverage_but_still_binds_match(
    tmp_path: Any,
    monkeypatch: pytest.MonkeyPatch,
    source_id: str,
) -> None:
    from services.orchestrator import reconcile as reconcile_module

    query_end = datetime(2026, 7, 22, 12, tzinfo=UTC)
    old_anchor = query_end - timedelta(days=8)
    repository = _file_cohort_repository(
        tmp_path / "absent",
        created_at=old_anchor,
        member_count=1,
        source_id=source_id,
    )
    commands: list[list[str]] = []

    def empty_page(command: list[str]) -> str:
        commands.append(command)
        return ""

    monkeypatch.setattr(reconcile_module, "_bounded_sacct_stdout", empty_page)
    outcome = reconcile_module.reconcile_reserved_unbound_jobs(
        repository,
        comment_query=reconcile_module.default_comment_sacct_querier(
            global_visibility_probe=lambda: True,
            now=lambda: query_end,
        ),
        grace=timedelta(0),
        now=lambda: query_end,
    )[0]
    pipeline_job_id = f"job_cycle_{source_id}_2026071200_forecast_fixture_forecast"
    persisted = repository.get_accepted_submit_pipeline_job(pipeline_job_id)
    assert outcome.action == "query_unavailable"
    assert outcome.reconciliation_decision == "accounting_unavailable"
    assert outcome.reconciliation_reason_class == "coverage_incomplete"
    assert persisted["status"] == "reserved"
    assert persisted["slurm_job_id"] is None
    assert persisted["reconciliation_reason_class"] == "coverage_incomplete"
    assert commands
    assert all(any(arg.startswith("--starttime=") for arg in command) for command in commands)
    assert all(any(arg.startswith("--endtime=") for arg in command) for command in commands)

    covered_repository = _file_cohort_repository(
        tmp_path / "covered",
        created_at=query_end - timedelta(minutes=1),
        member_count=1,
        source_id=source_id,
    )
    covered = reconcile_module.reconcile_reserved_unbound_jobs(
        covered_repository,
        comment_query=reconcile_module.default_comment_sacct_querier(
            global_visibility_probe=lambda: True,
            now=lambda: query_end,
        ),
        grace=timedelta(0),
        now=lambda: query_end,
    )[0]
    assert covered.action == "absence_retry_permitted"

    matched_repository = _file_cohort_repository(
        tmp_path / "matched",
        created_at=old_anchor,
        member_count=1,
        source_id=source_id,
    )
    matched = matched_repository.get_accepted_submit_pipeline_job(pipeline_job_id)
    comment = str(matched["slurm_comment"])
    row = f"72001|nhms_forecast|RUNNING|0:0|{comment}|||\n"
    monkeypatch.setattr(reconcile_module, "_bounded_sacct_stdout", lambda _command: row)
    bound = reconcile_module.reconcile_reserved_unbound_jobs(
        matched_repository,
        comment_query=reconcile_module.default_comment_sacct_querier(
            global_visibility_probe=lambda: True,
            now=lambda: query_end,
        ),
        now=lambda: query_end,
    )[0]
    assert bound.action == "bound"
    assert matched_repository.get_accepted_submit_pipeline_job(pipeline_job_id)["slurm_job_id"] == "72001"


@pytest.mark.parametrize("source_id", ["gfs", "ifs"])
@pytest.mark.parametrize(
    "coverage_case",
    [
        "declared_false",
        "missing_bounds",
        "reversed_bounds",
        "outside_anchor",
        "malformed_bounds",
        "naive_bounds",
    ],
)
def test_versioned_zero_recomputes_adapter_coverage_at_consumer_boundary(
    tmp_path: Any,
    source_id: str,
    coverage_case: str,
) -> None:
    from services.orchestrator.reconcile import CommentAccountingResult, reconcile_reserved_unbound_jobs

    anchor = datetime(2026, 7, 12, tzinfo=UTC)
    repository = _file_cohort_repository(
        tmp_path,
        created_at=anchor,
        member_count=1,
        source_id=source_id,
    )
    hydro_before = copy.deepcopy(repository._hydro_run_for(f"fcst_{source_id}_2026071200_model_0"))

    def declared_zero(_key: str, **_kwargs: Any) -> CommentAccountingResult:
        complete = coverage_case != "declared_false"
        start: Any = anchor - timedelta(seconds=1)
        end: Any = anchor + timedelta(seconds=1)
        if coverage_case == "missing_bounds":
            start = end = None
        elif coverage_case == "reversed_bounds":
            start, end = end, start
        elif coverage_case == "outside_anchor":
            start, end = anchor + timedelta(seconds=1), anchor + timedelta(seconds=2)
        elif coverage_case == "malformed_bounds":
            start, end = "2026-07-12T00:00:00Z", {"not": "a datetime"}
        elif coverage_case == "naive_bounds":
            start, end = datetime(2026, 7, 11, 23, 59), datetime(2026, 7, 12, 0, 1)
        return CommentAccountingResult(
            (),
            scope="global",
            coverage_start=start,
            coverage_end=end,
            coverage_complete=complete,
        )

    outcome = reconcile_reserved_unbound_jobs(
        repository,
        comment_query=declared_zero,
        grace=timedelta(0),
        now=lambda: anchor + timedelta(minutes=1),
    )[0]
    pipeline_job_id = f"job_cycle_{source_id}_2026071200_forecast_fixture_forecast"
    persisted = repository.get_accepted_submit_pipeline_job(pipeline_job_id)
    hydro = repository._hydro_run_for(f"fcst_{source_id}_2026071200_model_0")
    assert outcome.action == "query_unavailable"
    assert outcome.reconciliation_reason_class == "coverage_incomplete"
    assert persisted["status"] == "reserved"
    assert persisted["slurm_job_id"] is None
    assert persisted["reconciliation_reason_class"] == "coverage_incomplete"
    assert hydro == hydro_before


@pytest.mark.parametrize("source_id", ["gfs", "ifs"])
def test_missing_durable_attempt_anchor_blocks_zero_but_not_exact_match(
    tmp_path: Any,
    source_id: str,
) -> None:
    from services.orchestrator.reconcile import SacctRecord, reconcile_reserved_unbound_jobs

    anchor = datetime(2026, 7, 12, tzinfo=UTC)

    def hide_durable_anchor(repository: Any) -> None:
        original = repository.query_reserved_unbound_jobs

        def missing_anchor_rows() -> list[Any]:
            rows = original()
            for row in rows:
                row.submission_attempt_started_at = None
            return rows

        repository.query_reserved_unbound_jobs = missing_anchor_rows

    absent = _file_cohort_repository(
        tmp_path / "absent",
        created_at=anchor,
        member_count=1,
        source_id=source_id,
    )
    hide_durable_anchor(absent)
    unavailable = reconcile_reserved_unbound_jobs(
        absent,
        comment_query=_authoritative_absence_query,
        grace=timedelta(0),
        now=lambda: anchor + timedelta(minutes=1),
    )[0]
    pipeline_job_id = f"job_cycle_{source_id}_2026071200_forecast_fixture_forecast"
    persisted = absent.get_accepted_submit_pipeline_job(pipeline_job_id)
    assert unavailable.action == "query_unavailable"
    assert unavailable.reconciliation_reason_class == "coverage_incomplete"
    assert persisted["status"] == "reserved"
    assert persisted["slurm_job_id"] is None

    matched = _file_cohort_repository(
        tmp_path / "matched",
        created_at=anchor,
        member_count=1,
        source_id=source_id,
    )
    identity = matched.get_accepted_submit_pipeline_job(pipeline_job_id)
    hide_durable_anchor(matched)
    exact = SacctRecord(
        "72501",
        "RUNNING",
        "nhms_forecast",
        comment=str(identity["slurm_comment"]),
    )
    bound = reconcile_reserved_unbound_jobs(matched, comment_query=lambda _key: exact)[0]
    assert bound.action == "bound"
    assert matched.get_accepted_submit_pipeline_job(pipeline_job_id)["slurm_job_id"] == "72501"


@pytest.mark.parametrize("source_id", ["gfs", "ifs"])
def test_valid_custom_coverage_permits_exactly_one_retry_and_marker_free_is_unchanged(
    tmp_path: Any,
    source_id: str,
) -> None:
    from services.orchestrator.accepted_submit_identity import ACCEPTED_SUBMIT_CONTRACT_VERSION
    from services.orchestrator.file_orchestration_journal import FileOrchestrationJournalRepository
    from services.orchestrator.reconcile import CommentAccountingResult, reconcile_reserved_unbound_jobs

    anchor = datetime(2026, 7, 12, tzinfo=UTC)

    def valid_zero(_key: str, **_kwargs: Any) -> CommentAccountingResult:
        return CommentAccountingResult(
            (),
            scope="global",
            coverage_start=anchor - timedelta(seconds=1),
            coverage_end=anchor + timedelta(seconds=1),
            coverage_complete=True,
        )

    repository = _file_cohort_repository(
        tmp_path / "versioned",
        created_at=anchor,
        member_count=1,
        source_id=source_id,
    )
    pipeline_job_id = f"job_cycle_{source_id}_2026071200_forecast_fixture_forecast"
    assert (
        repository.permit_pipeline_job_retry(
            pipeline_job_id,
            accepted_submit_contract_version=ACCEPTED_SUBMIT_CONTRACT_VERSION,
            expected_submission_attempt=1,
            expected_submission_attempt_started_at=anchor + timedelta(seconds=1),
        )
        == 0
    )
    assert repository.get_accepted_submit_pipeline_job(pipeline_job_id)["status"] == "reserved"
    first = reconcile_reserved_unbound_jobs(
        repository,
        comment_query=valid_zero,
        grace=timedelta(0),
        now=lambda: anchor + timedelta(minutes=1),
    )
    second = reconcile_reserved_unbound_jobs(
        repository,
        comment_query=valid_zero,
        grace=timedelta(0),
        now=lambda: anchor + timedelta(minutes=1),
    )
    assert [outcome.action for outcome in first] == ["absence_retry_permitted"]
    assert second == []

    marker_template = _file_cohort_repository(
        tmp_path / "marker-template",
        created_at=anchor,
        member_count=1,
        source_id=source_id,
        versioned=False,
    )
    marker_row = marker_template.get_pipeline_job(pipeline_job_id)
    marker_row.pop("submission_attempt_started_at")
    marker_free = FileOrchestrationJournalRepository(tmp_path / "marker-free")
    assert marker_free.reserve_pipeline_job(marker_row) is not None
    legacy = reconcile_reserved_unbound_jobs(marker_free, comment_query=lambda _key: None)
    assert [outcome.action for outcome in legacy] == ["legacy_unversioned_read_only"]


@pytest.mark.parametrize(
    ("boundary", "reason_class", "payload", "max_rows", "max_bytes"),
    [
        ("rows", "bounded_output_rows_saturated", "\n\n", 1, 1024),
        ("bytes", "bounded_output_bytes_saturated", "123456789", 100, 8),
    ],
)
def test_versioned_accounting_saturation_is_public_bounded_unavailable_evidence(
    tmp_path: Any,
    monkeypatch: pytest.MonkeyPatch,
    boundary: str,
    reason_class: str,
    payload: str,
    max_rows: int,
    max_bytes: int,
) -> None:
    from services.orchestrator import reconcile as reconcile_module
    from services.orchestrator import scheduler_runtime

    repository = _file_cohort_repository(tmp_path, member_count=1)
    monkeypatch.setattr(reconcile_module, "MAX_COMMENT_SACCT_ROWS", max_rows)
    monkeypatch.setattr(reconcile_module, "MAX_COMMENT_SACCT_BYTES", max_bytes)
    monkeypatch.setattr(reconcile_module, "_bounded_sacct_stdout", lambda _command: payload)
    query_end = datetime(2026, 7, 12, 0, 1, tzinfo=UTC)
    outcome = reconcile_module.reconcile_reserved_unbound_jobs(
        repository,
        comment_query=reconcile_module.default_comment_sacct_querier(
            global_visibility_probe=lambda: True,
            now=lambda: query_end,
        ),
        now=lambda: query_end,
    )[0]
    pipeline_job_id = "job_cycle_gfs_2026071200_forecast_fixture_forecast"
    persisted = repository.get_accepted_submit_pipeline_job(pipeline_job_id)
    public = scheduler_runtime._restart_reconcile_attempt_evidence(repository, pipeline_job_id)
    assert outcome.action == "query_unavailable"
    assert outcome.match_count is None
    assert outcome.reconciliation_decision == "accounting_unavailable"
    assert outcome.reconciliation_reason_class == reason_class
    assert persisted["status"] == "reserved"
    assert persisted["slurm_job_id"] is None
    assert persisted["reconciliation_reason_class"] == reason_class
    assert public["reconciliation_reason_class"] == reason_class
    assert boundary in reason_class
    assert "nhms_idem:" not in str(public)
    assert str(repository.root) not in str(public)

    exact = reconcile_module.SacctRecord(
        "73001",
        "RUNNING",
        "nhms_forecast",
        comment=str(persisted["slurm_comment"]),
    )
    recovered = reconcile_module.reconcile_reserved_unbound_jobs(
        repository,
        comment_query=lambda _key: exact,
    )[0]
    rebound = repository.get_accepted_submit_pipeline_job(pipeline_job_id)
    assert recovered.action == "bound"
    assert rebound["slurm_job_id"] == "73001"
    assert rebound["reconciliation_reason_class"] is None


def test_legacy_custom_comment_adapter_remains_callable_but_cannot_prove_versioned_zero(
    tmp_path: Any,
) -> None:
    from services.orchestrator.reconcile import reconcile_reserved_unbound_jobs

    repository = _file_cohort_repository(tmp_path / "versioned", member_count=1)
    outcome = reconcile_reserved_unbound_jobs(
        repository,
        comment_query=lambda _key: None,
        grace=timedelta(0),
    )[0]
    assert outcome.action == "query_unavailable"
    assert outcome.reconciliation_reason_class == "coverage_incomplete"

    marker_free = _file_cohort_repository(
        tmp_path / "legacy",
        member_count=1,
        versioned=False,
    )
    legacy = reconcile_reserved_unbound_jobs(marker_free, comment_query=lambda _key: None)[0]
    assert legacy.action == "legacy_unversioned_read_only"


def test_parse_comment_sacct_rows_resolves_array_master() -> None:
    """Real (non-mock) parse of multi-row sacct output: an array stage stamped
    with the idempotency --comment reconciles back to its BARE master id. Array
    element rows (``<master>_<task>``) normalize to ``<master>``, ``.batch`` step
    sub-rows are skipped, and an unrelated Comment never false-matches.
    """

    from services.orchestrator.reconcile import SacctRecord, _parse_comment_sacct_rows
    from services.slurm_gateway.real_backend import SLURM_JOB_ID_RE

    stdout = (
        "77042_0|stageA|RUNNING|0:0|nhms_idem:K\n"
        "77042_1|stageA|RUNNING|0:0|nhms_idem:K\n"
        "77042.batch|batch|RUNNING|0:0|nhms_idem:K\n"
        "99999|other|RUNNING|0:0|nhms_idem:OTHER\n"
    )

    record = _parse_comment_sacct_rows(stdout, "nhms_idem:K")

    assert record is not None
    assert isinstance(record, SacctRecord)
    assert record.slurm_job_id == "77042"  # array element → bare master id.
    # The normalized id must pass the master/single-job id shape guard.
    assert SLURM_JOB_ID_RE.fullmatch("77042")


def test_parse_comment_sacct_rows_single_job() -> None:
    """A single (non-array) job with a matching Comment passes through unchanged;
    its ``.batch`` step sub-row is skipped.
    """

    from services.orchestrator.reconcile import _parse_comment_sacct_rows

    stdout = (
        "88001|stage|RUNNING|0:0|nhms_idem:K\n"
        "88001.batch|batch|RUNNING|0:0|nhms_idem:K\n"
    )

    record = _parse_comment_sacct_rows(stdout, "nhms_idem:K")

    assert record is not None
    assert record.slurm_job_id == "88001"  # no "_" → original id, untouched.


def test_parse_comment_sacct_rows_no_match_returns_none() -> None:
    """No row's Comment equals the target → None, the authoritative
    confirmed-absent answer that crash-recovery reconcile relies on.
    """

    from services.orchestrator.reconcile import _parse_comment_sacct_rows

    stdout = (
        "12345|stage|RUNNING|0:0|nhms_idem:OTHER\n"
        "12345.batch|batch|RUNNING|0:0|nhms_idem:OTHER\n"
        "67890_0|stage|RUNNING|0:0|nhms_idem:DIFFERENT\n"
    )

    assert _parse_comment_sacct_rows(stdout, "nhms_idem:K") is None


def test_comment_sacct_querier_scans_once_and_reaps_oversized_stream(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import os
    import threading

    from services.orchestrator import reconcile as reconcile_module

    scans = 0
    original_bounded = reconcile_module._bounded_sacct_stdout

    def bounded(_command: Any) -> str:
        nonlocal scans
        scans += 1
        return (
            "17667|nhms_forecast|RUNNING|0:0|nhms_idem:key-a|scheduler|account\n"
            "17668|nhms_forecast|PENDING|0:0|nhms_idem:key-b|scheduler|account\n"
        )

    monkeypatch.setattr(reconcile_module, "_bounded_sacct_stdout", bounded)
    query = reconcile_module.default_comment_sacct_querier(global_visibility_probe=lambda: True)
    assert query("key-a")[0].slurm_job_id == "17667"
    assert query("key-b")[0].slurm_job_id == "17668"
    assert scans == (reconcile_module.COMMENT_SACCT_LOOKBACK_DAYS * 24) // reconcile_module.COMMENT_SACCT_PAGE_HOURS

    class FakeProcess:
        def __init__(self) -> None:
            read_fd, self.write_fd = os.pipe()
            self.stdout = os.fdopen(read_fd, "rb", buffering=0)
            self.terminated = threading.Event()
            self.reaped = False
            self.thread = threading.Thread(target=self._write, daemon=True)
            self.thread.start()

        def _write(self) -> None:
            try:
                while not self.terminated.is_set():
                    os.write(self.write_fd, b"x" * 64)
            except OSError:
                pass
            finally:
                os.close(self.write_fd)

        def poll(self) -> int | None:
            return -15 if self.terminated.is_set() else None

        def terminate(self) -> None:
            self.terminated.set()

        kill = terminate

        def wait(self, timeout: float | None = None) -> int:
            self.thread.join(timeout)
            self.reaped = not self.thread.is_alive()
            return -15

    processes: list[FakeProcess] = []

    def popen(*_args: Any, **_kwargs: Any) -> FakeProcess:
        processes.append(FakeProcess())
        return processes[-1]

    monkeypatch.setattr(reconcile_module, "_bounded_sacct_stdout", original_bounded)
    monkeypatch.setattr(reconcile_module.subprocess, "Popen", popen)
    monkeypatch.setattr(reconcile_module, "MAX_COMMENT_SACCT_BYTES", 128)
    with pytest.raises(reconcile_module.ReconcileQueryUnavailable):
        reconcile_module.default_comment_sacct_querier(global_visibility_probe=lambda: True)("secret-key")
    assert len(processes) == 1
    assert processes[0].reaped is True


def test_file_submit_attempt_commit_is_cas_bound_idempotent_and_reopen_safe(tmp_path: Any) -> None:
    from services.orchestrator.accepted_submit_identity import AcceptedSubmitTransition
    from services.orchestrator.file_orchestration_journal import FileOrchestrationJournalRepository

    repository = _file_cohort_repository(tmp_path, member_count=18)
    key = "cycle_gfs_2026071200_forecast_fixture:forecast"
    job_id = "job_cycle_gfs_2026071200_forecast_fixture_forecast"

    applied = repository.commit_pipeline_job_submit_attempt(
        key,
        expected_submission_attempt=1,
        slurm_job_id="17667",
        transition=AcceptedSubmitTransition.accepted(status="submitted"),
    )
    idempotent = repository.commit_pipeline_job_submit_attempt(
        key,
        expected_submission_attempt=1,
        slurm_job_id="17667",
        transition=AcceptedSubmitTransition.accepted(status="submitted"),
    )
    collision = repository.commit_pipeline_job_submit_attempt(
        key,
        expected_submission_attempt=1,
        slurm_job_id="17668",
        transition=AcceptedSubmitTransition.accepted(status="submitted"),
    )
    stale = repository.commit_pipeline_job_submit_attempt(
        key,
        expected_submission_attempt=2,
        slurm_job_id="17667",
        transition=AcceptedSubmitTransition.accepted(status="submitted"),
    )

    assert (applied.outcome, idempotent.outcome, collision.outcome, stale.outcome) == (
        "applied",
        "idempotent",
        "collision",
        "stale",
    )
    reopened = FileOrchestrationJournalRepository(tmp_path / "journal")
    row = reopened.get_pipeline_job(job_id)
    assert row is not None
    assert row["slurm_job_id"] == "17667"
    assert row["submit_outcome"] == "accepted"
    assert [job.job_id for job in reopened.query_inflight_jobs()] == [job_id]
    assert reopened.query_reserved_unbound_jobs() == []


def test_active_reconcile_partition_finds_oldest_active_after_one_year_of_two_daily_cycles_and_two_sources(
    tmp_path: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from services.orchestrator import file_orchestration_journal as journal_module
    from services.orchestrator.accepted_submit_identity import (
        canonical_forecast_cohort_members,
        forecast_cohort_digest,
    )
    from services.orchestrator.file_orchestration_journal import _journal_record_for_write
    from services.orchestrator.reservation import slurm_comment_for

    repository = _file_cohort_repository(tmp_path, member_count=1, with_runtime_rows=False)
    key = "cycle_gfs_2026071200_forecast_fixture:forecast"
    job_id = "job_cycle_gfs_2026071200_forecast_fixture_forecast"
    _bind_current_file_cohort(repository, key, slurm_job_id="17667")
    master_path = repository.root / "pipeline-jobs" / f"{job_id}.json"
    template = json.loads(master_path.read_text(encoding="utf-8"))
    day_count = 365
    cycle_hours = (0, 12)
    source_ids = ("gfs", "ifs")
    candidates_per_master = 256
    history_count = 0
    history_start = datetime(2025, 7, 12, tzinfo=UTC)
    # Two cycles/day (00Z, 12Z) x two sources/cycle (GFS, IFS) is four
    # terminal cohort masters/day: 4 x 365 = 1,460 flat history records.
    for day in range(day_count):
        for cycle_hour in cycle_hours:
            cycle_time = history_start + timedelta(days=day, hours=cycle_hour)
            cycle_segment = cycle_time.strftime("%Y%m%d%H")
            cycle_iso = cycle_time.isoformat().replace("+00:00", "Z")
            for source_id in source_ids:
                history_count += 1
                run_id = f"cycle_{source_id}_{cycle_segment}_terminal_history"
                historical_job_id = f"job_{run_id}_forecast"
                cycle_id = f"{source_id}_{cycle_segment}"
                idempotency_key = f"{run_id}:forecast"
                slurm_job_id = str(20_000 + history_count)
                members = canonical_forecast_cohort_members(
                    source_id=source_id,
                    cycle_time=cycle_time,
                    basins=[
                        {
                            "model_id": f"history_{source_id}",
                            "basin_id": f"history_basin_{source_id}",
                            "task_id": 0,
                        }
                    ],
                )
                payload = copy.deepcopy(template["payload"])
                payload.update(
                    {
                        "job_id": historical_job_id,
                        "run_id": run_id,
                        "cycle_id": cycle_id,
                        "source_id": source_id,
                        "cycle_time": cycle_iso,
                        "idempotency_key": idempotency_key,
                        "slurm_comment": slurm_comment_for(idempotency_key),
                        "slurm_job_id": slurm_job_id,
                        "status": "succeeded",
                        "submit_outcome": "accepted",
                        "reconciliation_source": "slurm_exact_comment",
                        "reconciliation_decision": "matched_bound",
                        "reconciliation_reason_class": None,
                        "matched_slurm_job_id": slurm_job_id,
                        "cohort_members": list(members),
                        "candidate_projections": [
                            {
                                "candidate_id": members[0]["candidate_id"],
                                "run_id": members[0]["run_id"],
                                "model_id": members[0]["model_id"],
                                "array_task_id": 0,
                                "array_task_outcome": "succeeded",
                                "restart_stage": "state_save_qc",
                                "native_shud_resubmitted": False,
                            }
                        ],
                        "finished_at": cycle_iso,
                        "exit_code": 0,
                        "error_code": None,
                        "error_message": None,
                    }
                )
                payload["cohort_digest"] = forecast_cohort_digest(payload)
                historical = copy.deepcopy(template)
                historical.update(
                    {
                        "source_id": source_id,
                        "cycle_time": cycle_iso,
                        "job_id": historical_job_id,
                        "run_id": run_id,
                        "cycle_id": cycle_id,
                        "payload": payload,
                    }
                )
                repository._validated_direct_pipeline_job_record(
                    historical,
                    expected_job_id=historical_job_id,
                )
                path = repository.root / "pipeline-jobs" / f"{historical_job_id}.json"
                path.write_text(json.dumps(historical, sort_keys=True), encoding="utf-8")
                journal_record = _journal_record_for_write(
                    "pipeline_job",
                    payload,
                    source_id=source_id,
                    cycle_time=cycle_time,
                    model_id=None,
                    sequence=1,
                )
                repository._validate_outgoing_record(
                    journal_record,
                    source_id=source_id,
                    cycle_time=cycle_time,
                    record_type="pipeline_job",
                    model_id=None,
                )
                journal_path = (
                    repository.root / "journal" / source_id / f"{cycle_segment}.jsonl"
                )
                journal_path.parent.mkdir(parents=True, exist_ok=True)
                journal_path.write_text(
                    json.dumps(journal_record, separators=(",", ":"), sort_keys=True) + "\n",
                    encoding="utf-8",
                )

    # The first process performs the one strict migration over 1,460 real
    # terminal journal files. Every later process must trust only marker + inventory.
    migrated = type(repository)(repository.root)
    assert [job.job_id for job in migrated.query_inflight_jobs()] == [job_id]
    assert (repository.root / "reconcile-inventory-migration-v1.json").is_file()

    # Candidate history is sharded below by-cycle in production. Its annual
    # conceptual cardinality is 1,460 masters x 256 candidates = 373,760;
    # steady-state reconcile must not enumerate that tree, so materializing it
    # here would only make the test slower without strengthening the invariant.
    conceptual_candidate_count = (
        day_count * len(cycle_hours) * len(source_ids) * candidates_per_master
    )
    reopened = type(repository)(repository.root)
    read_optional_json = reopened._read_optional_json
    flat_history_reads = 0
    forbidden_scan_calls = 0
    virtual_candidate_accesses = 0
    directory_calls: list[str] = []
    stat_calls: list[str] = []
    read_calls: list[str] = []
    original_list_directory = journal_module.list_directory_no_follow_limited
    original_stat = journal_module.stat_no_follow

    def count_flat_history_reads(path: Any) -> Any:
        nonlocal flat_history_reads
        relative = str(path.relative_to(repository.root))
        read_calls.append(relative)
        if "by-cycle" in path.parts:
            return virtual_candidate_history_walker()
        if path.parent == repository.root / "pipeline-jobs" and "terminal_history" in path.name:
            flat_history_reads += 1
        return read_optional_json(path)

    def virtual_candidate_history_walker(*_args: Any, **_kwargs: Any) -> Any:
        nonlocal virtual_candidate_accesses
        virtual_candidate_accesses += conceptual_candidate_count
        raise AssertionError(
            f"candidate-history traversal attempted {conceptual_candidate_count} virtual reads"
        )

    def reject_global_scan(*_args: Any, **_kwargs: Any) -> Any:
        nonlocal forbidden_scan_calls
        forbidden_scan_calls += 1
        raise AssertionError("steady-state restart reconcile must use only the durable inventory")

    def bounded_list(path: Any, **kwargs: Any) -> Any:
        relative = str(path.relative_to(repository.root)) if path != repository.root else "."
        directory_calls.append(relative)
        if "by-cycle" in path.parts:
            return virtual_candidate_history_walker()
        assert relative not in {"pipeline-jobs", "journal", "pipeline-jobs/by-cycle"}
        return original_list_directory(path, **kwargs)

    def bounded_stat(path: Any, **kwargs: Any) -> Any:
        relative = str(path.relative_to(repository.root))
        stat_calls.append(relative)
        assert "terminal_history" not in relative
        if "by-cycle" in path.parts:
            return virtual_candidate_history_walker()
        return original_stat(path, **kwargs)

    monkeypatch.setattr(reopened, "_read_optional_json", count_flat_history_reads)
    monkeypatch.setattr(reopened, "_iter_reconcile_direct_pipeline_job_records", reject_global_scan)
    monkeypatch.setattr(
        reopened,
        "_iter_direct_pipeline_job_records_for_cycle",
        virtual_candidate_history_walker,
    )
    monkeypatch.setattr(
        reopened,
        "_direct_pipeline_job_records_for_cycle_cached",
        virtual_candidate_history_walker,
    )
    monkeypatch.setattr(journal_module, "list_directory_no_follow_limited", bounded_list)
    monkeypatch.setattr(journal_module, "stat_no_follow", bounded_stat)
    inflight = reopened.query_inflight_jobs()

    assert history_count == day_count * len(cycle_hours) * len(source_ids) == 1460
    historical_journal_paths = tuple(
        path
        for path in (repository.root / "journal").rglob("*.jsonl")
        if path.name != "2026071200.jsonl"
    )
    assert len(historical_journal_paths) == 1460
    assert conceptual_candidate_count == 365 * 2 * 2 * 256 == 373760
    assert [job.job_id for job in inflight] == [job_id]
    assert len(tuple((repository.root / "reconcile-inventory").glob("*.json"))) == 1
    assert forbidden_scan_calls == 0
    assert flat_history_reads == 0
    assert virtual_candidate_accesses == 0
    assert directory_calls == [".", "reconcile-inventory"]
    assert len(stat_calls) <= 8
    assert len(read_calls) <= 5


def test_reconcile_inventory_terminal_cleanup_and_stale_anchor_self_heal(tmp_path: Any) -> None:
    repository = _file_cohort_repository(tmp_path, member_count=1)
    key = "cycle_gfs_2026071200_forecast_fixture:forecast"
    job_id = "job_cycle_gfs_2026071200_forecast_fixture_forecast"
    _bind_current_file_cohort(repository, key, slurm_job_id="17667")
    active_path = repository.root / "reconcile-inventory" / f"{job_id}.json"
    assert active_path.is_file()

    stale_active = active_path.read_bytes()
    member = repository.get_pipeline_job(job_id)["cohort_members"][0]
    result = repository.project_forecast_cohort_tasks(
        job_id,
        master_slurm_job_id="17667",
        projections=[
            {
                **member,
                "array_task_outcome": "succeeded",
                "task_slurm_job_id": "17667_0",
                "restart_stage": "state_save_qc",
                "native_shud_resubmitted": False,
            }
        ],
        complete=True,
        master_status="succeeded",
        master_error_code=None,
        reconciliation_decision="matched_bound",
    )
    assert result["total"] > 0
    assert not active_path.exists()
    assert type(repository)(repository.root).query_inflight_jobs() == []

    # A crash after the canonical terminal write but before active-index
    # cleanup may leave a stale marker. Exact cycle replay wins, and the marker
    # is repaired while holding the same cycle lock.
    active_path.write_bytes(stale_active)
    assert type(repository)(repository.root).query_inflight_jobs() == []
    assert not active_path.exists()


def test_rejected_current_master_never_occupies_reconcile_inventory(tmp_path: Any) -> None:
    repository = _file_cohort_repository(tmp_path, member_count=1, with_runtime_rows=False)
    job_id = "job_cycle_gfs_2026071200_forecast_fixture_forecast"
    inventory_path = repository.root / "reconcile-inventory" / f"{job_id}.json"
    assert inventory_path.is_file()

    rejected = repository.reject_pipeline_job_submit_attempt(
        "cycle_gfs_2026071200_forecast_fixture:forecast",
        pipeline_job_id=job_id,
        expected_submission_attempt=1,
        finished_at=datetime(2026, 7, 12, 0, 1, tzinfo=UTC),
        error_code="SBATCH_SUBMISSION_FAILED",
        error_message="submit rejected",
        stage="forecast",
        job_type="run_shud_forecast_array",
    )
    assert rejected.committed
    assert not inventory_path.exists()
    assert type(repository)(repository.root).query_inflight_jobs() == []


def test_reservation_lost_current_master_never_becomes_task_projection_work(tmp_path: Any) -> None:
    from services.orchestrator.accepted_submit_identity import ACCEPTED_SUBMIT_CONTRACT_VERSION

    repository = _file_cohort_repository(tmp_path, member_count=1, with_runtime_rows=False)
    job_id = "job_cycle_gfs_2026071200_forecast_fixture_forecast"
    current = repository.get_pipeline_job(job_id)
    changed = repository.permit_pipeline_job_retry(
        job_id,
        accepted_submit_contract_version=ACCEPTED_SUBMIT_CONTRACT_VERSION,
        expected_submission_attempt=1,
        expected_submission_attempt_started_at=current["submission_attempt_started_at"],
    )

    assert changed == 1
    assert repository.get_pipeline_job(job_id)["status"] == "reservation_lost"
    assert not (repository.root / "reconcile-inventory" / f"{job_id}.json").exists()
    reopened = type(repository)(repository.root)
    assert reopened.query_reserved_unbound_jobs() == []
    assert reopened.query_inflight_jobs() == []


def test_reconcile_inventory_rolls_back_anchor_on_ordinary_pre_journal_failure(
    tmp_path: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from services.orchestrator.file_orchestration_journal import FileOrchestrationJournalRepository

    template_repository = _file_cohort_repository(tmp_path / "template", member_count=1)
    job_id = "job_cycle_gfs_2026071200_forecast_fixture_forecast"
    clean = dict(template_repository.get_pipeline_job(job_id))
    clean.update(
        {
            "status": "reserved",
            "slurm_job_id": None,
            "submit_outcome": None,
            "reconciliation_source": None,
            "reconciliation_decision": None,
            "reconciliation_reason_class": None,
            "matched_slurm_job_id": None,
            "candidate_projections": [],
        }
    )
    repository = FileOrchestrationJournalRepository(tmp_path / "failed" / "journal")

    def fail_append(**_kwargs: Any) -> None:
        raise RuntimeError("ordinary append failure")

    monkeypatch.setattr(repository, "_append_journal_record_unlocked", fail_append)
    with pytest.raises(RuntimeError, match="ordinary append failure"):
        repository.reserve_pipeline_job(clean)

    assert not (repository.root / "reconcile-inventory" / f"{job_id}.json").exists()
    assert not (repository.root / "pipeline-jobs" / f"{job_id}.json").exists()


def test_reconcile_inventory_orphan_anchor_self_cleans_after_pre_journal_crash(
    tmp_path: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from services.orchestrator.file_orchestration_journal import FileOrchestrationJournalRepository

    class SimulatedCrash(BaseException):
        pass

    template_repository = _file_cohort_repository(tmp_path / "template", member_count=1)
    job_id = "job_cycle_gfs_2026071200_forecast_fixture_forecast"
    clean = dict(template_repository.get_pipeline_job(job_id))
    clean.update(
        {
            "status": "reserved",
            "slurm_job_id": None,
            "submit_outcome": None,
            "reconciliation_source": None,
            "reconciliation_decision": None,
            "reconciliation_reason_class": None,
            "matched_slurm_job_id": None,
            "candidate_projections": [],
        }
    )
    repository = FileOrchestrationJournalRepository(tmp_path / "crashed" / "journal")

    def crash_before_journal(**_kwargs: Any) -> None:
        raise SimulatedCrash

    monkeypatch.setattr(repository, "_append_journal_record_unlocked", crash_before_journal)
    with pytest.raises(SimulatedCrash):
        repository.reserve_pipeline_job(clean)

    inventory_path = repository.root / "reconcile-inventory" / f"{job_id}.json"
    assert inventory_path.is_file()
    assert type(repository)(repository.root).query_reserved_unbound_jobs() == []
    assert not inventory_path.exists()


def test_reconcile_inventory_recovers_active_from_post_journal_direct_crash(
    tmp_path: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from services.orchestrator.file_orchestration_journal import FileOrchestrationJournalRepository

    class SimulatedCrash(BaseException):
        pass

    template_repository = _file_cohort_repository(tmp_path / "template", member_count=1)
    job_id = "job_cycle_gfs_2026071200_forecast_fixture_forecast"
    clean = dict(template_repository.get_pipeline_job(job_id))
    clean.update(
        {
            "status": "reserved",
            "slurm_job_id": None,
            "submit_outcome": None,
            "reconciliation_source": None,
            "reconciliation_decision": None,
            "reconciliation_reason_class": None,
            "matched_slurm_job_id": None,
            "candidate_projections": [],
        }
    )
    repository = FileOrchestrationJournalRepository(tmp_path / "crashed" / "journal")

    def crash_before_direct(*_args: Any, **_kwargs: Any) -> None:
        raise SimulatedCrash

    monkeypatch.setattr(repository, "_write_pipeline_job_direct_unlocked", crash_before_direct)
    with pytest.raises(SimulatedCrash):
        repository.reserve_pipeline_job(clean)

    assert not (repository.root / "pipeline-jobs" / f"{job_id}.json").exists()
    recovered = type(repository)(repository.root).query_reserved_unbound_jobs()
    assert [job.job_id for job in recovered] == [job_id]


def test_reconcile_inventory_terminal_journal_wins_after_direct_cleanup_crash(
    tmp_path: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class SimulatedCrash(BaseException):
        pass

    repository = _file_cohort_repository(tmp_path, member_count=1)
    key = "cycle_gfs_2026071200_forecast_fixture:forecast"
    job_id = "job_cycle_gfs_2026071200_forecast_fixture_forecast"
    _bind_current_file_cohort(repository, key, slurm_job_id="99006")
    member = repository.get_pipeline_job(job_id)["cohort_members"][0]

    def crash_before_direct(*_args: Any, **_kwargs: Any) -> None:
        raise SimulatedCrash

    monkeypatch.setattr(repository, "_write_pipeline_job_direct_unlocked", crash_before_direct)
    with pytest.raises(SimulatedCrash):
        repository.project_forecast_cohort_tasks(
            job_id,
            master_slurm_job_id="99006",
            projections=[
                {
                    **member,
                    "array_task_outcome": "succeeded",
                    "task_slurm_job_id": "99006_0",
                    "restart_stage": "state_save_qc",
                    "native_shud_resubmitted": False,
                }
            ],
            complete=True,
            master_status="succeeded",
            master_error_code=None,
            reconciliation_decision="matched_bound",
        )

    inventory_path = repository.root / "reconcile-inventory" / f"{job_id}.json"
    assert inventory_path.is_file()
    reopened = type(repository)(repository.root)
    assert reopened.query_inflight_jobs() == []
    assert reopened.get_pipeline_job(job_id)["status"] == "succeeded"
    assert not inventory_path.exists()


def test_reconcile_inventory_migration_is_resumable_and_backfills_marker_free_active(
    tmp_path: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repository = _file_cohort_repository(tmp_path, member_count=1, source_id="gfs")
    repository = _file_cohort_repository(
        tmp_path,
        member_count=1,
        source_id="ifs",
        versioned=False,
    )
    inventory_directory = repository.root / "reconcile-inventory"
    for path in inventory_directory.glob("*.json"):
        path.unlink()
    marker_path = repository.root / "reconcile-inventory-migration-v1.json"
    marker_path.unlink(missing_ok=True)

    class SimulatedCrash(BaseException):
        pass

    original_sync = repository._sync_reconcile_inventory_for_row_unlocked
    active_syncs = 0

    def interrupt_second_active(row: Any) -> bool:
        nonlocal active_syncs
        result = original_sync(row)
        if result:
            active_syncs += 1
            if active_syncs == 2:
                raise SimulatedCrash
        return result

    monkeypatch.setattr(repository, "_sync_reconcile_inventory_for_row_unlocked", interrupt_second_active)
    with pytest.raises(SimulatedCrash):
        repository.query_reserved_unbound_jobs()
    assert not marker_path.exists()
    assert len(tuple(inventory_directory.glob("*.json"))) == 2

    reopened = type(repository)(repository.root)
    recovered = reopened.query_reserved_unbound_jobs()
    assert {job.job_id for job in recovered} == {
        "job_cycle_gfs_2026071200_forecast_fixture_forecast",
        "job_cycle_ifs_2026071200_forecast_fixture_forecast",
    }
    assert marker_path.is_file()
    assert len(tuple(inventory_directory.glob("*.json"))) == 2


@pytest.mark.parametrize(
    "target_kind",
    ["inventory", "migration_marker", "rollback_receipt", "rollforward_receipt"],
)
def test_round8_atomic_temp_crash_residue_is_cleaned_after_real_child_kill(
    tmp_path: Any,
    target_kind: str,
) -> None:
    import signal
    import subprocess
    import sys

    repository = _file_cohort_repository(tmp_path, member_count=1, with_runtime_rows=False)
    job_id = "job_cycle_gfs_2026071200_forecast_fixture_forecast"
    assert [job.job_id for job in repository.query_reserved_unbound_jobs()] == [job_id]
    if target_kind == "inventory":
        target = repository.root / "reconcile-inventory" / f"{job_id}.json"
    elif target_kind == "rollback_receipt":
        target = repository.root / "reconcile-inventory-rollback-preparation-v2.json"
    elif target_kind == "rollforward_receipt":
        target = repository.root / "reconcile-inventory-rollforward-v1.json"
    else:
        target = repository.root / "reconcile-inventory-migration-v1.json"
        target.unlink()
    script = """
import os, signal, sys
from pathlib import Path
from packages.common import safe_fs
target = Path(sys.argv[1])
root = Path(sys.argv[2])
def crash(*_args, **_kwargs):
    os.kill(os.getpid(), signal.SIGKILL)
safe_fs.os.replace = crash
safe_fs.atomic_write_bytes_no_follow(
    target, b'{}', containment_root=root, require_durable_replace=True
)
"""
    completed = subprocess.run(
        [sys.executable, "-c", script, str(target), str(repository.root)],
        cwd=os.getcwd(),
        check=False,
    )
    assert completed.returncode == -signal.SIGKILL
    residues = tuple(target.parent.glob(f".{target.name}.*.tmp"))
    assert len(residues) == 1

    reopened = type(repository)(repository.root)
    assert [job.job_id for job in reopened.query_reserved_unbound_jobs()] == [job_id]
    assert not residues[0].exists()
    assert (repository.root / "reconcile-inventory-migration-v1.json").is_file()


@pytest.mark.parametrize(
    ("entry_name", "make_symlink"),
    [
        ("unknown.tmp", False),
        (".job_cycle_gfs_2026071200_forecast_fixture_forecast.json.bad.tmp", False),
        (
            ".job_cycle_gfs_2026071200_forecast_fixture_forecast.json."
            "0123456789abcdef0123456789abcdef.tmp",
            True,
        ),
    ],
)
def test_round8_inventory_unknown_or_nonregular_temp_entry_fails_closed(
    tmp_path: Any,
    entry_name: str,
    make_symlink: bool,
) -> None:
    from services.orchestrator.file_orchestration_journal import FileOrchestrationJournalError

    repository = _file_cohort_repository(tmp_path, member_count=1, with_runtime_rows=False)
    assert len(repository.query_reserved_unbound_jobs()) == 1
    entry = repository.root / "reconcile-inventory" / entry_name
    if make_symlink:
        entry.symlink_to(repository.root / "reconcile-inventory-migration-v1.json")
    else:
        entry.write_text("residue", encoding="utf-8")

    with pytest.raises(FileOrchestrationJournalError):
        type(repository)(repository.root).query_reserved_unbound_jobs()
    assert entry.exists() or entry.is_symlink()


def test_round8_migration_marker_malformed_temp_sibling_fails_closed(tmp_path: Any) -> None:
    from services.orchestrator.file_orchestration_journal import FileOrchestrationJournalError

    repository = _file_cohort_repository(tmp_path, member_count=1, with_runtime_rows=False)
    marker = repository.root / "reconcile-inventory-migration-v1.json"
    marker.unlink(missing_ok=True)
    sibling = repository.root / ".reconcile-inventory-migration-v1.json.bad.tmp"
    sibling.write_text("residue", encoding="utf-8")
    with pytest.raises(FileOrchestrationJournalError):
        type(repository)(repository.root).query_reserved_unbound_jobs()
    assert sibling.is_file()
    assert not marker.exists()


@pytest.mark.parametrize(
    "surface",
    [
        "direct",
        "direct_bytes",
        "direct_nonregular",
        "direct_unreadable",
        "journal",
        "journal_records",
        "journal_unreadable",
        "legacy",
        "over_limit",
    ],
)
def test_round8_migration_blocks_marker_until_every_authority_surface_is_repaired(
    tmp_path: Any,
    monkeypatch: pytest.MonkeyPatch,
    surface: str,
) -> None:
    from services.orchestrator import file_orchestration_journal as journal_module
    from services.orchestrator.file_orchestration_journal import (
        FileOrchestrationJournalError,
        FileOrchestrationJournalRepository,
    )

    repository = _file_cohort_repository(tmp_path, member_count=1, with_runtime_rows=False)
    job_id = "job_cycle_gfs_2026071200_forecast_fixture_forecast"
    marker = repository.root / "reconcile-inventory-migration-v1.json"
    marker.unlink(missing_ok=True)
    for path in (repository.root / "reconcile-inventory").glob("*.json"):
        path.unlink()
    repairs: list[tuple[Any, bytes | None]] = []
    unreadable_path = None
    if surface in {"direct", "direct_bytes", "direct_nonregular", "direct_unreadable"}:
        path = repository.root / "pipeline-jobs" / f"{job_id}.json"
        if surface != "direct_unreadable":
            repairs.append((path, path.read_bytes()))
        if surface == "direct_nonregular":
            path.unlink()
            path.symlink_to(repository.root / "journal" / "gfs" / "2026071200.jsonl")
        elif surface == "direct_bytes":
            path.write_bytes(b"x" * 65)
        elif surface == "direct_unreadable":
            unreadable_path = path
        else:
            path.write_bytes(b"{not-json")
    elif surface in {"journal", "journal_records", "journal_unreadable"}:
        path = repository.root / "journal" / "gfs" / "2026071200.jsonl"
        if surface != "journal_unreadable":
            repairs.append((path, path.read_bytes()))
        if surface == "journal_records":
            path.write_bytes(path.read_bytes() + path.read_bytes())
        elif surface == "journal_unreadable":
            unreadable_path = path
        else:
            path.write_bytes(b"{not-json\n")
    elif surface == "legacy":
        path = repository.root / "active-reconcile" / "malformed.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        repairs.append((path, None))
        path.write_bytes(b"{not-json")
    else:
        path = repository.root / "pipeline-jobs" / "extra.json"
        repairs.append((path, None))
        path.write_text("{}", encoding="utf-8")

    limits: dict[str, int] = {}
    original_record_limit = journal_module.MAX_FILE_JOURNAL_RECORDS
    if surface == "over_limit":
        limits["max_files"] = 1
    elif surface == "direct_bytes":
        limits["max_bytes"] = 64
    elif surface == "journal_records":
        monkeypatch.setattr(journal_module, "MAX_FILE_JOURNAL_RECORDS", 1)
    original_read = journal_module.read_bytes_limited_no_follow
    if unreadable_path is not None:

        def deny_authority_read(path: Any, **kwargs: Any) -> Any:
            if path == unreadable_path:
                raise PermissionError("injected unreadable authority surface")
            return original_read(path, **kwargs)

        monkeypatch.setattr(journal_module, "read_bytes_limited_no_follow", deny_authority_read)
    reopened = FileOrchestrationJournalRepository(repository.root, **limits)
    with pytest.raises(FileOrchestrationJournalError):
        reopened.query_reserved_unbound_jobs()
    assert not marker.exists()

    for path, content in repairs:
        if path.is_symlink():
            path.unlink()
        if content is None:
            path.unlink()
        else:
            path.write_bytes(content)
    if surface == "journal_records":
        monkeypatch.setattr(journal_module, "MAX_FILE_JOURNAL_RECORDS", original_record_limit)
    if unreadable_path is not None:
        monkeypatch.setattr(journal_module, "read_bytes_limited_no_follow", original_read)
    repaired = FileOrchestrationJournalRepository(repository.root)
    assert [job.job_id for job in repaired.query_reserved_unbound_jobs()] == [job_id]
    assert marker.is_file()


def test_round8_migration_discovers_journal_only_active_row(tmp_path: Any) -> None:
    repository = _file_cohort_repository(tmp_path, member_count=1, with_runtime_rows=False)
    job_id = "job_cycle_gfs_2026071200_forecast_fixture_forecast"
    (repository.root / "pipeline-jobs" / f"{job_id}.json").unlink()
    (repository.root / "reconcile-inventory-migration-v1.json").unlink(missing_ok=True)
    for path in (repository.root / "reconcile-inventory").glob("*.json"):
        path.unlink()

    reopened = type(repository)(repository.root)
    assert [job.job_id for job in reopened.query_reserved_unbound_jobs()] == [job_id]


@pytest.mark.parametrize("surface", ["journal", "legacy"])
@pytest.mark.parametrize("boundary", ["stat", "read"])
def test_round11_migration_disappearance_fails_without_marker_and_reopens_after_repair(
    tmp_path: Any,
    monkeypatch: pytest.MonkeyPatch,
    surface: str,
    boundary: str,
) -> None:
    from services.orchestrator.file_orchestration_journal import (
        FileOrchestrationJournalError,
        FileOrchestrationJournalRepository,
    )

    template = _file_cohort_repository(
        tmp_path / "template",
        member_count=1,
        with_runtime_rows=False,
    )
    job_id = "job_cycle_gfs_2026071200_forecast_fixture_forecast"
    direct = template.root / "pipeline-jobs" / f"{job_id}.json"
    journal = template.root / "journal" / "gfs" / "2026071200.jsonl"
    root = tmp_path / f"{surface}-{boundary}" / "journal"
    root.mkdir(parents=True)
    if surface == "journal":
        target = root / "journal" / "gfs" / "2026071200.jsonl"
        target.parent.mkdir(parents=True)
        target.write_bytes(journal.read_bytes())
    else:
        target = root / "active-reconcile" / f"{job_id}.json"
        target.parent.mkdir(parents=True)
        target.write_bytes(direct.read_bytes())
    original = target.read_bytes()
    repository = FileOrchestrationJournalRepository(root)
    marker = root / "reconcile-inventory-migration-v1.json"

    if boundary == "stat":
        iterator_name = (
            "_iter_migration_journal_paths"
            if surface == "journal"
            else "_iter_migration_legacy_active_paths"
        )
        original_iterator = getattr(repository, iterator_name)

        def disappear_after_enumeration() -> list[Any]:
            paths = original_iterator()
            target.unlink()
            return paths

        monkeypatch.setattr(repository, iterator_name, disappear_after_enumeration)
    elif surface == "journal":
        original_read = repository._read_jsonl

        def disappear_after_journal_read(path: Any) -> list[dict[str, Any]]:
            records = original_read(path)
            if path == target:
                target.unlink()
            return records

        monkeypatch.setattr(repository, "_read_jsonl", disappear_after_journal_read)
    else:
        original_read = repository._read_optional_json

        def disappear_after_legacy_read(path: Any) -> dict[str, Any] | None:
            payload = original_read(path)
            if path == target:
                target.unlink()
            return payload

        monkeypatch.setattr(repository, "_read_optional_json", disappear_after_legacy_read)

    with pytest.raises(FileOrchestrationJournalError):
        repository.query_reserved_unbound_jobs()
    assert repository._reconcile_inventory_migration_checked is False
    assert not marker.exists()

    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(original)
    repaired = FileOrchestrationJournalRepository(root)
    assert [job.job_id for job in repaired.query_reserved_unbound_jobs()] == [job_id]
    assert marker.is_file()


@pytest.mark.parametrize("surface", ["direct", "legacy", "journal"])
def test_round12_rollforward_strictly_backfills_once_under_real_scheduler_lease(
    tmp_path: Any,
    monkeypatch: pytest.MonkeyPatch,
    surface: str,
) -> None:
    from services.orchestrator.file_orchestration_journal import (
        FileOrchestrationJournalError,
        FileOrchestrationJournalRepository,
    )
    from services.orchestrator.file_orchestration_migration import (
        complete_file_journal_rollforward,
        prepare_file_journal_rollback,
        require_file_journal_rollback_prepared,
    )

    template = _file_cohort_repository(
        tmp_path / f"template-{surface}",
        member_count=1,
        with_runtime_rows=False,
    )
    job_id = "job_cycle_gfs_2026071200_forecast_fixture_forecast"
    key = "cycle_gfs_2026071200_forecast_fixture:forecast"
    if surface == "journal":
        _bind_current_file_cohort(template, key, slurm_job_id="88201", status="running")

    root = tmp_path / f"target-{surface}" / "journal"
    workspace = tmp_path / f"workspace-{surface}"
    workspace.mkdir()
    target = FileOrchestrationJournalRepository(root)
    assert target.query_reserved_unbound_jobs() == []
    marker = root / "reconcile-inventory-migration-v1.json"
    assert marker.is_file()
    receipt = prepare_file_journal_rollback(
        journal_root=root,
        workspace_root=workspace,
        scheduler_state="stopped",
        active_scheduler_processes=0,
        checked_at=datetime.now(UTC),
        checked_by="round11-test-operator",
        target_writer_generation="pre-reconcile-inventory",
    )
    assert receipt["status"] == "prepared"
    assert not marker.exists()
    assert require_file_journal_rollback_prepared(
        journal_root=root,
        workspace_root=workspace,
        receipt_id=receipt["receipt_id"],
        actual_writer_generation="pre-reconcile-inventory",
    )["receipt_id"] == receipt["receipt_id"]

    direct = template.root / "pipeline-jobs" / f"{job_id}.json"
    if surface == "direct":
        destination = root / "pipeline-jobs" / f"{job_id}.json"
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(direct.read_bytes())
    elif surface == "legacy":
        destination = root / "active-reconcile" / f"{job_id}.json"
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(direct.read_bytes())
    else:
        source_journal = template.root / "journal" / "gfs" / "2026071200.jsonl"
        destination = root / "journal" / "gfs" / "2026071200.jsonl"
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(source_journal.read_bytes())

    with pytest.raises(FileOrchestrationJournalError, match="file_journal_rollforward_required"):
        FileOrchestrationJournalRepository(root).query_inflight_jobs()

    rollforward = complete_file_journal_rollforward(
        journal_root=root,
        workspace_root=workspace,
        preparation_receipt_id=receipt["receipt_id"],
    )
    assert rollforward["preparation_receipt_id"] == receipt["receipt_id"]
    reopened = FileOrchestrationJournalRepository(root)
    if surface == "journal":
        assert [job.job_id for job in reopened.query_inflight_jobs()] == [job_id]
    else:
        assert [job.job_id for job in reopened.query_reserved_unbound_jobs()] == [job_id]
    assert (root / "reconcile-inventory" / f"{job_id}.json").is_file()
    assert marker.is_file()
    with pytest.raises(FileOrchestrationJournalError, match="file_journal_rollback_not_prepared"):
        require_file_journal_rollback_prepared(
            journal_root=root,
            workspace_root=workspace,
            receipt_id=receipt["receipt_id"],
            actual_writer_generation="pre-reconcile-inventory",
        )

    steady = FileOrchestrationJournalRepository(root)

    def backfill_forbidden() -> str:
        raise AssertionError("migration marker must make steady-state history replay impossible")

    monkeypatch.setattr(steady, "_stable_backfill_reconcile_inventory_unlocked", backfill_forbidden)
    if surface == "journal":
        assert [job.job_id for job in steady.query_inflight_jobs()] == [job_id]
    else:
        assert [job.job_id for job in steady.query_reserved_unbound_jobs()] == [job_id]


def test_round12_live_scheduler_lease_blocks_prepare_without_mutating_authority(
    tmp_path: Any,
) -> None:
    from services.orchestrator.file_orchestration_journal import (
        FileOrchestrationJournalError,
        FileOrchestrationJournalRepository,
    )
    from services.orchestrator.file_orchestration_migration import prepare_file_journal_rollback
    from services.orchestrator.scheduler_lease import FileSchedulerLease

    repository = FileOrchestrationJournalRepository(tmp_path / "journal")
    assert repository.query_inflight_jobs() == []
    marker = repository.root / "reconcile-inventory-migration-v1.json"
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    lock_path = workspace / "scheduler" / "production-scheduler.lock"
    holder = FileSchedulerLease(lock_path, ttl_seconds=60, workspace_root=workspace)
    assert holder.acquire(
        pass_id="live-production-scheduler",
        started_at=datetime.now(UTC),
    )["acquired"] is True
    assert marker.is_file()
    marker_before = marker.read_bytes()
    receipt_path = repository.root / "reconcile-inventory-rollback-preparation-v2.json"

    try:
        with pytest.raises(FileOrchestrationJournalError, match="file_journal_scheduler_lease_contended"):
            prepare_file_journal_rollback(
                journal_root=repository.root,
                workspace_root=workspace,
                scheduler_state="stopped",
                active_scheduler_processes=0,
                checked_at=datetime.now(UTC),
                checked_by="round12-test-operator",
                target_writer_generation="pre-reconcile-inventory",
            )
    finally:
        holder.release(pass_id="live-production-scheduler")

    assert marker.read_bytes() == marker_before
    assert not receipt_path.exists()


def test_round12_concurrent_prepare_holds_the_real_scheduler_mutation_authority(
    tmp_path: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import threading

    from services.orchestrator.file_orchestration_journal import FileOrchestrationJournalRepository
    from services.orchestrator.file_orchestration_migration import prepare_file_journal_rollback
    from services.orchestrator.scheduler_lease import FileSchedulerLease

    root = tmp_path / "journal"
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    repository = FileOrchestrationJournalRepository(root)
    assert repository.query_inflight_jobs() == []
    marker = root / "reconcile-inventory-migration-v1.json"
    marker_before = marker.read_bytes()
    entered = threading.Event()
    continue_prepare = threading.Event()
    original_prepare = (
        FileOrchestrationJournalRepository._prepare_reconcile_inventory_rollback_under_scheduler_lease
    )

    def pause_after_lease_acquisition(self: Any, **kwargs: Any) -> dict[str, Any]:
        entered.set()
        assert continue_prepare.wait(timeout=5)
        return original_prepare(self, **kwargs)

    monkeypatch.setattr(
        FileOrchestrationJournalRepository,
        "_prepare_reconcile_inventory_rollback_under_scheduler_lease",
        pause_after_lease_acquisition,
    )
    outcome: dict[str, Any] = {}

    def prepare() -> None:
        outcome.update(
            prepare_file_journal_rollback(
                journal_root=root,
                workspace_root=workspace,
                scheduler_state="stopped",
                active_scheduler_processes=0,
                checked_at=datetime.now(UTC),
                checked_by="round12-test-operator",
                target_writer_generation="pre-reconcile-inventory",
            )
        )

    thread = threading.Thread(target=prepare)
    thread.start()
    assert entered.wait(timeout=5)
    contender = FileSchedulerLease(
        workspace / "scheduler" / "production-scheduler.lock",
        ttl_seconds=60,
        workspace_root=workspace,
    )
    contender_result = contender.acquire(
        pass_id="concurrent-current-scheduler",
        started_at=datetime.now(UTC),
    )
    assert contender_result["acquired"] is False
    assert marker.read_bytes() == marker_before

    continue_prepare.set()
    thread.join(timeout=5)
    assert not thread.is_alive()
    assert outcome["status"] == "prepared"
    assert not marker.exists()


def _round14_clean_writer_checkout(root: Any, *, content: str) -> tuple[Any, str]:
    import subprocess
    from pathlib import Path

    checkout = Path(root)
    checkout.mkdir(parents=True)
    (checkout / ".gitignore").write_text(".venv/\n", encoding="utf-8")
    (checkout / "writer.txt").write_text(content, encoding="utf-8")
    subprocess.run(("git", "init", "-q"), cwd=checkout, check=True)
    subprocess.run(("git", "add", ".gitignore", "writer.txt"), cwd=checkout, check=True)
    subprocess.run(
        (
            "git",
            "-c",
            "user.name=Round14 Test",
            "-c",
            "user.email=round14@example.invalid",
            "commit",
            "-q",
            "-m",
            "writer fixture",
        ),
        cwd=checkout,
        check=True,
    )
    generation = subprocess.run(
        ("git", "rev-parse", "HEAD"),
        cwd=checkout,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    return checkout, generation


def test_round14_prepare_resumes_after_receipt_write_before_marker_unlink(
    tmp_path: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from pathlib import Path

    from services.orchestrator import file_orchestration_journal as journal_module
    from services.orchestrator.file_orchestration_journal import (
        FileOrchestrationJournalError,
        FileOrchestrationJournalRepository,
    )
    from services.orchestrator.file_orchestration_migration import (
        complete_file_journal_rollforward,
        prepare_file_journal_rollback,
        require_file_journal_rollback_prepared,
    )

    root = tmp_path / "journal"
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    repository = FileOrchestrationJournalRepository(root)
    assert repository.query_inflight_jobs() == []
    marker = root / "reconcile-inventory-migration-v1.json"
    receipt_path = root / "reconcile-inventory-rollback-preparation-v2.json"
    real_unlink = journal_module.unlink_no_follow
    crashed = False

    def crash_before_marker_unlink(path: Any, **kwargs: Any) -> None:
        nonlocal crashed
        if Path(path) == marker and not crashed:
            crashed = True
            raise OSError("injected crash after preparing receipt")
        real_unlink(path, **kwargs)

    monkeypatch.setattr(journal_module, "unlink_no_follow", crash_before_marker_unlink)
    with pytest.raises(
        FileOrchestrationJournalError,
        match="file_journal_rollback_preparation_unavailable",
    ):
        prepare_file_journal_rollback(
            journal_root=root,
            workspace_root=workspace,
            scheduler_state="stopped",
            active_scheduler_processes=0,
            checked_at=datetime.now(UTC),
            checked_by="round14-first-operator",
            target_writer_generation="writer-A",
        )
    preparing = json.loads(receipt_path.read_text(encoding="utf-8"))
    assert preparing["status"] == "preparing"
    assert marker.is_file()

    monkeypatch.setattr(journal_module, "unlink_no_follow", real_unlink)
    with pytest.raises(
        FileOrchestrationJournalError,
        match="file_journal_rollback_fence_conflict",
    ):
        prepare_file_journal_rollback(
            journal_root=root,
            workspace_root=workspace,
            scheduler_state="stopped",
            active_scheduler_processes=0,
            checked_at=datetime.now(UTC),
            checked_by="round14-wrong-generation",
            target_writer_generation="writer-B",
        )
    assert marker.is_file()
    assert json.loads(receipt_path.read_text(encoding="utf-8")) == preparing
    prepared = prepare_file_journal_rollback(
        journal_root=root,
        workspace_root=workspace,
        scheduler_state="stopped",
        active_scheduler_processes=0,
        checked_at=datetime.now(UTC),
        checked_by="round14-retry-operator",
        target_writer_generation="writer-A",
    )
    assert prepared["status"] == "prepared"
    assert prepared["receipt_id"] == preparing["receipt_id"]
    assert not marker.exists()
    assert prepare_file_journal_rollback(
        journal_root=root,
        workspace_root=workspace,
        scheduler_state="stopped",
        active_scheduler_processes=0,
        checked_at=datetime.now(UTC),
        checked_by="round14-idempotent-retry",
        target_writer_generation="writer-A",
    ) == prepared
    assert require_file_journal_rollback_prepared(
        journal_root=root,
        workspace_root=workspace,
        receipt_id=prepared["receipt_id"],
        actual_writer_generation="writer-A",
    )["status"] == "prepared"

    rollforward = complete_file_journal_rollforward(
        journal_root=root,
        workspace_root=workspace,
        preparation_receipt_id=prepared["receipt_id"],
    )
    assert rollforward["preparation_receipt_id"] == prepared["receipt_id"]
    assert marker.is_file()
    assert not receipt_path.exists()


def test_round14_writer_launch_is_strictly_generation_bound_and_fail_closed(
    tmp_path: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import sys

    from services.orchestrator import file_orchestration_migration as migration_module
    from services.orchestrator.file_orchestration_journal import (
        FileOrchestrationJournalError,
        FileOrchestrationJournalRepository,
    )
    from services.orchestrator.file_orchestration_migration import (
        launch_file_journal_rollback_writer,
        prepare_file_journal_rollback,
    )

    root = tmp_path / "journal"
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    repository = FileOrchestrationJournalRepository(root)
    assert repository.query_inflight_jobs() == []
    checkout_a, generation_a = _round14_clean_writer_checkout(
        tmp_path / "writer-a",
        content="writer A\n",
    )
    checkout_b, _generation_b = _round14_clean_writer_checkout(
        tmp_path / "writer-b",
        content="writer B\n",
    )
    receipt = prepare_file_journal_rollback(
        journal_root=root,
        workspace_root=workspace,
        scheduler_state="stopped",
        active_scheduler_processes=0,
        checked_at=datetime.now(UTC),
        checked_by="round14-operator",
        target_writer_generation=generation_a,
    )
    starts: list[tuple[tuple[str, ...], Any]] = []

    def runner(argv: tuple[str, ...], *, cwd: Any, check: bool) -> Any:
        assert check is False
        starts.append((argv, cwd))
        return type("Completed", (), {"returncode": 0})()

    monkeypatch.setattr(migration_module, "_run_rollback_writer", runner)

    launched = launch_file_journal_rollback_writer(
        journal_root=root,
        workspace_root=workspace,
        receipt_id=receipt["receipt_id"],
        writer_repository_root=checkout_a,
        writer_args=("plan-production", "--plan"),
    )
    assert launched["writer_exit_code"] == 0
    assert launched["actual_writer_generation"] == generation_a
    assert launched["writer_repository_root"] == str(checkout_a.resolve())
    assert len(starts) == 1
    command, cwd = starts[0]
    assert command == (
        sys.executable,
        "-m",
        "services.orchestrator.cli",
        "plan-production",
        "--plan",
    )
    assert cwd == checkout_a.resolve()

    real_resolver = migration_module._resolve_clean_writer_generation
    resolution_calls = 0

    def generation_changes_after_gate(repository_root: Any) -> tuple[Any, str]:
        nonlocal resolution_calls
        resolution_calls += 1
        resolved_root, resolved_generation = real_resolver(repository_root)
        if resolution_calls == 2:
            return resolved_root, "f" * len(resolved_generation)
        return resolved_root, resolved_generation

    monkeypatch.setattr(
        migration_module,
        "_resolve_clean_writer_generation",
        generation_changes_after_gate,
    )
    with pytest.raises(
        FileOrchestrationJournalError,
        match="file_journal_rollback_writer_generation_changed",
    ):
        launch_file_journal_rollback_writer(
            journal_root=root,
            workspace_root=workspace,
            receipt_id=receipt["receipt_id"],
            writer_repository_root=checkout_a,
            writer_args=("plan-production", "--plan"),
        )
    assert len(starts) == 1
    monkeypatch.setattr(
        migration_module,
        "_resolve_clean_writer_generation",
        real_resolver,
    )

    with pytest.raises(FileOrchestrationJournalError, match="file_journal_rollback_not_prepared"):
        launch_file_journal_rollback_writer(
            journal_root=root,
            workspace_root=workspace,
            receipt_id=receipt["receipt_id"],
            writer_repository_root=checkout_b,
            writer_args=("plan-production", "--plan"),
        )
    assert len(starts) == 1

    (checkout_a / "writer.txt").write_text("writer A dirty\n", encoding="utf-8")
    with pytest.raises(
        FileOrchestrationJournalError,
        match="file_journal_rollback_writer_generation_dirty",
    ):
        launch_file_journal_rollback_writer(
            journal_root=root,
            workspace_root=workspace,
            receipt_id=receipt["receipt_id"],
            writer_repository_root=checkout_a,
            writer_args=("plan-production", "--plan"),
        )
    (checkout_a / "writer.txt").write_text("writer A\n", encoding="utf-8")
    (checkout_a / "untracked.txt").write_text("untracked\n", encoding="utf-8")
    with pytest.raises(
        FileOrchestrationJournalError,
        match="file_journal_rollback_writer_generation_dirty",
    ):
        launch_file_journal_rollback_writer(
            journal_root=root,
            workspace_root=workspace,
            receipt_id=receipt["receipt_id"],
            writer_repository_root=checkout_a,
            writer_args=("plan-production", "--plan"),
        )
    unresolvable = tmp_path / "not-a-repository"
    unresolvable.mkdir()
    for repository_root in (unresolvable, tmp_path / "missing-repository"):
        with pytest.raises(FileOrchestrationJournalError):
            launch_file_journal_rollback_writer(
                journal_root=root,
                workspace_root=workspace,
                receipt_id=receipt["receipt_id"],
                writer_repository_root=repository_root,
                writer_args=("plan-production", "--plan"),
            )
    assert len(starts) == 1


def test_round14_writer_generation_git_probe_timeout_fails_closed(
    tmp_path: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import subprocess

    from services.orchestrator import file_orchestration_migration as migration_module
    from services.orchestrator.file_orchestration_journal import FileOrchestrationJournalError

    repository_root = tmp_path / "writer"
    repository_root.mkdir()
    observed: list[dict[str, Any]] = []

    def timeout(*_args: Any, **kwargs: Any) -> Any:
        observed.append(kwargs)
        raise subprocess.TimeoutExpired(cmd="git", timeout=kwargs["timeout"])

    monkeypatch.setattr(migration_module.subprocess, "run", timeout)
    with pytest.raises(
        FileOrchestrationJournalError,
        match="file_journal_rollback_writer_generation_unresolvable",
    ):
        migration_module._resolve_clean_writer_generation(repository_root)

    assert observed == [
        {
            "cwd": repository_root.resolve(),
            "check": False,
            "capture_output": True,
            "shell": False,
            "text": False,
            "timeout": migration_module.ROLLBACK_WRITER_GIT_TIMEOUT_SECONDS,
        }
    ]


@pytest.mark.parametrize("entrypoint", ["click", "argparse"])
def test_round12_rollback_commands_emit_verifiable_receipts(
    tmp_path: Any,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
    entrypoint: str,
) -> None:
    import inspect

    from services.orchestrator import cli as cli_module
    from services.orchestrator import file_orchestration_migration as migration_module
    from services.orchestrator.file_orchestration_journal import (
        FileOrchestrationJournalError,
        FileOrchestrationJournalRepository,
    )
    from services.orchestrator.file_orchestration_migration import (
        require_file_journal_rollback_prepared,
    )

    root = tmp_path / entrypoint / "journal"
    workspace = tmp_path / entrypoint / "workspace"
    workspace.mkdir(parents=True)
    repository = FileOrchestrationJournalRepository(root)
    assert repository.query_inflight_jobs() == []
    assert "actual_writer_generation" not in inspect.signature(
        cli_module._launch_file_journal_rollback_writer
    ).parameters
    checkout_a, generation_a = _round14_clean_writer_checkout(
        tmp_path / entrypoint / "writer-a",
        content="writer A\n",
    )
    checkout_b, _generation_b = _round14_clean_writer_checkout(
        tmp_path / entrypoint / "writer-b",
        content="writer B\n",
    )

    argv = [
        "prepare-file-journal-rollback",
        "--journal-root",
        str(root),
        "--workspace-root",
        str(workspace),
        "--scheduler-state",
        "stopped",
        "--active-scheduler-processes",
        "0",
        "--checked-at",
        "2026-07-23T12:00:00Z",
        "--checked-by",
        "node-22-operator",
        "--target-writer-generation",
        generation_a,
    ]
    result = (
        cli_module._click_main(argv)
        if entrypoint == "click"
        else cli_module._argparse_main(argv)
    )

    assert result == 0
    receipt = json.loads(capsys.readouterr().out)
    assert receipt["status"] == "prepared"
    assert require_file_journal_rollback_prepared(
        journal_root=root,
        workspace_root=workspace,
        receipt_id=receipt["receipt_id"],
        actual_writer_generation=generation_a,
    )["receipt_id"] == receipt["receipt_id"]

    started: list[tuple[tuple[str, ...], Any]] = []

    def run_writer(argv: tuple[str, ...], *, cwd: Any, check: bool) -> Any:
        assert check is False
        started.append((argv, cwd))
        return type("Completed", (), {"returncode": 0})()

    monkeypatch.setattr(migration_module, "_run_rollback_writer", run_writer)
    launch_argv = [
        "launch-file-journal-rollback-writer",
        "--journal-root",
        str(root),
        "--workspace-root",
        str(workspace),
        "--receipt-id",
        receipt["receipt_id"],
        "--writer-repository-root",
        str(checkout_a),
        "--",
        "plan-production",
        "--plan",
    ]
    result = (
        cli_module._click_main(launch_argv)
        if entrypoint == "click"
        else cli_module._argparse_main(launch_argv)
    )
    assert result == 0
    launch = json.loads(capsys.readouterr().out)
    assert launch["actual_writer_generation"] == generation_a
    assert launch["writer_repository_root"] == str(checkout_a.resolve())
    assert len(started) == 1
    assert started[0][1] == checkout_a.resolve()

    mismatch_argv = list(launch_argv)
    mismatch_argv[mismatch_argv.index(str(checkout_a))] = str(checkout_b)
    if entrypoint == "click":
        with pytest.raises(SystemExit) as error:
            cli_module._click_main(mismatch_argv)
        assert error.value.code == 2
    else:
        assert cli_module._argparse_main(mismatch_argv) == 2
    capsys.readouterr()
    assert len(started) == 1

    rollforward_argv = [
        "complete-file-journal-rollforward",
        "--journal-root",
        str(root),
        "--workspace-root",
        str(workspace),
        "--preparation-receipt-id",
        receipt["receipt_id"],
    ]
    result = (
        cli_module._click_main(rollforward_argv)
        if entrypoint == "click"
        else cli_module._argparse_main(rollforward_argv)
    )
    assert result == 0
    rollforward = json.loads(capsys.readouterr().out)
    assert rollforward["preparation_receipt_id"] == receipt["receipt_id"]
    with pytest.raises(FileOrchestrationJournalError, match="file_journal_rollback_not_prepared"):
        require_file_journal_rollback_prepared(
            journal_root=root,
            workspace_root=workspace,
            receipt_id=receipt["receipt_id"],
            actual_writer_generation=generation_a,
        )


def test_round12_tampered_and_wrong_root_receipts_fail_closed(tmp_path: Any) -> None:
    from services.orchestrator.file_orchestration_journal import (
        FileOrchestrationJournalError,
        FileOrchestrationJournalRepository,
    )
    from services.orchestrator.file_orchestration_migration import (
        complete_file_journal_rollforward,
        prepare_file_journal_rollback,
        require_file_journal_rollback_prepared,
    )

    root = tmp_path / "source" / "journal"
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    repository = FileOrchestrationJournalRepository(root)
    assert repository.query_inflight_jobs() == []
    receipt = prepare_file_journal_rollback(
        journal_root=root,
        workspace_root=workspace,
        scheduler_state="stopped",
        active_scheduler_processes=0,
        checked_at=datetime.now(UTC),
        checked_by="round12-test-operator",
        target_writer_generation="pre-reconcile-inventory",
    )
    receipt_path = root / "reconcile-inventory-rollback-preparation-v2.json"
    original = receipt_path.read_bytes()
    malformed = json.loads(original)
    malformed["receipt_id"] = "0" * 64
    receipt_path.write_text(json.dumps(malformed), encoding="utf-8")
    with pytest.raises(FileOrchestrationJournalError, match="file_journal_rollback_receipt_invalid"):
        require_file_journal_rollback_prepared(
            journal_root=root,
            workspace_root=workspace,
            receipt_id=receipt["receipt_id"],
            actual_writer_generation="pre-reconcile-inventory",
        )
    with pytest.raises(FileOrchestrationJournalError, match="file_journal_rollback_receipt_invalid"):
        complete_file_journal_rollforward(
            journal_root=root,
            workspace_root=workspace,
            preparation_receipt_id=receipt["receipt_id"],
        )
    receipt_path.write_bytes(original)
    with pytest.raises(FileOrchestrationJournalError, match="file_journal_rollforward_not_prepared"):
        complete_file_journal_rollforward(
            journal_root=root,
            workspace_root=workspace,
            preparation_receipt_id="f" * 64,
        )

    wrong_root = tmp_path / "wrong" / "journal"
    wrong_repository = FileOrchestrationJournalRepository(wrong_root)
    assert wrong_repository.query_inflight_jobs() == []
    (wrong_root / "reconcile-inventory-migration-v1.json").unlink()
    wrong_receipt_path = wrong_root / receipt_path.name
    wrong_receipt_path.write_bytes(original)
    with pytest.raises(FileOrchestrationJournalError, match="file_journal_rollback_receipt_wrong_root"):
        require_file_journal_rollback_prepared(
            journal_root=wrong_root,
            workspace_root=workspace,
            receipt_id=receipt["receipt_id"],
            actual_writer_generation="pre-reconcile-inventory",
        )
    with pytest.raises(FileOrchestrationJournalError, match="file_journal_rollback_receipt_wrong_root"):
        complete_file_journal_rollforward(
            journal_root=wrong_root,
            workspace_root=workspace,
            preparation_receipt_id=receipt["receipt_id"],
        )


def test_round8_legacy_active_migration_retains_oldest_across_513_rows(
    tmp_path: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from services.orchestrator import file_orchestration_journal as journal_module
    from services.orchestrator.reservation import slurm_comment_for

    repository = _file_cohort_repository(
        tmp_path / "template",
        member_count=1,
        with_runtime_rows=False,
        versioned=False,
    )
    template_job_id = "job_cycle_gfs_2026071200_forecast_fixture_forecast"
    template = json.loads(
        (repository.root / "pipeline-jobs" / f"{template_job_id}.json").read_text(encoding="utf-8")
    )
    target_root = tmp_path / "target" / "journal"
    active = target_root / "active-reconcile"
    active.mkdir(parents=True)
    expected_ids: list[str] = []
    for index in range(513):
        job_id = f"job_legacy_active_{index:04d}"
        run_id = f"cycle_gfs_2026071200_legacy_active_{index:04d}"
        idempotency_key = f"{run_id}:forecast"
        created_at = datetime(2025, 1, 1, tzinfo=UTC) + timedelta(seconds=index)
        created = created_at.isoformat().replace("+00:00", "Z")
        record = copy.deepcopy(template)
        record.update({"job_id": job_id, "run_id": run_id})
        record["payload"].update(
            {
                "job_id": job_id,
                "run_id": run_id,
                "idempotency_key": idempotency_key,
                "slurm_comment": slurm_comment_for(idempotency_key),
                "created_at": created,
                "updated_at": created,
            }
        )
        (active / f"{job_id}.json").write_text(json.dumps(record, sort_keys=True), encoding="utf-8")
        expected_ids.append(job_id)

    reopened = type(repository)(target_root)
    recovered = reopened.query_reserved_unbound_jobs()
    assert len(recovered) == 513
    assert recovered[0].job_id == expected_ids[0]
    assert {job.job_id for job in recovered} == set(expected_ids)
    assert len(tuple((target_root / "reconcile-inventory").glob("*.json"))) == 513

    # A fresh steady-state process must trust the completed migration marker
    # for discovery and enumerate only the bounded inventory. Exact authority
    # reads may still validate an anchor, but no legacy/direct/journal history
    # walker is allowed to restart the one-time migration.
    steady = type(repository)(target_root)
    history_discovery_calls: list[str] = []

    def history_discovery_forbidden(*_args: Any, **_kwargs: Any) -> Any:
        history_discovery_calls.append("called")
        raise AssertionError("steady reopen must not enumerate migration history")

    monkeypatch.setattr(steady, "_iter_legacy_active_reconcile_records", history_discovery_forbidden)
    monkeypatch.setattr(steady, "_iter_reconcile_direct_pipeline_job_records", history_discovery_forbidden)
    monkeypatch.setattr(journal_module, "_iter_jsonl_files", history_discovery_forbidden)
    steady_recovered = steady.query_reserved_unbound_jobs()

    assert history_discovery_calls == []
    assert len(steady_recovered) == 513
    assert steady_recovered[0].job_id == expected_ids[0]
    assert {job.job_id for job in steady_recovered} == set(expected_ids)


def test_current_master_public_compatibility_mutations_are_zero_write_rejected(
    tmp_path: Any,
) -> None:
    from services.orchestrator.accepted_submit_identity import AcceptedSubmitTransition
    from services.orchestrator.file_orchestration_journal import (
        FileOrchestrationJournalError,
        FileOrchestrationJournalRepository,
    )

    repository = _file_cohort_repository(tmp_path / "existing", member_count=1)
    job_id = "job_cycle_gfs_2026071200_forecast_fixture_forecast"
    key = "cycle_gfs_2026071200_forecast_fixture:forecast"
    before = repository.get_pipeline_job(job_id)

    def journal_bytes() -> bytes:
        return b"".join(
            path.read_bytes()
            for path in sorted(repository.root.glob("journal/**/*.jsonl"))
        )

    before_journal = journal_bytes()
    forbidden_calls = (
        lambda: repository.bind_pipeline_job_reservation(key, slurm_job_id="99001"),
        lambda: repository.bind_reservation(key, slurm_job_id="99001"),
        lambda: repository.transition_pipeline_job_submit_evidence(
            job_id,
            AcceptedSubmitTransition.timeout(),
        ),
        lambda: repository.permit_pipeline_job_retry(job_id),
        lambda: repository.record_pipeline_job_reconciliation(
            job_id,
            status="running",
        ),
        lambda: repository.update_pipeline_job_status(job_id, "running"),
        lambda: repository.update_job_status(job_id, "running"),
        lambda: repository.upsert_pipeline_job({**before, "status": "running"}),
    )
    for call in forbidden_calls:
        with pytest.raises(FileOrchestrationJournalError):
            call()
        assert repository.get_pipeline_job(job_id) == before
        assert journal_bytes() == before_journal

    for method_name in ("reserve_pipeline_job", "upsert_pipeline_job", "append_historical_pipeline_job"):
        empty = FileOrchestrationJournalRepository(tmp_path / method_name / "journal")
        method = getattr(empty, method_name)
        with pytest.raises(FileOrchestrationJournalError):
            method(dict(before))
        assert empty.get_pipeline_job(job_id) is None
        assert not tuple(empty.root.glob("journal/**/*.jsonl"))


@pytest.mark.parametrize(
    "transition_factory",
    [
        pytest.param(lambda transition: transition.begin_attempt(), id="begin_attempt"),
        pytest.param(lambda transition: transition.accepted(status="submitted"), id="accepted"),
        pytest.param(lambda transition: transition.rejected(), id="rejected"),
        pytest.param(
            lambda transition: transition.accounting(
                "matched_bound",
                submit_outcome="accepted",
                matched_slurm_job_id="88001",
                status="submitted",
            ),
            id="matched_bound",
        ),
        pytest.param(
            lambda transition: transition.accounting(
                "absence_retry_permitted",
                submit_outcome="submit_result_ambiguous",
                status="reservation_lost",
            ),
            id="absence_retry_permitted",
        ),
    ],
)
def test_round8_generic_versioned_submit_api_rejects_dedicated_authority_transitions_zero_write(
    tmp_path: Any,
    transition_factory: Any,
) -> None:
    from services.orchestrator.accepted_submit_identity import (
        ACCEPTED_SUBMIT_CONTRACT_VERSION,
        AcceptedSubmitTransition,
    )
    from services.orchestrator.file_orchestration_journal import FileOrchestrationJournalError

    repository = _file_cohort_repository(tmp_path, member_count=1)
    job_id = "job_cycle_gfs_2026071200_forecast_fixture_forecast"
    before = {
        str(path.relative_to(repository.root)): path.read_bytes()
        for path in repository.root.rglob("*")
        if path.is_file()
    }
    with pytest.raises(FileOrchestrationJournalError) as error:
        repository.transition_pipeline_job_submit_evidence(
            job_id,
            transition_factory(AcceptedSubmitTransition),
            accepted_submit_contract_version=ACCEPTED_SUBMIT_CONTRACT_VERSION,
            expected_submission_attempt=1,
            expected_statuses=("reserved",),
            require_unbound=True,
        )
    assert error.value.field == "transition"
    assert {
        str(path.relative_to(repository.root)): path.read_bytes()
        for path in repository.root.rglob("*")
        if path.is_file()
    } == before


@pytest.mark.parametrize(
    "missing_cas",
    ["attempt", "statuses", "unbound"],
)
def test_round8_generic_versioned_nonbinding_transition_requires_complete_cas(
    tmp_path: Any,
    missing_cas: str,
) -> None:
    from services.orchestrator.accepted_submit_identity import (
        ACCEPTED_SUBMIT_CONTRACT_VERSION,
        AcceptedSubmitTransition,
    )
    from services.orchestrator.file_orchestration_journal import FileOrchestrationJournalError

    repository = _file_cohort_repository(tmp_path, member_count=1)
    kwargs = {
        "accepted_submit_contract_version": ACCEPTED_SUBMIT_CONTRACT_VERSION,
        "expected_submission_attempt": 1,
        "expected_statuses": ("reserved",),
        "require_unbound": True,
    }
    if missing_cas == "attempt":
        kwargs["expected_submission_attempt"] = None
    elif missing_cas == "statuses":
        kwargs["expected_statuses"] = None
    else:
        kwargs["require_unbound"] = False
    with pytest.raises(FileOrchestrationJournalError, match="requires_cas"):
        repository.transition_pipeline_job_submit_evidence(
            "job_cycle_gfs_2026071200_forecast_fixture_forecast",
            AcceptedSubmitTransition.accounting(
                "absence_deferred",
                submit_outcome="submit_result_ambiguous",
                status="reserved",
            ),
            **kwargs,
        )


@pytest.mark.parametrize(
    ("current_status", "target_status", "expected_outcome"),
    [
        ("submitted", "pending", "applied"),
        ("submitted", "running", "applied"),
        ("pending", "running", "applied"),
        ("queued", "running", "applied"),
        ("running", "reconcile_unverified", "applied"),
        ("running", "pending", "stale"),
        ("reconcile_unverified", "running", "stale"),
        ("cancellation_pending", "running", "stale"),
        ("submitted", "invented", "stale"),
    ],
)
def test_round8_runtime_transition_graph_is_closed_monotonic_and_zero_writes_stale_edges(
    tmp_path: Any,
    current_status: str,
    target_status: str,
    expected_outcome: str,
) -> None:
    repository = _file_cohort_repository(tmp_path, member_count=1)
    key = "cycle_gfs_2026071200_forecast_fixture:forecast"
    job_id = "job_cycle_gfs_2026071200_forecast_fixture_forecast"
    commit_status = (
        current_status
        if current_status in {"submitted", "pending", "queued", "running"}
        else "submitted"
    )
    _bind_current_file_cohort(repository, key, slurm_job_id="88101", status=commit_status)
    if current_status == "reconcile_unverified":
        assert repository.transition_pipeline_job_runtime_status(
            job_id,
            "reconcile_unverified",
            expected_statuses=("submitted",),
        ).committed
    elif current_status == "cancellation_pending":
        assert repository.request_pipeline_job_cancellation(
            job_id,
            expected_statuses=("submitted",),
            reason="test",
        ).committed
    before = {
        str(path.relative_to(repository.root)): path.read_bytes()
        for path in repository.root.rglob("*")
        if path.is_file()
    }
    result = repository.transition_pipeline_job_runtime_status(
        job_id,
        target_status,
        expected_statuses=(current_status,),
    )
    assert result.outcome == expected_outcome
    if expected_outcome == "stale":
        assert {
            str(path.relative_to(repository.root)): path.read_bytes()
            for path in repository.root.rglob("*")
            if path.is_file()
        } == before


def test_round8_runtime_and_cancel_transitions_require_accepted_real_bound_master(
    tmp_path: Any,
) -> None:
    repository = _file_cohort_repository(tmp_path, member_count=1)
    job_id = "job_cycle_gfs_2026071200_forecast_fixture_forecast"
    before = repository.get_pipeline_job(job_id)
    runtime = repository.transition_pipeline_job_runtime_status(job_id, "running")
    cancel = repository.request_pipeline_job_cancellation(
        job_id,
        expected_statuses=("reserved",),
        reason="operator",
    )
    assert (runtime.outcome, cancel.outcome) == ("stale", "stale")
    assert repository.get_pipeline_job(job_id) == before


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("retry_count", 1),
        ("manual_retry_marker", True),
        ("previous_job_id", "previous-master"),
        ("candidate_projections", "valid_projection"),
        ("reconciliation_reason_class", "process_unavailable"),
    ],
)
def test_round8_clean_reservation_guard_rejects_each_retry_and_authority_field_zero_write(
    tmp_path: Any,
    field: str,
    value: Any,
) -> None:
    from services.orchestrator.file_orchestration_journal import (
        FileOrchestrationJournalError,
        FileOrchestrationJournalRepository,
    )

    template = _file_cohort_repository(tmp_path / "template", member_count=1)
    job_id = "job_cycle_gfs_2026071200_forecast_fixture_forecast"
    clean = dict(template.get_pipeline_job(job_id))
    clean.update(
        {
            "status": "reserved",
            "slurm_job_id": None,
            "submit_outcome": None,
            "reconciliation_source": None,
            "reconciliation_decision": None,
            "reconciliation_reason_class": None,
            "matched_slurm_job_id": None,
            "candidate_projections": [],
            "retry_count": 0,
            "manual_retry_marker": False,
            "previous_job_id": None,
        }
    )
    if field == "candidate_projections":
        clean[field] = [
            {
                **{
                    key: clean["cohort_members"][0][key]
                    for key in ("array_task_id", "candidate_id", "run_id", "model_id")
                },
                "array_task_outcome": "failed",
                "restart_stage": "forecast",
                "native_shud_resubmitted": False,
            }
        ]
    else:
        clean[field] = value
    repository = FileOrchestrationJournalRepository(tmp_path / field / "journal")
    with pytest.raises(FileOrchestrationJournalError) as error:
        repository.reserve_pipeline_job(clean)
    assert error.value.field == field
    assert repository.get_pipeline_job(job_id) is None
    assert not tuple(repository.root.glob("journal/**/*.jsonl"))
    assert not tuple(repository.root.glob("pipeline-jobs/*.json"))
    assert not tuple(repository.root.glob("reconcile-inventory/*.json"))


def test_round8_cancellation_intent_is_sticky_and_completion_is_idempotent_after_reopen(
    tmp_path: Any,
) -> None:
    repository = _file_cohort_repository(tmp_path, member_count=1)
    key = "cycle_gfs_2026071200_forecast_fixture:forecast"
    job_id = "job_cycle_gfs_2026071200_forecast_fixture_forecast"
    _bind_current_file_cohort(repository, key, slurm_job_id="88102", status="running")
    intent = repository.request_pipeline_job_cancellation(
        job_id,
        expected_statuses=("running",),
        reason="operator",
    )
    assert intent.outcome == "applied"
    reopened = type(repository)(repository.root)
    stale_poll = reopened.transition_pipeline_job_runtime_status(
        job_id,
        "running",
        expected_statuses=("cancellation_pending",),
    )
    assert stale_poll.outcome == "stale"
    finished_at = datetime(2026, 7, 12, 0, 3, tzinfo=UTC)
    first = reopened.complete_pipeline_job_cancellation(
        job_id,
        finished_at=finished_at,
        exit_code=0,
        error_code=None,
        error_message=None,
        log_uri=None,
    )
    second = type(repository)(repository.root).complete_pipeline_job_cancellation(
        job_id,
        finished_at=finished_at,
        exit_code=0,
        error_code=None,
        error_message=None,
        log_uri=None,
    )
    assert (first.outcome, second.outcome) == ("applied", "idempotent")


def test_marker_free_master_compatibility_mutations_remain_available(tmp_path: Any) -> None:
    repository = _file_cohort_repository(
        tmp_path,
        member_count=1,
        versioned=False,
        submit_outcome=None,
    )
    key = "cycle_gfs_2026071200_forecast_fixture:forecast"
    job_id = "job_cycle_gfs_2026071200_forecast_fixture_forecast"

    bound = repository.bind_pipeline_job_reservation(key, slurm_job_id="99002")
    assert bound is not None
    previous, updated = repository.update_pipeline_job_status(job_id, "running")
    assert previous == "submitted"
    assert updated["status"] == "running"


@pytest.mark.parametrize(
    ("master_status", "task_outcomes"),
    [
        ("failed", ("failed", "failed")),
        ("partially_failed", ("succeeded", "failed")),
    ],
)
def test_current_master_retry_never_persists_marker_free_clone(
    tmp_path: Any,
    master_status: str,
    task_outcomes: tuple[str, str],
) -> None:
    from types import SimpleNamespace

    from services.orchestrator.file_orchestration_journal import (
        FileJournalRetryService,
        _next_current_master_retry_identity,
    )

    repository = _file_cohort_repository(tmp_path, member_count=2)
    key = "cycle_gfs_2026071200_forecast_fixture:forecast"
    job_id = "job_cycle_gfs_2026071200_forecast_fixture_forecast"
    _bind_current_file_cohort(repository, key, slurm_job_id="99003")
    members = repository.get_pipeline_job(job_id)["cohort_members"]
    repository.project_forecast_cohort_tasks(
        job_id,
        master_slurm_job_id="99003",
        projections=[
            {
                **member,
                "array_task_outcome": task_outcomes[index],
                "task_slurm_job_id": f"99003_{index}",
                "restart_stage": "state_save_qc",
                "native_shud_resubmitted": False,
                "error_code": "SLURM_TIMEOUT" if task_outcomes[index] == "failed" else None,
            }
            for index, member in enumerate(members)
        ],
        complete=True,
        master_status=master_status,
        master_error_code="SLURM_TIMEOUT",
        reconciliation_decision="matched_bound",
    )
    failed = repository.get_pipeline_job(job_id)
    failed["error_code"] = "SLURM_TIMEOUT"

    pending = FileJournalRetryService(repository).handle_failed_job(SimpleNamespace(**failed))

    assert pending.job_id == f"{job_id}_retry_1"
    assert pending.status == "pending"
    assert _next_current_master_retry_identity(
        {"job_id": pending.job_id, "retry_count": 0}
    ) == (f"{job_id}_retry_2", 2)
    assert repository.get_pipeline_job(pending.job_id) is None
    durable_rows = repository.query_pipeline_jobs_by_cycle("gfs_2026071200")
    assert all(
        row.get("accepted_submit_contract_version") is not None
        for row in durable_rows
        if row.get("job_id", "").startswith("job_cycle_gfs_2026071200_forecast_fixture")
    )


def test_cycle_sync_uses_typed_runtime_transition_and_defers_terminal_master_truth(
    tmp_path: Any,
) -> None:
    from services.orchestrator.chain_forecast_control import sync_cycle_statuses

    repository = _file_cohort_repository(tmp_path, member_count=1)
    key = "cycle_gfs_2026071200_forecast_fixture:forecast"
    job_id = "job_cycle_gfs_2026071200_forecast_fixture_forecast"
    _bind_current_file_cohort(repository, key, slurm_job_id="99004")

    class Client:
        status = "running"

        def get_job_status(self, _slurm_job_id: str) -> dict[str, Any]:
            return {"status": self.status, "started_at": "2026-07-12T00:01:00Z", "exit_code": 0}

    class Harness:
        def __init__(self) -> None:
            self.repository = repository
            self.slurm_client = Client()

        def _query_pipeline_jobs_by_cycle(self, cycle_id: str) -> list[dict[str, Any]]:
            return self.repository.query_pipeline_jobs_by_cycle(cycle_id)

        def _display_log_publication_for_pipeline_job(self, _job: Any) -> None:
            return None

        def _try_publish_log_for_advertise(self, *_args: Any) -> None:
            return None

        def _raise_publish_error_after_durable_update(self, attempt: Any) -> None:
            assert attempt is None

    harness = Harness()
    updates = sync_cycle_statuses(harness, "gfs_2026071200")
    assert [row["status"] for row in updates] == ["running"]
    assert repository.get_pipeline_job(job_id)["status"] == "running"

    harness.slurm_client.status = "succeeded"
    assert sync_cycle_statuses(harness, "gfs_2026071200") == []
    assert repository.get_pipeline_job(job_id)["status"] == "running"
    assert [job.job_id for job in type(repository)(repository.root).query_inflight_jobs()] == [job_id]


@pytest.mark.parametrize("gateway_fails", [False, True])
def test_cycle_cancel_persists_typed_intent_before_gateway_and_reopens_safely(
    tmp_path: Any,
    gateway_fails: bool,
) -> None:
    from services.orchestrator.chain import SlurmClientError
    from services.orchestrator.chain_forecast_control import cancel_active_cycle_jobs

    repository = _file_cohort_repository(tmp_path, member_count=1)
    key = "cycle_gfs_2026071200_forecast_fixture:forecast"
    job_id = "job_cycle_gfs_2026071200_forecast_fixture_forecast"
    _bind_current_file_cohort(repository, key, slurm_job_id="99005")
    repository.transition_pipeline_job_runtime_status(
        job_id,
        "running",
        expected_statuses=("submitted",),
    )
    observed_statuses: list[str] = []

    class Client:
        def cancel_job(self, _slurm_job_id: str) -> dict[str, Any]:
            observed_statuses.append(type(repository)(repository.root).get_pipeline_job(job_id)["status"])
            if gateway_fails:
                raise SlurmClientError("SLURM_GATEWAY_UNAVAILABLE", "cancel failed")
            return {
                "status": "cancelled",
                "finished_at": "2026-07-12T00:02:00Z",
                "exit_code": 0,
            }

    class Harness:
        def __init__(self) -> None:
            self.repository = repository
            self.slurm_client = Client()

        def _query_pipeline_jobs_by_cycle(self, cycle_id: str) -> list[dict[str, Any]]:
            return self.repository.query_pipeline_jobs_by_cycle(cycle_id)

    harness = Harness()
    if gateway_fails:
        with pytest.raises(SlurmClientError, match="cancel failed"):
            cancel_active_cycle_jobs(harness, "gfs_2026071200", reason="operator_requested")
        expected_status = "cancellation_pending"
    else:
        cancelled = cancel_active_cycle_jobs(
            harness,
            "gfs_2026071200",
            reason="operator_requested",
        )
        assert [row["status"] for row in cancelled] == ["reconcile_unverified"]
        expected_status = "reconcile_unverified"

    assert observed_statuses == ["cancellation_pending"]
    reopened = type(repository)(repository.root)
    assert reopened.get_pipeline_job(job_id)["status"] == expected_status
    assert [job.job_id for job in reopened.query_inflight_jobs()] == [job_id]


def test_file_submit_attempt_barrier_race_commits_only_one_slurm_id(tmp_path: Any) -> None:
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier

    from services.orchestrator.accepted_submit_identity import AcceptedSubmitTransition
    from services.orchestrator.file_orchestration_journal import FileOrchestrationJournalRepository

    repository = _file_cohort_repository(tmp_path, member_count=18)
    key = "cycle_gfs_2026071200_forecast_fixture:forecast"
    barrier = Barrier(2)

    def commit(slurm_job_id: str) -> str:
        contender = FileOrchestrationJournalRepository(repository.root)
        barrier.wait()
        return contender.commit_pipeline_job_submit_attempt(
            key,
            expected_submission_attempt=1,
            slurm_job_id=slurm_job_id,
            transition=AcceptedSubmitTransition.accepted(status="submitted"),
        ).outcome

    with ThreadPoolExecutor(max_workers=2) as pool:
        outcomes = tuple(pool.map(commit, ("17667", "17668")))

    assert sorted(outcomes) == ["applied", "collision"]
    reopened = FileOrchestrationJournalRepository(repository.root)
    row = reopened.get_pipeline_job("job_cycle_gfs_2026071200_forecast_fixture_forecast")
    assert row is not None
    assert row["slurm_job_id"] in {"17667", "17668"}
    assert len(reopened.query_inflight_jobs()) == 1
    assert reopened.query_reserved_unbound_jobs() == []


def test_comment_sacct_global_zero_is_unavailable_without_visibility_proof(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from services.orchestrator import reconcile as reconcile_module

    calls: list[list[str]] = []
    monkeypatch.setattr(
        reconcile_module,
        "_bounded_sacct_stdout",
        lambda command: calls.append(list(command)) or "",
    )
    query = reconcile_module.default_comment_sacct_querier(global_visibility_probe=lambda: False)

    with pytest.raises(reconcile_module.ReconcileQueryUnavailable, match="visibility is unproven"):
        query("key", accepted_submit_contract_version="nhms.accepted_submit.v1")
    assert calls == []


def test_comment_sacct_legacy_global_query_does_not_require_visibility_probe(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from services.orchestrator import reconcile as reconcile_module

    calls: list[list[str]] = []
    monkeypatch.setattr(
        reconcile_module,
        "_bounded_sacct_stdout",
        lambda command: calls.append(list(command)) or "",
    )
    query = reconcile_module.default_comment_sacct_querier(global_visibility_probe=lambda: False)

    assert tuple(query("legacy-key")) == ()
    assert calls
    assert all("--allusers" in command for command in calls)


@pytest.mark.parametrize("member_count", [1, 18, 256])
def test_rejected_submit_batch_write_failure_reopens_as_unbound_recoverable_reservation(
    tmp_path: Any,
    monkeypatch: pytest.MonkeyPatch,
    member_count: int,
) -> None:
    from services.orchestrator.chain_types import OrchestratorError
    from services.orchestrator.file_orchestration_journal import FileOrchestrationJournalRepository

    repository = _file_cohort_repository(
        tmp_path / str(member_count),
        member_count=member_count,
        submit_outcome=None,
    )
    for index in range(member_count):
        repository.update_hydro_run_status(
            f"fcst_gfs_2026071200_model_{index}",
            "created",
            error_code=None,
            error_message=None,
        )
    job_id = "job_cycle_gfs_2026071200_forecast_fixture_forecast"

    def fail_batch(**_kwargs: Any) -> None:
        raise OrchestratorError("FILE_JOURNAL_WRITE_FAILED", "injected batch failure")

    monkeypatch.setattr(repository, "_append_journal_records_unlocked", fail_batch)
    with pytest.raises(OrchestratorError, match="injected batch failure"):
        repository.reject_pipeline_job_submit_attempt(
            "cycle_gfs_2026071200_forecast_fixture:forecast",
            expected_submission_attempt=1,
            finished_at=datetime(2026, 7, 12, 0, 1, tzinfo=UTC),
            error_code="VALIDATION_ERROR",
            error_message="pre-submit rejected",
            stage="forecast",
            job_type="run_shud_forecast_array",
        )

    reopened = FileOrchestrationJournalRepository(repository.root)
    master = reopened.get_pipeline_job(job_id)
    assert master["status"] == "reserved"
    assert master["submit_outcome"] is None
    assert master["slurm_job_id"] is None
    assert len(reopened.query_reserved_unbound_jobs()) == 1
    assert all(
        (reopened._hydro_run_for(f"fcst_gfs_2026071200_model_{index}") or {})["status"] == "created"
        for index in range(member_count)
    )


def test_marker_free_historical_candidate_remains_readable_but_unversioned(tmp_path: Any) -> None:
    from services.orchestrator.accepted_submit_identity import accepted_submit_contract_is_current
    from services.orchestrator.file_orchestration_journal import FileOrchestrationJournalRepository

    repository = FileOrchestrationJournalRepository(tmp_path / "journal")
    legacy = {
        "job_id": "job_fcst_gfs_2026071200_model_legacy_forecast_candidate_0",
        "run_id": "fcst_gfs_2026071200_model_legacy",
        "cycle_id": "gfs_2026071200",
        "job_type": "run_shud_forecast_array",
        "slurm_job_id": "17667_0",
        "array_task_id": 0,
        "model_id": "model_legacy",
        "status": "succeeded",
        "stage": "forecast",
        "candidate_id": "gfs:2026-07-12T00:00:00Z:model_legacy:forecast_gfs_deterministic",
        "submit_outcome": "historical_pre_1112_value",
        "restart_stage": "forecast",
    }
    repository.append_historical_pipeline_job(legacy)

    reopened = FileOrchestrationJournalRepository(repository.root)
    direct = reopened.get_pipeline_job(legacy["job_id"])
    queried = next(
        job for job in reopened.query_pipeline_jobs_by_cycle("gfs_2026071200")
        if job["job_id"] == legacy["job_id"]
    )
    latest = json.loads(
        (repository.root / "latest" / "gfs" / "2026071200" / "model_legacy.json").read_text()
    )
    latest_row = next(job for job in latest["pipeline_jobs"] if job["job_id"] == legacy["job_id"])
    assert direct == queried == latest_row
    assert accepted_submit_contract_is_current(direct) is False


def test_marker_free_historical_master_is_read_only_to_accepted_submit_reconcile(tmp_path: Any) -> None:
    from services.orchestrator.reconcile import reconcile_reserved_unbound_jobs

    repository = _file_cohort_repository(
        tmp_path,
        member_count=1,
        with_runtime_rows=False,
        submit_outcome=None,
        versioned=False,
    )
    before = repository.get_pipeline_job("job_cycle_gfs_2026071200_forecast_fixture_forecast")

    outcomes = reconcile_reserved_unbound_jobs(repository, comment_query=lambda _key: None)

    assert outcomes[0].action == "legacy_unversioned_read_only"
    assert repository.get_pipeline_job(before["job_id"]) == before


@pytest.mark.parametrize("version", ["nhms.accepted_submit.v2", None, 1])
def test_explicit_unknown_or_malformed_accepted_submit_version_fails_closed(
    tmp_path: Any,
    version: Any,
) -> None:
    from services.orchestrator.file_orchestration_journal import (
        FileOrchestrationJournalError,
        FileOrchestrationJournalRepository,
    )

    repository = FileOrchestrationJournalRepository(tmp_path / "journal")
    row = {
        "job_id": "job_fcst_gfs_2026071200_model_legacy_forecast_candidate_0",
        "run_id": "fcst_gfs_2026071200_model_legacy",
        "cycle_id": "gfs_2026071200",
        "job_type": "run_shud_forecast_array",
        "slurm_job_id": "17667_0",
        "array_task_id": 0,
        "model_id": "model_legacy",
        "status": "succeeded",
        "stage": "forecast",
        "candidate_id": "gfs:2026-07-12T00:00:00Z:model_legacy:forecast_gfs_deterministic",
        "submit_outcome": "accepted",
        "restart_stage": "forecast",
        "accepted_submit_contract_version": version,
    }

    with pytest.raises(FileOrchestrationJournalError) as error:
        repository.upsert_pipeline_job(row)
    assert error.value.field == "accepted_submit_contract_version"


@pytest.mark.parametrize(
    ("controller_private_data", "slurmdbd_private_data", "expected"),
    [
        ("none", "none", True),
        ("accounts,events", "users", True),
        ("jobs", "none", False),
        ("none", "all", False),
        (None, "none", False),
        ("none", None, False),
    ],
)
def test_global_accounting_visibility_probe_requires_controller_and_slurmdbd_private_data(
    monkeypatch: pytest.MonkeyPatch,
    controller_private_data: str | None,
    slurmdbd_private_data: str | None,
    expected: bool,
) -> None:
    from services.orchestrator import reconcile as reconcile_module

    commands: list[list[str]] = []

    def run(command: Any) -> str:
        commands.append(list(command))
        value = controller_private_data if str(command[0]).endswith("scontrol") else slurmdbd_private_data
        return f"PrivateData = {value}\n" if value is not None else ""

    monkeypatch.setattr(reconcile_module, "_bounded_visibility_stdout", run)
    assert reconcile_module.default_global_accounting_visibility_probe("/opt/slurm/bin")() is expected
    assert commands == [
        ["/opt/slurm/bin/scontrol", "show", "config"],
        ["/opt/slurm/bin/sacctmgr", "show", "config"],
    ]


def test_global_accounting_visibility_probe_fails_closed_but_checks_both_when_one_is_unavailable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from services.orchestrator import reconcile as reconcile_module

    commands: list[list[str]] = []

    def run(command: Any) -> str:
        commands.append(list(command))
        if str(command[0]).endswith("scontrol"):
            raise reconcile_module.ReconcileQueryUnavailable("controller config unavailable")
        return "PrivateData = none\n"

    monkeypatch.setattr(reconcile_module, "_bounded_visibility_stdout", run)
    assert reconcile_module.default_global_accounting_visibility_probe()() is False
    assert commands == [["scontrol", "show", "config"], ["sacctmgr", "show", "config"]]


@pytest.mark.parametrize("stream_fd", [1, 2])
def test_global_accounting_visibility_process_bounds_stdout_and_stderr(
    monkeypatch: pytest.MonkeyPatch,
    stream_fd: int,
) -> None:
    import sys

    from services.orchestrator import reconcile as reconcile_module

    monkeypatch.setattr(reconcile_module, "MAX_VISIBILITY_PROBE_BYTES", 64)
    command = [
        sys.executable,
        "-c",
        f"import os; os.write({stream_fd}, b'x' * 4096)",
    ]
    with pytest.raises(reconcile_module.ReconcileQuerySaturated) as error:
        reconcile_module._bounded_visibility_stdout(command)
    assert error.value.boundary == "bytes"


def test_global_accounting_visibility_process_timeout_is_bounded(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import sys

    from services.orchestrator import reconcile as reconcile_module

    monkeypatch.setattr(reconcile_module, "COMMENT_SACCT_VISIBILITY_TIMEOUT_SECONDS", 0.05)
    with pytest.raises(reconcile_module.ReconcileQueryUnavailable, match="timed out"):
        reconcile_module._bounded_visibility_stdout(
            [sys.executable, "-c", "import time; time.sleep(10)"],
        )


@pytest.mark.parametrize("boundary", ["bytes", "rows", "timeout"])
@pytest.mark.parametrize("stream_fd", [1, 2])
def test_round8_visibility_probe_saturation_and_timeout_reap_actual_child_pid(
    tmp_path: Any,
    monkeypatch: pytest.MonkeyPatch,
    boundary: str,
    stream_fd: int,
) -> None:
    import sys

    from services.orchestrator import reconcile as reconcile_module

    pid_path = tmp_path / f"{boundary}-{stream_fd}.pid"
    if boundary == "bytes":
        monkeypatch.setattr(reconcile_module, "MAX_VISIBILITY_PROBE_BYTES", 64)
        body = f"os.write({stream_fd}, b'x' * 4096)"
        expected_error = reconcile_module.ReconcileQuerySaturated
    elif boundary == "rows":
        monkeypatch.setattr(reconcile_module, "MAX_VISIBILITY_PROBE_BYTES", 1024 * 1024)
        monkeypatch.setattr(reconcile_module, "MAX_VISIBILITY_PROBE_ROWS", 2)
        body = f"os.write({stream_fd}, b'x\\n' * 64)"
        expected_error = reconcile_module.ReconcileQuerySaturated
    else:
        monkeypatch.setattr(reconcile_module, "COMMENT_SACCT_VISIBILITY_TIMEOUT_SECONDS", 0.05)
        body = "time.sleep(10)"
        expected_error = reconcile_module.ReconcileQueryUnavailable
    script = (
        "import os, pathlib, sys, time; "
        "pathlib.Path(sys.argv[1]).write_text(str(os.getpid())); "
        + body
    )
    with pytest.raises(expected_error) as error:
        reconcile_module._bounded_visibility_stdout([sys.executable, "-c", script, str(pid_path)])
    if boundary in {"bytes", "rows"}:
        assert error.value.boundary == boundary
    child_pid = int(pid_path.read_text(encoding="utf-8"))
    with pytest.raises(ProcessLookupError):
        os.kill(child_pid, 0)


@pytest.mark.parametrize(
    ("stdout", "expected"),
    [
        ("PrivateData = none\nPrivateData = none\n", True),
        ("PrivateData = none\nPrivateData = jobs\n", False),
        ("PrivateData = all\nPrivateData = none\n", False),
        ("unrelated = none\n", False),
    ],
)
def test_private_data_visibility_requires_every_occurrence_to_allow_jobs(
    stdout: str,
    expected: bool,
) -> None:
    from services.orchestrator import reconcile as reconcile_module

    assert reconcile_module._private_data_allows_global_jobs(stdout) is expected


def test_comment_sacct_production_cadence_pages_are_independently_bounded_and_cached(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from services.orchestrator import reconcile as reconcile_module

    page_count = (reconcile_module.COMMENT_SACCT_LOOKBACK_DAYS * 24) // reconcile_module.COMMENT_SACCT_PAGE_HOURS
    stages = ("forcing", "forecast", "state_save_qc")
    expected_ids: dict[str, str] = {}

    def page_rows(page_index: int) -> str:
        rows: list[str] = []
        for source_index, source in enumerate(("gfs", "ifs")):
            for stage_index, stage in enumerate(stages):
                key = f"{source}:day{page_index:02d}:{stage}"
                master_id = str(17000 + page_index * 10 + source_index * len(stages) + stage_index)
                expected_ids[key] = master_id
                for task_id in range(256):
                    rows.append(
                        f"{master_id}_{task_id}|nhms_{stage}|RUNNING|0:0|nhms_idem:{key}|scheduler|account\n"
                    )
                    rows.append(
                        f"{master_id}_{task_id}.batch|batch|RUNNING|0:0|nhms_idem:{key}|scheduler|account\n"
                    )
        assert len(rows) == 2 * 3 * 256 * 2
        return "".join(rows)

    commands: list[list[str]] = []
    scope_pages = {"owner": 0, "global": 0}

    def bounded(command: Any) -> str:
        commands.append(list(command))
        scope = "global" if "--allusers" in command else "owner"
        page_index = scope_pages[scope]
        scope_pages[scope] += 1
        return page_rows(page_index)

    monkeypatch.setattr(reconcile_module, "_bounded_sacct_stdout", bounded)
    query = reconcile_module.default_comment_sacct_querier(
        global_visibility_probe=lambda: True,
        now=lambda: datetime(2026, 7, 22, 12, tzinfo=UTC),
    )

    target = f"ifs:day{page_count - 1:02d}:state_save_qc"
    proof = reconcile_module._query_comment_accounting_proof(
        query,
        target,
        expected_user="scheduler",
        expected_account="account",
    )
    assert proof.kind == "owned_match"
    assert [record.slurm_job_id for record in proof.records] == [expected_ids[target]]
    assert scope_pages == {"owner": page_count, "global": page_count}
    assert len(commands) == page_count * 2

    cached_target = "gfs:day00:forcing"
    cached_proof = reconcile_module._query_comment_accounting_proof(
        query,
        cached_target,
        expected_user="scheduler",
        expected_account="account",
    )
    assert cached_proof.kind == "owned_match"
    assert [record.slurm_job_id for record in cached_proof.records] == [expected_ids[cached_target]]
    assert len(commands) == page_count * 2
    assert all(any(item.startswith("--endtime=") for item in command) for command in commands)


def test_comment_sacct_session_freezes_advancing_clock_window_for_all_keys_and_scopes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from services.orchestrator import reconcile as reconcile_module

    page_count = (reconcile_module.COMMENT_SACCT_LOOKBACK_DAYS * 24) // reconcile_module.COMMENT_SACCT_PAGE_HOURS
    base_now = datetime(2026, 7, 22, 12, tzinfo=UTC)
    now_calls: list[datetime] = []

    def advancing_now() -> datetime:
        value = base_now + timedelta(seconds=len(now_calls))
        now_calls.append(value)
        return value

    commands: list[list[str]] = []
    scope_pages = {"owner": 0, "global": 0}
    late_key = "gfs:late:forecast"
    early_key = "ifs:early:state_save_qc"

    def bounded(command: Any) -> str:
        commands.append(list(command))
        scope = "global" if "--allusers" in command else "owner"
        page_index = scope_pages[scope]
        scope_pages[scope] += 1
        if page_index == 0:
            return f"17668_0.batch|batch|RUNNING|0:0|nhms_idem:{early_key}|scheduler|account\n"
        if page_index == page_count - 1:
            return f"17667_0|nhms_forecast|RUNNING|0:0|nhms_idem:{late_key}|scheduler|account\n"
        return ""

    monkeypatch.setattr(reconcile_module, "_bounded_sacct_stdout", bounded)
    query = reconcile_module.default_comment_sacct_querier(
        global_visibility_probe=lambda: True,
        now=advancing_now,
    )

    late_proof = reconcile_module._query_comment_accounting_proof(
        query,
        late_key,
        expected_user="scheduler",
        expected_account="account",
    )
    assert late_proof.kind == "owned_match"
    assert [record.slurm_job_id for record in late_proof.records] == ["17667"]
    assert len(commands) == page_count * 2
    assert scope_pages == {"owner": page_count, "global": page_count}

    early_proof = reconcile_module._query_comment_accounting_proof(
        query,
        early_key,
        expected_user="scheduler",
        expected_account="account",
    )
    assert early_proof.kind == "owned_match"
    assert [record.slurm_job_id for record in early_proof.records] == ["17668"]
    assert len(commands) == page_count * 2
    assert now_calls == [base_now]
    assert "--endtime=2026-07-22T12:00:00" in commands[0]


def test_comment_sacct_global_collision_is_detected_across_separate_pages(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from services.orchestrator import reconcile as reconcile_module

    page_count = (reconcile_module.COMMENT_SACCT_LOOKBACK_DAYS * 24) // reconcile_module.COMMENT_SACCT_PAGE_HOURS
    scope_pages = {"owner": 0, "global": 0}
    target = "gfs:collision:forecast"

    def bounded(command: Any) -> str:
        scope = "global" if "--allusers" in command else "owner"
        page_index = scope_pages[scope]
        scope_pages[scope] += 1
        if page_index == 0:
            return f"17667_0.batch|batch|RUNNING|0:0|nhms_idem:{target}|scheduler|account\n"
        if scope == "global" and page_index == page_count - 1:
            return f"17668_0|nhms_forecast|RUNNING|0:0|nhms_idem:{target}|foreign|other\n"
        return ""

    monkeypatch.setattr(reconcile_module, "_bounded_sacct_stdout", bounded)
    proof = reconcile_module._query_comment_accounting_proof(
        reconcile_module.default_comment_sacct_querier(global_visibility_probe=lambda: True),
        target,
        expected_user="scheduler",
        expected_account="account",
    )

    assert proof.kind == "foreign_collision"
    assert [record.slurm_job_id for record in proof.records] == ["17667", "17668"]
    assert scope_pages == {"owner": page_count, "global": page_count}


@pytest.mark.parametrize("boundary", ["row", "byte"])
def test_comment_sacct_rejects_any_single_page_over_its_bound(
    monkeypatch: pytest.MonkeyPatch,
    boundary: str,
) -> None:
    from services.orchestrator import reconcile as reconcile_module

    monkeypatch.setattr(reconcile_module, "MAX_COMMENT_SACCT_ROWS", 2)
    monkeypatch.setattr(reconcile_module, "MAX_COMMENT_SACCT_BYTES", 8)
    payload = "\n\n\n" if boundary == "row" else "123456789"
    monkeypatch.setattr(reconcile_module, "_bounded_sacct_stdout", lambda _command: payload)

    with pytest.raises(reconcile_module.ReconcileQuerySaturated, match="bounded output") as error:
        reconcile_module.default_comment_sacct_querier(global_visibility_probe=lambda: True)("key")
    expected = "rows" if boundary == "row" else "bytes"
    assert error.value.boundary == expected
    assert error.value.reason_class == f"bounded_output_{expected}_saturated"


def test_bounded_sacct_rejects_max_newlines_plus_unterminated_row(
    tmp_path: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from services.orchestrator import reconcile as reconcile_module

    executable = tmp_path / "sacct"
    executable.write_text(
        "#!/bin/sh\ni=0\nwhile [ $i -lt 20000 ]; do printf '\\n'; i=$((i+1)); done\nprintf 'unterminated'\n",
        encoding="utf-8",
    )
    executable.chmod(0o755)
    monkeypatch.setattr(reconcile_module, "MAX_COMMENT_SACCT_ROWS", 20_000)

    with pytest.raises(reconcile_module.ReconcileQueryUnavailable, match="bounded output"):
        reconcile_module._bounded_sacct_stdout([str(executable)])


def test_comment_sacct_querier_proves_owner_candidate_against_global_scope(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from services.orchestrator import reconcile as reconcile_module

    commands: list[list[str]] = []

    def bounded(command: Any) -> str:
        commands.append(list(command))
        foreign = "".join(
            f"{17000 + index}|nhms_forecast|RUNNING|0:0|nhms_idem:key|foreign|other\n"
            for index in range(100)
        )
        owned = "17667|nhms_forecast|RUNNING|0:0|nhms_idem:key|scheduler|account\n"
        return owned + foreign if "--allusers" in command else foreign + owned

    monkeypatch.setattr(reconcile_module, "_bounded_sacct_stdout", bounded)
    proof = reconcile_module._query_comment_accounting_proof(
        reconcile_module.default_comment_sacct_querier(global_visibility_probe=lambda: True),
        "key",
        expected_user="scheduler",
        expected_account="account",
    )

    assert proof.kind == "foreign_collision"
    page_count = (reconcile_module.COMMENT_SACCT_LOOKBACK_DAYS * 24) // reconcile_module.COMMENT_SACCT_PAGE_HOURS
    assert len(commands) == page_count + 1
    assert "--user=scheduler" in commands[0]
    assert "--accounts=account" in commands[0]
    assert "--allusers" not in commands[0]
    assert "--allusers" in commands[page_count]


def test_comment_sacct_global_overlimit_after_owner_candidate_fails_closed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from services.orchestrator import reconcile as reconcile_module

    commands: list[list[str]] = []

    def bounded(command: Any) -> str:
        commands.append(list(command))
        if "--allusers" in command:
            raise reconcile_module.ReconcileQueryUnavailable("sacct query exceeded bounded output")
        return "17667|nhms_forecast|RUNNING|0:0|nhms_idem:key|scheduler|account\n"

    monkeypatch.setattr(reconcile_module, "_bounded_sacct_stdout", bounded)

    with pytest.raises(reconcile_module.ReconcileQueryUnavailable, match="bounded output"):
        reconcile_module._query_comment_accounting_proof(
            reconcile_module.default_comment_sacct_querier(global_visibility_probe=lambda: True),
            "key",
            expected_user="scheduler",
            expected_account="account",
        )

    page_count = (reconcile_module.COMMENT_SACCT_LOOKBACK_DAYS * 24) // reconcile_module.COMMENT_SACCT_PAGE_HOURS
    assert len(commands) == page_count + 1
    assert "--user=scheduler" in commands[0]
    assert "--allusers" in commands[-1]


def test_inflight_sacct_querier_uses_shared_bounded_stream_reader(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from services.orchestrator import reconcile as reconcile_module

    commands: list[list[str]] = []

    def bounded(command: Any) -> str:
        commands.append(list(command))
        return "17667|nhms_forecast|RUNNING|0:0|nhms_idem:key|scheduler|account\n"

    monkeypatch.setattr(reconcile_module, "_bounded_sacct_stdout", bounded)

    record = reconcile_module.default_sacct_querier()("17667")

    assert record is not None
    assert record.slurm_job_id == "17667"
    assert len(commands) == 1
    assert "--jobs=17667" in commands[0]


@pytest.mark.parametrize("boundary", ["byte", "row", "wall_time"])
def test_real_sacct_process_bounds_reap_and_leave_inflight_cohort_unchanged(
    tmp_path: Any,
    monkeypatch: pytest.MonkeyPatch,
    boundary: str,
) -> None:
    from services.orchestrator import reconcile as reconcile_module

    executable_root = tmp_path / f"fake-sacct-{boundary}"
    executable_root.mkdir()
    executable = executable_root / "sacct"
    pid_path = tmp_path / f"{boundary}.pid"
    terminated_path = tmp_path / f"{boundary}.terminated"
    executable.write_text(
        """#!/bin/sh
printf '%s' "$$" > "$FAKE_SACCT_PID_PATH"
terminated() {
    : > "$FAKE_SACCT_TERMINATED_PATH"
    exit 0
}
trap terminated TERM INT
case "$FAKE_SACCT_BOUNDARY" in
    byte)
        while :; do printf 'xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx'; done
        ;;
    row)
        while :; do printf '17667|nhms_forecast|RUNNING|0:0||scheduler|account\\n'; done
        ;;
    wall_time)
        exec sleep 60
        ;;
esac
""",
        encoding="utf-8",
    )
    executable.chmod(0o755)
    monkeypatch.setenv("FAKE_SACCT_BOUNDARY", boundary)
    monkeypatch.setenv("FAKE_SACCT_PID_PATH", str(pid_path))
    monkeypatch.setenv("FAKE_SACCT_TERMINATED_PATH", str(terminated_path))
    monkeypatch.setattr(reconcile_module, "MAX_COMMENT_SACCT_BYTES", 128 if boundary == "byte" else 1_000_000)
    monkeypatch.setattr(reconcile_module, "MAX_COMMENT_SACCT_ROWS", 2 if boundary == "row" else 10_000)
    monkeypatch.setattr(
        reconcile_module,
        "COMMENT_SACCT_TIMEOUT_SECONDS",
        1.0 if boundary == "wall_time" else 2.0,
    )

    repository = _file_cohort_repository(tmp_path / "state", member_count=1)
    key = "cycle_gfs_2026071200_forecast_fixture:forecast"
    _bind_current_file_cohort(repository, key, slurm_job_id="17667")
    job_id = "job_cycle_gfs_2026071200_forecast_fixture_forecast"
    before = repository.get_pipeline_job(job_id)

    outcomes = reconcile_inflight_jobs(
        repository,
        sacct_query=reconcile_module.default_sacct_querier(str(executable_root)),
    )

    assert len(outcomes) == 1
    assert outcomes[0].action == "query_unavailable"
    assert outcomes[0].durable_write_count == 0
    assert len(repr(outcomes[0])) < 1_000
    assert repository.get_pipeline_job(job_id) == before
    assert not before.get("candidate_projections")
    if boundary != "wall_time":
        assert terminated_path.exists()
    child_pid = int(pid_path.read_text(encoding="utf-8"))
    with pytest.raises(ChildProcessError):
        os.waitpid(child_pid, os.WNOHANG)


def test_parse_master_sacct_row_returns_exact_array_task_row() -> None:
    from services.orchestrator.reconcile import _parse_master_sacct_row

    stdout = (
        "12345|nhms_run_shud_forecast_array|COMPLETED|0:0|master-comment\n"
        "12345_2|nhms_run_shud_forecast_array|FAILED|1:0|task-2\n"
        "12345_3|nhms_run_shud_forecast_array|COMPLETED|0:0|task-3\n"
        "12345_3.batch|batch|COMPLETED|0:0|task-3\n"
    )

    record = _parse_master_sacct_row(stdout, "12345_3")

    assert record is not None
    assert record.slurm_job_id == "12345_3"
    assert record.task_id == "3"
    assert record.array_task_id == "3"
    assert record.raw_state == "COMPLETED"


@pytest.mark.parametrize(
    ("member_rows", "expected_state", "expected_exit_code"),
    [
        (
            "15144_0|nhms_forecast|PENDING|0:0|\n"
            "15144_1|nhms_forecast|PENDING|0:0|\n",
            "PENDING",
            None,
        ),
        (
            "15144_0|nhms_forecast|COMPLETED|0:0|\n"
            "15144_1|nhms_forecast|RUNNING|0:0|\n",
            "RUNNING",
            None,
        ),
        (
            "15144_0|nhms_forecast|FAILED|1:0|\n"
            "15144_1|nhms_forecast|RUNNING|0:0|\n",
            "RUNNING",
            None,
        ),
        (
            "15144_0|nhms_forecast|COMPLETED|0:0|\n"
            "15144_1|nhms_forecast|TIMEOUT|1:0|\n",
            "TIMEOUT",
            "1:0",
        ),
        (
            "15144_0|nhms_forecast|COMPLETED|0:0|\n"
            "15144_1|nhms_forecast|CANCELLED|0:15|\n",
            "CANCELLED",
            "0:15",
        ),
    ],
)
def test_parse_master_sacct_row_aggregates_array_member_statuses(
    member_rows: str,
    expected_state: str,
    expected_exit_code: str | None,
) -> None:
    from services.orchestrator.reconcile import _parse_master_sacct_row

    record = _parse_master_sacct_row(member_rows, "15144")

    assert record is not None
    assert record.slurm_job_id == "15144"
    assert record.job_name == "nhms_forecast"
    assert record.raw_state == expected_state
    assert record.exit_code == expected_exit_code
    assert record.array_member_job_ids == ("15144_0", "15144_1")


def test_file_restart_reconcile_retries_unverified_array_master_without_resubmit(
    tmp_path: Any,
) -> None:
    from services.orchestrator.file_orchestration_journal import (
        FileOrchestrationJournalRepository,
    )
    from services.orchestrator.reconcile import _parse_master_sacct_row

    cycle_time = datetime(2026, 7, 18, 1, tzinfo=UTC)
    cycle_id = "gfs_2026071801"
    repository = FileOrchestrationJournalRepository(tmp_path / "journal")
    repository.upsert_pipeline_job(
        {
            "job_id": "job_gfs_2026071801_model_a_forecast",
            "run_id": "fcst_gfs_2026071801_model_a",
            "cycle_id": cycle_id,
            "job_type": "run_shud_forecast_array",
            "slurm_job_id": "15144",
            "array_task_id": None,
            "model_id": "model_a",
            "status": "submitted",
            "stage": "forecast",
            "idempotency_key": "gfs:gfs_2026071801:model_a:forecast",
            "candidate_id": (
                "gfs:2026-07-18T01:00:00Z:model_a:forecast_gfs_deterministic"
            ),
        }
    )

    query_count = 0

    def _sacct_query(_slurm_job_id: str) -> SacctRecord | None:
        nonlocal query_count
        query_count += 1
        if query_count == 1:
            return None
        return _parse_master_sacct_row(
            "15144_0|nhms_forecast|COMPLETED|0:0|\n"
            "15144_0.batch|batch|COMPLETED|0:0|\n",
            "15144",
        )

    first = reconcile_inflight_jobs(repository, sacct_query=_sacct_query)
    assert first[0].action == "unverified"
    assert repository.get_pipeline_job(first[0].job_id)["status"] == (
        RECONCILE_UNVERIFIED_STATUS
    )
    assert [job.job_id for job in repository.query_inflight_jobs()] == [first[0].job_id]

    second = reconcile_inflight_jobs(repository, sacct_query=_sacct_query)

    assert query_count == 2
    assert second[0].action == "terminal"
    assert second[0].status == "succeeded"
    recovered = repository.get_pipeline_job(second[0].job_id)
    assert recovered is not None
    assert recovered["status"] == "succeeded"
    assert recovered["error_code"] is None
    assert repository.has_active_pipeline(
        source_id="gfs",
        cycle_time=cycle_time,
        model_id="model_a",
    ) is False
    # Restart reconcile only updates the existing durable row.  The inactive
    # gate now permits the scheduler to advance to state_save_qc without a
    # duplicate sbatch for the forecast stage.
    jobs = repository.query_pipeline_jobs_by_cycle(cycle_id)
    assert [job["job_id"] for job in jobs] == [second[0].job_id]


# --- FINDING-1: cached reconcile session rollback on crash recovery ------------


def _reconcile_store_shell(store: Any) -> Any:
    """A minimal carrier exposing only ``_reconcile_store`` so the unbound
    ProductionScheduler method can be bound onto it without the heavy ctor.
    """

    import types

    from services.orchestrator.scheduler import ProductionScheduler

    shell = types.SimpleNamespace(_reconcile_store=store)
    ProductionScheduler._reset_reconcile_store_after_error.__get__(
        shell, ProductionScheduler
    )()
    return shell


def test_reset_reconcile_store_after_error_rolls_back_session() -> None:
    """A failed commit leaves the cached session pending-rollback; recovery rolls
    it back so the connection stays reusable, and KEEPS the cached store (the
    common, recoverable case — no needless rebuild).
    """

    import types

    rollback_calls: list[int] = []
    session = types.SimpleNamespace(rollback=lambda: rollback_calls.append(1))
    store = types.SimpleNamespace(session=session)

    shell = _reconcile_store_shell(store)

    assert rollback_calls == [1]  # rolled back exactly once.
    assert shell._reconcile_store is store  # cache preserved, not dropped.


def test_reset_reconcile_store_after_error_drops_store_when_rollback_fails() -> None:
    """If rollback itself raises (the connection is truly dead) the cache is
    dropped so the next pass rebuilds a clean store via _restart_reconcile_store.
    """

    import types

    def _boom() -> None:
        raise RuntimeError("connection dead")

    session = types.SimpleNamespace(rollback=_boom)
    store = types.SimpleNamespace(session=session)

    shell = _reconcile_store_shell(store)

    assert shell._reconcile_store is None  # poisoned/dead → dropped.


def test_reset_reconcile_store_after_error_noop_when_no_store() -> None:
    """No cached store → the reset is a clean no-op (no attribute access, no
    raise). Guards the early-return guard.
    """

    shell = _reconcile_store_shell(None)

    assert shell._reconcile_store is None


# --- B-LOW: created_at fallback when updated_at is NULL still grants grace ------


def test_young_by_created_at_fallback_when_updated_at_none_defers() -> None:
    """A legacy reserved-unbound row whose ``updated_at`` is NULL but whose
    ``created_at`` is fresh must still earn grace via the created_at fallback: a
    confirmed-but-young absence is deferred (absence_unconfirmed), the row stays
    ``reserved``, and update_job_status is never called → no reclaim → no double
    submit. Locks the fallback so NULL updated_at on legacy rows doesn't regress
    the grace protection.
    """

    from datetime import UTC, datetime

    from services.orchestrator.reconcile import (
        ABSENCE_UNCONFIRMED_ACTION,
        reconcile_reserved_unbound_jobs,
    )

    fixed_now = datetime(2026, 6, 4, 12, 0, 0, tzinfo=UTC)

    class _NoUpdatedAtJob:
        job_id = "job_legacy_null_updated"
        idempotency_key = "gfs:cyc:basin:forcing"
        status = "reserved"
        slurm_job_id = None
        updated_at = None  # primary anchor absent (legacy NULL).
        created_at = fixed_now  # fresh → grace via the fallback.

    update_calls: list[tuple[str, str]] = []

    class _FakeStore:
        def query_reserved_unbound_jobs(self) -> list[Any]:
            return [_NoUpdatedAtJob()]

        def update_job_status(self, job_id: str, status: str, **_kwargs: Any) -> None:
            update_calls.append((job_id, status))

    def _comment_query(_idem: str) -> Any:
        return None  # confirmed-absent (not yet visible in accounting).

    outcomes = reconcile_reserved_unbound_jobs(
        _FakeStore(),
        comment_query=_comment_query,
        now=lambda: fixed_now,
    )

    assert len(outcomes) == 1
    assert outcomes[0].action == ABSENCE_UNCONFIRMED_ACTION
    assert outcomes[0].status == "reserved"
    assert update_calls == []  # young by created_at fallback → not demoted.


def test_restart_reconcile_store_bounds_db_connect_timeout(monkeypatch: Any) -> None:
    """_restart_reconcile_store must build its engine with a bounded
    connect_timeout so a misconfigured/unreachable database_url fails fast
    instead of hanging the daemon at pass start. Patches sqlalchemy.create_engine
    at the source (the method does a local ``from sqlalchemy import create_engine``)
    and asserts the connect_args carry the bound."""
    from sqlalchemy import create_engine as _real_create_engine

    from services.orchestrator.scheduler import (
        RECONCILE_DB_CONNECT_TIMEOUT_SECONDS,
        RECONCILE_DB_STATEMENT_TIMEOUT_MS,
        ProductionScheduler,
    )

    calls: list[tuple[tuple[Any, ...], dict[str, Any]]] = []

    def _fake_create_engine(*args: Any, **kwargs: Any):
        calls.append((args, kwargs))
        # Return a real, side-effect-free engine so PipelineStore(Session(engine))
        # constructs without touching the (fake) postgres URL.
        return _real_create_engine("sqlite://")

    monkeypatch.setattr("sqlalchemy.create_engine", _fake_create_engine)

    class _Config:
        database_url = "postgresql://u:p@db.invalid:5432/x"

    class _Shell:
        config = _Config()
        _reconcile_store = None

    shell = _Shell()
    ProductionScheduler._restart_reconcile_store.__get__(shell, ProductionScheduler)()

    assert len(calls) == 1
    _args, kwargs = calls[0]
    assert "connect_args" in kwargs
    connect_timeout = kwargs["connect_args"]["connect_timeout"]
    assert connect_timeout == RECONCILE_DB_CONNECT_TIMEOUT_SECONDS
    assert isinstance(connect_timeout, int) and connect_timeout > 0
    # Post-connect slow-query bound: a reachable-but-slow DB must not stall the
    # pass at reconcile time.
    options = kwargs["connect_args"]["options"]
    assert f"statement_timeout={RECONCILE_DB_STATEMENT_TIMEOUT_MS}" in options
    assert "statement_timeout=10000" in options


# --- FINDING-2: reconcile store build is best-effort to ANY database_url ------
# A malformed/unbuildable database_url makes SQLAlchemy's make_url() raise
# synchronously inside create_engine. That exception must NEVER propagate out of
# _restart_reconcile_store / _run_restart_reconcile (which run at pass start,
# before the submit-path DB-host preflight). It is swallowed as a best-effort
# skip; the preflight still runs. Zero-leak: no raw error message (DSN incl.
# password) may surface — only the exception class name.


def _malformed_url_shell(database_url: str) -> Any:
    """A minimal carrier exposing the attributes _restart_reconcile_store and
    _run_restart_reconcile touch, so the unbound methods can be bound without the
    heavy ctor. Mirrors the duck-typed shells used elsewhere in this file.
    """

    import types

    config = types.SimpleNamespace(
        database_url=database_url,
        dry_run=False,
        restart_reconcile_enabled=True,
    )
    return types.SimpleNamespace(
        config=config,
        _reconcile_store=None,
        _reconcile_store_build_error=None,
    )


@pytest.mark.parametrize(
    "database_url",
    [
        "postgresql://nhms:secret@bad::host/nhms",
        "postgresql://nhms:secret@[::1/nhms",
    ],
)
def test_restart_reconcile_store_swallows_malformed_database_url(
    database_url: str,
) -> None:
    """A malformed database_url must make _restart_reconcile_store return None
    (best-effort skip) WITHOUT raising, and must not stash the raw error message
    (which embeds the password) — only the exception class name."""
    from services.orchestrator.scheduler import ProductionScheduler

    shell = _malformed_url_shell(database_url)
    store = ProductionScheduler._restart_reconcile_store.__get__(
        shell, ProductionScheduler
    )()

    assert store is None
    assert shell._reconcile_store is None
    # Class name only — provably secret-free.
    assert shell._reconcile_store_build_error is not None
    assert "secret" not in shell._reconcile_store_build_error


@pytest.mark.parametrize(
    "database_url",
    [
        "postgresql://nhms:secret@bad::host/nhms",
        "postgresql://nhms:secret@[::1/nhms",
    ],
)
def test_run_restart_reconcile_skips_on_malformed_database_url(
    database_url: str,
) -> None:
    """_run_restart_reconcile must not propagate a malformed-url build failure:
    it returns a best-effort skip dict the pass tolerates, and that dict carries
    zero credentials (zero-leak by construction — error_type is a class name)."""
    import json

    from services.orchestrator.scheduler import ProductionScheduler

    shell = _malformed_url_shell(database_url)
    # _run_restart_reconcile calls self._restart_reconcile_store() internally, so
    # bind that helper onto the shell too.
    shell._restart_reconcile_store = ProductionScheduler._restart_reconcile_store.__get__(
        shell, ProductionScheduler
    )
    result = ProductionScheduler._run_restart_reconcile.__get__(
        shell, ProductionScheduler
    )()

    assert result is not None
    assert result["status"] == "skipped"
    assert result["reason"] == "reconcile_store_build_failed"
    assert "error_type" in result
    assert "secret" not in json.dumps(result)


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
    import json

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
    import json

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
