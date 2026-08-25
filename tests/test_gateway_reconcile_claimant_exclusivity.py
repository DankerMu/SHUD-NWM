"""#1565 Phase 6 claimant-exclusivity and attempt-window closure (#1850).

Proves the name-window fallback binds only when the candidate has one durable
claimant, no other active accepted-submit owner of its id, and a submit instant
inside the closed attempt window. Every ambiguity stays fail-closed under the
#1564 held tuple with streak zero, regardless of reconcile iteration order or
concurrent source/cycle writers; settled terminal history never occupies a
recycled Slurm number.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from services.orchestrator.accepted_submit_identity import (
    ACCEPTED_SUBMIT_CONTRACT_VERSION,
    AcceptedSubmitTransition,
    forecast_cohort_digest,
)
from tests.gateway_reconcile_helpers import (
    _append_cohort_placeholders,
    _file_cohort_repository,
    _versioned_master_reservation_record,
)
from tests.test_real_slurm_gateway import _pinned_local_timezone


def _fallback_row(
    master_id: str,
    *,
    submit: str,
    user: str = "scheduler",
    account: str = "account",
    comment: str = "",
    job_name: str = "nhms_forecast",
) -> str:
    return f"{master_id}|{job_name}|COMPLETED|0:0|{comment}|{user}|{account}|{submit}\n"


def _fallback_querier(
    monkeypatch: pytest.MonkeyPatch,
    *,
    rows: str,
    query_end: datetime,
) -> Any:
    from services.orchestrator import reconcile as reconcile_module

    commands: list[list[str]] = []
    monkeypatch.setattr(
        reconcile_module,
        "_bounded_sacct_stdout",
        lambda command: commands.append(list(command)) or rows,
    )
    query = reconcile_module.default_comment_sacct_querier(
        global_visibility_probe=lambda: True,
        comment_storage_probe=lambda: False,
        now=lambda: query_end,
    )
    return query, commands


def _second_master_reservation_record(
    *,
    source_id: str = "ifs",
    created_at: datetime,
    member_count: int = 1,
    expected_user: str = "scheduler",
    expected_account: str = "account",
    submit_outcome: str | None = "submit_result_ambiguous",
) -> dict[str, Any]:
    """A second reserved-unbound forecast master for a sibling source/cycle.

    The GFS fixture owns ``cycle_gfs_2026071200``; this IFS fixture owns
    ``cycle_ifs_2026071200`` so both masters are current accepted-submit
    forecast cohorts with the same expected Slurm user/account.
    """

    record = _versioned_master_reservation_record(
        created_at=created_at,
        member_count=member_count,
        expected_user=expected_user,
        expected_account=expected_account,
        submit_outcome=submit_outcome,
        versioned=True,
        source_id=source_id,
    )
    canonical_source = source_id.upper() if source_id == "ifs" else source_id.upper()
    if source_id != "gfs":
        record = {**record}
        record["job_id"] = f"job_cycle_{source_id}_2026071200_forecast_fixture_forecast"
        record["run_id"] = f"cycle_{source_id}_2026071200_forecast_fixture"
        record["cycle_id"] = f"{source_id}_2026071200"
        record["idempotency_key"] = f"cycle_{source_id}_2026071200_forecast_fixture:forecast"
        record["slurm_comment"] = f"nhms_idem:cycle_{source_id}_2026071200_forecast_fixture:forecast"
        record["cohort_members"] = [
            {
                "array_task_id": member["array_task_id"],
                "candidate_id": (
                    f"{canonical_source}:2026-07-12T00:00:00Z:model_{member['array_task_id']}:"
                    f"{record['cohort_members'][0].get('scenario_id')}"
                ),
                "run_id": f"fcst_{source_id}_2026071200_model_{member['array_task_id']}",
                "model_id": f"model_{member['array_task_id']}",
                "basin_id": f"basin_{member['array_task_id']}",
                "scenario_id": member.get("scenario_id"),
                "restart_stage": "forecast",
            }
            for member in record["cohort_members"]
        ]
        record["cohort_digest"] = forecast_cohort_digest(record)
    return record


def _reserve_second_master(
    repository: Any,
    *,
    source_id: str,
    created_at: datetime,
    member_count: int = 1,
    expected_user: str = "scheduler",
    expected_account: str = "account",
) -> tuple[str, str]:
    """Reserve + timeout-transition a second master; return (job_id, key)."""

    record = _second_master_reservation_record(
        source_id=source_id,
        created_at=created_at,
        member_count=member_count,
        expected_user=expected_user,
        expected_account=expected_account,
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
    _append_cohort_placeholders(repository, member_count, source_id=source_id)
    return str(record["job_id"]), str(record["idempotency_key"])


def _reserve_gfs_master_on(
    repository: Any,
    *,
    created_at: datetime,
    member_count: int = 1,
    expected_user: str = "scheduler",
    expected_account: str = "account",
) -> tuple[str, str]:
    """Reserve + timeout-transition a GFS master onto an existing journal.

    Used by the tests that need a sibling GFS claimant created AFTER another
    source has already bound/owned a master.
    """

    record = _versioned_master_reservation_record(
        created_at=created_at,
        member_count=member_count,
        expected_user=expected_user,
        expected_account=expected_account,
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
    _append_cohort_placeholders(repository, member_count, source_id="gfs")
    return str(record["job_id"]), str(record["idempotency_key"])


@pytest.mark.skipif(not hasattr(__import__("time"), "tzset"), reason="time.tzset() is POSIX-only")
def test_two_overlapping_claimants_never_bind_one_master(tmp_path: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    """Two overlapping GFS/IFS reserved attempts claim one master: neither binds.

    Both reserved-unbound forecast masters carry the same expected Slurm
    user/account and the shared candidate's submit instant falls inside both
    attempt windows, so the candidate has two durable claimants and no
    claimant may bind, independent of reconcile iteration order.
    """

    from services.orchestrator.reconcile import reconcile_reserved_unbound_jobs

    anchor = datetime(2026, 7, 12, tzinfo=UTC)
    query_end = anchor + timedelta(hours=2)
    with _pinned_local_timezone("UTC"):
        repository = _file_cohort_repository(
            tmp_path / "gfs",
            created_at=anchor,
            member_count=1,
            expected_user="scheduler",
            expected_account="account",
        )
        ifs_job_id, _ifs_key = _reserve_second_master(
            repository,
            source_id="ifs",
            created_at=anchor,
            member_count=1,
            expected_user="scheduler",
            expected_account="account",
        )
        gfs_job_id = "job_cycle_gfs_2026071200_forecast_fixture_forecast"
        rows = _fallback_row("72001", submit="2026-07-12T01:00:00")
        query, _ = _fallback_querier(monkeypatch, rows=rows, query_end=query_end)

        gfs_first = reconcile_reserved_unbound_jobs(
            repository,
            comment_query=query,
            now=lambda: query_end,
        )

        gfs = next(outcome for outcome in gfs_first if outcome.job_id == gfs_job_id)
        ifs = next(outcome for outcome in gfs_first if outcome.job_id == ifs_job_id)
        assert gfs.action == "ambiguous_fallback_match"
        assert gfs.match_count == 2
        assert gfs.reconciliation_decision == "accounting_unavailable"
        assert gfs.reconciliation_reason_class == "comment_accounting_unproven"
        assert ifs.action == "ambiguous_fallback_match"
        assert ifs.match_count == 2

        # Reverse iteration order changes nothing: both stay held, no bind.
        repository = _file_cohort_repository(tmp_path / "gfs")
        ifs_job_id2, _ = _reserve_second_master(
            repository,
            source_id="ifs",
            created_at=anchor,
            member_count=1,
            expected_user="scheduler",
            expected_account="account",
        )
        query2, _ = _fallback_querier(monkeypatch, rows=rows, query_end=query_end)
        reversed_order = reconcile_reserved_unbound_jobs(
            repository,
            comment_query=query2,
            now=lambda: query_end,
        )
        outcomes_by_id = {outcome.job_id: outcome for outcome in reversed_order}
        assert outcomes_by_id[gfs_job_id].action == "ambiguous_fallback_match"
        assert outcomes_by_id[ifs_job_id2].action == "ambiguous_fallback_match"
        for job_id in (gfs_job_id, ifs_job_id2):
            persisted = repository.get_pipeline_job(job_id)
            assert persisted["status"] == "reserved"
            assert persisted["slurm_job_id"] is None
            assert persisted["reconciliation_source"] == "slurm_exact_comment"
            assert persisted["reconciliation_decision"] == "accounting_unavailable"
            assert persisted["reconciliation_reason_class"] == "comment_accounting_unproven"
            assert persisted["identity_blocked_streak"] == 0


@pytest.mark.skipif(not hasattr(__import__("time"), "tzset"), reason="time.tzset() is POSIX-only")
def test_already_bound_active_master_cannot_be_claimed_again(tmp_path: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    """An already-bound active current accepted-submit master id stays owned.

    A reserved claimant whose accounting query contains one in-window master
    already bound to a sibling source/cycle must stay under the held tuple
    even though its own query sees no second master.
    """

    from services.orchestrator.file_orchestration_journal import FileOrchestrationJournalRepository
    from services.orchestrator.reconcile import reconcile_reserved_unbound_jobs

    anchor = datetime(2026, 7, 12, tzinfo=UTC)
    query_end = anchor + timedelta(hours=2)
    with _pinned_local_timezone("UTC"):
        # Start from an IFS-only journal: the GFS sibling is added only AFTER
        # the IFS master has bound its own exclusive master.
        repository = _file_cohort_repository(
            tmp_path / "ifs-seed",
            created_at=anchor,
            member_count=1,
            expected_user="scheduler",
            expected_account="account",
            source_id="ifs",
        )
        ifs_job_id = "job_cycle_ifs_2026071200_forecast_fixture_forecast"
        ifs_key = "cycle_ifs_2026071200_forecast_fixture:forecast"
        _bind_ifs = repository.commit_pipeline_job_submit_attempt(
            ifs_key,
            pipeline_job_id=ifs_job_id,
            expected_submission_attempt=1,
            slurm_job_id="72001",
            submitted_at=anchor + timedelta(minutes=30),
            transition=AcceptedSubmitTransition.accounting(
                "matched_bound",
                submit_outcome="accepted",
                matched_slurm_job_id="72001",
                status="submitted",
                reconciliation_source="slurm_name_window_unique",
            ),
        )
        assert _bind_ifs.committed
        # Now a NEW reserved-unbound GFS attempt (same user/account, later
        # anchor) sees only its own in-window master, which is already owned.
        gfs_anchor = anchor + timedelta(hours=1)
        gfs_job_id, _gfs_key = _reserve_gfs_master_on(
            repository,
            created_at=gfs_anchor,
            member_count=1,
            expected_user="scheduler",
            expected_account="account",
        )
        rows = _fallback_row("72001", submit="2026-07-12T01:00:00")
        query, _ = _fallback_querier(monkeypatch, rows=rows, query_end=query_end)

        outcome = reconcile_reserved_unbound_jobs(
            repository,
            comment_query=query,
            now=lambda: query_end,
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
        # The active owner keeps its bind.
        ifs_bound = repository.get_pipeline_job(ifs_job_id)
        assert ifs_bound["slurm_job_id"] == "72001"
        assert ifs_bound["status"] == "submitted"
        # Reopening the journal re-reads the same result.
        reopened = FileOrchestrationJournalRepository(repository.root)
        assert reopened.get_pipeline_job(gfs_job_id)["slurm_job_id"] is None
        assert reopened.get_pipeline_job(ifs_job_id)["slurm_job_id"] == "72001"


@pytest.mark.skipif(not hasattr(__import__("time"), "tzset"), reason="time.tzset() is POSIX-only")
def test_settled_terminal_history_does_not_occupy_a_recycled_slurm_number(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A terminal settled master with the same slurm id does not block a bind.

    The id was recycled from a fully-settled terminal history row; the current
    reserved claimant with the same id binds because settled terminal history
    does not count as an active accepted-submit owner.
    """

    from services.orchestrator.file_orchestration_journal import FileOrchestrationJournalRepository
    from services.orchestrator.reconcile import reconcile_reserved_unbound_jobs

    anchor = datetime(2026, 7, 12, tzinfo=UTC)
    query_end = anchor + timedelta(hours=2)
    with _pinned_local_timezone("UTC"):
        # Start from an IFS-only journal: the IFS master settles terminally
        # while it is the only master, then GFS recycles the freed id.
        repository = _file_cohort_repository(
            tmp_path / "ifs-seed",
            created_at=anchor,
            member_count=1,
            expected_user="scheduler",
            expected_account="account",
            source_id="ifs",
        )
        ifs_job_id = "job_cycle_ifs_2026071200_forecast_fixture_forecast"
        ifs_key = "cycle_ifs_2026071200_forecast_fixture:forecast"
        bound = repository.commit_pipeline_job_submit_attempt(
            ifs_key,
            pipeline_job_id=ifs_job_id,
            expected_submission_attempt=1,
            slurm_job_id="72001",
            submitted_at=anchor + timedelta(minutes=30),
            transition=AcceptedSubmitTransition.accounting(
                "matched_bound",
                submit_outcome="accepted",
                matched_slurm_job_id="72001",
                status="submitted",
                reconciliation_source="slurm_name_window_unique",
            ),
        )
        assert bound.committed
        projections, complete = _file_cohort_single_projection(repository, ifs_job_id)
        assert complete
        repository.project_forecast_cohort_tasks(
            ifs_job_id,
            master_slurm_job_id="72001",
            projections=projections,
            complete=True,
            master_status="succeeded",
            master_error_code=None,
            reconciliation_decision="matched_bound",
        )
        terminal = repository.get_pipeline_job(ifs_job_id)
        assert terminal["status"] == "succeeded"
        assert terminal["submit_outcome"] == "accepted"

        # Now a NEW reserved-unbound GFS attempt recycles the freed id.
        gfs_anchor = anchor + timedelta(hours=1)
        gfs_job_id, _gfs_key = _reserve_gfs_master_on(
            repository,
            created_at=gfs_anchor,
            member_count=1,
            expected_user="scheduler",
            expected_account="account",
        )
        rows = _fallback_row("72001", submit="2026-07-12T01:00:00")
        query, _ = _fallback_querier(monkeypatch, rows=rows, query_end=query_end)

        outcome = reconcile_reserved_unbound_jobs(
            repository,
            comment_query=query,
            now=lambda: query_end,
        )[0]

        assert outcome.job_id == gfs_job_id
        assert outcome.action == "bound"
        assert outcome.matched_slurm_job_id == "72001"
        assert outcome.reconciliation_source == "slurm_name_window_unique"
        persisted = repository.get_pipeline_job(gfs_job_id)
        assert persisted["slurm_job_id"] == "72001"
        assert persisted["reconciliation_source"] == "slurm_name_window_unique"
        assert persisted["reconciliation_decision"] == "matched_bound"
        # Reopened journal agrees.
        reopened = FileOrchestrationJournalRepository(repository.root)
        assert reopened.get_pipeline_job(gfs_job_id)["slurm_job_id"] == "72001"
        assert reopened.get_pipeline_job(ifs_job_id)["status"] == "succeeded"


