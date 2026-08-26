"""#1850 Round 2 invariant closure: attempt-scoped binding provenance.

The governing invariant: one durable claimant may bind one uniquely owned
Slurm accounting incarnation, and every later transition/locator must preserve
the canonical bind provenance needed to identify that incarnation; derived
payload state may neither manufacture vacancy nor rewrite provenance;
uncertainty is fail-closed.

These tests cover the three verified FIX_NOW defects on the pre-fix head:

- cand-r2-state-01: a complete terminal ``matched_bound`` projection must not
  replace a name-window fallback bind's provenance with the exact-comment
  factory default.
- cand-r2-state-02: the gateway/commit ``submitted_at`` (T1) is never canonical
  accounting Submit evidence; a settled same-id sibling without canonical
  provenance blocks fail-closed even when its legacy ``submitted_at`` differs
  from the candidate Submit (including microsecond-only differences).
- cand-r2-state-03: source/cycle discovery comes unconditionally from the safe
  master filename, so a validator-accepted wrong-kind payload planted under a
  settled master filename can never suppress canonical replay.

Plus the transition/validation surface: binding provenance is immutable for one
bound attempt, cleared by clean reservation/reclaim, and present on the
scheduler attempt evidence projection.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from services.orchestrator.accepted_submit_identity import (
    ACCEPTED_SUBMIT_CONTRACT_VERSION,
    AcceptedSubmitTransition,
    normalize_accepted_submit_evidence,
)
from tests.gateway_reconcile_helpers import (
    _append_cohort_placeholders,
    _commit_name_window_fallback_bind,
    _file_cohort_repository,
    _versioned_master_reservation_record,
)
from tests.test_real_slurm_gateway import _pinned_local_timezone

_ANCHOR = datetime(2026, 7, 12, tzinfo=UTC)

# #1850 Fix A additive field names. Imported lazily in each test so the module
# stays collectible against pre-change source (the red proof half of the
# regression matrix must fail on the FIXED assertions, not on collection).
def _SLURM_BINDING_SOURCE_FIELD() -> str:
    from services.orchestrator.accepted_submit_identity import SLURM_BINDING_SOURCE_FIELD

    return SLURM_BINDING_SOURCE_FIELD


def _SLURM_ACCOUNTING_SUBMITTED_AT_FIELD() -> str:
    from services.orchestrator.accepted_submit_identity import (
        SLURM_ACCOUNTING_SUBMITTED_AT_FIELD,
    )

    return SLURM_ACCOUNTING_SUBMITTED_AT_FIELD


def _single_projection(repository: Any, job_id: str) -> tuple[list[dict[str, Any]], bool]:
    """A single-member complete cohort projection, mirroring terminal reconcile."""
    row = repository.get_pipeline_job(job_id)
    members = row["cohort_members"]
    projections = [
        {
            "candidate_id": members[0].get("candidate_id"),
            "run_id": members[0].get("run_id"),
            "model_id": members[0].get("model_id"),
            "array_task_id": members[0]["array_task_id"],
            "array_task_outcome": "succeeded",
            "task_slurm_job_id": f"{row['slurm_job_id']}_{members[0]['array_task_id']}",
            "error_code": None,
            "restart_stage": "state_save_qc",
            "native_shud_resubmitted": False,
        }
    ]
    return projections, True


def _incomplete_projection(repository: Any, job_id: str) -> tuple[list[dict[str, Any]], bool]:
    """A single-member incomplete projection (outcome unverified)."""
    row = repository.get_pipeline_job(job_id)
    members = row["cohort_members"]
    projections = [
        {
            "candidate_id": members[0].get("candidate_id"),
            "run_id": members[0].get("run_id"),
            "model_id": members[0].get("model_id"),
            "array_task_id": members[0]["array_task_id"],
            "array_task_outcome": "unverified",
            "task_slurm_job_id": None,
            "error_code": None,
            "restart_stage": "forecast",
            "native_shud_resubmitted": False,
        }
    ]
    return projections, False


def _fallback_querier(monkeypatch: pytest.MonkeyPatch, *, rows: str, query_end: datetime) -> Any:
    from services.orchestrator import reconcile as reconcile_module

    monkeypatch.setattr(reconcile_module, "_bounded_sacct_stdout", lambda command: rows)
    return reconcile_module.default_comment_sacct_querier(
        global_visibility_probe=lambda: True,
        comment_storage_probe=lambda: False,
        now=lambda: query_end,
    )


def _fallback_row(master_id: str, *, submit: str) -> str:
    return f"{master_id}|nhms_forecast|COMPLETED|0:0||scheduler|account|{submit}\n"


def _reserve_gfs_sibling(repository: Any, *, created_at: datetime) -> str:
    record = _versioned_master_reservation_record(
        created_at=created_at,
        member_count=1,
        expected_user="scheduler",
        expected_account="account",
        source_id="gfs",
    )
    repository.reserve_pipeline_job(record)
    repository.transition_pipeline_job_submit_evidence(
        record["job_id"],
        AcceptedSubmitTransition.timeout(),
        accepted_submit_contract_version=ACCEPTED_SUBMIT_CONTRACT_VERSION,
        expected_submission_attempt=1,
        expected_statuses=("reserved",),
        require_unbound=True,
    )
    _append_cohort_placeholders(repository, 1, source_id="gfs")
    return str(record["job_id"])


# ---------------------------------------------------------------------------
# cand-r2-state-01: terminal projection preserves fallback bind provenance
# ---------------------------------------------------------------------------


@pytest.mark.skipif(not hasattr(__import__("time"), "tzset"), reason="time.tzset() is POSIX-only")
def test_terminal_projection_preserves_fallback_binding_provenance(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Real fallback bind -> complete projection preserves immutable provenance.

    Before the fix this was red: ``project_forecast_cohort_tasks`` called
    ``AcceptedSubmitTransition.accounting()`` with the exact-comment factory
    default, replaced the full tuple, and the durable/public row falsely
    claimed exact-comment recovery.
    """

    from services.orchestrator.file_orchestration_journal import (
        FileOrchestrationJournalRepository,
    )

    with _pinned_local_timezone("UTC"):
        repository = _file_cohort_repository(
            tmp_path / "gfs",
            created_at=_ANCHOR,
            member_count=1,
            expected_user="scheduler",
            expected_account="account",
        )
        job_id = "job_cycle_gfs_2026071200_forecast_fixture_forecast"
        key = "cycle_gfs_2026071200_forecast_fixture:forecast"
        bind = _commit_name_window_fallback_bind(
            repository, key, slurm_job_id="72001", canonical_submit=_ANCHOR + timedelta(hours=1)
        )
        assert bind.committed
        assert bind.wrote
        bound_row = repository.get_pipeline_job(job_id)
        assert bound_row[_SLURM_BINDING_SOURCE_FIELD()] == "slurm_name_window_unique"
        assert bound_row[_SLURM_ACCOUNTING_SUBMITTED_AT_FIELD()] == "2026-07-12T01:00:00Z"
        assert bound_row["reconciliation_source"] == "slurm_name_window_unique"
        assert bound_row["reconciliation_decision"] == "matched_bound"

        projections, complete = _single_projection(repository, job_id)
        assert complete
        repository.project_forecast_cohort_tasks(
            job_id,
            master_slurm_job_id="72001",
            projections=projections,
            complete=True,
            master_status="succeeded",
            master_error_code=None,
            reconciliation_decision="matched_bound",
        )

        terminal = repository.get_pipeline_job(job_id)
        assert terminal["status"] == "succeeded"
        assert terminal["reconciliation_decision"] == "matched_bound"
        assert terminal["matched_slurm_job_id"] == "72001"
        # Immutable binding provenance survives the terminal projection.
        assert terminal[_SLURM_BINDING_SOURCE_FIELD()] == "slurm_name_window_unique"
        assert terminal[_SLURM_ACCOUNTING_SUBMITTED_AT_FIELD()] == "2026-07-12T01:00:00Z"
        # The current matched-bound source is restored to name-window, never the
        # exact-comment factory default.
        assert terminal["reconciliation_source"] == "slurm_name_window_unique"

        # Reopened journal agrees.
        reopened = FileOrchestrationJournalRepository(repository.root)
        reopened_terminal = reopened.get_pipeline_job(job_id)
        assert reopened_terminal[_SLURM_BINDING_SOURCE_FIELD()] == "slurm_name_window_unique"
        assert reopened_terminal["reconciliation_source"] == "slurm_name_window_unique"


@pytest.mark.skipif(not hasattr(__import__("time"), "tzset"), reason="time.tzset() is POSIX-only")
def test_defer_then_complete_projection_restores_fallback_source(
    tmp_path: Any,
) -> None:
    """Fallback bind -> incomplete coverage defer -> later complete projection.

    The defer reasserts the exact-comment held tuple (accounting_unavailable,
    no matched id) but must retain the immutable binding provenance; the later
    complete matched-bound projection restores the name-window current source
    and the matched id from binding provenance.
    """

    with _pinned_local_timezone("UTC"):
        repository = _file_cohort_repository(
            tmp_path / "gfs",
            created_at=_ANCHOR,
            member_count=1,
            expected_user="scheduler",
            expected_account="account",
        )
        job_id = "job_cycle_gfs_2026071200_forecast_fixture_forecast"
        key = "cycle_gfs_2026071200_forecast_fixture:forecast"
        bind = _commit_name_window_fallback_bind(
            repository, key, slurm_job_id="72001", canonical_submit=_ANCHOR + timedelta(hours=1)
        )
        assert bind.committed

        # Incomplete coverage: defer parks the row, clears matched id, keeps the
        # current tuple exact-comment sourced.
        projections, complete = _incomplete_projection(repository, job_id)
        assert not complete
        repository.project_forecast_cohort_tasks(
            job_id,
            master_slurm_job_id="72001",
            projections=projections,
            complete=False,
            master_status="succeeded",
            master_error_code=None,
            reconciliation_decision="matched_bound",
        )
        deferred = repository.get_pipeline_job(job_id)
        assert deferred["status"] == "reconcile_unverified"
        assert deferred["reconciliation_decision"] == "accounting_unavailable"
        assert deferred["reconciliation_reason_class"] == "coverage_incomplete"
        assert deferred["matched_slurm_job_id"] is None
        # Binding provenance survives the defer.
        assert deferred[_SLURM_BINDING_SOURCE_FIELD()] == "slurm_name_window_unique"
        assert deferred[_SLURM_ACCOUNTING_SUBMITTED_AT_FIELD()] == "2026-07-12T01:00:00Z"

        # Later complete projection restores source and matched id.
        full, full_complete = _single_projection(repository, job_id)
        assert full_complete
        repository.project_forecast_cohort_tasks(
            job_id,
            master_slurm_job_id="72001",
            projections=full,
            complete=True,
            master_status="succeeded",
            master_error_code=None,
            reconciliation_decision="matched_bound",
        )
        terminal = repository.get_pipeline_job(job_id)
        assert terminal["status"] == "succeeded"
        assert terminal["reconciliation_decision"] == "matched_bound"
        assert terminal["matched_slurm_job_id"] == "72001"
        assert terminal["reconciliation_source"] == "slurm_name_window_unique"
        assert terminal[_SLURM_BINDING_SOURCE_FIELD()] == "slurm_name_window_unique"
        assert terminal[_SLURM_ACCOUNTING_SUBMITTED_AT_FIELD()] == "2026-07-12T01:00:00Z"


