"""#1565 Phase 6 claimant-exclusivity and attempt-window closure (#1850).

Proves the name-window fallback binds only when the candidate has one durable
claimant, no other active accepted-submit owner of its id, and a submit instant
inside the closed attempt window. Every ambiguity stays fail-closed under the
#1564 held tuple with streak zero, regardless of reconcile iteration order or
concurrent source/cycle writers; settled terminal history never occupies a
recycled Slurm number.

This module keeps the first lane: claimant races, recycled ids, typed/normal
commit races, row order and malformed/orphan anchors. #3177 moved the other
lanes to ``tests/test_gateway_reconcile_claimant_incarnation.py``,
``tests/test_gateway_reconcile_claimant_stale_authority.py`` and
``tests/test_gateway_reconcile_claimant_replay_handoff.py``; the helpers they
share are in ``tests/gateway_reconcile_claimant_exclusivity_helpers.py``.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from services.orchestrator.accepted_submit_identity import AcceptedSubmitTransition
from tests.gateway_reconcile_claimant_exclusivity_helpers import (
    _fallback_querier,
    _fallback_row,
    _file_cohort_single_projection,
    _reserve_gfs_master_on,
    _reserve_second_master,
)
from tests.gateway_reconcile_helpers import _file_cohort_repository
from tests.test_real_slurm_gateway import _pinned_local_timezone


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
            slurm_accounting_submitted_at=anchor + timedelta(minutes=30),
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
            slurm_accounting_submitted_at=anchor + timedelta(minutes=30),
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
                slurm_accounting_submitted_at=anchor + timedelta(hours=1),
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