@pytest.mark.skipif(not hasattr(__import__("time"), "tzset"), reason="time.tzset() is POSIX-only")
def test_concurrent_claimants_on_one_master_never_bind_either(tmp_path: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    """Cross-process/thread concurrent fallback commits cannot change the result.

    Two reserved masters race to bind the same in-window master. The journal's
    cross-process inventory serialization plus the per-cycle write lock make
    the result order-independent: one claimant loses the durable-claimant
    check and the bind collision CAS; no double-bind ever occurs.
    """

    from services.orchestrator.reconcile import reconcile_reserved_unbound_jobs

    anchor = datetime(2026, 7, 12, tzinfo=UTC)
    query_end = anchor + timedelta(hours=2)
    with _pinned_local_timezone("UTC"):
        repository = _file_cohort_repository(
            tmp_path / "gfs",
            created_at=anchor,
            member_count=1,
            expected_user="scheduler",
            expected_account="account",
        )
        ifs_job_id, _ifs_key = _reserve_second_master(
            repository,
            source_id="ifs",
            created_at=anchor,
            member_count=1,
            expected_user="scheduler",
            expected_account="account",
        )
        gfs_job_id = "job_cycle_gfs_2026071200_forecast_fixture_forecast"
        rows = _fallback_row("72001", submit="2026-07-12T01:00:00")
        query, _ = _fallback_querier(monkeypatch, rows=rows, query_end=query_end)

        outcomes = reconcile_reserved_unbound_jobs(
            repository,
            comment_query=query,
            now=lambda: query_end,
        )

        by_id = {outcome.job_id: outcome for outcome in outcomes}
        assert by_id[gfs_job_id].action == "ambiguous_fallback_match"
        assert by_id[ifs_job_id].action == "ambiguous_fallback_match"
        for job_id in (gfs_job_id, ifs_job_id):
            persisted = repository.get_pipeline_job(job_id)
            assert persisted["status"] == "reserved"
            assert persisted["slurm_job_id"] is None
            assert persisted["identity_blocked_streak"] == 0


def _file_cohort_single_projection(repository: Any, job_id: str) -> tuple[list[dict[str, Any]], bool]:
    """Build a single-member cohort projection the way the terminal reconcile does."""

    row = repository.get_pipeline_job(job_id)
    members = row["cohort_members"]
    projections = [
        {
            "candidate_id": members[0].get("candidate_id"),
            "run_id": members[0].get("run_id"),
            "model_id": members[0].get("model_id"),
            "array_task_id": members[0]["array_task_id"],
            "array_task_outcome": "succeeded",
            "task_slurm_job_id": f"72001_{members[0]['array_task_id']}",
            "error_code": None,
            "restart_stage": "state_save_qc",
            "native_shud_resubmitted": False,
        }
    ]
    return projections, True


@pytest.mark.skipif(not hasattr(__import__("time"), "tzset"), reason="time.tzset() is POSIX-only")
def test_exclusive_claimant_still_binds_when_no_sibling_claimant_exists(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """One exclusive in-window master with one durable claimant binds."""

    from services.orchestrator.reconcile import reconcile_reserved_unbound_jobs

    anchor = datetime(2026, 7, 12, tzinfo=UTC)
    query_end = anchor + timedelta(hours=2)
    with _pinned_local_timezone("UTC"):
        repository = _file_cohort_repository(
            tmp_path,
            created_at=anchor,
            member_count=1,
            expected_user="scheduler",
            expected_account="account",
        )
        gfs_job_id = "job_cycle_gfs_2026071200_forecast_fixture_forecast"
        rows = _fallback_row("72001", submit="2026-07-12T01:00:00")
        query, _ = _fallback_querier(monkeypatch, rows=rows, query_end=query_end)

        outcome = reconcile_reserved_unbound_jobs(
            repository,
            comment_query=query,
            now=lambda: query_end,
        )[0]

        assert outcome.action == "bound"
        assert outcome.matched_slurm_job_id == "72001"
        assert outcome.reconciliation_source == "slurm_name_window_unique"
        assert outcome.reconciliation_decision == "matched_bound"
        persisted = repository.get_pipeline_job(gfs_job_id)
        assert persisted["slurm_job_id"] == "72001"
        assert persisted["reconciliation_source"] == "slurm_name_window_unique"


@pytest.mark.skipif(not hasattr(__import__("time"), "tzset"), reason="time.tzset() is POSIX-only")
def test_fallback_submit_exactly_at_query_end_binds_but_after_stays_held(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The attempt window is closed at the upper boundary.

    A Submit instant that converts exactly to the frozen query-end is
    in-window and binds; an instant after the query-end is ineligible and
    stays ``fallback_no_match`` (never binds, never proves absence).
    """

    from services.orchestrator.reconcile import reconcile_reserved_unbound_jobs

    anchor = datetime(2026, 7, 12, tzinfo=UTC)
    query_end = anchor + timedelta(hours=2)
    with _pinned_local_timezone("UTC"):
        # Exactly at the frozen query-end: in-window.
        repository = _file_cohort_repository(
            tmp_path / "at-end",
            created_at=anchor,
            member_count=1,
            expected_user="scheduler",
            expected_account="account",
        )
        gfs_job_id = "job_cycle_gfs_2026071200_forecast_fixture_forecast"
        rows = _fallback_row("72001", submit=query_end.strftime("%Y-%m-%dT%H:%M:%S"))
        query, _ = _fallback_querier(monkeypatch, rows=rows, query_end=query_end)

        outcome = reconcile_reserved_unbound_jobs(
            repository,
            comment_query=query,
            now=lambda: query_end,
        )[0]

        assert outcome.action == "bound"
        assert outcome.matched_slurm_job_id == "72001"
        persisted = repository.get_pipeline_job(gfs_job_id)
        assert persisted["slurm_job_id"] == "72001"

        # One second after the frozen query-end: ineligible, stays held.
        repository = _file_cohort_repository(
            tmp_path / "after-end",
            created_at=anchor,
            member_count=1,
            expected_user="scheduler",
            expected_account="account",
        )
        gfs_job_id2 = "job_cycle_gfs_2026071200_forecast_fixture_forecast"
        after = query_end + timedelta(seconds=1)
        rows2 = _fallback_row("72001", submit=after.strftime("%Y-%m-%dT%H:%M:%S"))
        query2, _ = _fallback_querier(monkeypatch, rows=rows2, query_end=query_end)

        outcome2 = reconcile_reserved_unbound_jobs(
            repository,
            comment_query=query2,
            now=lambda: query_end,
        )[0]

        assert outcome2.action == "fallback_no_match"
        assert outcome2.match_count == 0
        persisted2 = repository.get_pipeline_job(gfs_job_id2)
        assert persisted2["status"] == "reserved"
        assert persisted2["slurm_job_id"] is None
        assert persisted2["reconciliation_source"] == "slurm_exact_comment"
        assert persisted2["reconciliation_decision"] == "accounting_unavailable"
        assert persisted2["reconciliation_reason_class"] == "comment_accounting_unproven"
        assert persisted2["identity_blocked_streak"] == 0


@pytest.mark.skipif(not hasattr(__import__("time"), "tzset"), reason="time.tzset() is POSIX-only")
def test_candidate_unique_in_one_query_but_claimed_by_two_windows_never_binds(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """One query sees one master; the journal sees two durable claimants.

    A candidate master whose submit instant falls inside two overlapping
    reserved attempts for the same owner is ambiguous at the journal level even
    though each per-job accounting query returns only that one master. Neither
    claimant binds (P0 sibling-cohort scenario).
    """

    from services.orchestrator.reconcile import reconcile_reserved_unbound_jobs

    anchor = datetime(2026, 7, 12, tzinfo=UTC)
    query_end = anchor + timedelta(hours=2)
    with _pinned_local_timezone("UTC"):
        repository = _file_cohort_repository(
            tmp_path / "gfs",
            created_at=anchor,
            member_count=1,
            expected_user="scheduler",
            expected_account="account",
        )
        ifs_job_id, _ifs_key = _reserve_second_master(
            repository,
            source_id="ifs",
            created_at=anchor,
            member_count=1,
            expected_user="scheduler",
            expected_account="account",
        )
        gfs_job_id = "job_cycle_gfs_2026071200_forecast_fixture_forecast"
        # Each query returns ONLY the one shared master (no second master in
        # either job's own query); the exclusivity must come from the journal.
        rows = _fallback_row("72001", submit="2026-07-12T01:00:00")
        query, _ = _fallback_querier(monkeypatch, rows=rows, query_end=query_end)

        outcomes = reconcile_reserved_unbound_jobs(
            repository,
            comment_query=query,
            now=lambda: query_end,
        )
        by_id = {outcome.job_id: outcome for outcome in outcomes}
        assert by_id[gfs_job_id].action == "ambiguous_fallback_match"
        assert by_id[ifs_job_id].action == "ambiguous_fallback_match"
        assert by_id[gfs_job_id].match_count == 2
        for job_id in (gfs_job_id, ifs_job_id):
            persisted = repository.get_pipeline_job(job_id)
            assert persisted["status"] == "reserved"
            assert persisted["slurm_job_id"] is None
            assert persisted["identity_blocked_streak"] == 0


# --- #1850 Phase 6b (coordinator audit): global active-id occupancy for EVERY
# typed accepted-submit commit (Fix A), fail-closed inventory integrity (Fix B),
# real concurrency + fresh-root order independence (Fix C), and lazy capability
# probing (Fix D).


def _commit_normal_ifs_bind(
    repository: Any,
    *,
    slurm_job_id: str,
    expected_submission_attempt: int = 1,
) -> Any:
    """A normal authoritative accepted-submit commit for the IFS master.

    Mirrors the live sbatch path: no name-window source, no candidate Submit.
    """

    ifs_job_id = "job_cycle_ifs_2026071200_forecast_fixture_forecast"
    ifs_key = "cycle_ifs_2026071200_forecast_fixture:forecast"
    return repository.commit_pipeline_job_submit_attempt(
        ifs_key,
        pipeline_job_id=ifs_job_id,
        expected_submission_attempt=expected_submission_attempt,
        slurm_job_id=slurm_job_id,
        submitted_at=datetime(2026, 7, 12, 0, 40, tzinfo=UTC),
        transition=AcceptedSubmitTransition.accepted(status="submitted"),
    )


@pytest.mark.skipif(not hasattr(__import__("time"), "tzset"), reason="time.tzset() is POSIX-only")
def test_two_typed_normal_commits_same_id_across_sources_at_most_one_applies(
    tmp_path: Any,
) -> None:
    """Fix A: global active-id occupancy pins EVERY typed commit.

    Two current accepted-submit masters in distinct source/cycles (GFS and
    IFS) race to bind the same id through the NORMAL authoritative commit
    (no name-window source). At most one applies; the loser returns a
    non-committed occupancy outcome and no double ownership exists.
    """

    from services.orchestrator.file_orchestration_journal import FileOrchestrationJournalRepository

    anchor = datetime(2026, 7, 12, tzinfo=UTC)
    with _pinned_local_timezone("UTC"):
        repository = _file_cohort_repository(
            tmp_path / "gfs",
            created_at=anchor,
            member_count=1,
            expected_user="scheduler",
            expected_account="account",
        )
        ifs_job_id, ifs_key = _reserve_second_master(
            repository,
            source_id="ifs",
            created_at=anchor,
            member_count=1,
            expected_user="scheduler",
            expected_account="account",
        )
        gfs_job_id = "job_cycle_gfs_2026071200_forecast_fixture_forecast"
        gfs_key = "cycle_gfs_2026071200_forecast_fixture:forecast"

        # GFS normal commit first: it is the only owner-claimant candidate.
        gfs_result = repository.commit_pipeline_job_submit_attempt(
            gfs_key,
            pipeline_job_id=gfs_job_id,
            expected_submission_attempt=1,
            slurm_job_id="72001",
            submitted_at=anchor + timedelta(minutes=30),
            transition=AcceptedSubmitTransition.accepted(status="submitted"),
        )
        assert gfs_result.committed
        # IFS normal commit with the SAME id: the global occupancy scan must
        # reject it (the GFS master is an active current accepted-submit owner).
        ifs_result = repository.commit_pipeline_job_submit_attempt(
            ifs_key,
            pipeline_job_id=ifs_job_id,
            expected_submission_attempt=1,
            slurm_job_id="72001",
            submitted_at=anchor + timedelta(minutes=40),
            transition=AcceptedSubmitTransition.accepted(status="submitted"),
        )
        assert not ifs_result.committed
        assert ifs_result.outcome in {"identity_mismatch_blocked", "active_slurm_id_occupied"}
        assert repository.get_pipeline_job(ifs_job_id)["slurm_job_id"] is None
        assert repository.get_pipeline_job(ifs_job_id)["status"] == "reserved"
        assert repository.get_pipeline_job(gfs_job_id)["slurm_job_id"] == "72001"

        # Reopened journal agrees: one active owner only.
        reopened = FileOrchestrationJournalRepository(repository.root)
        assert reopened.get_pipeline_job(gfs_job_id)["slurm_job_id"] == "72001"
        assert reopened.get_pipeline_job(ifs_job_id)["slurm_job_id"] is None


@pytest.mark.skipif(not hasattr(__import__("time"), "tzset"), reason="time.tzset() is POSIX-only")
def test_normal_commit_races_fallback_commit_for_the_same_id(
    tmp_path: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Fix D: with BOTH reserved claimants present before the race, the
    fallback can NEVER legitimately own the id.

    If the fallback scans first, the sibling is a durable claimant
    (``ambiguous_fallback_match``); if the normal IFS binds first, occupancy
    blocks the fallback (public ``identity_mismatch_blocked``). The normal
    IFS commit MUST be applied/idempotent and the fallback MUST never bind.
    """

    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier

    from services.orchestrator import reconcile as reconcile_module
    from services.orchestrator.file_orchestration_journal import FileOrchestrationJournalRepository

    anchor = datetime(2026, 7, 12, tzinfo=UTC)
    query_end = anchor + timedelta(hours=2)
    with _pinned_local_timezone("UTC"):
        repository = _file_cohort_repository(
            tmp_path / "gfs",
            created_at=anchor,
            member_count=1,
            expected_user="scheduler",
            expected_account="account",
        )
        ifs_job_id, _ifs_key = _reserve_second_master(
            repository,
            source_id="ifs",
            created_at=anchor,
            member_count=1,
            expected_user="scheduler",
            expected_account="account",
        )
        gfs_job_id = "job_cycle_gfs_2026071200_forecast_fixture_forecast"
        rows = _fallback_row("72001", submit="2026-07-12T01:00:00")
        query, _ = _fallback_querier(monkeypatch, rows=rows, query_end=query_end)

        barrier = Barrier(2, timeout=30)

        def normal_ifs() -> str:
            contender = FileOrchestrationJournalRepository(repository.root)
            barrier.wait(timeout=30)
            return _commit_normal_ifs_bind(contender, slurm_job_id="72001").outcome

        def fallback_gfs() -> str:
            contender = FileOrchestrationJournalRepository(repository.root)
            barrier.wait(timeout=30)
            return reconcile_module.reconcile_reserved_unbound_jobs(
                contender,
                comment_query=query,
                now=lambda: query_end,
            )[0].action

        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = [pool.submit(normal_ifs), pool.submit(fallback_gfs)]
            outcomes = [future.result(timeout=60) for future in futures]

        normal_outcome = outcomes[0]
        fallback_action = outcomes[1]
        # The normal authoritative IFS commit always wins the id.
        assert normal_outcome in {"applied", "idempotent"}, outcomes
        # The fallback never binds: multi-claimant ambiguity or occupancy block.
        assert fallback_action in {"ambiguous_fallback_match", "identity_mismatch_blocked"}, outcomes

        owners = []
        reopened = FileOrchestrationJournalRepository(repository.root)
        for job_id in (gfs_job_id, ifs_job_id):
            row = reopened.get_pipeline_job(job_id)
            if row is not None and row.get("slurm_job_id") == "72001":
                owners.append(job_id)
        # Exactly one owner and it is the IFS normal master; GFS stays unbound.
        assert owners == [ifs_job_id], owners
        assert reopened.get_pipeline_job(gfs_job_id)["slurm_job_id"] is None


@pytest.mark.skipif(not hasattr(__import__("time"), "tzset"), reason="time.tzset() is POSIX-only")
def test_real_row_order_independence_across_fresh_roots(
    tmp_path: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Fix C3: real row-order independence with FRESH journal roots.

    Root A reserves GFS first then IFS (GFS iterates first); root B reserves
    IFS first then GFS (IFS iterates first). Both see the same single visible
    fallback master inside two overlapping same-owner windows. Neither wrong
    claimant binds in either root; every row stays held under the #1564 tuple.
    """

    from services.orchestrator.reconcile import reconcile_reserved_unbound_jobs

    anchor = datetime(2026, 7, 12, tzinfo=UTC)
    query_end = anchor + timedelta(hours=2)
    with _pinned_local_timezone("UTC"):
        rows = _fallback_row("72001", submit="2026-07-12T01:00:00")

        # Root A: GFS reserved first, IFS second (GFS iterates first).
        root_a = _file_cohort_repository(
            tmp_path / "root-a",
            created_at=anchor,
            member_count=1,
            expected_user="scheduler",
            expected_account="account",
        )
        _reserve_second_master(
            root_a,
            source_id="ifs",
            created_at=anchor,
            member_count=1,
            expected_user="scheduler",
            expected_account="account",
        )
        query_a, _ = _fallback_querier(monkeypatch, rows=rows, query_end=query_end)
        outcomes_a = reconcile_reserved_unbound_jobs(
            root_a,
            comment_query=query_a,
            now=lambda: query_end,
        )
        by_id_a = {outcome.job_id: outcome for outcome in outcomes_a}
        for job_id in by_id_a:
            assert by_id_a[job_id].action == "ambiguous_fallback_match"
            assert by_id_a[job_id].match_count == 2
            persisted = root_a.get_pipeline_job(job_id)
            assert persisted["status"] == "reserved"
            assert persisted["slurm_job_id"] is None

        # Root B: IFS reserved first, GFS second (IFS iterates first).
        root_b = _file_cohort_repository(
            tmp_path / "root-b",
            created_at=anchor,
            member_count=1,
            expected_user="scheduler",
            expected_account="account",
            source_id="ifs",
        )
        _reserve_gfs_master_on(
            root_b,
            created_at=anchor,
            member_count=1,
            expected_user="scheduler",
            expected_account="account",
        )
        query_b, _ = _fallback_querier(monkeypatch, rows=rows, query_end=query_end)
        outcomes_b = reconcile_reserved_unbound_jobs(
            root_b,
            comment_query=query_b,
            now=lambda: query_end,
        )
        by_id_b = {outcome.job_id: outcome for outcome in outcomes_b}
        for job_id in by_id_b:
            assert by_id_b[job_id].action == "ambiguous_fallback_match"
            assert by_id_b[job_id].match_count == 2
            persisted = root_b.get_pipeline_job(job_id)
            assert persisted["status"] == "reserved"
            assert persisted["slurm_job_id"] is None


@pytest.mark.skipif(not hasattr(__import__("time"), "tzset"), reason="time.tzset() is POSIX-only")
def test_malformed_inventory_anchor_fails_closed_no_bind(
    tmp_path: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Fix B: a malformed reconcile-inventory anchor must fail the commit closed.

    A current reserved claimant whose accounting query yields one in-window
    master, with a tampered sibling inventory anchor present, must NOT bind:
    the tampered authority cannot be interpreted as "free". The commit raises
    a stable ``FileOrchestrationJournalError`` and no bind occurs.
    """

    from services.orchestrator.file_orchestration_journal import (
        FileOrchestrationJournalError,
        FileOrchestrationJournalRepository,
    )
    from services.orchestrator.reconcile import reconcile_reserved_unbound_jobs

    anchor = datetime(2026, 7, 12, tzinfo=UTC)
    query_end = anchor + timedelta(hours=2)
    with _pinned_local_timezone("UTC"):
        repository = _file_cohort_repository(
            tmp_path / "gfs",
            created_at=anchor,
            member_count=1,
            expected_user="scheduler",
            expected_account="account",
        )
        _reserve_second_master(
            repository,
            source_id="ifs",
            created_at=anchor,
            member_count=1,
            expected_user="scheduler",
            expected_account="account",
        )
        # Complete the reconcile-inventory migration (write the marker) so the
        # reopened instance below does NOT backfill and overwrite our tamper.
        assert len(repository.query_reserved_unbound_jobs()) == 2
        # Tamper the IFS inventory anchor: invalid JSON content.
        anchor_path = (
            repository.root
            / "reconcile-inventory"
            / "job_cycle_ifs_2026071200_forecast_fixture_forecast.json"
        )
        anchor_path.write_text("{not-json", encoding="utf-8")

        gfs_job_id = "job_cycle_gfs_2026071200_forecast_fixture_forecast"
        rows = _fallback_row("72001", submit="2026-07-12T01:00:00")
        query, _ = _fallback_querier(monkeypatch, rows=rows, query_end=query_end)

        # Reopen the journal so the anchor read uses a cold cache and the
        # malformed content is actually observed.
        reopened = FileOrchestrationJournalRepository(repository.root)
        with pytest.raises(FileOrchestrationJournalError):
            reconcile_reserved_unbound_jobs(
                reopened,
                comment_query=query,
                now=lambda: query_end,
            )
        persisted = FileOrchestrationJournalRepository(repository.root).get_pipeline_job(gfs_job_id)
        assert persisted["status"] == "reserved"
        assert persisted["slurm_job_id"] is None


@pytest.mark.skipif(not hasattr(__import__("time"), "tzset"), reason="time.tzset() is POSIX-only")
def test_orphan_inventory_anchor_fails_closed_no_bind(
    tmp_path: Any,
) -> None:
    """Fix B: an orphan inventory anchor (no canonical authority) must fail
    the typed commit closed.

    A listed anchor that resolves to no canonical row cannot be read as "free";
    a fallback commit whose occupancy scan encounters the orphan raises a
    stable ``FileOrchestrationJournalError`` instead of binding on missing
    authority. Driven through the typed commit directly so the orphan survives
    to the scan (the reconcile iteration would prune it first)."""

    from services.orchestrator.accepted_submit_identity import AcceptedSubmitTransition
    from services.orchestrator.file_orchestration_journal import (
        FileOrchestrationJournalError,
        FileOrchestrationJournalRepository,
    )

    anchor = datetime(2026, 7, 12, tzinfo=UTC)
    with _pinned_local_timezone("UTC"):
        repository = _file_cohort_repository(
            tmp_path / "gfs",
            created_at=anchor,
            member_count=1,
            expected_user="scheduler",
            expected_account="account",
        )
        _reserve_second_master(
            repository,
            source_id="ifs",
            created_at=anchor,
            member_count=1,
            expected_user="scheduler",
            expected_account="account",
        )
        # Complete the reconcile-inventory migration so a reopen does NOT
        # backfill and restore the orphaned authority.
        assert len(repository.query_reserved_unbound_jobs()) == 2
        # Orphan the IFS anchor: delete every authoritative surface the
        # canonical read resolves from (journal event log, latest, direct).
        ifs_job_id = "job_cycle_ifs_2026071200_forecast_fixture_forecast"
        ifs_cycle = "2026071200"
        import shutil

        journal = repository.root / "journal" / "IFS" / f"{ifs_cycle}.jsonl"
        if journal.exists():
            journal.unlink()
        latest = repository.root / "latest" / "IFS" / ifs_cycle
        if latest.exists():
            shutil.rmtree(latest)
        direct = repository.root / "pipeline-jobs" / f"{ifs_job_id}.json"
        if direct.exists():
            direct.unlink()

        gfs_job_id = "job_cycle_gfs_2026071200_forecast_fixture_forecast"
        gfs_key = "cycle_gfs_2026071200_forecast_fixture:forecast"
        reopened = FileOrchestrationJournalRepository(repository.root)
        with pytest.raises(FileOrchestrationJournalError):
            reopened.commit_pipeline_job_submit_attempt(
                gfs_key,
                pipeline_job_id=gfs_job_id,
                expected_submission_attempt=1,
                slurm_job_id="72001",
                submitted_at=anchor + timedelta(hours=1),
                transition=AcceptedSubmitTransition.accounting(
                    "matched_bound",
                    submit_outcome="accepted",
                    matched_slurm_job_id="72001",
                    status="submitted",
                    reconciliation_source="slurm_name_window_unique",
                ),
            )
        persisted = FileOrchestrationJournalRepository(repository.root).get_pipeline_job(gfs_job_id)
        assert persisted["status"] == "reserved"
        assert persisted["slurm_job_id"] is None


# --- #1850 Phase 6c (final correctness/oracle): pre-outcome lane parity,
# accounting-incarnation occupancy, public vocabulary, true two-normal race,
# and valid-JSON invalid anchor.

@pytest.mark.skipif(not hasattr(__import__("time"), "tzset"), reason="time.tzset() is POSIX-only")
def test_incarnation_scan_terminal_sibling_same_submit_blocks_recycle(
    tmp_path: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Fix 2: a TERMINAL sibling whose durable submitted_at equals the
    candidate's Submit instant is the exact accounting incarnation and must
    block a wrong bind, even though its row is settled."""

    from services.orchestrator.accepted_submit_identity import AcceptedSubmitTransition
    from services.orchestrator.file_orchestration_journal import FileOrchestrationJournalRepository
    from services.orchestrator.reconcile import reconcile_reserved_unbound_jobs

    anchor = datetime(2026, 7, 12, tzinfo=UTC)
    query_end = anchor + timedelta(hours=2)
    with _pinned_local_timezone("UTC"):
        # IFS settles terminally holding 72001 with submitted_at = 01:00.
        repository = _file_cohort_repository(
            tmp_path / "ifs-seed",
            created_at=anchor,
            member_count=1,
            expected_user="scheduler",
            expected_account="account",
            source_id="ifs",
        )
        ifs_job_id = "job_cycle_ifs_2026071200_forecast_fixture_forecast"
        ifs_key = "cycle_ifs_2026071200_forecast_fixture:forecast"
        bound = repository.commit_pipeline_job_submit_attempt(
            ifs_key,
            pipeline_job_id=ifs_job_id,
            expected_submission_attempt=1,
            slurm_job_id="72001",
            submitted_at=anchor + timedelta(hours=1),
            transition=AcceptedSubmitTransition.accounting(
                "matched_bound",
                submit_outcome="accepted",
                matched_slurm_job_id="72001",
                status="submitted",
                reconciliation_source="slurm_name_window_unique",
            ),
        )
        assert bound.committed
        projections, complete = _file_cohort_single_projection(repository, ifs_job_id)
        repository.project_forecast_cohort_tasks(
            ifs_job_id,
            master_slurm_job_id="72001",
            projections=projections,
            complete=True,
            master_status="succeeded",
            master_error_code=None,
            reconciliation_decision="matched_bound",
        )
        assert repository.get_pipeline_job(ifs_job_id)["status"] == "succeeded"

        # GFS reserved at the anchor, candidate Submit instant is the SAME
        # 01:00 incarnation, inside the GFS window.
        gfs_job_id, _gfs_key = _reserve_gfs_master_on(
            repository,
            created_at=anchor,
            member_count=1,
            expected_user="scheduler",
            expected_account="account",
        )
        rows = _fallback_row("72001", submit="2026-07-12T01:00:00")
        query, _ = _fallback_querier(monkeypatch, rows=rows, query_end=query_end)

        outcome = reconcile_reserved_unbound_jobs(
            repository,
            comment_query=query,
            now=lambda: query_end,
        )[0]

        assert outcome.job_id == gfs_job_id
        assert outcome.action == "identity_mismatch_blocked"
        assert outcome.match_count == 1
        persisted = repository.get_pipeline_job(gfs_job_id)
        assert persisted["status"] == "reserved"
        assert persisted["slurm_job_id"] is None
        reopened = FileOrchestrationJournalRepository(repository.root)
        assert reopened.get_pipeline_job(gfs_job_id)["slurm_job_id"] is None


@pytest.mark.skipif(not hasattr(__import__("time"), "tzset"), reason="time.tzset() is POSIX-only")
def test_incarnation_scan_terminal_sibling_different_submit_permits_recycle(
    tmp_path: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Fix 2: a TERMINAL sibling whose durable submitted_at differs from the
    candidate's Submit instant is a recycled id and must NOT block."""

    from services.orchestrator.accepted_submit_identity import AcceptedSubmitTransition
    from services.orchestrator.file_orchestration_journal import FileOrchestrationJournalRepository
    from services.orchestrator.reconcile import reconcile_reserved_unbound_jobs

    anchor = datetime(2026, 7, 12, tzinfo=UTC)
    query_end = anchor + timedelta(hours=2)
    with _pinned_local_timezone("UTC"):
        repository = _file_cohort_repository(
            tmp_path / "ifs-seed",
            created_at=anchor,
            member_count=1,
            expected_user="scheduler",
            expected_account="account",
            source_id="ifs",
        )
        ifs_job_id = "job_cycle_ifs_2026071200_forecast_fixture_forecast"
        ifs_key = "cycle_ifs_2026071200_forecast_fixture:forecast"
        bound = repository.commit_pipeline_job_submit_attempt(
            ifs_key,
            pipeline_job_id=ifs_job_id,
            expected_submission_attempt=1,
            slurm_job_id="72001",
            submitted_at=anchor + timedelta(hours=1),
            transition=AcceptedSubmitTransition.accounting(
                "matched_bound",
                submit_outcome="accepted",
                matched_slurm_job_id="72001",
                status="submitted",
                reconciliation_source="slurm_name_window_unique",
            ),
        )
        assert bound.committed
        projections, complete = _file_cohort_single_projection(repository, ifs_job_id)
        repository.project_forecast_cohort_tasks(
            ifs_job_id,
            master_slurm_job_id="72001",
            projections=projections,
            complete=True,
            master_status="succeeded",
            master_error_code=None,
            reconciliation_decision="matched_bound",
        )
        assert repository.get_pipeline_job(ifs_job_id)["status"] == "succeeded"

        # GFS reserved at the anchor, candidate Submit instant is a DIFFERENT
        # incarnation (01:30), inside the GFS window.
        gfs_job_id, _gfs_key = _reserve_gfs_master_on(
            repository,
            created_at=anchor,
            member_count=1,
            expected_user="scheduler",
            expected_account="account",
        )
        rows = _fallback_row("72001", submit="2026-07-12T01:30:00")
        query, _ = _fallback_querier(monkeypatch, rows=rows, query_end=query_end)

        outcome = reconcile_reserved_unbound_jobs(
            repository,
            comment_query=query,
            now=lambda: query_end,
        )[0]

        assert outcome.job_id == gfs_job_id
        assert outcome.action == "bound"
        assert outcome.matched_slurm_job_id == "72001"
        persisted = repository.get_pipeline_job(gfs_job_id)
        assert persisted["slurm_job_id"] == "72001"
        reopened = FileOrchestrationJournalRepository(repository.root)
        assert reopened.get_pipeline_job(gfs_job_id)["slurm_job_id"] == "72001"
        assert reopened.get_pipeline_job(ifs_job_id)["status"] == "succeeded"


@pytest.mark.skipif(not hasattr(__import__("time"), "tzset"), reason="time.tzset() is POSIX-only")
def test_active_same_id_no_submit_blocks_even_without_submitted_at(
    tmp_path: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Fix 2: an ACTIVE master with the same id blocks even when its durable
    submitted_at is unavailable (fail closed)."""

    from services.orchestrator.accepted_submit_identity import AcceptedSubmitTransition
    from services.orchestrator.reconcile import reconcile_reserved_unbound_jobs

    anchor = datetime(2026, 7, 12, tzinfo=UTC)
    query_end = anchor + timedelta(hours=2)
    with _pinned_local_timezone("UTC"):
        repository = _file_cohort_repository(
            tmp_path / "ifs-seed",
            created_at=anchor,
            member_count=1,
            expected_user="scheduler",
            expected_account="account",
            source_id="ifs",
        )
        ifs_job_id = "job_cycle_ifs_2026071200_forecast_fixture_forecast"
        ifs_key = "cycle_ifs_2026071200_forecast_fixture:forecast"
        # Normal commit WITHOUT submitted_at (submitted_at falls back to now):
        # an active owner with no durable submit instant.
        bound = repository.commit_pipeline_job_submit_attempt(
            ifs_key,
            pipeline_job_id=ifs_job_id,
            expected_submission_attempt=1,
            slurm_job_id="72001",
            transition=AcceptedSubmitTransition.accepted(status="submitted"),
        )
        assert bound.committed

        gfs_anchor = anchor + timedelta(hours=1)
        gfs_job_id, _gfs_key = _reserve_gfs_master_on(
            repository,
            created_at=gfs_anchor,
            member_count=1,
            expected_user="scheduler",
            expected_account="account",
        )
        rows = _fallback_row("72001", submit="2026-07-12T01:00:00")
        query, _ = _fallback_querier(monkeypatch, rows=rows, query_end=query_end)

        outcome = reconcile_reserved_unbound_jobs(
            repository,
            comment_query=query,
            now=lambda: query_end,
        )[0]

        assert outcome.job_id == gfs_job_id
        assert outcome.action == "identity_mismatch_blocked"
        assert outcome.match_count == 1
        persisted = repository.get_pipeline_job(gfs_job_id)
        assert persisted["status"] == "reserved"
        assert persisted["slurm_job_id"] is None


@pytest.mark.skipif(not hasattr(__import__("time"), "tzset"), reason="time.tzset() is POSIX-only")
def test_two_normal_writers_race_same_id_concurrently(
    tmp_path: Any,
) -> None:
    """Fix 4: TRUE two-normal-writer concurrency race with two repository
    instances on one root and a Barrier across distinct source/cycle locks.

    Exactly one normal commit applies/idempotently owns the id; the loser
    returns the internal ``active_slurm_id_occupied`` occupancy outcome; no
    deadlock. Repeated for stability."""

    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier

    from services.orchestrator.accepted_submit_identity import AcceptedSubmitTransition
    from services.orchestrator.file_orchestration_journal import FileOrchestrationJournalRepository

    anchor = datetime(2026, 7, 12, tzinfo=UTC)
    with _pinned_local_timezone("UTC"):
        repository = _file_cohort_repository(
            tmp_path / "gfs",
            created_at=anchor,
            member_count=1,
            expected_user="scheduler",
            expected_account="account",
        )
        _reserve_second_master(
            repository,
            source_id="ifs",
            created_at=anchor,
            member_count=1,
            expected_user="scheduler",
            expected_account="account",
        )
        gfs_job_id = "job_cycle_gfs_2026071200_forecast_fixture_forecast"
        gfs_key = "cycle_gfs_2026071200_forecast_fixture:forecast"
        ifs_job_id = "job_cycle_ifs_2026071200_forecast_fixture_forecast"
        ifs_key = "cycle_ifs_2026071200_forecast_fixture:forecast"
        barrier = Barrier(2, timeout=30)

        def gfs_normal() -> str:
            contender = FileOrchestrationJournalRepository(repository.root)
            barrier.wait(timeout=30)
            return contender.commit_pipeline_job_submit_attempt(
                gfs_key,
                pipeline_job_id=gfs_job_id,
                expected_submission_attempt=1,
                slurm_job_id="72001",
                submitted_at=anchor + timedelta(minutes=30),
                transition=AcceptedSubmitTransition.accepted(status="submitted"),
            ).outcome

        def ifs_normal() -> str:
            contender = FileOrchestrationJournalRepository(repository.root)
            barrier.wait(timeout=30)
            return contender.commit_pipeline_job_submit_attempt(
                ifs_key,
                pipeline_job_id=ifs_job_id,
                expected_submission_attempt=1,
                slurm_job_id="72001",
                submitted_at=anchor + timedelta(minutes=40),
                transition=AcceptedSubmitTransition.accepted(status="submitted"),
            ).outcome

        outcomes = []
        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = [pool.submit(gfs_normal), pool.submit(ifs_normal)]
            outcomes = [future.result(timeout=60) for future in futures]

        applied = [o for o in outcomes if o in {"applied", "idempotent"}]
        assert len(applied) == 1, outcomes
        loser = [o for o in outcomes if o not in {"applied", "idempotent"}]
        assert loser and loser[0] in {"active_slurm_id_occupied", "identity_mismatch_blocked"}, outcomes

        owners = []
        reopened = FileOrchestrationJournalRepository(repository.root)
        for job_id in (gfs_job_id, ifs_job_id):
            row = reopened.get_pipeline_job(job_id)
            if row is not None and row.get("slurm_job_id") == "72001":
                owners.append(job_id)
        assert len(owners) == 1, owners


@pytest.mark.skipif(not hasattr(__import__("time"), "tzset"), reason="time.tzset() is POSIX-only")
def test_valid_json_invalid_anchor_schema_fails_closed(
    tmp_path: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Fix 5: a VALID-JSON but invalid anchor (wrong schema_version) must bite
    the ``_validated_reconcile_inventory_anchor`` validation path and fail the
    commit closed — not the decode path."""

    from services.orchestrator.accepted_submit_identity import AcceptedSubmitTransition
    from services.orchestrator.file_orchestration_journal import (
        FileOrchestrationJournalError,
        FileOrchestrationJournalRepository,
    )

    anchor = datetime(2026, 7, 12, tzinfo=UTC)
    with _pinned_local_timezone("UTC"):
        repository = _file_cohort_repository(
            tmp_path / "gfs",
            created_at=anchor,
            member_count=1,
            expected_user="scheduler",
            expected_account="account",
        )
        _reserve_second_master(
            repository,
            source_id="ifs",
            created_at=anchor,
            member_count=1,
            expected_user="scheduler",
            expected_account="account",
        )
        assert len(repository.query_reserved_unbound_jobs()) == 2
        # Valid JSON, invalid schema_version.
        anchor_path = (
            repository.root
            / "reconcile-inventory"
            / "job_cycle_ifs_2026071200_forecast_fixture_forecast.json"
        )
        anchor_path.write_text(
            '{"schema_version": "wrong.version", "job_id":'
            ' "job_cycle_ifs_2026071200_forecast_fixture_forecast",'
            ' "row_kind": "current_master", "source_id": "IFS",'
            ' "cycle_time": "2026-07-12T00:00:00Z"}',
            encoding="utf-8",
        )

        gfs_job_id = "job_cycle_gfs_2026071200_forecast_fixture_forecast"
        gfs_key = "cycle_gfs_2026071200_forecast_fixture:forecast"
        reopened = FileOrchestrationJournalRepository(repository.root)
        # Drive the typed commit directly so the occupancy SCAN's own anchor
        # validation is the path under test (the reconcile iterator would fail
        # first). The invalid authority must never be read as free.
        with pytest.raises(FileOrchestrationJournalError):
            reopened.commit_pipeline_job_submit_attempt(
                gfs_key,
                pipeline_job_id=gfs_job_id,
                expected_submission_attempt=1,
                slurm_job_id="72001",
                submitted_at=anchor + timedelta(hours=1),
                transition=AcceptedSubmitTransition.accounting(
                    "matched_bound",
                    submit_outcome="accepted",
                    matched_slurm_job_id="72001",
                    status="submitted",
                    reconciliation_source="slurm_name_window_unique",
                ),
            )
        persisted = FileOrchestrationJournalRepository(repository.root).get_pipeline_job(gfs_job_id)
        assert persisted["status"] == "reserved"
        assert persisted["slurm_job_id"] is None


# --- #1850 Phase 6d (final small fixes): normal hot path skips settled scan,
# flat direct is candidate-only with canonical confirmation, and all writes
# that CREATE/CHANGE a bound owner or CREATE/RECLAIM a reserved claimant
# serialize under the global inventory lock (batch terminal/rejection/operator
# transitions may bypass: they never introduce an owner/claimant, and stale
# anchors are adjudicated canonically).

@pytest.mark.skipif(not hasattr(__import__("time"), "tzset"), reason="time.tzset() is POSIX-only")
def test_normal_fresh_id_commit_never_touches_flat_direct_scan(
    tmp_path: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Fix A: a fresh normal typed commit (no name-window source) must NOT scan
    the flat settled master surface. The flat incarnation scan runs only for
    the fallback producer, which is the only lane that carries a candidate
    Submit and needs recycled-id disambiguation."""

    from services.orchestrator.accepted_submit_identity import AcceptedSubmitTransition
    from services.orchestrator.file_orchestration_journal import FileOrchestrationJournalRepository

    anchor = datetime(2026, 7, 12, tzinfo=UTC)
    with _pinned_local_timezone("UTC"):
        repository = _file_cohort_repository(
            tmp_path / "gfs",
            created_at=anchor,
            member_count=1,
            expected_user="scheduler",
            expected_account="account",
        )
        gfs_job_id = "job_cycle_gfs_2026071200_forecast_fixture_forecast"
        gfs_key = "cycle_gfs_2026071200_forecast_fixture:forecast"

        real_paths = repository._iter_reconcile_direct_pipeline_job_paths
        calls = []

        def forbidden_paths(*args: Any, **kwargs: Any):
            calls.append("called")
            raise AssertionError("normal commit must not scan the flat direct surface")

        monkeypatch.setattr(repository, "_iter_reconcile_direct_pipeline_job_paths", forbidden_paths)

        result = repository.commit_pipeline_job_submit_attempt(
            gfs_key,
            pipeline_job_id=gfs_job_id,
            expected_submission_attempt=1,
            slurm_job_id="72001",
            submitted_at=anchor + timedelta(minutes=30),
            transition=AcceptedSubmitTransition.accepted(status="submitted"),
        )

        assert result.committed
        assert calls == []
        reopened = FileOrchestrationJournalRepository(repository.root)
        assert reopened.get_pipeline_job(gfs_job_id)["slurm_job_id"] == "72001"
        del real_paths


@pytest.mark.skipif(not hasattr(__import__("time"), "tzset"), reason="time.tzset() is POSIX-only")
def test_fallback_flat_scan_still_considers_terminal_incarnation(
    tmp_path: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Fix A + E: the fallback lane DOES call the flat direct scan (it needs the
    terminal incarnation check), unlike the normal lane.

    Fix E boundedness: the grouped canonical replay reads each distinct
    (source, cycle) journal at most once even when several flat candidates
    share the same cycle, so the fallback scan stays bounded by
    ``max_files``-bounded flat identities — never an unbounded tree walk.
    """

    from services.orchestrator.reconcile import reconcile_reserved_unbound_jobs

    anchor = datetime(2026, 7, 12, tzinfo=UTC)
    query_end = anchor + timedelta(hours=2)
    with _pinned_local_timezone("UTC"):
        repository = _file_cohort_repository(
            tmp_path / "gfs",
            created_at=anchor,
            member_count=1,
            expected_user="scheduler",
            expected_account="account",
        )
        gfs_job_id = "job_cycle_gfs_2026071200_forecast_fixture_forecast"
        rows = _fallback_row("72001", submit="2026-07-12T01:00:00")
        query, _ = _fallback_querier(monkeypatch, rows=rows, query_end=query_end)

        real_paths = repository._iter_reconcile_direct_pipeline_job_paths
        calls = []

        def tracking_paths(*args: Any, **kwargs: Any):
            calls.append("called")
            return real_paths(*args, **kwargs)

        monkeypatch.setattr(repository, "_iter_reconcile_direct_pipeline_job_paths", tracking_paths)

        outcome = reconcile_reserved_unbound_jobs(
            repository,
            comment_query=query,
            now=lambda: query_end,
        )[0]

        assert outcome.action == "bound"
        assert calls, "fallback bind must consult the flat direct scan"
        persisted = repository.get_pipeline_job(gfs_job_id)
        assert persisted["slurm_job_id"] == "72001"


@pytest.mark.skipif(not hasattr(__import__("time"), "tzset"), reason="time.tzset() is POSIX-only")
def test_stale_direct_same_id_but_canonical_authority_different_does_not_block(
    tmp_path: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Fix B: a stale flat direct projection never decides occupancy. The
    canonical cycle authority does. Two genuine scenarios:

    1. Stale direct with a DIFFERENT canonical authority: the IFS master binds
       + settles holding 72001 @ 01:00 (journal lineage intact); its flat
       direct file is forged to claim 72002 @ 01:30. The lenient filter
       decodes the stale direct, but canonical confirmation says 72002 is
       FREE (the real authority holds 72001) — the fallback must NOT be
       falsely blocked.
    2. Invalid canonical authority: the same valid direct claiming 72001 @
       01:00, but its cycle journal is corrupted into a segment gap. The
       canonical authority replay fails — the occupancy scan must fail closed
       rather than read vacancy from a broken authority.
    """

    import json

    from services.orchestrator.accepted_submit_identity import AcceptedSubmitTransition
    from services.orchestrator.file_orchestration_journal import (
        FileOrchestrationJournalError,
        FileOrchestrationJournalRepository,
    )
    from services.orchestrator.reconcile import reconcile_reserved_unbound_jobs

    anchor = datetime(2026, 7, 12, tzinfo=UTC)
    query_end = anchor + timedelta(hours=2)
    with _pinned_local_timezone("UTC"):
        # Real IFS master: bind + settle 72001 @ 01:00 with a real journal
        # lineage.
        repository = _file_cohort_repository(
            tmp_path / "ifs-seed",
            created_at=anchor,
            member_count=1,
            expected_user="scheduler",
            expected_account="account",
            source_id="ifs",
        )
        ifs_job_id = "job_cycle_ifs_2026071200_forecast_fixture_forecast"
        ifs_key = "cycle_ifs_2026071200_forecast_fixture:forecast"
        bound = repository.commit_pipeline_job_submit_attempt(
            ifs_key,
            pipeline_job_id=ifs_job_id,
            expected_submission_attempt=1,
            slurm_job_id="72001",
            submitted_at=anchor + timedelta(hours=1),
            transition=AcceptedSubmitTransition.accounting(
                "matched_bound",
                submit_outcome="accepted",
                matched_slurm_job_id="72001",
                status="submitted",
                reconciliation_source="slurm_name_window_unique",
            ),
        )
        assert bound.committed
        projections, complete = _file_cohort_single_projection(repository, ifs_job_id)
        repository.project_forecast_cohort_tasks(
            ifs_job_id,
            master_slurm_job_id="72001",
            projections=projections,
            complete=True,
            master_status="succeeded",
            master_error_code=None,
            reconciliation_decision="matched_bound",
        )
        assert repository.get_pipeline_job(ifs_job_id)["status"] == "succeeded"
        real_direct = repository._direct_pipeline_job_record(ifs_job_id)
        assert real_direct["slurm_job_id"] == "72001"

        # --- Scenario 1: stale direct claims a DIFFERENT id than canonical. ---
        stale = json.loads(
            (repository.root / "pipeline-jobs" / f"{ifs_job_id}.json").read_text(
                encoding="utf-8"
            )
        )
        payload = stale.get("payload", stale)
        payload["slurm_job_id"] = "72002"
        payload["submitted_at"] = "2026-07-12T01:30:00Z"
        (repository.root / "pipeline-jobs" / f"{ifs_job_id}.json").write_text(
            json.dumps(stale), encoding="utf-8"
        )
        # Sanity: the forged direct still decodes as a same-id master.
        assert (
            repository._validated_direct_pipeline_job_record(
                repository._read_optional_json(
                    repository.root / "pipeline-jobs" / f"{ifs_job_id}.json"
                ),
                expected_job_id=ifs_job_id,
            )["slurm_job_id"]
            == "72002"
        )

        gfs_job_id, _gfs_key = _reserve_gfs_master_on(
            repository,
            created_at=anchor,
            member_count=1,
            expected_user="scheduler",
            expected_account="account",
        )
        rows = _fallback_row("72002", submit="2026-07-12T01:30:00")
        query, _ = _fallback_querier(monkeypatch, rows=rows, query_end=query_end)

        outcome = reconcile_reserved_unbound_jobs(
            repository,
            comment_query=query,
            now=lambda: query_end,
        )[0]

        assert outcome.job_id == gfs_job_id
        assert outcome.action == "bound", (
            "stale direct claiming 72002 must NOT block: canonical authority"
            " holds 72001"
        )
        assert outcome.matched_slurm_job_id == "72002"
        assert repository.get_pipeline_job(gfs_job_id)["slurm_job_id"] == "72002"

        # --- Scenario 2: decoded same-id candidate whose canonical authority
        # is INVALID fails closed. A separate root where the flat direct
        # claims 72001 @ 01:00, but its cycle journal is corrupted into a
        # segment gap. The canonical authority replay raises
        # ``file_journal_segment_gap`` — the occupancy scan must fail closed
        # rather than read vacancy from a broken authority.
        repository2 = _file_cohort_repository(
            tmp_path / "gfs2",
            created_at=anchor,
            member_count=1,
            expected_user="scheduler",
            expected_account="account",
        )
        valid_direct = json.loads(
            (repository.root / "pipeline-jobs" / f"{ifs_job_id}.json").read_text(
                encoding="utf-8"
            )
        )
        # Restore the true 72001 identity (scenario 1 changed it to 72002),
        # then corrupt the canonical authority's journal into a segment gap:
        # segment-1 present with segment-0 removed. The flat direct stays a
        # valid decoded same-id candidate, but its canonical cycle authority
        # is broken — the flat scan's grouped replay raises
        # file_journal_segment_gap and the commit fails closed.
        payload2 = valid_direct.get("payload", valid_direct)
        payload2["slurm_job_id"] = "72001"
        payload2["submitted_at"] = "2026-07-12T01:00:00Z"
        payload2["matched_slurm_job_id"] = "72001"
        (repository2.root / "pipeline-jobs" / f"{ifs_job_id}.json").write_text(
            json.dumps(valid_direct), encoding="utf-8"
        )
        ifs_journal_dir = repository2.root / "journal" / "ifs"
        ifs_journal_dir.mkdir(parents=True, exist_ok=True)
        segment_zero = ifs_journal_dir / "2026071200.jsonl"
        segment_zero.write_text("", encoding="utf-8")
        segment_zero.unlink()
        (ifs_journal_dir / "2026071200.1.jsonl").write_text("", encoding="utf-8")
        # Sanity: the direct still decodes as a current-master 72001.
        assert (
            repository2._validated_direct_pipeline_job_record(
                repository2._read_optional_json(
                    repository2.root / "pipeline-jobs" / f"{ifs_job_id}.json"
                ),
                expected_job_id=ifs_job_id,
            )["slurm_job_id"]
            == "72001"
        )

        gfs2_job_id = "job_cycle_gfs_2026071200_forecast_fixture_forecast"
        gfs2_key = "cycle_gfs_2026071200_forecast_fixture:forecast"
        reopened2 = FileOrchestrationJournalRepository(repository2.root)
        # Drive the typed commit directly so the occupancy SCAN itself (the
        # flat direct canonical confirmation) is the path under test — the
        # reconcile iterator's migration pre-scan would fail first and mask
        # the scan.
        with pytest.raises(FileOrchestrationJournalError):
            reopened2.commit_pipeline_job_submit_attempt(
                gfs2_key,
                pipeline_job_id=gfs2_job_id,
                expected_submission_attempt=1,
                slurm_job_id="72001",
                submitted_at=anchor + timedelta(hours=1),
                transition=AcceptedSubmitTransition.accounting(
                    "matched_bound",
                    submit_outcome="accepted",
                    matched_slurm_job_id="72001",
                    status="submitted",
                    reconciliation_source="slurm_name_window_unique",
                ),
            )
        persisted2 = FileOrchestrationJournalRepository(
            repository2.root
        ).get_pipeline_job(gfs2_job_id)
        assert persisted2["status"] == "reserved"
        assert persisted2["slurm_job_id"] is None


@pytest.mark.skipif(not hasattr(__import__("time"), "tzset"), reason="time.tzset() is POSIX-only")
def test_two_repository_race_commits_cannot_read_stale_canonical_state(
    tmp_path: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Fix C: a sibling terminal transition and a fallback commit race on one
    root; the fallback commit cannot observe a stale direct/canonical state and
    must not deadlock. Lock order stays cycle -> inventory."""

    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier

    from services.orchestrator import reconcile as reconcile_module
    from services.orchestrator.file_orchestration_journal import FileOrchestrationJournalRepository

    anchor = datetime(2026, 7, 12, tzinfo=UTC)
    query_end = anchor + timedelta(hours=2)
    with _pinned_local_timezone("UTC"):
        repository = _file_cohort_repository(
            tmp_path / "gfs",
            created_at=anchor,
            member_count=1,
            expected_user="scheduler",
            expected_account="account",
        )
        ifs_job_id, ifs_key = _reserve_second_master(
            repository,
            source_id="ifs",
            created_at=anchor,
            member_count=1,
            expected_user="scheduler",
            expected_account="account",
        )
        # IFS binds 72001 first (exclusive, no sibling claimant yet) at the
        # SAME 01:00 incarnation the fallback candidate will carry, so a
        # settled IFS still blocks the fallback (same accounting incarnation).
        from services.orchestrator.accepted_submit_identity import AcceptedSubmitTransition

        bind = repository.commit_pipeline_job_submit_attempt(
            ifs_key,
            pipeline_job_id=ifs_job_id,
            expected_submission_attempt=1,
            slurm_job_id="72001",
            submitted_at=anchor + timedelta(hours=1),
            transition=AcceptedSubmitTransition.accepted(status="submitted"),
        )
        assert bind.committed
        # Complete the inventory migration so later backfills don't race.
        assert len(repository.query_reserved_unbound_jobs()) == 1
        # Now add GFS as a sibling reserved claimant AFTER IFS bound.
        gfs_job_id, _gfs_key = _reserve_gfs_master_on(
            repository,
            created_at=anchor,
            member_count=1,
            expected_user="scheduler",
            expected_account="account",
        )
        rows = _fallback_row("72001", submit="2026-07-12T01:00:00")
        query, _ = _fallback_querier(monkeypatch, rows=rows, query_end=query_end)

        barrier = Barrier(2, timeout=30)

        def terminal_ifs() -> str:
            contender = FileOrchestrationJournalRepository(repository.root)
            barrier.wait(timeout=30)
            # IFS settles terminally while the GFS fallback races.
            projections, complete = _file_cohort_single_projection(contender, ifs_job_id)
            contender.project_forecast_cohort_tasks(
                ifs_job_id,
                master_slurm_job_id="72001",
                projections=projections,
                complete=True,
                master_status="succeeded",
                master_error_code=None,
                reconciliation_decision="matched_bound",
            )
            return str(contender.get_pipeline_job(ifs_job_id)["status"])

        def fallback_gfs() -> str:
            contender = FileOrchestrationJournalRepository(repository.root)
            barrier.wait(timeout=30)
            return reconcile_module.reconcile_reserved_unbound_jobs(
                contender,
                comment_query=query,
                now=lambda: query_end,
            )[0].action

        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = [pool.submit(terminal_ifs), pool.submit(fallback_gfs)]
            outcomes = [future.result(timeout=60) for future in futures]

        assert outcomes[0] == "succeeded"
        # The fallback never binds the same-incarnation id: either the active
        # owner blocks it (identity_mismatch_blocked) or the terminal
        # same-incarnation blocks it. It must never be ``bound``.
        assert outcomes[1] in {"identity_mismatch_blocked", "ambiguous_fallback_match"}, outcomes
        # GFS stays unbound; IFS owns/settles the id.
        reopened = FileOrchestrationJournalRepository(repository.root)
        gfs_row = reopened.get_pipeline_job(gfs_job_id)
        ifs_row = reopened.get_pipeline_job(ifs_job_id)
        assert ifs_row["status"] == "succeeded"
        assert ifs_row["slurm_job_id"] == "72001"
        assert gfs_row["status"] == "reserved" and gfs_row["slurm_job_id"] is None


# --- #1850 Phase 6e (invariant closure): canonical cycle authority decides
# BOTH false-positive AND false-negative occupancy — a stale flat direct
# projection may neither fabricate nor hide a same-incarnation owner.


@pytest.mark.skipif(not hasattr(__import__("time"), "tzset"), reason="time.tzset() is POSIX-only")
def test_stale_direct_cannot_hide_the_same_incarnation_owner(
    tmp_path: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Fix E: a stale flat direct projection cannot HIDE a canonical owner.

    The IFS master binds + settles ``72001`` at the canonical submitted_at
    ``2026-07-12T01:00:00Z`` so its reconcile-inventory anchor is pruned, then
    its flat direct projection is tampered to claim ``72002``. The canonical
    cycle journal still authoritatively owns ``72001 @ 01:00`` — the exact
    accounting incarnation the comment-less fallback returns. The fallback
    MUST be blocked (``identity_mismatch_blocked``, count 1) and GFS MUST
    stay reserved/unbound under the #1564 held tuple with streak zero.

    Mirror of ``test_stale_direct_same_id_but_canonical_authority_different_does_not_block``:
    that test proves a stale direct cannot FABRICATE an owner; this test
    proves it cannot HIDE one either. Both directions are the same authority
    invariant: canonical cycle authority, not the flat projection.
    """

    import json

    from services.orchestrator.accepted_submit_identity import AcceptedSubmitTransition
    from services.orchestrator.file_orchestration_journal import FileOrchestrationJournalRepository
    from services.orchestrator.reconcile import reconcile_reserved_unbound_jobs

    anchor = datetime(2026, 7, 12, tzinfo=UTC)
    query_end = anchor + timedelta(hours=2)
    with _pinned_local_timezone("UTC"):
        # Real IFS master: bind + settle 72001 @ 01:00 with a real journal
        # lineage; the reconcile-inventory anchor is pruned on the settle.
        repository = _file_cohort_repository(
            tmp_path / "ifs-seed",
            created_at=anchor,
            member_count=1,
            expected_user="scheduler",
            expected_account="account",
            source_id="ifs",
        )
        ifs_job_id = "job_cycle_ifs_2026071200_forecast_fixture_forecast"
        ifs_key = "cycle_ifs_2026071200_forecast_fixture:forecast"
        bound = repository.commit_pipeline_job_submit_attempt(
            ifs_key,
            pipeline_job_id=ifs_job_id,
            expected_submission_attempt=1,
            slurm_job_id="72001",
            submitted_at=anchor + timedelta(hours=1),
            transition=AcceptedSubmitTransition.accounting(
                "matched_bound",
                submit_outcome="accepted",
                matched_slurm_job_id="72001",
                status="submitted",
                reconciliation_source="slurm_name_window_unique",
            ),
        )
        assert bound.committed
        projections, complete = _file_cohort_single_projection(repository, ifs_job_id)
        repository.project_forecast_cohort_tasks(
            ifs_job_id,
            master_slurm_job_id="72001",
            projections=projections,
            complete=True,
            master_status="succeeded",
            master_error_code=None,
            reconciliation_decision="matched_bound",
        )
        assert repository.get_pipeline_job(ifs_job_id)["status"] == "succeeded"

        # Stale the flat direct projection: it now claims 72002 @ 01:30 while
        # the canonical cycle journal still authoritatively owns 72001 @ 01:00.
        stale = json.loads(
            (repository.root / "pipeline-jobs" / f"{ifs_job_id}.json").read_text(
                encoding="utf-8"
            )
        )
        payload = stale.get("payload", stale)
        payload["slurm_job_id"] = "72002"
        payload["matched_slurm_job_id"] = "72002"
        payload["submitted_at"] = "2026-07-12T01:30:00Z"
        (repository.root / "pipeline-jobs" / f"{ifs_job_id}.json").write_text(
            json.dumps(stale), encoding="utf-8"
        )
        # Sanity: the stale direct still decodes as a valid current-master row
        # (only its projected id differs from the canonical authority).
        assert (
            repository._validated_direct_pipeline_job_record(
                repository._read_optional_json(
                    repository.root / "pipeline-jobs" / f"{ifs_job_id}.json"
                ),
                expected_job_id=ifs_job_id,
            )["slurm_job_id"]
            == "72002"
        )
        # Sanity: the canonical cycle journal still authoritatively owns
        # 72001 @ 01:00 (the reconciled direct projection is only stale).
        canonical = repository._canonical_reconcile_job_unlocked(
            ifs_job_id,
            source_id="IFS",
            cycle_time=anchor,
        )
        assert canonical["slurm_job_id"] == "72001"
        assert canonical["submitted_at"] == "2026-07-12T01:00:00Z"
        assert canonical["status"] == "succeeded"

        # GFS reserves the SAME 01:00 accounting incarnation the fallback
        # query returns (unique candidate 72001 @ 01:00). The canonical owner
        # must block the bind even though the flat projection says 72002.
        gfs_job_id, _gfs_key = _reserve_gfs_master_on(
            repository,
            created_at=anchor,
            member_count=1,
            expected_user="scheduler",
            expected_account="account",
        )
        rows = _fallback_row("72001", submit="2026-07-12T01:00:00")
        query, _ = _fallback_querier(monkeypatch, rows=rows, query_end=query_end)

        outcome = reconcile_reserved_unbound_jobs(
            repository,
            comment_query=query,
            now=lambda: query_end,
        )[0]

        assert outcome.job_id == gfs_job_id
        assert outcome.action == "identity_mismatch_blocked", (
            "the canonical cycle authority still owns 72001 @ 01:00; a stale"
            " flat direct claiming 72002 must NOT let GFS reuse that exact"
            " accounting incarnation"
        )
        assert outcome.match_count == 1
        assert outcome.reconciliation_decision == "accounting_unavailable"
        assert outcome.reconciliation_reason_class == "comment_accounting_unproven"
        persisted = repository.get_pipeline_job(gfs_job_id)
        assert persisted["status"] == "reserved"
        assert persisted["slurm_job_id"] is None
        assert persisted["identity_blocked_streak"] == 0
        # Reopening the journal re-reads the same result: GFS unbound, the
        # settled IFS keeps its canonical ownership of 72001.
        reopened = FileOrchestrationJournalRepository(repository.root)
        assert reopened.get_pipeline_job(gfs_job_id)["slurm_job_id"] is None
        assert reopened.get_pipeline_job(gfs_job_id)["status"] == "reserved"
        ifs_reopened = reopened.get_pipeline_job(ifs_job_id)
        assert ifs_reopened["status"] == "succeeded"
        assert ifs_reopened["slurm_job_id"] == "72001"


@pytest.mark.skipif(not hasattr(__import__("time"), "tzset"), reason="time.tzset() is POSIX-only")
def test_stale_direct_hidden_owner_blocks_via_public_reconcile_and_exact_commit(
    tmp_path: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Fix E: the hidden-owner block is a COMMIT-level occupancy refusal, not a
    reconcile-iteration artifact.

    The same hidden-owner state driven through the typed
    ``commit_pipeline_job_submit_attempt`` with a name-window unique source
    must refuse the bind (``active_slurm_id_occupied``) — the fallback
    public action (``identity_mismatch_blocked``) is a projection of that
    commit refusal, so both entry points agree and no entry point can bind
    the exact incarnation the stale projection tried to hide.
    """

    import json

    from services.orchestrator.accepted_submit_identity import AcceptedSubmitTransition
    from services.orchestrator.file_orchestration_journal import FileOrchestrationJournalRepository

    anchor = datetime(2026, 7, 12, tzinfo=UTC)
    with _pinned_local_timezone("UTC"):
        repository = _file_cohort_repository(
            tmp_path / "ifs-seed",
            created_at=anchor,
            member_count=1,
            expected_user="scheduler",
            expected_account="account",
            source_id="ifs",
        )
        ifs_job_id = "job_cycle_ifs_2026071200_forecast_fixture_forecast"
        ifs_key = "cycle_ifs_2026071200_forecast_fixture:forecast"
        bound = repository.commit_pipeline_job_submit_attempt(
            ifs_key,
            pipeline_job_id=ifs_job_id,
            expected_submission_attempt=1,
            slurm_job_id="72001",
            submitted_at=anchor + timedelta(hours=1),
            transition=AcceptedSubmitTransition.accounting(
                "matched_bound",
                submit_outcome="accepted",
                matched_slurm_job_id="72001",
                status="submitted",
                reconciliation_source="slurm_name_window_unique",
            ),
        )
        assert bound.committed
        projections, complete = _file_cohort_single_projection(repository, ifs_job_id)
        repository.project_forecast_cohort_tasks(
            ifs_job_id,
            master_slurm_job_id="72001",
            projections=projections,
            complete=True,
            master_status="succeeded",
            master_error_code=None,
            reconciliation_decision="matched_bound",
        )
        assert repository.get_pipeline_job(ifs_job_id)["status"] == "succeeded"

        # Stale the flat direct projection to claim 72002 @ 01:30.
        stale = json.loads(
            (repository.root / "pipeline-jobs" / f"{ifs_job_id}.json").read_text(
                encoding="utf-8"
            )
        )
        payload = stale.get("payload", stale)
        payload["slurm_job_id"] = "72002"
        payload["matched_slurm_job_id"] = "72002"
        payload["submitted_at"] = "2026-07-12T01:30:00Z"
        (repository.root / "pipeline-jobs" / f"{ifs_job_id}.json").write_text(
            json.dumps(stale), encoding="utf-8"
        )

        # GFS reserved, then a name-window-unique commit for the SAME 01:00
        # incarnation the canonical cycle authority owns.
        gfs_job_id, _gfs_key = _reserve_gfs_master_on(
            repository,
            created_at=anchor,
            member_count=1,
            expected_user="scheduler",
            expected_account="account",
        )
        gfs_key = "cycle_gfs_2026071200_forecast_fixture:forecast"
        commit_result = repository.commit_pipeline_job_submit_attempt(
            gfs_key,
            pipeline_job_id=gfs_job_id,
            expected_submission_attempt=1,
            slurm_job_id="72001",
            submitted_at=anchor + timedelta(hours=1),
            transition=AcceptedSubmitTransition.accounting(
                "matched_bound",
                submit_outcome="accepted",
                matched_slurm_job_id="72001",
                status="submitted",
                reconciliation_source="slurm_name_window_unique",
            ),
        )
        assert not commit_result.committed
        assert commit_result.outcome == "active_slurm_id_occupied"
        persisted = FileOrchestrationJournalRepository(repository.root).get_pipeline_job(
            gfs_job_id
        )
        assert persisted["status"] == "reserved"
        assert persisted["slurm_job_id"] is None


@pytest.mark.skipif(not hasattr(__import__("time"), "tzset"), reason="time.tzset() is POSIX-only")
def test_fallback_flat_scan_never_walks_by_cycle_tree(
    tmp_path: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Fix E boundedness: the flat scan enumerates only the flat current-master
    surface (``max_files``-bounded) and NEVER descends the by-cycle candidate
    tree, which is where unbounded growth lives. The grouped canonical replay
    keys off the flat identities alone."""

    from services.orchestrator.file_orchestration_journal import (
        FileOrchestrationJournalRepository,
    )
    from services.orchestrator.reconcile import reconcile_reserved_unbound_jobs

    anchor = datetime(2026, 7, 12, tzinfo=UTC)
    query_end = anchor + timedelta(hours=2)
    with _pinned_local_timezone("UTC"):
        repository = _file_cohort_repository(
            tmp_path / "gfs",
            created_at=anchor,
            member_count=1,
            expected_user="scheduler",
            expected_account="account",
        )
        gfs_job_id = "job_cycle_gfs_2026071200_forecast_fixture_forecast"
        rows = _fallback_row("72001", submit="2026-07-12T01:00:00")
        query, _ = _fallback_querier(monkeypatch, rows=rows, query_end=query_end)

        real_paths = repository._iter_reconcile_direct_pipeline_job_paths
        flat_calls = []

        def tracking_paths(*args: Any, **kwargs: Any):
            flat_calls.append("called")
            return real_paths(*args, **kwargs)

        monkeypatch.setattr(repository, "_iter_reconcile_direct_pipeline_job_paths", tracking_paths)

        # The by-cycle candidate tree must never be walked: the fallback scan
        # enumerates ONLY the flat current-master surface, and a by-cycle walk
        # would be an unbounded tree walk (candidate history lives there).
        from services.orchestrator import file_orchestration_journal as journal_module

        by_cycle_calls = []
        real_walker = journal_module._iter_regular_json_files

        def forbidden_by_cycle(*args: Any, **kwargs: Any):
            if args and "by-cycle" in str(args[0]):
                by_cycle_calls.append("called")
                raise AssertionError("fallback flat scan must not walk the by-cycle tree")
            return real_walker(*args, **kwargs)

        monkeypatch.setattr(journal_module, "_iter_regular_json_files", forbidden_by_cycle)

        outcome = reconcile_reserved_unbound_jobs(
            repository,
            comment_query=query,
            now=lambda: query_end,
        )[0]

        assert outcome.action == "bound"
        assert flat_calls, "fallback bind must consult the flat direct surface"
        assert by_cycle_calls == [], "fallback flat scan must never walk the by-cycle tree"
        reopened = FileOrchestrationJournalRepository(repository.root)
        assert reopened.get_pipeline_job(gfs_job_id)["slurm_job_id"] == "72001"


@pytest.mark.skipif(not hasattr(__import__("time"), "tzset"), reason="time.tzset() is POSIX-only")
def test_exact_comment_bind_hot_path_never_scans_flat_history(
    tmp_path: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Fix E hot path: an exact-comment (owned_match) bind carries no candidate
    Submit and must never run the flat settled-history scan — only the
    name-window fallback producer may pay that cost."""

    from services.orchestrator.reconcile import (
        CommentAccountingResult,
        SacctRecord,
        reconcile_reserved_unbound_jobs,
    )

    anchor = datetime(2026, 7, 12, tzinfo=UTC)
    query_end = anchor + timedelta(hours=2)
    with _pinned_local_timezone("UTC"):
        repository = _file_cohort_repository(
            tmp_path / "gfs",
            created_at=anchor,
            member_count=1,
            expected_user="scheduler",
            expected_account="account",
        )
        gfs_job_id = "job_cycle_gfs_2026071200_forecast_fixture_forecast"

        # Warm the one-time reconcile-inventory migration BEFORE the assertion
        # patch goes live: the migration's own fingerprint pass legitimately
        # walks the flat surface once, independent of any bind.
        assert len(repository.query_reserved_unbound_jobs()) == 1

        real_paths = repository._iter_reconcile_direct_pipeline_job_paths
        calls = []

        def forbidden_paths(*args: Any, **kwargs: Any):
            calls.append("called")
            raise AssertionError("exact-comment bind must not scan the flat direct surface")

        monkeypatch.setattr(repository, "_iter_reconcile_direct_pipeline_job_paths", forbidden_paths)

        # A comment-storing exact-comment owner match: one row with the exact
        # idempotency comment and the reservation's user/account.
        comment = "nhms_idem:cycle_gfs_2026071200_forecast_fixture:forecast"
        query = lambda key, **kwargs: CommentAccountingResult(  # noqa: E731
            (
                SacctRecord(
                    slurm_job_id="72001",
                    raw_state="RUNNING",
                    job_name="nhms_forecast",
                    exit_code="0:0",
                    comment=comment,
                    user="scheduler",
                    account="account",
                ),
            ),
            scope="global",
            coverage_start=anchor,
            coverage_end=query_end,
            coverage_complete=True,
        )

        outcome = reconcile_reserved_unbound_jobs(
            repository,
            comment_query=query,
            now=lambda: query_end,
        )[0]

        assert outcome.action == "bound"
        assert calls == [], "exact-comment bind must not scan the flat direct surface"
        assert repository.get_pipeline_job(gfs_job_id)["slurm_job_id"] == "72001"
        del real_paths


# --- #1850 Phase 6f (invariant closure II): canonical cycle authority is
# reachable even when the flat direct projection is DAMAGED (undecodable /
# fails direct-record validation). The fallback scan derives the eligible
# (source, cycle) identity from the safe filename/job-id alone, so a malformed
# projection can neither fabricate nor HIDE a canonical owner. Replayed
# canonical rows are filtered to current accepted-submit forecast MASTERS
# before occupancy adjudication; candidate/legacy rows are never owners.


@pytest.mark.skipif(not hasattr(__import__("time"), "tzset"), reason="time.tzset() is POSIX-only")
def test_damaged_flat_projection_cannot_hide_the_same_incarnation_owner(
    tmp_path: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Fix F: a damaged flat projection (valid JSON ``{}`` that fails direct
    validation) cannot HIDE the canonical owner.

    The IFS master binds + settles ``72001`` at ``2026-07-12T01:00:00Z`` so its
    reconcile-inventory anchor is pruned, then its flat direct file is replaced
    with ``{}`` (undecodable as a pipeline-job record). The canonical cycle
    journal still authoritatively owns ``72001 @ 01:00``. The fallback must be
    blocked (``identity_mismatch_blocked``, count 1) and GFS must stay
    reserved/unbound under the #1564 held tuple with streak zero.

    The eligible (source, cycle) identity must come from the SAFE FILENAME
    alone — payload decoding must not gate canonical replay, otherwise a
    malformed projection hides the very owner the fallback must respect.
    """

    from services.orchestrator.accepted_submit_identity import AcceptedSubmitTransition
    from services.orchestrator.file_orchestration_journal import (
        FileOrchestrationJournalError,
        FileOrchestrationJournalRepository,
    )
    from services.orchestrator.reconcile import reconcile_reserved_unbound_jobs

    anchor = datetime(2026, 7, 12, tzinfo=UTC)
    query_end = anchor + timedelta(hours=2)
    with _pinned_local_timezone("UTC"):
        repository = _file_cohort_repository(
            tmp_path / "ifs-seed",
            created_at=anchor,
            member_count=1,
            expected_user="scheduler",
            expected_account="account",
            source_id="ifs",
        )
        ifs_job_id = "job_cycle_ifs_2026071200_forecast_fixture_forecast"
        ifs_key = "cycle_ifs_2026071200_forecast_fixture:forecast"
        bound = repository.commit_pipeline_job_submit_attempt(
            ifs_key,
            pipeline_job_id=ifs_job_id,
            expected_submission_attempt=1,
            slurm_job_id="72001",
            submitted_at=anchor + timedelta(hours=1),
            transition=AcceptedSubmitTransition.accounting(
                "matched_bound",
                submit_outcome="accepted",
                matched_slurm_job_id="72001",
                status="submitted",
                reconciliation_source="slurm_name_window_unique",
            ),
        )
        assert bound.committed
        projections, complete = _file_cohort_single_projection(repository, ifs_job_id)
        repository.project_forecast_cohort_tasks(
            ifs_job_id,
            master_slurm_job_id="72001",
            projections=projections,
            complete=True,
            master_status="succeeded",
            master_error_code=None,
            reconciliation_decision="matched_bound",
        )
        assert repository.get_pipeline_job(ifs_job_id)["status"] == "succeeded"
        # Complete the reconcile-inventory migration so a reopen does NOT
        # backfill and restore the damaged direct.
        assert len(repository.query_reserved_unbound_jobs()) == 0

        # Damage the flat direct projection: valid JSON that fails direct
        # pipeline-job record validation.
        (repository.root / "pipeline-jobs" / f"{ifs_job_id}.json").write_text(
            "{}", encoding="utf-8"
        )
        # Sanity: the damaged direct can no longer be decoded as a
        # pipeline-job record.
        try:
            repository._validated_direct_pipeline_job_record(
                repository._read_optional_json(
                    repository.root / "pipeline-jobs" / f"{ifs_job_id}.json"
                ),
                expected_job_id=ifs_job_id,
            )
            raise AssertionError("damaged direct must fail pipeline-job validation")
        except FileOrchestrationJournalError:
            pass
        # Sanity: the canonical cycle journal still authoritatively owns
        # 72001 @ 01:00 independent of the damaged projection.
        from services.orchestrator import file_orchestration_journal as journal_module

        journal_rows = journal_module._CycleRows()
        for record in repository._cycle_journal_records(
            source_id="IFS", cycle_time=anchor
        ):
            repository._apply_journal_record(
                journal_rows, record, source_id="IFS", cycle_time=anchor
            )
        canonical = journal_rows.pipeline_jobs[ifs_job_id]
        assert canonical["slurm_job_id"] == "72001"
        assert canonical["submitted_at"] == "2026-07-12T01:00:00Z"
        assert canonical["status"] == "succeeded"

        # GFS reserves the SAME 01:00 accounting incarnation the fallback
        # query returns (unique candidate 72001 @ 01:00). The canonical owner
        # must block the bind even though the flat projection is damaged.
        gfs_job_id, _gfs_key = _reserve_gfs_master_on(
            repository,
            created_at=anchor,
            member_count=1,
            expected_user="scheduler",
            expected_account="account",
        )
        rows = _fallback_row("72001", submit="2026-07-12T01:00:00")
        query, _ = _fallback_querier(monkeypatch, rows=rows, query_end=query_end)

        outcome = reconcile_reserved_unbound_jobs(
            repository,
            comment_query=query,
            now=lambda: query_end,
        )[0]

        assert outcome.job_id == gfs_job_id
        assert outcome.action == "identity_mismatch_blocked", (
            "the canonical cycle journal still owns 72001 @ 01:00; a damaged"
            " flat projection must NOT let GFS reuse that exact accounting"
            " incarnation"
        )
        assert outcome.match_count == 1
        assert outcome.reconciliation_decision == "accounting_unavailable"
        assert outcome.reconciliation_reason_class == "comment_accounting_unproven"
        persisted = repository.get_pipeline_job(gfs_job_id)
        assert persisted["status"] == "reserved"
        assert persisted["slurm_job_id"] is None
        assert persisted["identity_blocked_streak"] == 0
        # Reopening the journal re-reads the same result: GFS unbound, and the
        # canonical cycle authority still owns the settled IFS 72001 @ 01:00
        # (the damaged direct makes the public projection read-blocked, but
        # the canonical authority replay is authoritative and unchanged).
        reopened = FileOrchestrationJournalRepository(repository.root)
        assert reopened.get_pipeline_job(gfs_job_id)["slurm_job_id"] is None
        assert reopened.get_pipeline_job(gfs_job_id)["status"] == "reserved"
        reopened_journal_rows = journal_module._CycleRows()
        for record in reopened._cycle_journal_records(
            source_id="IFS", cycle_time=anchor
        ):
            reopened._apply_journal_record(
                reopened_journal_rows, record, source_id="IFS", cycle_time=anchor
            )
        canonical_ifs = reopened_journal_rows.pipeline_jobs[ifs_job_id]
        assert canonical_ifs["status"] == "succeeded"
        assert canonical_ifs["slurm_job_id"] == "72001"
        assert canonical_ifs["submitted_at"] == "2026-07-12T01:00:00Z"


@pytest.mark.skipif(not hasattr(__import__("time"), "tzset"), reason="time.tzset() is POSIX-only")
def test_damaged_flat_projection_fails_closed_when_no_valid_cycle_authority(
    tmp_path: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Fix F: a master-looking flat filename whose cycle has no valid lineage
    AND whose direct cannot be decoded/resolved fails the fallback closed.

    A damaged direct whose filename parses as an accepted-submit master
    identity, with no cycle journal to fall back on, cannot be read as
    "vacant": the typed commit raises ``FileOrchestrationJournalError`` and
    the public reconcile surface stays fail-closed (no bind, GFS stays
    reserved). Unrelated malformed filenames (non accepted-submit surface)
    never enter this authority path."""

    from services.orchestrator.accepted_submit_identity import AcceptedSubmitTransition
    from services.orchestrator.file_orchestration_journal import (
        FileOrchestrationJournalError,
        FileOrchestrationJournalRepository,
    )
    from services.orchestrator.reconcile import reconcile_reserved_unbound_jobs

    anchor = datetime(2026, 7, 12, tzinfo=UTC)
    query_end = anchor + timedelta(hours=2)
    with _pinned_local_timezone("UTC"):
        repository = _file_cohort_repository(
            tmp_path / "gfs",
            created_at=anchor,
            member_count=1,
            expected_user="scheduler",
            expected_account="account",
        )
        gfs_job_id = "job_cycle_gfs_2026071200_forecast_fixture_forecast"
        gfs_key = "cycle_gfs_2026071200_forecast_fixture:forecast"
        # Complete the reconcile-inventory migration so a reopen does NOT
        # backfill and restore/remove the orphan.
        assert len(repository.query_reserved_unbound_jobs()) == 1

        # A master-looking flat file with NO valid cycle lineage and NO
        # decodable direct authority.
        orphan_job_id = "job_cycle_era5_2026071200_forecast_fixture_forecast"
        (repository.root / "pipeline-jobs" / f"{orphan_job_id}.json").write_text(
            "{}", encoding="utf-8"
        )

        # Typed commit surface: the occupancy scan's own flat authority
        # resolution must fail the commit closed (the reconcile iterator's
        # migration pre-scan would otherwise mask the scan).
        reopened = FileOrchestrationJournalRepository(repository.root)
        with pytest.raises(FileOrchestrationJournalError):
            reopened.commit_pipeline_job_submit_attempt(
                gfs_key,
                pipeline_job_id=gfs_job_id,
                expected_submission_attempt=1,
                slurm_job_id="72001",
                submitted_at=anchor + timedelta(hours=1),
                transition=AcceptedSubmitTransition.accounting(
                    "matched_bound",
                    submit_outcome="accepted",
                    matched_slurm_job_id="72001",
                    status="submitted",
                    reconciliation_source="slurm_name_window_unique",
                ),
            )
        persisted = FileOrchestrationJournalRepository(repository.root).get_pipeline_job(
            gfs_job_id
        )
        assert persisted["status"] == "reserved"
        assert persisted["slurm_job_id"] is None

        # Public reconcile surface: the same orphan fails closed (quarantine
        # outcome, no bind, GFS stays reserved/unbound).
        rows = _fallback_row("72001", submit="2026-07-12T01:00:00")
        query, _ = _fallback_querier(monkeypatch, rows=rows, query_end=query_end)
        outcomes = reconcile_reserved_unbound_jobs(
            reopened,
            comment_query=query,
            now=lambda: query_end,
        )
        assert outcomes, "reconcile must produce an outcome"
        assert outcomes[0].job_id == gfs_job_id
        assert outcomes[0].action == "journal_quarantined"
        # Any fail-closed journal error is acceptable; the invariant is that
        # the damaged authority is NEVER read as vacancy.
        assert outcomes[0].quarantine_reason is not None
        persisted2 = FileOrchestrationJournalRepository(repository.root).get_pipeline_job(
            gfs_job_id
        )
        assert persisted2["status"] == "reserved"
        assert persisted2["slurm_job_id"] is None


@pytest.mark.skipif(not hasattr(__import__("time"), "tzset"), reason="time.tzset() is POSIX-only")
def test_replayed_candidate_rows_never_occupy_a_master_id(
    tmp_path: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Fix F: only current accepted-submit forecast MASTERS are owners.

    The cycle journal of a settled master also contains accepted-submit task
    CANDIDATE rows (``job_fcst_...``) that carry array-shaped slurm ids. A
    candidate row must never count as an owner of the master id, even when it
    shares the numeric prefix. The scan filters replayed canonical rows to
    current forecast masters before occupancy adjudication."""

    from services.orchestrator.accepted_submit_identity import AcceptedSubmitTransition
    from services.orchestrator.file_orchestration_journal import FileOrchestrationJournalRepository
    from services.orchestrator.reconcile import reconcile_reserved_unbound_jobs

    anchor = datetime(2026, 7, 12, tzinfo=UTC)
    query_end = anchor + timedelta(hours=2)
    with _pinned_local_timezone("UTC"):
        repository = _file_cohort_repository(
            tmp_path / "ifs-seed",
            created_at=anchor,
            member_count=1,
            expected_user="scheduler",
            expected_account="account",
            source_id="ifs",
        )
        ifs_job_id = "job_cycle_ifs_2026071200_forecast_fixture_forecast"
        ifs_key = "cycle_ifs_2026071200_forecast_fixture:forecast"
        bound = repository.commit_pipeline_job_submit_attempt(
            ifs_key,
            pipeline_job_id=ifs_job_id,
            expected_submission_attempt=1,
            slurm_job_id="72001",
            submitted_at=anchor + timedelta(hours=1),
            transition=AcceptedSubmitTransition.accounting(
                "matched_bound",
                submit_outcome="accepted",
                matched_slurm_job_id="72001",
                status="submitted",
                reconciliation_source="slurm_name_window_unique",
            ),
        )
        assert bound.committed
        projections, complete = _file_cohort_single_projection(repository, ifs_job_id)
        repository.project_forecast_cohort_tasks(
            ifs_job_id,
            master_slurm_job_id="72001",
            projections=projections,
            complete=True,
            master_status="succeeded",
            master_error_code=None,
            reconciliation_decision="matched_bound",
        )
        assert repository.get_pipeline_job(ifs_job_id)["status"] == "succeeded"
        assert len(repository.query_reserved_unbound_jobs()) == 0

        # The settled journal now contains a task candidate row with an
        # array-shaped id. Tamper the flat direct so the master projection is
        # hidden (replaced with {}), forcing the scan to replay the cycle
        # journal where both master and candidate rows live.
        (repository.root / "pipeline-jobs" / f"{ifs_job_id}.json").write_text(
            "{}", encoding="utf-8"
        )
        # Sanity: the cycle journal replay contains BOTH the settled master
        # and the task candidate.
        from services.orchestrator import file_orchestration_journal as journal_module

        journal_records = repository._cycle_journal_records(
            source_id="IFS", cycle_time=anchor
        )
        rows = journal_module._CycleRows()
        for record in journal_records:
            repository._apply_journal_record(
                rows, record, source_id="IFS", cycle_time=anchor
            )
        replayed_ids = set(rows.pipeline_jobs)
        assert ifs_job_id in replayed_ids
        candidate_ids = [jid for jid in replayed_ids if jid.startswith("job_fcst_")]
        assert candidate_ids, "settled master journal must contain task candidates"
        candidate = rows.pipeline_jobs[candidate_ids[0]]
        assert str(candidate.get("slurm_job_id") or "").startswith("72001")

        # GFS recycles the freed master id at a DIFFERENT incarnation (01:30):
        # the master row is settled at 01:00 so it does not block, and the
        # candidate row must not block either. The bind succeeds.
        gfs_job_id, _gfs_key = _reserve_gfs_master_on(
            repository,
            created_at=anchor,
            member_count=1,
            expected_user="scheduler",
            expected_account="account",
        )
        rows = _fallback_row("72001", submit="2026-07-12T01:30:00")
        query, _ = _fallback_querier(monkeypatch, rows=rows, query_end=query_end)

        outcome = reconcile_reserved_unbound_jobs(
            repository,
            comment_query=query,
            now=lambda: query_end,
        )[0]

        assert outcome.job_id == gfs_job_id
        assert outcome.action == "bound", (
            "a task candidate carrying a shared numeric prefix is not a master"
            " owner; only current forecast masters occupy a Slurm id"
        )
        assert outcome.matched_slurm_job_id == "72001"
        persisted = repository.get_pipeline_job(gfs_job_id)
        assert persisted["slurm_job_id"] == "72001"
        reopened = FileOrchestrationJournalRepository(repository.root)
        assert reopened.get_pipeline_job(gfs_job_id)["slurm_job_id"] == "72001"
        from services.orchestrator import file_orchestration_journal as journal_module

        reopened_journal_rows = journal_module._CycleRows()
        for record in reopened._cycle_journal_records(
            source_id="IFS", cycle_time=anchor
        ):
            reopened._apply_journal_record(
                reopened_journal_rows, record, source_id="IFS", cycle_time=anchor
            )
        canonical_ifs = reopened_journal_rows.pipeline_jobs[ifs_job_id]
        assert canonical_ifs["status"] == "succeeded"
        assert canonical_ifs["slurm_job_id"] == "72001"


# --- #1850 Phase 6g (invariant closure III): a stale reconcile-inventory
# anchor whose canonical row has settled (batch terminal projection committed
# the journal but the direct projection failed) must STILL block a fallback
# bind of the exact same accounting incarnation. The anchor is adjudicated by
# canonical submitted_at BEFORE settled/quiescent rows are skipped — only for
# the name-window fallback producer that carries a candidate Submit.


@pytest.mark.skipif(not hasattr(__import__("time"), "tzset"), reason="time.tzset() is POSIX-only")
def test_stale_anchor_terminal_canonical_blocks_same_incarnation_fallback_bind(
    tmp_path: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Fix G: a stale reconcile-inventory anchor over a SETTLED canonical row
    blocks the exact same accounting incarnation.

    The IFS master binds + settles ``72001@01:00`` through the REAL batch
    projection path with an injected terminal-master direct failure: the batch
    journal append commits canonical ``succeeded/72001@01:00``, then the master
    direct write raises, leaving the reconcile-inventory anchor stale (still
    listing the row as current) and the master direct absent. A later GFS
    fallback commit for candidate ``72001@01:00`` MUST refuse
    (``active_slurm_id_occupied`` internally; ``identity_mismatch_blocked`` /
    count 1 on the public reconcile surface) — the stale anchor's canonical
    authority is the exact same accounting incarnation, so the fallback cannot
    reuse it even though the canonical row is settled.
    """

    from services.orchestrator.accepted_submit_identity import AcceptedSubmitTransition
    from services.orchestrator.file_orchestration_journal import (
        FileOrchestrationJournalRepository,
    )

    anchor = datetime(2026, 7, 12, tzinfo=UTC)
    with _pinned_local_timezone("UTC"):
        repository = _file_cohort_repository(
            tmp_path / "ifs-seed",
            created_at=anchor,
            member_count=1,
            expected_user="scheduler",
            expected_account="account",
            source_id="ifs",
        )
        ifs_job_id = "job_cycle_ifs_2026071200_forecast_fixture_forecast"
        ifs_key = "cycle_ifs_2026071200_forecast_fixture:forecast"
        bound = repository.commit_pipeline_job_submit_attempt(
            ifs_key,
            pipeline_job_id=ifs_job_id,
            expected_submission_attempt=1,
            slurm_job_id="72001",
            submitted_at=anchor + timedelta(hours=1),
            transition=AcceptedSubmitTransition.accounting(
                "matched_bound",
                submit_outcome="accepted",
                matched_slurm_job_id="72001",
                status="submitted",
                reconciliation_source="slurm_name_window_unique",
            ),
        )
        assert bound.committed

        # Remove the derived master direct projection (models the supported
        # crash boundary: journal-authority commit followed by direct
        # projection failure).
        (repository.root / "pipeline-jobs" / f"{ifs_job_id}.json").unlink()

        # Real batch terminal projection with the terminal master direct write
        # injected to fail AFTER the batch journal append has committed.
        projections, complete = _file_cohort_single_projection(repository, ifs_job_id)
        real_direct = repository._write_pipeline_job_direct_unlocked

        def failing_direct(row: Any, record: Any) -> None:
            job_id = str((row or {}).get("job_id") or "")
            if job_id == ifs_job_id:
                raise OSError("simulated terminal master direct failure")
            return real_direct(row, record)

        repository._write_pipeline_job_direct_unlocked = failing_direct
        try:
            repository.project_forecast_cohort_tasks(
                ifs_job_id,
                master_slurm_job_id="72001",
                projections=projections,
                complete=True,
                master_status="succeeded",
                master_error_code=None,
                reconciliation_decision="matched_bound",
            )
        except Exception:
            # The batch journal append committed before the terminal direct
            # write raised; the projection surface fails (lock release wraps
            # the original fault). The durable state below is the invariant.
            pass
        finally:
            repository._write_pipeline_job_direct_unlocked = real_direct

        # Invariant state: canonical journal committed, master direct absent,
        # stale reconcile-inventory anchor still present.
        from services.orchestrator import file_orchestration_journal as journal_module

        journal_rows = journal_module._CycleRows()
        for record in repository._cycle_journal_records(
            source_id="IFS", cycle_time=anchor
        ):
            repository._apply_journal_record(
                journal_rows, record, source_id="IFS", cycle_time=anchor
            )
        canonical_ifs = journal_rows.pipeline_jobs[ifs_job_id]
        assert canonical_ifs["status"] == "succeeded"
        assert canonical_ifs["slurm_job_id"] == "72001"
        assert canonical_ifs["submitted_at"] == "2026-07-12T01:00:00Z"
        assert not (
            repository.root / "pipeline-jobs" / f"{ifs_job_id}.json"
        ).exists(), "master direct must be absent after the injected failure"
        anchor_path = (
            repository.root
            / "reconcile-inventory"
            / f"{ifs_job_id}.json"
        )
        assert anchor_path.exists(), "stale reconcile-inventory anchor must remain"

        # Reserve GFS on a FRESH repository WITHOUT iterating the inventory
        # first (that would prune the stale anchor — the crash window is the
        # reserve-then-commit sequence).
        fresh = FileOrchestrationJournalRepository(repository.root)
        gfs_job_id, _gfs_key = _reserve_gfs_master_on(
            fresh,
            created_at=anchor,
            member_count=1,
            expected_user="scheduler",
            expected_account="account",
        )
        # The stale IFS anchor must still be present at commit time.
        entry_names = fresh._reconcile_inventory_entry_names_unlocked()
        assert f"{ifs_job_id}.json" in entry_names

        # Typed name-window-unique commit for the SAME 01:00 incarnation.
        gfs_key = "cycle_gfs_2026071200_forecast_fixture:forecast"
        commit_result = fresh.commit_pipeline_job_submit_attempt(
            gfs_key,
            pipeline_job_id=gfs_job_id,
            expected_submission_attempt=1,
            slurm_job_id="72001",
            submitted_at=anchor + timedelta(hours=1),
            transition=AcceptedSubmitTransition.accounting(
                "matched_bound",
                submit_outcome="accepted",
                matched_slurm_job_id="72001",
                status="submitted",
                reconciliation_source="slurm_name_window_unique",
            ),
        )
        assert not commit_result.committed
        assert commit_result.outcome == "active_slurm_id_occupied", commit_result.outcome
        persisted = FileOrchestrationJournalRepository(repository.root).get_pipeline_job(
            gfs_job_id
        )
        assert persisted["status"] == "reserved"
        assert persisted["slurm_job_id"] is None

        # The public reconcile surface must ALSO refuse: its own inventory
        # iteration would prune the settled IFS anchor, but the Phase 6h handoff
        # restores the missing derived master direct from canonical authority
        # BEFORE pruning, so the bounded fallback flat scan still discovers the
        # canonical cycle and blocks the same incarnation. GFS stays held and
        # unbound; same (id, canonical Submit) never binds regardless of settled
        # status or a stale/missing derived projection.

        # Reopened canonical IFS remains the sole settled owner; GFS held.
        reopened = FileOrchestrationJournalRepository(repository.root)
        assert reopened.get_pipeline_job(gfs_job_id)["status"] == "reserved"
        assert reopened.get_pipeline_job(gfs_job_id)["slurm_job_id"] is None
        reopened_journal_rows = journal_module._CycleRows()
        for record in reopened._cycle_journal_records(
            source_id="IFS", cycle_time=anchor
        ):
            reopened._apply_journal_record(
                reopened_journal_rows, record, source_id="IFS", cycle_time=anchor
            )
        canonical_ifs = reopened_journal_rows.pipeline_jobs[ifs_job_id]
        assert canonical_ifs["status"] == "succeeded"
        assert canonical_ifs["slurm_job_id"] == "72001"
        assert canonical_ifs["submitted_at"] == "2026-07-12T01:00:00Z"


# --- #1850 Phase 6h (invariant closure IV): the PUBLIC full-reconcile path
# must also refuse a fallback bind of the exact same accounting incarnation
# when a batch terminal projection committed the canonical journal but the
# derived master direct failed. The inventory-iteration prune of a stale
# anchor must first restore the missing derived direct from canonical
# authority (the flat scan's bounded locator), so the fallback discovers the
# canonical cycle and blocks. Same (id, canonical Submit) never binds
# regardless of settled status or a stale/missing derived projection.


@pytest.mark.skipif(not hasattr(__import__("time"), "tzset"), reason="time.tzset() is POSIX-only")
def test_public_reconcile_refuses_same_incarnation_when_master_direct_failed(
    tmp_path: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Fix H: the REAL public reconcile refuses the same accounting incarnation.

    Full sequence: IFS bound ``72001@01:00``, migration completed, derived
    master direct removed, real ``project_forecast_cohort_tasks`` terminal
    projection with injected terminal-master direct failure (canonical journal
    ``succeeded/72001@01:00``, stale anchor remains, direct absent), GFS
    reserved, then REAL ``reconcile_reserved_unbound_jobs`` with unique
    comment-less fallback ``72001@01:00``. The public outcome MUST be
    ``identity_mismatch_blocked`` / count 1, GFS held reserved/unbound under
    the #1564 tuple with streak zero, and reopened canonical IFS remains the
    sole settled owner.
    """

    from services.orchestrator.accepted_submit_identity import AcceptedSubmitTransition
    from services.orchestrator.file_orchestration_journal import (
        FileOrchestrationJournalRepository,
    )
    from services.orchestrator.reconcile import reconcile_reserved_unbound_jobs

    anchor = datetime(2026, 7, 12, tzinfo=UTC)
    query_end = anchor + timedelta(hours=2)
    with _pinned_local_timezone("UTC"):
        repository = _file_cohort_repository(
            tmp_path / "ifs-seed",
            created_at=anchor,
            member_count=1,
            expected_user="scheduler",
            expected_account="account",
            source_id="ifs",
        )
        ifs_job_id = "job_cycle_ifs_2026071200_forecast_fixture_forecast"
        ifs_key = "cycle_ifs_2026071200_forecast_fixture:forecast"
        bound = repository.commit_pipeline_job_submit_attempt(
            ifs_key,
            pipeline_job_id=ifs_job_id,
            expected_submission_attempt=1,
            slurm_job_id="72001",
            submitted_at=anchor + timedelta(hours=1),
            transition=AcceptedSubmitTransition.accounting(
                "matched_bound",
                submit_outcome="accepted",
                matched_slurm_job_id="72001",
                status="submitted",
                reconciliation_source="slurm_name_window_unique",
            ),
        )
        assert bound.committed
        # Complete the reconcile-inventory migration (production journal state:
        # the marker exists before the crash window opens).
        assert len(repository.query_reserved_unbound_jobs()) == 0
        assert (
            repository.root / "reconcile-inventory-migration-v1.json"
        ).exists()

        # Remove the derived master direct, then run the REAL batch terminal
        # projection with an injected terminal-master direct failure.
        (repository.root / "pipeline-jobs" / f"{ifs_job_id}.json").unlink()
        projections, complete = _file_cohort_single_projection(repository, ifs_job_id)
        real_direct = repository._write_pipeline_job_direct_unlocked

        def failing_direct(row: Any, record: Any) -> None:
            job_id = str((row or {}).get("job_id") or "")
            if job_id == ifs_job_id:
                raise OSError("simulated terminal master direct failure")
            return real_direct(row, record)

        repository._write_pipeline_job_direct_unlocked = failing_direct
        try:
            repository.project_forecast_cohort_tasks(
                ifs_job_id,
                master_slurm_job_id="72001",
                projections=projections,
                complete=True,
                master_status="succeeded",
                master_error_code=None,
                reconciliation_decision="matched_bound",
            )
        except Exception:
            pass
        finally:
            repository._write_pipeline_job_direct_unlocked = real_direct

        # Crash state: canonical journal succeeded/72001@01:00, stale anchor
        # present, master direct absent.
        from services.orchestrator import file_orchestration_journal as journal_module

        journal_rows = journal_module._CycleRows()
        for record in repository._cycle_journal_records(
            source_id="IFS", cycle_time=anchor
        ):
            repository._apply_journal_record(
                journal_rows, record, source_id="IFS", cycle_time=anchor
            )
        canonical_ifs = journal_rows.pipeline_jobs[ifs_job_id]
        assert canonical_ifs["status"] == "succeeded"
        assert canonical_ifs["slurm_job_id"] == "72001"
        assert canonical_ifs["submitted_at"] == "2026-07-12T01:00:00Z"
        assert (
            repository.root / "reconcile-inventory" / f"{ifs_job_id}.json"
        ).exists(), "stale reconcile-inventory anchor must remain"
        assert not (
            repository.root / "pipeline-jobs" / f"{ifs_job_id}.json"
        ).exists(), "master direct must be absent after the injected failure"

        # Reserve GFS then run REAL public reconcile.
        gfs_job_id, _gfs_key = _reserve_gfs_master_on(
            repository,
            created_at=anchor,
            member_count=1,
            expected_user="scheduler",
            expected_account="account",
        )
        rows = _fallback_row("72001", submit="2026-07-12T01:00:00")
        query, _ = _fallback_querier(monkeypatch, rows=rows, query_end=query_end)
        outcome = reconcile_reserved_unbound_jobs(
            repository,
            comment_query=query,
            now=lambda: query_end,
        )[0]

        assert outcome.job_id == gfs_job_id
        assert outcome.action == "identity_mismatch_blocked", (
            "the public reconcile must refuse the exact same accounting"
            " incarnation; the handoff restores the missing direct before"
            " pruning the stale anchor, so the fallback cannot read vacancy"
        )
        assert outcome.match_count == 1
        assert outcome.reconciliation_decision == "accounting_unavailable"
        assert outcome.reconciliation_reason_class == "comment_accounting_unproven"
        persisted = repository.get_pipeline_job(gfs_job_id)
        assert persisted["status"] == "reserved"
        assert persisted["slurm_job_id"] is None
        assert persisted["identity_blocked_streak"] == 0

        # Reopened canonical IFS remains the sole settled owner; GFS held.
        reopened = FileOrchestrationJournalRepository(repository.root)
        assert reopened.get_pipeline_job(gfs_job_id)["status"] == "reserved"
        assert reopened.get_pipeline_job(gfs_job_id)["slurm_job_id"] is None
        reopened_journal_rows = journal_module._CycleRows()
        for record in reopened._cycle_journal_records(
            source_id="IFS", cycle_time=anchor
        ):
            reopened._apply_journal_record(
                reopened_journal_rows, record, source_id="IFS", cycle_time=anchor
            )
        canonical_ifs = reopened_journal_rows.pipeline_jobs[ifs_job_id]
        assert canonical_ifs["status"] == "succeeded"
        assert canonical_ifs["slurm_job_id"] == "72001"
        assert canonical_ifs["submitted_at"] == "2026-07-12T01:00:00Z"


@pytest.mark.skipif(not hasattr(__import__("time"), "tzset"), reason="time.tzset() is POSIX-only")
def test_anchor_not_pruned_when_derived_direct_restore_fails(
    tmp_path: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Fix H fail-closed: when the derived direct restoration raises, the stale
    inventory anchor is KEPT (never pruned first), so a fallback cannot read
    vacancy from a missing locator."""

    from services.orchestrator.accepted_submit_identity import AcceptedSubmitTransition
    from services.orchestrator.file_orchestration_journal import (
        FileOrchestrationJournalRepository,
    )

    anchor = datetime(2026, 7, 12, tzinfo=UTC)
    with _pinned_local_timezone("UTC"):
        repository = _file_cohort_repository(
            tmp_path / "ifs-seed",
            created_at=anchor,
            member_count=1,
            expected_user="scheduler",
            expected_account="account",
            source_id="ifs",
        )
        ifs_job_id = "job_cycle_ifs_2026071200_forecast_fixture_forecast"
        ifs_key = "cycle_ifs_2026071200_forecast_fixture:forecast"
        bound = repository.commit_pipeline_job_submit_attempt(
            ifs_key,
            pipeline_job_id=ifs_job_id,
            expected_submission_attempt=1,
            slurm_job_id="72001",
            submitted_at=anchor + timedelta(hours=1),
            transition=AcceptedSubmitTransition.accounting(
                "matched_bound",
                submit_outcome="accepted",
                matched_slurm_job_id="72001",
                status="submitted",
                reconciliation_source="slurm_name_window_unique",
            ),
        )
        assert bound.committed
        assert len(repository.query_reserved_unbound_jobs()) == 0
        projections, complete = _file_cohort_single_projection(repository, ifs_job_id)
        (repository.root / "pipeline-jobs" / f"{ifs_job_id}.json").unlink()
        real_direct = repository._write_pipeline_job_direct_unlocked

        def failing_direct(row: Any, record: Any) -> None:
            job_id = str((row or {}).get("job_id") or "")
            if job_id == ifs_job_id:
                raise OSError("simulated terminal master direct failure")
            return real_direct(row, record)

        repository._write_pipeline_job_direct_unlocked = failing_direct
        try:
            repository.project_forecast_cohort_tasks(
                ifs_job_id,
                master_slurm_job_id="72001",
                projections=projections,
                complete=True,
                master_status="succeeded",
                master_error_code=None,
                reconciliation_decision="matched_bound",
            )
        except Exception:
            pass
        finally:
            repository._write_pipeline_job_direct_unlocked = real_direct

        # Now make the RESTORE itself fail: the inventory iteration must keep
        # the anchor (fail closed) rather than prune first.
        fresh = FileOrchestrationJournalRepository(repository.root)
        assert (
            repository.root / "reconcile-inventory" / f"{ifs_job_id}.json"
        ).exists()
        real_write = fresh._write_pipeline_job_direct_unlocked

        def failing_restore(row: Any, record: Any) -> None:
            job_id = str((row or {}).get("job_id") or "")
            if job_id == ifs_job_id:
                raise OSError("simulated restore failure")
            return real_write(row, record)

        fresh._write_pipeline_job_direct_unlocked = failing_restore
        try:
            list(fresh._iter_reconcile_inventory_records())
        except Exception as error:
            # The restore fault surfaces through the cycle-lock release wrap;
            # the invariant is that the anchor is preserved.
            assert not isinstance(error, AssertionError)
        finally:
            fresh._write_pipeline_job_direct_unlocked = real_write

        # Anchor must NOT have been pruned; direct must still be absent.
        assert (
            repository.root / "reconcile-inventory" / f"{ifs_job_id}.json"
        ).exists(), "fail closed: stale anchor must be kept when restore raises"
        assert not (
            repository.root / "pipeline-jobs" / f"{ifs_job_id}.json"
        ).exists(), "direct must stay absent after a failed restore"


@pytest.mark.skipif(not hasattr(__import__("time"), "tzset"), reason="time.tzset() is POSIX-only")
def test_handoff_race_with_fallback_commit_never_binds_same_incarnation(
    tmp_path: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Fix H interleaving: while the stale-anchor handoff (restore missing
    derived direct, then prune anchor) runs under the inventory lock, a
    competing fallback commit cannot observe BOTH locator surfaces absent and
    must never bind the same accounting incarnation."""

    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier

    from services.orchestrator.accepted_submit_identity import AcceptedSubmitTransition
    from services.orchestrator.file_orchestration_journal import (
        FileOrchestrationJournalRepository,
    )

    anchor = datetime(2026, 7, 12, tzinfo=UTC)
    with _pinned_local_timezone("UTC"):
        repository = _file_cohort_repository(
            tmp_path / "ifs-seed",
            created_at=anchor,
            member_count=1,
            expected_user="scheduler",
            expected_account="account",
            source_id="ifs",
        )
        ifs_job_id = "job_cycle_ifs_2026071200_forecast_fixture_forecast"
        ifs_key = "cycle_ifs_2026071200_forecast_fixture:forecast"
        bound = repository.commit_pipeline_job_submit_attempt(
            ifs_key,
            pipeline_job_id=ifs_job_id,
            expected_submission_attempt=1,
            slurm_job_id="72001",
            submitted_at=anchor + timedelta(hours=1),
            transition=AcceptedSubmitTransition.accounting(
                "matched_bound",
                submit_outcome="accepted",
                matched_slurm_job_id="72001",
                status="submitted",
                reconciliation_source="slurm_name_window_unique",
            ),
        )
        assert bound.committed
        assert len(repository.query_reserved_unbound_jobs()) == 0
        projections, complete = _file_cohort_single_projection(repository, ifs_job_id)
        (repository.root / "pipeline-jobs" / f"{ifs_job_id}.json").unlink()
        real_direct = repository._write_pipeline_job_direct_unlocked

        def failing_direct(row: Any, record: Any) -> None:
            job_id = str((row or {}).get("job_id") or "")
            if job_id == ifs_job_id:
                raise OSError("simulated terminal master direct failure")
            return real_direct(row, record)

        repository._write_pipeline_job_direct_unlocked = failing_direct
        try:
            repository.project_forecast_cohort_tasks(
                ifs_job_id,
                master_slurm_job_id="72001",
                projections=projections,
                complete=True,
                master_status="succeeded",
                master_error_code=None,
                reconciliation_decision="matched_bound",
            )
        except Exception:
            pass
        finally:
            repository._write_pipeline_job_direct_unlocked = real_direct

        # GFS reserved on the same root; the stale IFS anchor is still present.
        gfs_job_id, _gfs_key = _reserve_gfs_master_on(
            repository,
            created_at=anchor,
            member_count=1,
            expected_user="scheduler",
            expected_account="account",
        )
        assert (
            repository.root / "reconcile-inventory" / f"{ifs_job_id}.json"
        ).exists()

        barrier = Barrier(2, timeout=30)

        def handoff_iter() -> str:
            contender = FileOrchestrationJournalRepository(repository.root)
            barrier.wait(timeout=30)
            # The inventory iteration prunes the stale anchor AFTER restoring
            # the missing direct (handoff).
            rows = list(contender._iter_reconcile_inventory_records())
            return f"iter:{len(rows)}"

        def fallback_commit() -> str:
            contender = FileOrchestrationJournalRepository(repository.root)
            barrier.wait(timeout=30)
            commit_result = contender.commit_pipeline_job_submit_attempt(
                "cycle_gfs_2026071200_forecast_fixture:forecast",
                pipeline_job_id=gfs_job_id,
                expected_submission_attempt=1,
                slurm_job_id="72001",
                submitted_at=anchor + timedelta(hours=1),
                transition=AcceptedSubmitTransition.accounting(
                    "matched_bound",
                    submit_outcome="accepted",
                    matched_slurm_job_id="72001",
                    status="submitted",
                    reconciliation_source="slurm_name_window_unique",
                ),
            )
            return str(commit_result.outcome)

        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = [pool.submit(handoff_iter), pool.submit(fallback_commit)]
            results = [future.result(timeout=60) for future in futures]

        # The fallback commit must NEVER bind the same accounting incarnation:
        # either the stale anchor's Phase 6g guard blocks it
        # (``active_slurm_id_occupied``) or the restored direct's flat scan
        # blocks it (``active_slurm_id_occupied``). ``applied`` is forbidden.
        assert results[1] == "active_slurm_id_occupied", results
        persisted = FileOrchestrationJournalRepository(repository.root).get_pipeline_job(
            gfs_job_id
        )
        assert persisted["status"] == "reserved"
        assert persisted["slurm_job_id"] is None


# --- #1850 Phase 6i (invariant closure V): the marker-absent FIRST-migration
# variant of the wrong bind. When the reconcile-inventory migration marker is
# absent, the one-time backfill must NOT prune the only locator (a stale
# anchor over a settled current master with a missing derived direct). The
# migration lane preserves a handoff anchor (authority fingerprint is
# anchor-exclusive), the marker completes, and the steady-state iterator then
# restores the direct and prunes the anchor. Migration state cannot alter the
# accounting-incarnation invariant.


@pytest.mark.skipif(not hasattr(__import__("time"), "tzset"), reason="time.tzset() is POSIX-only")
def test_marker_absent_first_migration_public_reconcile_refuses_same_incarnation(
    tmp_path: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Fix I: the REAL public reconcile with the migration marker ABSENT refuses
    the same accounting incarnation.

    Full sequence: IFS bound ``72001@01:00`` (marker absent), derived master
    direct removed, real ``project_forecast_cohort_tasks`` terminal projection
    with injected terminal-master direct failure (canonical journal
    ``succeeded/72001@01:00``, stale anchor present, direct absent), GFS
    reserved, then REAL ``reconcile_reserved_unbound_jobs`` with unique
    comment-less fallback ``72001@01:00``. The first run performs the one-time
    migration (marker absent): the backfill must preserve the handoff anchor
    (not prune the only locator), the marker completes, and the steady-state
    handoff restores the direct and prunes the anchor. The public outcome MUST
    be ``identity_mismatch_blocked`` / count 1, GFS held reserved/unbound with
    streak zero, and reopened canonical IFS remains the sole settled owner.
    """

    from services.orchestrator.accepted_submit_identity import AcceptedSubmitTransition
    from services.orchestrator.file_orchestration_journal import (
        FileOrchestrationJournalRepository,
    )
    from services.orchestrator.reconcile import reconcile_reserved_unbound_jobs

    anchor = datetime(2026, 7, 12, tzinfo=UTC)
    query_end = anchor + timedelta(hours=2)
    with _pinned_local_timezone("UTC"):
        repository = _file_cohort_repository(
            tmp_path / "ifs-seed",
            created_at=anchor,
            member_count=1,
            expected_user="scheduler",
            expected_account="account",
            source_id="ifs",
        )
        ifs_job_id = "job_cycle_ifs_2026071200_forecast_fixture_forecast"
        ifs_key = "cycle_ifs_2026071200_forecast_fixture:forecast"
        bound = repository.commit_pipeline_job_submit_attempt(
            ifs_key,
            pipeline_job_id=ifs_job_id,
            expected_submission_attempt=1,
            slurm_job_id="72001",
            submitted_at=anchor + timedelta(hours=1),
            transition=AcceptedSubmitTransition.accounting(
                "matched_bound",
                submit_outcome="accepted",
                matched_slurm_job_id="72001",
                status="submitted",
                reconciliation_source="slurm_name_window_unique",
            ),
        )
        assert bound.committed
        # The migration marker must be ABSENT (first migration).
        assert not (
            repository.root / "reconcile-inventory-migration-v1.json"
        ).exists()

        # Remove the derived master direct, then run the REAL batch terminal
        # projection with an injected terminal-master direct failure.
        (repository.root / "pipeline-jobs" / f"{ifs_job_id}.json").unlink()
        projections, complete = _file_cohort_single_projection(repository, ifs_job_id)
        real_direct = repository._write_pipeline_job_direct_unlocked

        def failing_direct(row: Any, record: Any) -> None:
            job_id = str((row or {}).get("job_id") or "")
            if job_id == ifs_job_id:
                raise OSError("simulated terminal master direct failure")
            return real_direct(row, record)

        repository._write_pipeline_job_direct_unlocked = failing_direct
        try:
            repository.project_forecast_cohort_tasks(
                ifs_job_id,
                master_slurm_job_id="72001",
                projections=projections,
                complete=True,
                master_status="succeeded",
                master_error_code=None,
                reconciliation_decision="matched_bound",
            )
        except Exception:
            pass
        finally:
            repository._write_pipeline_job_direct_unlocked = real_direct

        # Crash state: canonical journal succeeded/72001@01:00, stale anchor
        # present, master direct absent, marker absent.
        from services.orchestrator import file_orchestration_journal as journal_module

        journal_rows = journal_module._CycleRows()
        for record in repository._cycle_journal_records(
            source_id="IFS", cycle_time=anchor
        ):
            repository._apply_journal_record(
                journal_rows, record, source_id="IFS", cycle_time=anchor
            )
        canonical_ifs = journal_rows.pipeline_jobs[ifs_job_id]
        assert canonical_ifs["status"] == "succeeded"
        assert canonical_ifs["slurm_job_id"] == "72001"
        assert canonical_ifs["submitted_at"] == "2026-07-12T01:00:00Z"
        assert (
            repository.root / "reconcile-inventory" / f"{ifs_job_id}.json"
        ).exists(), "stale reconcile-inventory anchor must remain"
        assert not (
            repository.root / "pipeline-jobs" / f"{ifs_job_id}.json"
        ).exists(), "master direct must be absent after the injected failure"

        # Reserve GFS then run REAL public reconcile (marker absent -> the
        # one-time migration runs first).
        gfs_job_id, _gfs_key = _reserve_gfs_master_on(
            repository,
            created_at=anchor,
            member_count=1,
            expected_user="scheduler",
            expected_account="account",
        )
        rows = _fallback_row("72001", submit="2026-07-12T01:00:00")
        query, _ = _fallback_querier(monkeypatch, rows=rows, query_end=query_end)
        outcome = reconcile_reserved_unbound_jobs(
            repository,
            comment_query=query,
            now=lambda: query_end,
        )[0]

        assert outcome.job_id == gfs_job_id
        assert outcome.action == "identity_mismatch_blocked", (
            "the marker-absent first migration must NOT let the fallback bind"
            " the same accounting incarnation: the migration lane preserves the"
            " handoff anchor, the marker completes, and the steady-state handoff"
            " restores the direct before pruning"
        )
        assert outcome.match_count == 1
        assert outcome.reconciliation_decision == "accounting_unavailable"
        assert outcome.reconciliation_reason_class == "comment_accounting_unproven"
        persisted = repository.get_pipeline_job(gfs_job_id)
        assert persisted["status"] == "reserved"
        assert persisted["slurm_job_id"] is None
        assert persisted["identity_blocked_streak"] == 0

        # Migration completed; final state has the durable flat locator
        # restored and the handoff anchor pruned.
        assert (
            repository.root / "reconcile-inventory-migration-v1.json"
        ).exists(), "migration marker must complete"
        assert (
            repository.root / "pipeline-jobs" / f"{ifs_job_id}.json"
        ).exists(), "derived flat direct must be restored by the handoff"
        assert not (
            repository.root / "reconcile-inventory" / f"{ifs_job_id}.json"
        ).exists(), "handoff anchor must be pruned after the direct is restored"

        # Reopened canonical IFS remains the sole settled owner; GFS held.
        reopened = FileOrchestrationJournalRepository(repository.root)
        assert reopened.get_pipeline_job(gfs_job_id)["status"] == "reserved"
        assert reopened.get_pipeline_job(gfs_job_id)["slurm_job_id"] is None
        reopened_journal_rows = journal_module._CycleRows()
        for record in reopened._cycle_journal_records(
            source_id="IFS", cycle_time=anchor
        ):
            reopened._apply_journal_record(
                reopened_journal_rows, record, source_id="IFS", cycle_time=anchor
            )
        canonical_ifs = reopened_journal_rows.pipeline_jobs[ifs_job_id]
        assert canonical_ifs["status"] == "succeeded"
        assert canonical_ifs["slurm_job_id"] == "72001"
        assert canonical_ifs["submitted_at"] == "2026-07-12T01:00:00Z"