@pytest.mark.skipif(not hasattr(__import__("time"), "tzset"), reason="time.tzset() is POSIX-only")
def test_terminal_projection_exact_comment_sibling_stays_exact_comment(
    tmp_path: Any,
) -> None:
    """An exact-comment/gateway sibling projected terminally stays exact-comment.

    The source-restoration helper must not rewrite a normal/exact-comment bind
    into name-window; only fallback-bound rows restore name-window.
    """

    from services.orchestrator.file_orchestration_journal import (
        FileOrchestrationJournalRepository,
    )

    with _pinned_local_timezone("UTC"):
        repository = _file_cohort_repository(
            tmp_path / "ifs",
            created_at=_ANCHOR,
            member_count=1,
            expected_user="scheduler",
            expected_account="account",
            source_id="ifs",
        )
        job_id = "job_cycle_ifs_2026071200_forecast_fixture_forecast"
        key = "cycle_ifs_2026071200_forecast_fixture:forecast"
        bind = repository.commit_pipeline_job_submit_attempt(
            key,
            pipeline_job_id=job_id,
            expected_submission_attempt=1,
            slurm_job_id="72001",
            submitted_at=_ANCHOR + timedelta(hours=1),
            transition=AcceptedSubmitTransition.accepted(status="submitted"),
        )
        assert bind.committed
        bound_row = repository.get_pipeline_job(job_id)
        assert bound_row[_SLURM_BINDING_SOURCE_FIELD()] == "gateway_submit"
        assert bound_row[_SLURM_ACCOUNTING_SUBMITTED_AT_FIELD()] is None

        projections, _complete = _single_projection(repository, job_id)
        repository.project_forecast_cohort_tasks(
            job_id,
            master_slurm_job_id="72001",
            projections=projections,
            complete=True,
            master_status="succeeded",
            master_error_code=None,
            reconciliation_decision="matched_bound",
        )
        terminal = repository.get_pipeline_job(job_id)
        assert terminal["status"] == "succeeded"
        assert terminal["reconciliation_source"] == "slurm_exact_comment"
        assert terminal[_SLURM_BINDING_SOURCE_FIELD()] == "gateway_submit"
        assert terminal[_SLURM_ACCOUNTING_SUBMITTED_AT_FIELD()] is None
        reopened = FileOrchestrationJournalRepository(repository.root)
        assert reopened.get_pipeline_job(job_id)["reconciliation_source"] == "slurm_exact_comment"


# ---------------------------------------------------------------------------
# cand-r2-state-02: gateway/commit submitted_at is never incarnation proof
# ---------------------------------------------------------------------------


@pytest.mark.skipif(not hasattr(__import__("time"), "tzset"), reason="time.tzset() is POSIX-only")
def test_gateway_bound_settled_sibling_blocks_fallback_even_with_different_legacy_submit(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Normal gateway bind with legacy submitted_at=T1, no canonical accounting
    Submit -> settle -> sibling fallback same numeric id with sacct T0 != T1:
    held identity_mismatch_blocked/1, #1564 tuple and streak zero, no double owner.

    Before the fix the mixed timestamp T0 != T1 was interpreted as recycle and
    double-bound the same job.
    """

    from services.orchestrator.file_orchestration_journal import (
        FileOrchestrationJournalRepository,
    )
    from services.orchestrator.reconcile import reconcile_reserved_unbound_jobs

    with _pinned_local_timezone("UTC"):
        repository = _file_cohort_repository(
            tmp_path / "ifs",
            created_at=_ANCHOR,
            member_count=1,
            expected_user="scheduler",
            expected_account="account",
            source_id="ifs",
        )
        job_id = "job_cycle_ifs_2026071200_forecast_fixture_forecast"
        key = "cycle_ifs_2026071200_forecast_fixture:forecast"
        # Gateway acceptance time T1 = 01:00:00.123456 (microseconds, distinct
        # from the whole-second sacct T0 the fallback later sees).
        gateway_t1 = _ANCHOR + timedelta(hours=1) + timedelta(microseconds=123456)
        bind = repository.commit_pipeline_job_submit_attempt(
            key,
            pipeline_job_id=job_id,
            expected_submission_attempt=1,
            slurm_job_id="72001",
            submitted_at=gateway_t1,
            transition=AcceptedSubmitTransition.accepted(status="submitted"),
        )
        assert bind.committed
        bound_row = repository.get_pipeline_job(job_id)
        assert bound_row[_SLURM_BINDING_SOURCE_FIELD()] == "gateway_submit"
        assert bound_row[_SLURM_ACCOUNTING_SUBMITTED_AT_FIELD()] is None
        assert bound_row["submitted_at"] == "2026-07-12T01:00:00.123456Z"

        projections, _complete = _single_projection(repository, job_id)
        repository.project_forecast_cohort_tasks(
            job_id,
            master_slurm_job_id="72001",
            projections=projections,
            complete=True,
            master_status="succeeded",
            master_error_code=None,
            reconciliation_decision="matched_bound",
        )
        assert repository.get_pipeline_job(job_id)["status"] == "succeeded"

        # GFS fallback sees the same numeric id with sacct Submit T0 (whole
        # second, no microseconds) -- different from the gateway T1.
        gfs_job_id = _reserve_gfs_sibling(repository, created_at=_ANCHOR + timedelta(hours=1))
        query_end = _ANCHOR + timedelta(hours=2)
        query = _fallback_querier(
            monkeypatch,
            rows=_fallback_row("72001", submit="2026-07-12T01:00:00"),
            query_end=query_end,
        )
        outcome = reconcile_reserved_unbound_jobs(
            repository, comment_query=query, now=lambda: query_end
        )[0]

        assert outcome.job_id == gfs_job_id
        assert outcome.action == "identity_mismatch_blocked"
        assert outcome.match_count == 1
        assert outcome.reconciliation_decision == "accounting_unavailable"
        assert outcome.reconciliation_reason_class == "comment_accounting_unproven"
        persisted = repository.get_pipeline_job(gfs_job_id)
        assert persisted["status"] == "reserved"
        assert persisted["slurm_job_id"] is None
        assert persisted["identity_blocked_streak"] == 0
        # The settled gateway-bound IFS owner keeps its bind; no double owner.
        ifs = repository.get_pipeline_job(job_id)
        assert ifs["slurm_job_id"] == "72001"
        reopened = FileOrchestrationJournalRepository(repository.root)
        assert reopened.get_pipeline_job(gfs_job_id)["slurm_job_id"] is None
        assert reopened.get_pipeline_job(job_id)["slurm_job_id"] == "72001"


@pytest.mark.skipif(not hasattr(__import__("time"), "tzset"), reason="time.tzset() is POSIX-only")
def test_exact_comment_bound_settled_sibling_without_canonical_submit_blocks_fallback(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Exact-comment matched bind without canonical Submit -> settle -> sibling
    fallback same id at a different candidate Submit: also fail-closed."""

    from services.orchestrator.file_orchestration_journal import (
        FileOrchestrationJournalRepository,
    )
    from services.orchestrator.reconcile import reconcile_reserved_unbound_jobs

    with _pinned_local_timezone("UTC"):
        repository = _file_cohort_repository(
            tmp_path / "ifs",
            created_at=_ANCHOR,
            member_count=1,
            expected_user="scheduler",
            expected_account="account",
            source_id="ifs",
        )
        job_id = "job_cycle_ifs_2026071200_forecast_fixture_forecast"
        key = "cycle_ifs_2026071200_forecast_fixture:forecast"
        # Exact-comment recovery bind: matched_bound with exact-comment source,
        # legacy submitted_at T1, NO canonical accounting Submit.
        bind = repository.commit_pipeline_job_submit_attempt(
            key,
            pipeline_job_id=job_id,
            expected_submission_attempt=1,
            slurm_job_id="72001",
            submitted_at=_ANCHOR + timedelta(hours=1),
            transition=AcceptedSubmitTransition.accounting(
                "matched_bound",
                submit_outcome="accepted",
                matched_slurm_job_id="72001",
                status="submitted",
                reconciliation_source="slurm_exact_comment",
            ),
        )
        assert bind.committed
        bound_row = repository.get_pipeline_job(job_id)
        assert bound_row[_SLURM_BINDING_SOURCE_FIELD()] == "slurm_exact_comment"
        assert bound_row[_SLURM_ACCOUNTING_SUBMITTED_AT_FIELD()] is None

        projections, _complete = _single_projection(repository, job_id)
        repository.project_forecast_cohort_tasks(
            job_id,
            master_slurm_job_id="72001",
            projections=projections,
            complete=True,
            master_status="succeeded",
            master_error_code=None,
            reconciliation_decision="matched_bound",
        )
        assert repository.get_pipeline_job(job_id)["status"] == "succeeded"

        gfs_job_id = _reserve_gfs_sibling(repository, created_at=_ANCHOR + timedelta(hours=1))
        query_end = _ANCHOR + timedelta(hours=2)
        query = _fallback_querier(
            monkeypatch,
            rows=_fallback_row("72001", submit="2026-07-12T02:00:00"),
            query_end=query_end,
        )
        outcome = reconcile_reserved_unbound_jobs(
            repository, comment_query=query, now=lambda: query_end
        )[0]

        assert outcome.job_id == gfs_job_id
        assert outcome.action == "identity_mismatch_blocked"
        assert outcome.match_count == 1
        assert outcome.reconciliation_decision == "accounting_unavailable"
        persisted = repository.get_pipeline_job(gfs_job_id)
        assert persisted["status"] == "reserved"
        assert persisted["slurm_job_id"] is None
        assert persisted["identity_blocked_streak"] == 0
        # Settled exact-comment owner keeps its bind.
        assert repository.get_pipeline_job(job_id)["slurm_job_id"] == "72001"
        reopened = FileOrchestrationJournalRepository(repository.root)
        assert reopened.get_pipeline_job(job_id)["slurm_job_id"] == "72001"


@pytest.mark.skipif(not hasattr(__import__("time"), "tzset"), reason="time.tzset() is POSIX-only")
def test_settled_fallback_same_canonical_submit_blocks_and_different_permits_recycle(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Fallback-bound settled row canonical Submit T0: same id T0 blocks;
    same id canonical T2 permits genuine recycle. Legacy submitted_at is
    irrelevant in both."""

    from services.orchestrator.file_orchestration_journal import (
        FileOrchestrationJournalRepository,
    )
    from services.orchestrator.reconcile import reconcile_reserved_unbound_jobs

    with _pinned_local_timezone("UTC"):
        # Settle IFS at canonical 01:00 with a fallback bind.
        repository = _file_cohort_repository(
            tmp_path / "ifs",
            created_at=_ANCHOR,
            member_count=1,
            expected_user="scheduler",
            expected_account="account",
            source_id="ifs",
        )
        job_id = "job_cycle_ifs_2026071200_forecast_fixture_forecast"
        key = "cycle_ifs_2026071200_forecast_fixture:forecast"
        bind = _commit_name_window_fallback_bind(
            repository, key, slurm_job_id="72001", canonical_submit=_ANCHOR + timedelta(hours=1)
        )
        assert bind.committed
        projections, _complete = _single_projection(repository, job_id)
        repository.project_forecast_cohort_tasks(
            job_id,
            master_slurm_job_id="72001",
            projections=projections,
            complete=True,
            master_status="succeeded",
            master_error_code=None,
            reconciliation_decision="matched_bound",
        )
        assert repository.get_pipeline_job(job_id)["status"] == "succeeded"

        # Same canonical incarnation 01:00: blocks.
        gfs_job_id = _reserve_gfs_sibling(repository, created_at=_ANCHOR + timedelta(hours=1))
        query_end = _ANCHOR + timedelta(hours=2)
        query = _fallback_querier(
            monkeypatch,
            rows=_fallback_row("72001", submit="2026-07-12T01:00:00"),
            query_end=query_end,
        )
        outcome = reconcile_reserved_unbound_jobs(
            repository, comment_query=query, now=lambda: query_end
        )[0]
        assert outcome.job_id == gfs_job_id
        assert outcome.action == "identity_mismatch_blocked"
        assert outcome.match_count == 1
        assert repository.get_pipeline_job(gfs_job_id)["slurm_job_id"] is None

        # Different canonical incarnation 02:00: genuine recycle permits bind.
        repository2 = _file_cohort_repository(
            tmp_path / "ifs2",
            created_at=_ANCHOR,
            member_count=1,
            expected_user="scheduler",
            expected_account="account",
            source_id="ifs",
        )
        bind2 = _commit_name_window_fallback_bind(
            repository2, key, slurm_job_id="72001", canonical_submit=_ANCHOR + timedelta(hours=1)
        )
        assert bind2.committed
        projections2, _ = _single_projection(repository2, job_id)
        repository2.project_forecast_cohort_tasks(
            job_id,
            master_slurm_job_id="72001",
            projections=projections2,
            complete=True,
            master_status="succeeded",
            master_error_code=None,
            reconciliation_decision="matched_bound",
        )
        gfs_job_id2 = _reserve_gfs_sibling(repository2, created_at=_ANCHOR + timedelta(hours=1))
        query2 = _fallback_querier(
            monkeypatch,
            rows=_fallback_row("72001", submit="2026-07-12T02:00:00"),
            query_end=query_end,
        )
        outcome2 = reconcile_reserved_unbound_jobs(
            repository2, comment_query=query2, now=lambda: query_end
        )[0]
        assert outcome2.job_id == gfs_job_id2
        assert outcome2.action == "bound"
        assert outcome2.matched_slurm_job_id == "72001"
        assert outcome2.reconciliation_source == "slurm_name_window_unique"
        persisted2 = repository2.get_pipeline_job(gfs_job_id2)
        assert persisted2["slurm_job_id"] == "72001"
        assert persisted2[_SLURM_ACCOUNTING_SUBMITTED_AT_FIELD()] == "2026-07-12T02:00:00Z"
        reopened2 = FileOrchestrationJournalRepository(repository2.root)
        assert reopened2.get_pipeline_job(gfs_job_id2)["slurm_job_id"] == "72001"
        assert reopened2.get_pipeline_job(job_id)["slurm_job_id"] == "72001"


# ---------------------------------------------------------------------------
# cand-r2-state-03: safe filename owns cycle discovery
# ---------------------------------------------------------------------------


def _plant_wrong_kind_payload(repository: Any, job_id: str, *, legacy: bool) -> None:
    """Plant a validator-accepted wrong-kind payload under the master filename."""
    from services.orchestrator.file_orchestration_journal import (
        FILE_ORCHESTRATION_JOURNAL_SCHEMA_VERSION,
    )

    if legacy:
        payload = {
            "job_id": job_id,
            "run_id": "cycle_ifs_2026071200_forecast_fixture",
            "cycle_id": "ifs_2026071200",
            "job_type": "run_shud_forecast_array",
            "model_id": None,
            "status": "succeeded",
            "stage": "forecast",
            "candidate_id": None,
            "submitted_at": "2026-07-12T01:00:00Z",
        }
    else:
        # A valid accepted-submit CANDIDATE row shape under the master filename.
        payload = {
            "accepted_submit_contract_version": ACCEPTED_SUBMIT_CONTRACT_VERSION,
            "job_id": job_id,
            "run_id": "cycle_ifs_2026071200_forecast_fixture",
            "cycle_id": "ifs_2026071200",
            "job_type": "run_shud_forecast_array",
            "model_id": "model_0",
            "array_task_id": 0,
            "candidate_id": "IFS:2026-07-12T00:00:00Z:model_0:default",
            "status": "succeeded",
            "stage": "forecast",
            "submit_outcome": "accepted",
            "restart_stage": "forecast",
            "native_shud_resubmitted": False,
        }
    record = {
        "schema_version": FILE_ORCHESTRATION_JOURNAL_SCHEMA_VERSION,
        "record_type": "pipeline_job",
        "source_id": "IFS",
        "cycle_time": "2026-07-12T00:00:00Z",
        "job_id": job_id,
        "payload": payload,
    }
    (repository.root / "pipeline-jobs" / f"{job_id}.json").write_text(
        json.dumps(record), encoding="utf-8"
    )
    # The planted payload must be genuinely validator-accepted (never a
    # monkeypatched validator).
    direct = repository._validated_direct_pipeline_job_record(
        repository._read_optional_json(repository.root / "pipeline-jobs" / f"{job_id}.json"),
        expected_job_id=job_id,
    )
    assert direct["job_id"] == job_id


@pytest.mark.skipif(not hasattr(__import__("time"), "tzset"), reason="time.tzset() is POSIX-only")
@pytest.mark.parametrize("legacy", [True, False], ids=["legacy_payload", "candidate_payload"])
def test_valid_wrong_kind_payload_under_master_filename_cannot_hide_canonical_owner(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch, legacy: bool
) -> None:
    """A validator-accepted legacy/candidate payload planted under a settled
    master filename still triggers canonical replay and blocks the same
    incarnation. Before the fix the decoded wrong-kind payload early-continued
    before retaining the cycle, hiding the canonical owner."""

    from services.orchestrator.file_orchestration_journal import (
        FileOrchestrationJournalRepository,
    )
    from services.orchestrator.reconcile import reconcile_reserved_unbound_jobs

    with _pinned_local_timezone("UTC"):
        repository = _file_cohort_repository(
            tmp_path / "ifs",
            created_at=_ANCHOR,
            member_count=1,
            expected_user="scheduler",
            expected_account="account",
            source_id="ifs",
        )
        job_id = "job_cycle_ifs_2026071200_forecast_fixture_forecast"
        key = "cycle_ifs_2026071200_forecast_fixture:forecast"
        bind = _commit_name_window_fallback_bind(
            repository, key, slurm_job_id="72001", canonical_submit=_ANCHOR + timedelta(hours=1)
        )
        assert bind.committed
        projections, _complete = _single_projection(repository, job_id)
        repository.project_forecast_cohort_tasks(
            job_id,
            master_slurm_job_id="72001",
            projections=projections,
            complete=True,
            master_status="succeeded",
            master_error_code=None,
            reconciliation_decision="matched_bound",
        )
        assert repository.get_pipeline_job(job_id)["status"] == "succeeded"
        assert len(repository.query_reserved_unbound_jobs()) == 0

        # Plant a validator-accepted wrong-kind payload over the master direct.
        _plant_wrong_kind_payload(repository, job_id, legacy=legacy)

        gfs_job_id = _reserve_gfs_sibling(repository, created_at=_ANCHOR + timedelta(hours=1))
        query_end = _ANCHOR + timedelta(hours=2)
        query = _fallback_querier(
            monkeypatch,
            rows=_fallback_row("72001", submit="2026-07-12T01:00:00"),
            query_end=query_end,
        )
        outcome = reconcile_reserved_unbound_jobs(
            repository, comment_query=query, now=lambda: query_end
        )[0]

        assert outcome.job_id == gfs_job_id
        assert outcome.action == "identity_mismatch_blocked", (
            "a valid wrong-kind payload under the master filename must not hide"
            " the canonical settled owner"
        )
        assert outcome.match_count == 1
        assert outcome.reconciliation_decision == "accounting_unavailable"
        persisted = repository.get_pipeline_job(gfs_job_id)
        assert persisted["status"] == "reserved"
        assert persisted["slurm_job_id"] is None
        assert persisted["identity_blocked_streak"] == 0
        # Canonical owner unchanged and still authoritative.
        assert repository.get_pipeline_job(job_id)["slurm_job_id"] == "72001"
        reopened = FileOrchestrationJournalRepository(repository.root)
        assert reopened.get_pipeline_job(gfs_job_id)["slurm_job_id"] is None
        assert reopened.get_pipeline_job(job_id)["status"] == "succeeded"
        assert reopened.get_pipeline_job(job_id)["slurm_job_id"] == "72001"


# ---------------------------------------------------------------------------
# Transition / validation surface (required matrix item 9)
# ---------------------------------------------------------------------------


def test_normalizer_rejects_invalid_binding_source() -> None:
    """A durable row carrying an unknown binding source is corrupt evidence."""
    from services.orchestrator.accepted_submit_identity import AcceptedSubmitEvidenceError

    with pytest.raises(AcceptedSubmitEvidenceError) as error:
        normalize_accepted_submit_evidence(
            {
                **_versioned_master_reservation_record(member_count=1),
                "status": "submitted",
                "submit_outcome": "accepted",
                "reconciliation_source": "slurm_name_window_unique",
                "reconciliation_decision": "matched_bound",
                "matched_slurm_job_id": "72001",
                _SLURM_BINDING_SOURCE_FIELD(): "not_a_source",
                _SLURM_ACCOUNTING_SUBMITTED_AT_FIELD(): "2026-07-12T01:00:00Z",
            }
        )
    assert error.value.field == _SLURM_BINDING_SOURCE_FIELD()


def test_normalizer_rejects_naive_noncanonical_accounting_timestamp() -> None:
    """A canonical accounting Submit must be a strict aware-UTC instant."""
    from services.orchestrator.accepted_submit_identity import AcceptedSubmitEvidenceError

    with pytest.raises(AcceptedSubmitEvidenceError) as error:
        normalize_accepted_submit_evidence(
            {
                **_versioned_master_reservation_record(member_count=1),
                "status": "submitted",
                "submit_outcome": "accepted",
                "reconciliation_source": "slurm_name_window_unique",
                "reconciliation_decision": "matched_bound",
                "matched_slurm_job_id": "72001",
                _SLURM_BINDING_SOURCE_FIELD(): "slurm_name_window_unique",
                _SLURM_ACCOUNTING_SUBMITTED_AT_FIELD(): "not-a-timestamp",
            }
        )
    assert error.value.field == _SLURM_ACCOUNTING_SUBMITTED_AT_FIELD()


def test_normalizer_rejects_name_window_source_without_canonical(tmp_path: Any) -> None:
    """An explicit name-window binding source without a strict canonical
    accounting Submit is not a legal fallback bind (#1850 round 2 issue 3)."""
    from services.orchestrator.accepted_submit_identity import AcceptedSubmitEvidenceError

    with pytest.raises(AcceptedSubmitEvidenceError) as error:
        normalize_accepted_submit_evidence(
            {
                **_versioned_master_reservation_record(member_count=1),
                "status": "submitted",
                "submit_outcome": "accepted",
                "slurm_job_id": "72001",
                "reconciliation_source": "slurm_name_window_unique",
                "reconciliation_decision": "matched_bound",
                "matched_slurm_job_id": "72001",
                _SLURM_BINDING_SOURCE_FIELD(): "slurm_name_window_unique",
            }
        )
    assert error.value.field == _SLURM_ACCOUNTING_SUBMITTED_AT_FIELD()


def test_ordinary_accepted_with_canonical_input_rejected_at_typed_commit(
    tmp_path: Any,
) -> None:
    """The ordinary accepted lane cannot carry canonical accounting input.

    Binding provenance is not a transition field (removed in round 2); the
    typed commit derives it from the transition shape. The one forgeable input
    left at the commit boundary is the canonical ``slurm_accounting_submitted_at``
    keyword: an ordinary accepted commit carrying it is refused (the gateway
    lane may never mint canonical accounting Submit) and the journal is
    unchanged."""

    from services.orchestrator.file_orchestration_journal import FileOrchestrationJournalError

    with _pinned_local_timezone("UTC"):
        repository = _file_cohort_repository(
            tmp_path / "gfs",
            created_at=_ANCHOR,
            member_count=1,
            expected_user="scheduler",
            expected_account="account",
        )
        job_id = "job_cycle_gfs_2026071200_forecast_fixture_forecast"
        key = "cycle_gfs_2026071200_forecast_fixture:forecast"
        current = repository.query_candidate_state(key)

        # Ordinary accepted carrying canonical accounting input is refused:
        # the gateway lane may never mint canonical accounting Submit.
        with pytest.raises(FileOrchestrationJournalError) as error:
            repository.commit_pipeline_job_submit_attempt(
                key,
                pipeline_job_id=str(current["job_id"]),
                expected_submission_attempt=1,
                slurm_job_id="72001",
                slurm_accounting_submitted_at=_ANCHOR + timedelta(hours=1),
                transition=AcceptedSubmitTransition.accepted(status="submitted"),
            )
        assert error.value.reason == "file_journal_evidence_invariant_invalid"
        assert error.value.field == _SLURM_ACCOUNTING_SUBMITTED_AT_FIELD()
        # Journal unchanged.
        assert repository.get_pipeline_job(job_id)["status"] == "reserved"
        assert repository.get_pipeline_job(job_id)["slurm_job_id"] is None


def test_name_window_matched_bind_without_canonical_timestamp_fails_typed_commit(
    tmp_path: Any,
) -> None:
    """The name-window fallback bind REQUIRES canonical accounting Submit."""

    from services.orchestrator.file_orchestration_journal import FileOrchestrationJournalError

    with _pinned_local_timezone("UTC"):
        repository = _file_cohort_repository(
            tmp_path / "gfs",
            created_at=_ANCHOR,
            member_count=1,
            expected_user="scheduler",
            expected_account="account",
        )
        key = "cycle_gfs_2026071200_forecast_fixture:forecast"
        current = repository.query_candidate_state(key)
        with pytest.raises(FileOrchestrationJournalError) as error:
            repository.commit_pipeline_job_submit_attempt(
                key,
                pipeline_job_id=str(current["job_id"]),
                expected_submission_attempt=1,
                slurm_job_id="72001",
                submitted_at=_ANCHOR + timedelta(hours=1),
                transition=AcceptedSubmitTransition.accounting(
                    "matched_bound",
                    submit_outcome="accepted",
                    matched_slurm_job_id="72001",
                    status="submitted",
                    reconciliation_source="slurm_name_window_unique",
                ),
            )
        assert error.value.reason == "file_journal_submit_instant_required"
        assert error.value.field == _SLURM_ACCOUNTING_SUBMITTED_AT_FIELD()


def test_clean_reservation_rejects_binding_provenance_dirty_fields(tmp_path: Any) -> None:
    from services.orchestrator.file_orchestration_journal import (
        FileOrchestrationJournalError,
        FileOrchestrationJournalRepository,
    )

    clean = FileOrchestrationJournalRepository(tmp_path / "dirty" / "journal")
    record = _versioned_master_reservation_record(
        member_count=1, expected_user="scheduler", expected_account="account"
    )
    record[_SLURM_BINDING_SOURCE_FIELD()] = "slurm_name_window_unique"
    record[_SLURM_ACCOUNTING_SUBMITTED_AT_FIELD()] = "2026-07-12T01:00:00Z"
    with pytest.raises(FileOrchestrationJournalError) as error:
        clean.reserve_pipeline_job(record)
    assert error.value.reason == "file_journal_clean_reservation_required"
    # The dirty-field set reports the lexicographically-first dirty field; both
    # binding-provenance fields are in the set, so either is the guard talking.
    assert error.value.field in {
        _SLURM_BINDING_SOURCE_FIELD(),
        _SLURM_ACCOUNTING_SUBMITTED_AT_FIELD(),
    }
    # Contamination control: the same shape without binding provenance reserves
    # cleanly, so the refusal is the dirty-field guard talking, not a malformed
    # record.
    legal = _versioned_master_reservation_record(
        member_count=1, expected_user="scheduler", expected_account="account"
    )
    assert clean.reserve_pipeline_job(legal) is not None


def test_reclaimable_row_cannot_carry_binding_provenance_and_reclaim_starts_clean(
    tmp_path: Any,
) -> None:
    """Binding provenance is impossible on a reclaimable (unbound) row, and a
    legal reclaim opens a fresh attempt with provenance cleared.

    The typed lifecycle only mints provenance on a successful bind (numeric
    Slurm id). Every path that produces a reclaimable ``reservation_lost`` row
    (operator demotion, permit-retry, release) runs on unbound reserved rows
    that never had provenance, and reclaim nulls the fields directly. So a
    reclaimable row carrying provenance is a durable impossible shape (rejected
    by the normalizer), and the reclaim's new attempt always starts with both
    provenance fields None.
    """

    from services.orchestrator.accepted_submit_identity import (
        OPERATOR_VERIFIED_ABSENCE_DECISION,
        AcceptedSubmitEvidenceError,
        normalize_accepted_submit_evidence,
    )
    from services.orchestrator.file_orchestration_journal import (
        FileOrchestrationJournalError,
        FileOrchestrationJournalRepository,
    )

    with _pinned_local_timezone("UTC"):
        # The impossible shape: reservation_lost / operator_verified_absence /
        # unbound with explicit name-window provenance is rejected.
        impossible = {
            **_versioned_master_reservation_record(member_count=1),
            "status": "reservation_lost",
            "submit_outcome": "submit_result_ambiguous",
            "reconciliation_source": "slurm_exact_comment",
            "reconciliation_decision": OPERATOR_VERIFIED_ABSENCE_DECISION,
            "reconciliation_reason_class": None,
            "matched_slurm_job_id": None,
            "slurm_job_id": None,
            _SLURM_BINDING_SOURCE_FIELD(): "slurm_name_window_unique",
            _SLURM_ACCOUNTING_SUBMITTED_AT_FIELD(): "2026-07-12T01:00:00Z",
        }
        with pytest.raises(AcceptedSubmitEvidenceError) as error:
            normalize_accepted_submit_evidence(impossible)
        assert error.value.field == "slurm_job_id"

        # The same impossible shape is refused at the durable write boundary.
        repository = _file_cohort_repository(
            tmp_path / "gfs",
            created_at=_ANCHOR,
            member_count=1,
            expected_user="scheduler",
            expected_account="account",
        )
        job_id = "job_cycle_gfs_2026071200_forecast_fixture_forecast"
        key = "cycle_gfs_2026071200_forecast_fixture:forecast"
        bind = _commit_name_window_fallback_bind(
            repository, key, slurm_job_id="72001", canonical_submit=_ANCHOR + timedelta(hours=1)
        )
        assert bind.committed
        current = repository.get_pipeline_job(job_id)
        impossible_row = dict(current)
        impossible_row.update(
            {
                "status": "reservation_lost",
                "submit_outcome": "submit_result_ambiguous",
                "reconciliation_source": "slurm_exact_comment",
                "reconciliation_decision": OPERATOR_VERIFIED_ABSENCE_DECISION,
                "reconciliation_reason_class": None,
                "matched_slurm_job_id": None,
                "slurm_job_id": None,
            }
        )
        with pytest.raises(FileOrchestrationJournalError) as error:
            repository._write_pipeline_job_unlocked(
                impossible_row, exclusive_direct=False, model_id=None
            )
        assert error.value.reason == "file_journal_evidence_invariant_invalid"
        # The failed write left the bound row unchanged.
        assert repository.get_pipeline_job(job_id)["slurm_job_id"] == "72001"

        # A legal reclaim only ever applies to an unbound held row (which never
        # carried provenance). Drive the real held-cohort producer + operator
        # demotion door and reclaim it: the new attempt's provenance fields are
        # None and the demote-then-reclaim lifecycle stays intact.
        from tests.orchestrator_demote_reserved_job_helpers import (
            JOB_ID as HELD_JOB_ID,
        )  # noqa: I001 - kept with the held-reclaim helpers it drives.
        from tests.orchestrator_demote_reserved_job_helpers import (
            _demote_kwargs,
            _held_cohort_repository,
        )

        held = _held_cohort_repository(tmp_path / "held", member_count=1)
        held_job_id = HELD_JOB_ID
        held_row = held.get_accepted_submit_pipeline_job(held_job_id)
        assert held_row["status"] == "reserved"
        assert held_row[_SLURM_BINDING_SOURCE_FIELD()] is None
        demoted = held.demote_operator_verified_reserved_job(
            held_job_id,
            **_demote_kwargs(held_row, verification_note="round-3 reclaim provenance test"),
        )
        assert demoted is not None
        released = held.get_accepted_submit_pipeline_job(held_job_id)
        assert released["status"] == "reservation_lost"
        assert released["reconciliation_decision"] == OPERATOR_VERIFIED_ABSENCE_DECISION
        assert released[_SLURM_BINDING_SOURCE_FIELD()] is None

        from tests.orchestrator_demote_reserved_job_helpers import (
            STARTED_AT as HELD_STARTED_AT,
        )

        record = _versioned_master_reservation_record(
            created_at=_ANCHOR,
            member_count=1,
            expected_user="scheduler",
            expected_account="account",
            source_id="gfs",
        )
        record.update(
            {
                **released,
                "expected_submission_attempt": released["submission_attempt"],
                "expected_submission_attempt_started_at": released[
                    "submission_attempt_started_at"
                ],
                "status": "reserved",
                "submission_attempt": int(released["submission_attempt"]) + 1,
                "submission_attempt_started_at": HELD_STARTED_AT + timedelta(hours=3),
                "submit_outcome": None,
                "reconciliation_source": None,
                "reconciliation_decision": None,
                "matched_slurm_job_id": None,
                "slurm_job_id": None,
            }
        )
        reclaimed = held.reclaim_pipeline_job_reservation(record)
        assert reclaimed is not None
        after = held.get_accepted_submit_pipeline_job(held_job_id)
        assert after[_SLURM_BINDING_SOURCE_FIELD()] is None
        assert after[_SLURM_ACCOUNTING_SUBMITTED_AT_FIELD()] is None
        assert after["submitted_at"] is None
        assert after["submission_attempt"] == 2
        # Reopened journal agrees.
        reopened = FileOrchestrationJournalRepository(held.root)
        assert reopened.get_pipeline_job(held_job_id)[_SLURM_BINDING_SOURCE_FIELD()] is None


def test_old_row_missing_binding_fields_remains_readable_and_fail_closed() -> None:
    """A v1 current-contract row predating the additive fields stays readable;
    missing provenance never proves recycle (the settled-incarnation helper
    fails closed on it)."""

    from services.orchestrator.file_orchestration_journal import _settled_incarnation_matches_candidate

    row = {
        **_versioned_master_reservation_record(member_count=1),
        "status": "succeeded",
        "submit_outcome": "accepted",
        "reconciliation_source": "slurm_exact_comment",
        "reconciliation_decision": "matched_bound",
        "slurm_job_id": "72001",
        "matched_slurm_job_id": "72001",
        "submitted_at": "2026-07-12T01:00:00.123456Z",
    }
    normalized = normalize_accepted_submit_evidence(row)
    assert normalized[_SLURM_BINDING_SOURCE_FIELD()] is None
    assert normalized[_SLURM_ACCOUNTING_SUBMITTED_AT_FIELD()] is None
    # Fail closed: no canonical provenance => same-id blocks (True), even though
    # the legacy submitted_at differs from the candidate.
    assert _settled_incarnation_matches_candidate(row, _ANCHOR + timedelta(hours=2)) is True


# ---------------------------------------------------------------------------
# Scheduler attempt evidence projection (required matrix item 10)
# ---------------------------------------------------------------------------


@pytest.mark.skipif(not hasattr(__import__("time"), "tzset"), reason="time.tzset() is POSIX-only")
def test_scheduler_attempt_evidence_exposes_binding_provenance_additively(
    tmp_path: Any,
) -> None:
    """``_restart_reconcile_attempt_evidence`` exposes the additive binding
    provenance fields without changing any existing key/value."""

    from services.orchestrator.scheduler_runtime import _restart_reconcile_attempt_evidence

    with _pinned_local_timezone("UTC"):
        repository = _file_cohort_repository(
            tmp_path / "gfs",
            created_at=_ANCHOR,
            member_count=1,
            expected_user="scheduler",
            expected_account="account",
        )
        job_id = "job_cycle_gfs_2026071200_forecast_fixture_forecast"
        key = "cycle_gfs_2026071200_forecast_fixture:forecast"
        bind = _commit_name_window_fallback_bind(
            repository, key, slurm_job_id="72001", canonical_submit=_ANCHOR + timedelta(hours=1)
        )
        assert bind.committed
        evidence = _restart_reconcile_attempt_evidence(repository, job_id)
        assert evidence["slurm_binding_source"] == "slurm_name_window_unique"
        assert evidence["slurm_accounting_submitted_at"] == "2026-07-12T01:00:00Z"
        # Pre-existing keys/values unchanged.
        assert evidence["reconciliation_source"] == "slurm_name_window_unique"
        assert evidence["reconciliation_decision"] == "matched_bound"
        assert evidence["matched_slurm_job_id"] == "72001"
        assert evidence["submit_outcome"] == "accepted"
        assert evidence["submission_attempt"] == 1


def test_ordinary_upsert_cannot_mutate_or_drop_binding_provenance(tmp_path: Any) -> None:
    """Binding provenance is closed master state: an ordinary upsert that tries
    to change or drop it on an existing bound row must be refused."""

    from services.orchestrator.file_orchestration_journal import FileOrchestrationJournalError

    with _pinned_local_timezone("UTC"):
        repository = _file_cohort_repository(
            tmp_path / "gfs",
            created_at=_ANCHOR,
            member_count=1,
            expected_user="scheduler",
            expected_account="account",
        )
        job_id = "job_cycle_gfs_2026071200_forecast_fixture_forecast"
        key = "cycle_gfs_2026071200_forecast_fixture:forecast"
        bind = _commit_name_window_fallback_bind(
            repository, key, slurm_job_id="72001", canonical_submit=_ANCHOR + timedelta(hours=1)
        )
        assert bind.committed
        public = repository.get_pipeline_job(job_id)
        assert public[_SLURM_BINDING_SOURCE_FIELD()] == "slurm_name_window_unique"

        # Attempt to mutate the binding source through the ordinary upsert.
        forged = {**public, _SLURM_BINDING_SOURCE_FIELD(): "slurm_exact_comment"}
        with pytest.raises(FileOrchestrationJournalError) as error:
            repository.upsert_pipeline_job(forged)
        assert error.value.reason == "file_journal_evidence_invariant_invalid"
        assert error.value.field == _SLURM_BINDING_SOURCE_FIELD()
        assert repository.get_pipeline_job(job_id)[_SLURM_BINDING_SOURCE_FIELD()] == "slurm_name_window_unique"

        # Omitting the field is a silent keep (the merge copies only explicitly
        # carried keys), never a drop: the persisted provenance survives an
        # ordinary partial upsert that does not mention it.
        dropped = {k: v for k, v in public.items() if k != _SLURM_BINDING_SOURCE_FIELD()}
        kept = repository.upsert_pipeline_job(dropped)
        assert kept is not None
        assert repository.get_pipeline_job(job_id)[_SLURM_BINDING_SOURCE_FIELD()] == "slurm_name_window_unique"

        # Forging the canonical accounting Submit on the current row is refused
        # the same way.
        forged_submit = {**public, _SLURM_ACCOUNTING_SUBMITTED_AT_FIELD(): "2026-07-12T02:00:00Z"}
        with pytest.raises(FileOrchestrationJournalError) as error:
            repository.upsert_pipeline_job(forged_submit)
        assert error.value.reason == "file_journal_evidence_invariant_invalid"
        assert error.value.field == _SLURM_ACCOUNTING_SUBMITTED_AT_FIELD()
        assert repository.get_pipeline_job(job_id)[_SLURM_ACCOUNTING_SUBMITTED_AT_FIELD()] == "2026-07-12T01:00:00Z"


# ---------------------------------------------------------------------------
# #1850 round 2 typed-boundary regressions: single canonical authority, no
# duplicate/forgeable transition provenance, legacy submitted_at never proves
# recycle or drives the scan.
# ---------------------------------------------------------------------------


def test_legacy_submitted_at_differs_from_canonical_does_not_change_incarnation_scan(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The claimant/occupancy scan uses ONLY the canonical accounting Submit.

    A fallback commit with legacy ``submitted_at`` (B) differing from the
    canonical (C) must still adjudicate incarnation by C: a same-C sibling
    blocks, a different-C sibling recycles, and the durable canonical equals C
    (never B). Legacy ``submitted_at`` is stored as the gateway/commit time
    only."""

    from services.orchestrator.accepted_submit_identity import AcceptedSubmitTransition
    from services.orchestrator.file_orchestration_journal import (
        FileOrchestrationJournalRepository,
    )
    from services.orchestrator.reconcile import reconcile_reserved_unbound_jobs

    with _pinned_local_timezone("UTC"):
        repository = _file_cohort_repository(
            tmp_path / "ifs",
            created_at=_ANCHOR,
            member_count=1,
            expected_user="scheduler",
            expected_account="account",
            source_id="ifs",
        )
        job_id = "job_cycle_ifs_2026071200_forecast_fixture_forecast"
        key = "cycle_ifs_2026071200_forecast_fixture:forecast"
        current = repository.query_candidate_state(key)
        # B=01:00 legacy, C=03:00 canonical: the mismatch must NOT change
        # incarnation adjudication.
        bind = repository.commit_pipeline_job_submit_attempt(
            key,
            pipeline_job_id=str(current["job_id"]),
            expected_submission_attempt=1,
            slurm_job_id="72001",
            submitted_at=_ANCHOR + timedelta(hours=1),
            slurm_accounting_submitted_at=_ANCHOR + timedelta(hours=3),
            transition=AcceptedSubmitTransition.accounting(
                "matched_bound",
                submit_outcome="accepted",
                matched_slurm_job_id="72001",
                status="submitted",
                reconciliation_source="slurm_name_window_unique",
            ),
        )
        assert bind.committed
        bound = repository.get_pipeline_job(job_id)
        assert bound[_SLURM_ACCOUNTING_SUBMITTED_AT_FIELD()] == "2026-07-12T03:00:00Z"
        assert bound[_SLURM_BINDING_SOURCE_FIELD()] == "slurm_name_window_unique"
        # Legacy submitted_at is the stored gateway/commit time (B), not C.
        assert bound["submitted_at"] == "2026-07-12T01:00:00Z"

        projections, _complete = _single_projection(repository, job_id)
        repository.project_forecast_cohort_tasks(
            job_id,
            master_slurm_job_id="72001",
            projections=projections,
            complete=True,
            master_status="succeeded",
            master_error_code=None,
            reconciliation_decision="matched_bound",
        )
        assert repository.get_pipeline_job(job_id)["status"] == "succeeded"

        # GFS candidate Submit 03:00 (== C): same incarnation blocks even
        # though legacy B differs.
        gfs_job_id = _reserve_gfs_sibling(repository, created_at=_ANCHOR + timedelta(hours=1))
        query_end = _ANCHOR + timedelta(hours=4)
        query = _fallback_querier(
            monkeypatch,
            rows=_fallback_row("72001", submit="2026-07-12T03:00:00"),
            query_end=query_end,
        )
        outcome = reconcile_reserved_unbound_jobs(
            repository, comment_query=query, now=lambda: query_end
        )[0]
        assert outcome.job_id == gfs_job_id
        assert outcome.action == "identity_mismatch_blocked"
        assert outcome.match_count == 1
        persisted = repository.get_pipeline_job(gfs_job_id)
        assert persisted["status"] == "reserved"
        assert persisted["slurm_job_id"] is None

        # GFS candidate Submit 04:00 (different canonical): legitimate recycle.
        repository2 = _file_cohort_repository(
            tmp_path / "ifs2",
            created_at=_ANCHOR,
            member_count=1,
            expected_user="scheduler",
            expected_account="account",
            source_id="ifs",
        )
        current2 = repository2.query_candidate_state(key)
        bind2 = repository2.commit_pipeline_job_submit_attempt(
            key,
            pipeline_job_id=str(current2["job_id"]),
            expected_submission_attempt=1,
            slurm_job_id="72001",
            submitted_at=_ANCHOR + timedelta(hours=1),
            slurm_accounting_submitted_at=_ANCHOR + timedelta(hours=3),
            transition=AcceptedSubmitTransition.accounting(
                "matched_bound",
                submit_outcome="accepted",
                matched_slurm_job_id="72001",
                status="submitted",
                reconciliation_source="slurm_name_window_unique",
            ),
        )
        assert bind2.committed
        projections2, _ = _single_projection(repository2, job_id)
        repository2.project_forecast_cohort_tasks(
            job_id,
            master_slurm_job_id="72001",
            projections=projections2,
            complete=True,
            master_status="succeeded",
            master_error_code=None,
            reconciliation_decision="matched_bound",
        )
        gfs_job_id2 = _reserve_gfs_sibling(repository2, created_at=_ANCHOR + timedelta(hours=1))
        query2 = _fallback_querier(
            monkeypatch,
            rows=_fallback_row("72001", submit="2026-07-12T04:00:00"),
            query_end=query_end,
        )
        outcome2 = reconcile_reserved_unbound_jobs(
            repository2, comment_query=query2, now=lambda: query_end
        )[0]
        assert outcome2.job_id == gfs_job_id2
        assert outcome2.action == "bound"
        assert outcome2.matched_slurm_job_id == "72001"
        assert repository2.get_pipeline_job(gfs_job_id2)[
            _SLURM_ACCOUNTING_SUBMITTED_AT_FIELD()
        ] == "2026-07-12T04:00:00Z"
        reopened2 = FileOrchestrationJournalRepository(repository2.root)
        assert reopened2.get_pipeline_job(job_id)["slurm_job_id"] == "72001"


def test_reconcile_fallback_persists_only_canonical_submit_never_legacy_duplicate(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """End-to-end reconcile fallback writes the parsed sacct Submit into the
    canonical field only; the legacy submitted_at is the commit-now default and
    never equals/pretends to be the sacct Submit."""

    from services.orchestrator.file_orchestration_journal import (
        FileOrchestrationJournalRepository,
    )
    from services.orchestrator.reconcile import reconcile_reserved_unbound_jobs

    with _pinned_local_timezone("UTC"):
        repository = _file_cohort_repository(
            tmp_path / "gfs",
            created_at=_ANCHOR,
            member_count=1,
            expected_user="scheduler",
            expected_account="account",
        )
        job_id = "job_cycle_gfs_2026071200_forecast_fixture_forecast"
        query_end = _ANCHOR + timedelta(hours=2)
        query = _fallback_querier(
            monkeypatch,
            rows=_fallback_row("72001", submit="2026-07-12T01:00:00"),
            query_end=query_end,
        )
        outcome = reconcile_reserved_unbound_jobs(
            repository, comment_query=query, now=lambda: query_end
        )[0]
        assert outcome.job_id == job_id
        assert outcome.action == "bound"
        bound = repository.get_pipeline_job(job_id)
        assert bound[_SLURM_ACCOUNTING_SUBMITTED_AT_FIELD()] == "2026-07-12T01:00:00Z"
        assert bound[_SLURM_BINDING_SOURCE_FIELD()] == "slurm_name_window_unique"
        # The legacy submitted_at is NOT the sacct Submit: the reconcile path
        # does not pass it, so it falls back to the commit-now default which is
        # AFTER the query-end (the fallback Submit), i.e. clearly distinct.
        assert bound["submitted_at"] != "2026-07-12T01:00:00Z"
        # Reopened journal agrees.
        reopened = FileOrchestrationJournalRepository(repository.root)
        assert reopened.get_pipeline_job(job_id)[
            _SLURM_ACCOUNTING_SUBMITTED_AT_FIELD()
        ] == "2026-07-12T01:00:00Z"


def test_ordinary_accepted_commit_writes_gateway_provenance(tmp_path: Any) -> None:
    """A legal ordinary accepted commit (decision None, no canonical input)
    records gateway_submit binding provenance and no canonical accounting
    Submit."""

    from services.orchestrator.accepted_submit_identity import AcceptedSubmitTransition

    with _pinned_local_timezone("UTC"):
        repository = _file_cohort_repository(
            tmp_path / "gfs",
            created_at=_ANCHOR,
            member_count=1,
            expected_user="scheduler",
            expected_account="account",
        )
        job_id = "job_cycle_gfs_2026071200_forecast_fixture_forecast"
        key = "cycle_gfs_2026071200_forecast_fixture:forecast"
        current = repository.query_candidate_state(key)
        bind = repository.commit_pipeline_job_submit_attempt(
            key,
            pipeline_job_id=str(current["job_id"]),
            expected_submission_attempt=1,
            slurm_job_id="72001",
            submitted_at=_ANCHOR + timedelta(minutes=30),
            transition=AcceptedSubmitTransition.accepted(status="submitted"),
        )
        assert bind.committed
        bound = repository.get_pipeline_job(job_id)
        assert bound[_SLURM_BINDING_SOURCE_FIELD()] == "gateway_submit"
        assert bound[_SLURM_ACCOUNTING_SUBMITTED_AT_FIELD()] is None
        # The live gateway submit carries no accounting reconciliation tuple.
        assert bound["reconciliation_source"] is None
        assert bound["reconciliation_decision"] is None
        assert bound["submit_outcome"] == "accepted"


# ---------------------------------------------------------------------------
# #1850 round 3: conflicting idempotent replay and durable matched-bound
# source/provenance consistency (single canonical authority).
# ---------------------------------------------------------------------------


def test_conflicting_fallback_replay_is_noncommitted_and_durable_unchanged(
    tmp_path: Any,
) -> None:
    """Same reservation/attempt/id/tuple replayed with a DIFFERENT canonical
    Submit must be non-committed (``stale``), zero-write, and leave the durable
    canonical unchanged. Only the exact same (id, canonical Submit, source)
    replay may be ``idempotent``."""

    from services.orchestrator.accepted_submit_identity import AcceptedSubmitTransition
    from services.orchestrator.file_orchestration_journal import (
        FileOrchestrationJournalRepository,
    )

    with _pinned_local_timezone("UTC"):
        repository = _file_cohort_repository(
            tmp_path / "gfs",
            created_at=_ANCHOR,
            member_count=1,
            expected_user="scheduler",
            expected_account="account",
        )
        job_id = "job_cycle_gfs_2026071200_forecast_fixture_forecast"
        key = "cycle_gfs_2026071200_forecast_fixture:forecast"
        current = repository.query_candidate_state(key)
        pid = str(current["job_id"])

        def fallback(canonical: datetime) -> Any:
            return repository.commit_pipeline_job_submit_attempt(
                key,
                pipeline_job_id=pid,
                expected_submission_attempt=1,
                slurm_job_id="72001",
                slurm_accounting_submitted_at=canonical,
                transition=AcceptedSubmitTransition.accounting(
                    "matched_bound",
                    submit_outcome="accepted",
                    matched_slurm_job_id="72001",
                    status="submitted",
                    reconciliation_source="slurm_name_window_unique",
                ),
            )

        first = fallback(_ANCHOR + timedelta(hours=1))
        assert first.outcome == "applied"
        assert first.wrote
        assert repository.get_pipeline_job(job_id)[
            _SLURM_ACCOUNTING_SUBMITTED_AT_FIELD()
        ] == "2026-07-12T01:00:00Z"

        # Conflicting replay: same tuple, different canonical Submit.
        conflicting = fallback(_ANCHOR + timedelta(hours=2))
        assert not conflicting.committed
        assert not conflicting.wrote
        assert conflicting.outcome == "stale"
        assert repository.get_pipeline_job(job_id)[
            _SLURM_ACCOUNTING_SUBMITTED_AT_FIELD()
        ] == "2026-07-12T01:00:00Z"
        reopened = FileOrchestrationJournalRepository(repository.root)
        assert reopened.get_pipeline_job(job_id)[
            _SLURM_ACCOUNTING_SUBMITTED_AT_FIELD()
        ] == "2026-07-12T01:00:00Z"

        # Exact same replay: idempotent, zero-write.
        same = fallback(_ANCHOR + timedelta(hours=1))
        assert same.committed
        assert not same.wrote
        assert same.outcome == "idempotent"


def test_ordinary_and_exact_comment_same_lane_replays_are_idempotent_and_cross_lane_is_not(
    tmp_path: Any,
) -> None:
    """Ordinary and exact-comment same-lane replays stay idempotent; a
    cross-lane replay of the same id/tuple is never mistaken for idempotent."""

    from services.orchestrator.accepted_submit_identity import AcceptedSubmitTransition
    from services.orchestrator.file_orchestration_journal import (
        FileOrchestrationJournalRepository,
    )

    with _pinned_local_timezone("UTC"):
        repository = _file_cohort_repository(
            tmp_path / "gfs",
            created_at=_ANCHOR,
            member_count=1,
            expected_user="scheduler",
            expected_account="account",
        )
        job_id = "job_cycle_gfs_2026071200_forecast_fixture_forecast"
        key = "cycle_gfs_2026071200_forecast_fixture:forecast"
        current = repository.query_candidate_state(key)
        pid = str(current["job_id"])

        ordinary = AcceptedSubmitTransition.accepted(status="submitted")
        exact = AcceptedSubmitTransition.accounting(
            "matched_bound",
            submit_outcome="accepted",
            matched_slurm_job_id="72001",
            status="submitted",
            reconciliation_source="slurm_exact_comment",
        )

        first = repository.commit_pipeline_job_submit_attempt(
            key,
            pipeline_job_id=pid,
            expected_submission_attempt=1,
            slurm_job_id="72001",
            submitted_at=_ANCHOR + timedelta(minutes=30),
            transition=ordinary,
        )
        assert first.outcome == "applied"
        assert repository.get_pipeline_job(job_id)[_SLURM_BINDING_SOURCE_FIELD()] == "gateway_submit"

        # Same ordinary lane replay: idempotent.
        same = repository.commit_pipeline_job_submit_attempt(
            key,
            pipeline_job_id=pid,
            expected_submission_attempt=1,
            slurm_job_id="72001",
            submitted_at=_ANCHOR + timedelta(minutes=30),
            transition=ordinary,
        )
        assert same.outcome == "idempotent"
        assert not same.wrote

        # Cross-lane replay: exact-comment matched on the gateway-bound row is
        # NOT idempotent (different bind lane / derived binding source).
        cross = repository.commit_pipeline_job_submit_attempt(
            key,
            pipeline_job_id=pid,
            expected_submission_attempt=1,
            slurm_job_id="72001",
            submitted_at=_ANCHOR + timedelta(minutes=30),
            transition=exact,
        )
        assert not cross.committed
        assert not cross.wrote
        assert cross.outcome == "stale"
        assert repository.get_pipeline_job(job_id)[_SLURM_BINDING_SOURCE_FIELD()] == "gateway_submit"
        reopened = FileOrchestrationJournalRepository(repository.root)
        assert reopened.get_pipeline_job(job_id)[_SLURM_BINDING_SOURCE_FIELD()] == "gateway_submit"

        # Exact-comment lane on a fresh row: applied then same-lane idempotent.
        repository2 = _file_cohort_repository(
            tmp_path / "ifs",
            created_at=_ANCHOR,
            member_count=1,
            expected_user="scheduler",
            expected_account="account",
            source_id="ifs",
        )
        job_id2 = "job_cycle_ifs_2026071200_forecast_fixture_forecast"
        key2 = "cycle_ifs_2026071200_forecast_fixture:forecast"
        current2 = repository2.query_candidate_state(key2)
        pid2 = str(current2["job_id"])
        applied2 = repository2.commit_pipeline_job_submit_attempt(
            key2,
            pipeline_job_id=pid2,
            expected_submission_attempt=1,
            slurm_job_id="72001",
            submitted_at=_ANCHOR + timedelta(minutes=30),
            transition=exact,
        )
        assert applied2.outcome == "applied"
        assert repository2.get_pipeline_job(job_id2)[_SLURM_BINDING_SOURCE_FIELD()] == "slurm_exact_comment"
        replay2 = repository2.commit_pipeline_job_submit_attempt(
            key2,
            pipeline_job_id=pid2,
            expected_submission_attempt=1,
            slurm_job_id="72001",
            submitted_at=_ANCHOR + timedelta(minutes=30),
            transition=exact,
        )
        assert replay2.outcome == "idempotent"
        assert not replay2.wrote


def test_matched_bound_source_provenance_mismatch_rows_rejected() -> None:
    """A durable matched_bound row whose current reconciliation source
    contradicts its immutable binding provenance is corrupt evidence (design
    D3): the current source must equal the legal restoration from binding
    provenance."""

    from services.orchestrator.accepted_submit_identity import (
        AcceptedSubmitEvidenceError,
        normalize_accepted_submit_evidence,
    )

    # name-window current source + gateway binding source -> reject
    with pytest.raises(AcceptedSubmitEvidenceError) as error:
        normalize_accepted_submit_evidence(
            {
                **_versioned_master_reservation_record(member_count=1),
                "status": "submitted",
                "submit_outcome": "accepted",
                "slurm_job_id": "72001",
                "reconciliation_source": "slurm_name_window_unique",
                "reconciliation_decision": "matched_bound",
                "matched_slurm_job_id": "72001",
                _SLURM_BINDING_SOURCE_FIELD(): "gateway_submit",
                _SLURM_ACCOUNTING_SUBMITTED_AT_FIELD(): None,
            }
        )
    assert error.value.field == "reconciliation_source"

    # exact-comment current source + name-window binding + canonical -> reject
    with pytest.raises(AcceptedSubmitEvidenceError) as error:
        normalize_accepted_submit_evidence(
            {
                **_versioned_master_reservation_record(member_count=1),
                "status": "submitted",
                "submit_outcome": "accepted",
                "slurm_job_id": "72001",
                "reconciliation_source": "slurm_exact_comment",
                "reconciliation_decision": "matched_bound",
                "matched_slurm_job_id": "72001",
                _SLURM_BINDING_SOURCE_FIELD(): "slurm_name_window_unique",
                _SLURM_ACCOUNTING_SUBMITTED_AT_FIELD(): "2026-07-12T01:00:00Z",
            }
        )
    assert error.value.field == "reconciliation_source"


def test_fallback_defer_held_tuple_remains_readable_and_later_restores(
    tmp_path: Any,
) -> None:
    """A fallback-bound row parked at accounting_unavailable keeps current
    source exact-comment with immutable name-window binding provenance; the
    later matched_bound projection restores the name-window current source."""

    from services.orchestrator.accepted_submit_identity import (
        normalize_accepted_submit_evidence,
    )

    with _pinned_local_timezone("UTC"):
        repository = _file_cohort_repository(
            tmp_path / "gfs",
            created_at=_ANCHOR,
            member_count=1,
            expected_user="scheduler",
            expected_account="account",
        )
        job_id = "job_cycle_gfs_2026071200_forecast_fixture_forecast"
        key = "cycle_gfs_2026071200_forecast_fixture:forecast"
        bind = _commit_name_window_fallback_bind(
            repository, key, slurm_job_id="72001", canonical_submit=_ANCHOR + timedelta(hours=1)
        )
        assert bind.committed

        # Defer (incomplete coverage) parks the row on the held tuple.
        projections, _complete = _incomplete_projection(repository, job_id)
        repository.project_forecast_cohort_tasks(
            job_id,
            master_slurm_job_id="72001",
            projections=projections,
            complete=False,
            master_status="succeeded",
            master_error_code=None,
            reconciliation_decision="matched_bound",
        )
        deferred = repository.get_pipeline_job(job_id)
        assert deferred["status"] == "reconcile_unverified"
        assert deferred["reconciliation_decision"] == "accounting_unavailable"
        assert deferred["reconciliation_source"] == "slurm_exact_comment"
        assert deferred[_SLURM_BINDING_SOURCE_FIELD()] == "slurm_name_window_unique"
        assert deferred[_SLURM_ACCOUNTING_SUBMITTED_AT_FIELD()] == "2026-07-12T01:00:00Z"
        # The held tuple is readable by the durable normalizer.
        normalized = normalize_accepted_submit_evidence(deferred)
        assert normalized["reconciliation_source"] == "slurm_exact_comment"
        assert normalized[_SLURM_BINDING_SOURCE_FIELD()] == "slurm_name_window_unique"

        # Later complete projection restores the name-window current source.
        full, _complete = _single_projection(repository, job_id)
        repository.project_forecast_cohort_tasks(
            job_id,
            master_slurm_job_id="72001",
            projections=full,
            complete=True,
            master_status="succeeded",
            master_error_code=None,
            reconciliation_decision="matched_bound",
        )
        terminal = repository.get_pipeline_job(job_id)
        assert terminal["status"] == "succeeded"
        assert terminal["reconciliation_source"] == "slurm_name_window_unique"
        assert terminal[_SLURM_BINDING_SOURCE_FIELD()] == "slurm_name_window_unique"
        assert terminal[_SLURM_ACCOUNTING_SUBMITTED_AT_FIELD()] == "2026-07-12T01:00:00Z"


# ---------------------------------------------------------------------------
# #1850 round 4: typed-boundary closure.  Fix A refuses non-bind commit
# shapes; Fix B closes the durable normalizer on bound identity; Fix C keeps
# legacy v1 replays idempotent without minting provenance.
# ---------------------------------------------------------------------------


def _commit_illegal_bind_shape_rejected(
    repository: Any,
    key: str,
    pid: str,
    *,
    decision: str,
    status: str,
    reason_class: str | None = None,
    identity_blocked_streak: int = 0,
) -> None:
    """Drive the typed commit with an ``accepted`` transition that carries an
    accounting decision but is NOT one of the three legal bind lanes. Such a
    transition derives ``binding_source=None`` (Fix A): the commit must refuse
    it BEFORE any mutation or occupancy scan, leaving the reservation and the
    journal zero-changed. The transition itself is construction-legal (an
    accepted-submit with a valid accounting decision/status/reason tuple), so
    this is genuinely a commit-API boundary, not a transition-class refusal."""

    from services.orchestrator.accepted_submit_identity import AcceptedSubmitTransition
    from services.orchestrator.file_orchestration_journal import (
        FileOrchestrationJournalError,
        FileOrchestrationJournalRepository,
    )

    transition = AcceptedSubmitTransition.accounting(
        decision,
        submit_outcome="accepted",
        reconciliation_reason_class=reason_class,
        status=status,
        identity_blocked_streak=identity_blocked_streak,
    )
    journal_path = repository.root / "journal" / "gfs" / "2026071200.jsonl"
    before_bytes = journal_path.read_bytes() if journal_path.exists() else b""
    with pytest.raises(FileOrchestrationJournalError) as error:
        repository.commit_pipeline_job_submit_attempt(
            key,
            pipeline_job_id=pid,
            expected_submission_attempt=1,
            slurm_job_id="72001",
            submitted_at=_ANCHOR + timedelta(minutes=30),
            transition=transition,
        )
    assert error.value.reason == "file_journal_authority_transition_requires_typed_api"
    assert error.value.field == "transition"
    assert not repository.get_pipeline_job(pid)["slurm_job_id"], (
        "an illegal bind shape must never bind the numeric id"
    )
    assert repository.get_pipeline_job(pid)[_SLURM_BINDING_SOURCE_FIELD()] is None
    assert repository.get_pipeline_job(pid)[_SLURM_ACCOUNTING_SUBMITTED_AT_FIELD()] is None
    after_bytes = journal_path.read_bytes() if journal_path.exists() else b""
    assert after_bytes == before_bytes, "an illegal bind shape must not write journal bytes"
    reopened = FileOrchestrationJournalRepository(repository.root)
    assert reopened.get_pipeline_job(pid)[_SLURM_BINDING_SOURCE_FIELD()] is None
    assert reopened.get_pipeline_job(pid)["slurm_job_id"] is None


def test_illegal_accepted_accounting_decision_commits_all_rejected_zero_write(
    tmp_path: Any,
) -> None:
    """Fix A: the typed commit accepts ONLY the three legal bind shapes. An
    ``accepted`` transition carrying a held/defer/blocked accounting decision
    (accounting_unavailable, identity_mismatch_blocked, absence_deferred) is not
    a bind -- it must be refused with the stable authority error, never bind the
    numeric id, never mint ``binding_source=None`` provenance, and leave the
    journal bytes zero-changed (no mutation, no occupancy scan)."""

    with _pinned_local_timezone("UTC"):
        repository = _file_cohort_repository(
            tmp_path / "gfs",
            created_at=_ANCHOR,
            member_count=1,
            expected_user="scheduler",
            expected_account="account",
        )
        key = "cycle_gfs_2026071200_forecast_fixture:forecast"
        pid = str(repository.query_candidate_state(key)["job_id"])
        for decision, status, reason_class in (
            ("accounting_unavailable", "submitted", "comment_accounting_unproven"),
            ("identity_mismatch_blocked", "submitted", None),
            ("absence_deferred", "submitted", None),
        ):
            _commit_illegal_bind_shape_rejected(
                repository,
                key,
                pid,
                decision=decision,
                status=status,
                reason_class=reason_class,
            )


def test_legal_three_bind_lanes_still_commit_after_illegal_shape_refused(
    tmp_path: Any,
) -> None:
    """The three legal bind lanes (ordinary gateway, exact-comment matched,
    name-window matched fallback) still commit and mint their derived binding
    provenance after an illegal accepted accounting-decision shape was refused
    on the same reservation (fail-closed refusal does not wedge the attempt)."""

    from services.orchestrator.accepted_submit_identity import AcceptedSubmitTransition
    from services.orchestrator.file_orchestration_journal import (
        FileOrchestrationJournalError,
    )

    with _pinned_local_timezone("UTC"):
        repository = _file_cohort_repository(
            tmp_path / "gfs",
            created_at=_ANCHOR,
            member_count=1,
            expected_user="scheduler",
            expected_account="account",
        )
        key = "cycle_gfs_2026071200_forecast_fixture:forecast"
        pid = str(repository.query_candidate_state(key)["job_id"])
        # Illegal shape first: refused, reservation untouched.
        with pytest.raises(FileOrchestrationJournalError):
            repository.commit_pipeline_job_submit_attempt(
                key,
                pipeline_job_id=pid,
                expected_submission_attempt=1,
                slurm_job_id="72001",
                submitted_at=_ANCHOR + timedelta(minutes=30),
                transition=AcceptedSubmitTransition.accounting(
                    "accounting_unavailable",
                    submit_outcome="accepted",
                    reconciliation_reason_class="comment_accounting_unproven",
                    status="submitted",
                ),
            )
        assert repository.get_pipeline_job(pid)["slurm_job_id"] is None

        # Ordinary gateway lane: applied, gateway provenance.
        ordinary = repository.commit_pipeline_job_submit_attempt(
            key,
            pipeline_job_id=pid,
            expected_submission_attempt=1,
            slurm_job_id="72001",
            submitted_at=_ANCHOR + timedelta(minutes=30),
            transition=AcceptedSubmitTransition.accepted(status="submitted"),
        )
        assert ordinary.outcome == "applied"
        assert repository.get_pipeline_job(pid)[_SLURM_BINDING_SOURCE_FIELD()] == "gateway_submit"

        # Exact-comment matched lane on a fresh row (separate root so the
        # shared journal root stays single-owner per test).
        repository2 = _file_cohort_repository(
            tmp_path / "ifs",
            created_at=_ANCHOR,
            member_count=1,
            expected_user="scheduler",
            expected_account="account",
            source_id="ifs",
        )
        key2 = "cycle_ifs_2026071200_forecast_fixture:forecast"
        pid2 = str(repository2.query_candidate_state(key2)["job_id"])
        exact = repository2.commit_pipeline_job_submit_attempt(
            key2,
            pipeline_job_id=pid2,
            expected_submission_attempt=1,
            slurm_job_id="72001",
            submitted_at=_ANCHOR + timedelta(minutes=30),
            transition=AcceptedSubmitTransition.accounting(
                "matched_bound",
                submit_outcome="accepted",
                matched_slurm_job_id="72001",
                status="submitted",
                reconciliation_source="slurm_exact_comment",
            ),
        )
        assert exact.outcome == "applied"
        assert repository2.get_pipeline_job(pid2)[_SLURM_BINDING_SOURCE_FIELD()] == "slurm_exact_comment"

        # Name-window matched fallback lane (separate root again).
        repository3 = _file_cohort_repository(
            tmp_path / "era5",
            created_at=_ANCHOR,
            member_count=1,
            expected_user="scheduler",
            expected_account="account",
            source_id="era5",
        )
        key3 = "cycle_era5_2026071200_forecast_fixture:forecast"
        pid3 = str(repository3.query_candidate_state(key3)["job_id"])
        fallback = _commit_name_window_fallback_bind(
            repository3, key3, slurm_job_id="72001", canonical_submit=_ANCHOR + timedelta(hours=1)
        )
        assert fallback.committed
        assert repository3.get_pipeline_job(pid3)[_SLURM_BINDING_SOURCE_FIELD()] == "slurm_name_window_unique"
        assert repository3.get_pipeline_job(pid3)[
            _SLURM_ACCOUNTING_SUBMITTED_AT_FIELD()
        ] == "2026-07-12T01:00:00Z"


def test_normalizer_rejects_explicit_provenance_on_non_accepted_outcomes() -> None:
    """Fix B: explicit binding provenance is legal ONLY on an ``accepted``
    submit outcome. A durable row that is ``rejected`` or
    ``submit_result_ambiguous`` yet carries a numeric bound id AND explicit
    provenance would be a bind-shaped non-bind -- corrupt evidence, rejected
    by the durable normalizer (closed world on bound identity)."""

    from services.orchestrator.accepted_submit_identity import (
        AcceptedSubmitEvidenceError,
        normalize_accepted_submit_evidence,
    )

    base = {
        **_versioned_master_reservation_record(member_count=1),
        "status": "submitted",
        "slurm_job_id": "72001",
    }
    for outcome, decision, source in (
        ("rejected", None, "gateway_submit"),
        ("submit_result_ambiguous", None, "gateway_submit"),
        ("rejected", "matched_bound", "slurm_exact_comment"),
        ("submit_result_ambiguous", "matched_bound", "slurm_exact_comment"),
    ):
        row = dict(base)
        row["submit_outcome"] = outcome
        if decision is None:
            row["reconciliation_source"] = None
            row["reconciliation_decision"] = None
            row["matched_slurm_job_id"] = None
            row[_SLURM_BINDING_SOURCE_FIELD()] = source
            row[_SLURM_ACCOUNTING_SUBMITTED_AT_FIELD()] = None
        else:
            row["reconciliation_source"] = source
            row["reconciliation_decision"] = decision
            row["matched_slurm_job_id"] = "72001"
            row[_SLURM_BINDING_SOURCE_FIELD()] = "slurm_exact_comment"
            row[_SLURM_ACCOUNTING_SUBMITTED_AT_FIELD()] = None
        with pytest.raises(AcceptedSubmitEvidenceError) as error:
            normalize_accepted_submit_evidence(row)
        assert error.value.field == "submit_outcome", f"{outcome}/{decision}"


def test_normalizer_rejects_matched_bound_mismatched_matched_id() -> None:
    """Fix B: a ``matched_bound`` row must be internally consistent -- its
    matched Slurm id must equal the bound numeric ``slurm_job_id``. A matched
    id that contradicts the owned id is corrupt evidence and must be rejected
    even when the source/provenance pairing is otherwise legal."""

    from services.orchestrator.accepted_submit_identity import (
        AcceptedSubmitEvidenceError,
        normalize_accepted_submit_evidence,
    )

    # exact-comment matched row, matched id contradicts bound id.
    with pytest.raises(AcceptedSubmitEvidenceError) as error:
        normalize_accepted_submit_evidence(
            {
                **_versioned_master_reservation_record(member_count=1),
                "status": "submitted",
                "submit_outcome": "accepted",
                "slurm_job_id": "72001",
                "reconciliation_source": "slurm_exact_comment",
                "reconciliation_decision": "matched_bound",
                "matched_slurm_job_id": "72002",
                _SLURM_BINDING_SOURCE_FIELD(): "slurm_exact_comment",
                _SLURM_ACCOUNTING_SUBMITTED_AT_FIELD(): None,
            }
        )
    assert error.value.field == "matched_slurm_job_id"

    # name-window matched row, matched id contradicts bound id (even with the
    # canonical Submit present).
    with pytest.raises(AcceptedSubmitEvidenceError) as error:
        normalize_accepted_submit_evidence(
            {
                **_versioned_master_reservation_record(member_count=1),
                "status": "submitted",
                "submit_outcome": "accepted",
                "slurm_job_id": "72001",
                "reconciliation_source": "slurm_name_window_unique",
                "reconciliation_decision": "matched_bound",
                "matched_slurm_job_id": "72002",
                _SLURM_BINDING_SOURCE_FIELD(): "slurm_name_window_unique",
                _SLURM_ACCOUNTING_SUBMITTED_AT_FIELD(): "2026-07-12T01:00:00Z",
            }
        )
    assert error.value.field == "matched_slurm_job_id"

    # A legal matched-bound row with consistent ids still normalizes.
    normalized = normalize_accepted_submit_evidence(
        {
            **_versioned_master_reservation_record(member_count=1),
            "status": "submitted",
            "submit_outcome": "accepted",
            "slurm_job_id": "72001",
            "reconciliation_source": "slurm_exact_comment",
            "reconciliation_decision": "matched_bound",
            "matched_slurm_job_id": "72001",
            _SLURM_BINDING_SOURCE_FIELD(): "slurm_exact_comment",
            _SLURM_ACCOUNTING_SUBMITTED_AT_FIELD(): None,
        }
    )
    assert normalized["matched_slurm_job_id"] == "72001"


def _legacy_v1_bound_row(
    *,
    source: str,
    decision: str | None,
    matched: str | None,
    slurm_job_id: str,
    source_id: str = "gfs",
) -> dict[str, Any]:
    """A pre-#1850 v1 bound row: bound id and accounting tuple WITHOUT either
    additive binding-provenance field (what a v1 writer legitimately produced).
    The two provenance keys are omitted entirely, so the durable row stays
    readable and the replay must NOT backfill/mint them."""
    row = {
        **_versioned_master_reservation_record(member_count=1, source_id=source_id),
        "status": "succeeded",
        "submit_outcome": "accepted",
        "reconciliation_source": source,
        "reconciliation_decision": decision,
        "matched_slurm_job_id": matched,
        "slurm_job_id": slurm_job_id,
        "submitted_at": "2026-07-12T01:00:00.123456Z",
    }
    row.pop(_SLURM_BINDING_SOURCE_FIELD(), None)
    row.pop(_SLURM_ACCOUNTING_SUBMITTED_AT_FIELD(), None)
    return row


def _write_master_row(repository: Any, row: dict[str, Any]) -> None:
    """Append a raw v1-style master row into the journal (the authoritative
    surface the typed commit replays), then assert it decodes as a current
    bound master. A legacy v1 writer produced exactly this shape: bound id and
    accounting tuple with NEITHER additive provenance field."""
    from services.orchestrator.accepted_submit_identity import (
        accepted_submit_contract_is_current,
        accepted_submit_row_kind,
    )
    from services.orchestrator.file_orchestration_journal import (
        _cycle_time_from_job,
        _source_id_from_job,
    )

    job_id = str(row["job_id"])
    repository._append_validated_record_unlocked(
        "pipeline_job",
        row,
        source_id=_source_id_from_job(row),
        cycle_time=_cycle_time_from_job(row),
        model_id=None,
    )
    decoded = repository._pipeline_job_for_id_unlocked(job_id)
    assert decoded is not None
    assert accepted_submit_row_kind(decoded) == "master"
    assert accepted_submit_contract_is_current(decoded)


def _snapshot_row(repository: Any, job_id: str) -> dict[str, Any]:
    """The authoritative journal-replayed master row (what the typed commit
    reads), used to prove a replay is byte-for-byte zero-write."""
    decoded = repository._pipeline_job_for_id_unlocked(str(job_id))
    assert decoded is not None
    return dict(decoded)


def test_legacy_v1_same_lane_replay_idempotent_no_backfill(tmp_path: Any) -> None:
    """Fix C: a pre-#1850 v1 row (both additive provenance fields absent)
    replayed same-lane with the same id/tuple must be ``idempotent`` and
    zero-write -- and must NOT be backfilled/minted with provenance by the
    replay (read-compatible, the replay never rewrites historical authority).
    Covered for BOTH the ordinary (gateway_submit) lane and the exact-comment
    matched lane. Cross-lane or a name-window replay on the missing-fields row
    is never idempotent."""

    from services.orchestrator.accepted_submit_identity import AcceptedSubmitTransition
    from services.orchestrator.file_orchestration_journal import (
        FileOrchestrationJournalRepository,
    )

    with _pinned_local_timezone("UTC"):
        repository = _file_cohort_repository(
            tmp_path / "gfs",
            created_at=_ANCHOR,
            member_count=1,
            expected_user="scheduler",
            expected_account="account",
        )
        job_id = "job_cycle_gfs_2026071200_forecast_fixture_forecast"
        key = "cycle_gfs_2026071200_forecast_fixture:forecast"

        # --- Ordinary lane: v1 row bound as gateway submit. ---
        _write_master_row(
            repository,
            _legacy_v1_bound_row(
                source=None,
                decision=None,
                matched=None,
                slurm_job_id="72001",
            ),
        )
        assert repository.get_pipeline_job(job_id)["slurm_job_id"] == "72001"
        assert repository.get_pipeline_job(job_id).get(_SLURM_BINDING_SOURCE_FIELD()) is None
        before = _snapshot_row(repository, job_id)

        replay = repository.commit_pipeline_job_submit_attempt(
            key,
            pipeline_job_id=job_id,
            expected_submission_attempt=1,
            slurm_job_id="72001",
            submitted_at=_ANCHOR + timedelta(minutes=30),
            transition=AcceptedSubmitTransition.accepted(status="submitted"),
        )
        assert replay.committed
        assert replay.outcome == "idempotent"
        assert not replay.wrote
        assert _snapshot_row(repository, job_id) == before, (
            "replay must be zero-write and must NOT backfill/mint provenance"
        )
        reopened = FileOrchestrationJournalRepository(repository.root)
        assert reopened.get_pipeline_job(job_id).get(_SLURM_BINDING_SOURCE_FIELD()) is None

        # --- Exact-comment lane: v1 row bound with matched tuple. ---
        repository2 = _file_cohort_repository(
            tmp_path / "ifs",
            created_at=_ANCHOR,
            member_count=1,
            expected_user="scheduler",
            expected_account="account",
            source_id="ifs",
        )
        job_id2 = "job_cycle_ifs_2026071200_forecast_fixture_forecast"
        key2 = "cycle_ifs_2026071200_forecast_fixture:forecast"
        _write_master_row(
            repository2,
            _legacy_v1_bound_row(
                source="slurm_exact_comment",
                decision="matched_bound",
                matched="72001",
                slurm_job_id="72001",
                source_id="ifs",
            ),
        )
        before2 = _snapshot_row(repository2, job_id2)
        replay2 = repository2.commit_pipeline_job_submit_attempt(
            key2,
            pipeline_job_id=job_id2,
            expected_submission_attempt=1,
            slurm_job_id="72001",
            submitted_at=_ANCHOR + timedelta(minutes=30),
            transition=AcceptedSubmitTransition.accounting(
                "matched_bound",
                submit_outcome="accepted",
                matched_slurm_job_id="72001",
                status="submitted",
                reconciliation_source="slurm_exact_comment",
            ),
        )
        assert replay2.outcome == "idempotent"
        assert not replay2.wrote
        assert _snapshot_row(repository2, job_id2) == before2


def test_legacy_v1_partial_provenance_and_contradiction_not_idempotent(
    tmp_path: Any,
) -> None:
    """Fix C edge closure: a legacy row with only ONE of the two additive
    fields present, or a replay that explicitly contradicts the durable
    provenance/canonical, must NOT be idempotent (stale, zero-write). The
    missing-both-fields compatibility applies ONLY to a fully-legacy row."""

    from services.orchestrator.accepted_submit_identity import AcceptedSubmitTransition
    from services.orchestrator.file_orchestration_journal import (
        FileOrchestrationJournalRepository,
    )

    with _pinned_local_timezone("UTC"):
        repository = _file_cohort_repository(
            tmp_path / "gfs",
            created_at=_ANCHOR,
            member_count=1,
            expected_user="scheduler",
            expected_account="account",
        )
        job_id = "job_cycle_gfs_2026071200_forecast_fixture_forecast"
        key = "cycle_gfs_2026071200_forecast_fixture:forecast"

        # Single-field-only legacy-ish row: binding source present, canonical
        # absent. Not a full v1 shape; never idempotent.
        partial = _legacy_v1_bound_row(
            source=None,
            decision=None,
            matched=None,
            slurm_job_id="72001",
        )
        partial[_SLURM_BINDING_SOURCE_FIELD()] = "gateway_submit"
        _write_master_row(repository, partial)
        replay = repository.commit_pipeline_job_submit_attempt(
            key,
            pipeline_job_id=job_id,
            expected_submission_attempt=1,
            slurm_job_id="72001",
            submitted_at=_ANCHOR + timedelta(minutes=30),
            transition=AcceptedSubmitTransition.accepted(status="submitted"),
        )
        assert replay.outcome == "stale"
        assert not replay.wrote
        assert repository.get_pipeline_job(job_id)[_SLURM_BINDING_SOURCE_FIELD()] == "gateway_submit"
        reopened = FileOrchestrationJournalRepository(repository.root)
        assert reopened.get_pipeline_job(job_id)[_SLURM_BINDING_SOURCE_FIELD()] == "gateway_submit"


def test_legacy_v1_cross_lane_replay_not_idempotent(tmp_path: Any) -> None:
    """Fix C lane closure: a fully-legacy missing-both-fields row is idempotent
    only for its OWN legal lane (ordinary or exact-comment). A cross-lane
    replay (exact-comment matched on the legacy ordinary row) and a name-window
    replay on the missing-fields row are never idempotent -- name-window
    provenance is this PR's addition and a missing-fields row cannot be a legal
    fallback bind."""

    from services.orchestrator.accepted_submit_identity import AcceptedSubmitTransition
    from services.orchestrator.file_orchestration_journal import (
        FileOrchestrationJournalRepository,
    )

    with _pinned_local_timezone("UTC"):
        repository = _file_cohort_repository(
            tmp_path / "gfs",
            created_at=_ANCHOR,
            member_count=1,
            expected_user="scheduler",
            expected_account="account",
        )
        job_id = "job_cycle_gfs_2026071200_forecast_fixture_forecast"
        key = "cycle_gfs_2026071200_forecast_fixture:forecast"
        _write_master_row(
            repository,
            _legacy_v1_bound_row(
                source=None,
                decision=None,
                matched=None,
                slurm_job_id="72001",
            ),
        )

        # Cross-lane: exact-comment matched on the legacy ordinary row.
        cross = repository.commit_pipeline_job_submit_attempt(
            key,
            pipeline_job_id=job_id,
            expected_submission_attempt=1,
            slurm_job_id="72001",
            submitted_at=_ANCHOR + timedelta(minutes=30),
            transition=AcceptedSubmitTransition.accounting(
                "matched_bound",
                submit_outcome="accepted",
                matched_slurm_job_id="72001",
                status="submitted",
                reconciliation_source="slurm_exact_comment",
            ),
        )
        assert cross.outcome == "stale"
        assert not cross.wrote
        assert repository.get_pipeline_job(job_id).get(_SLURM_BINDING_SOURCE_FIELD()) is None

        # Name-window fallback replay on the missing-fields row: never
        # idempotent -- name-window provenance is this PR's addition and a
        # missing-fields row cannot be a legal fallback bind, so the replay
        # resolves stale, zero-write, and mints nothing.
        name_window = repository.commit_pipeline_job_submit_attempt(
            key,
            pipeline_job_id=job_id,
            expected_submission_attempt=1,
            slurm_job_id="72001",
            slurm_accounting_submitted_at=_ANCHOR + timedelta(hours=1),
            transition=AcceptedSubmitTransition.accounting(
                "matched_bound",
                submit_outcome="accepted",
                matched_slurm_job_id="72001",
                status="submitted",
                reconciliation_source="slurm_name_window_unique",
            ),
        )
        assert name_window.outcome == "stale"
        assert not name_window.wrote
        assert repository.get_pipeline_job(job_id).get(_SLURM_BINDING_SOURCE_FIELD()) is None
        reopened = FileOrchestrationJournalRepository(repository.root)
        assert reopened.get_pipeline_job(job_id).get(_SLURM_BINDING_SOURCE_FIELD()) is None
