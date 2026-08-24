"""Identity-blocked release: streak convergence, grace anchoring, CAS
refusal, and the non-reclaimable released terminal.
"""

from __future__ import annotations

from datetime import (
    UTC,
    datetime,
    timedelta,
)
from typing import Any

import pytest

from services.orchestrator.reconcile import SacctRecord
from tests.gateway_reconcile_helpers import (
    _authoritative_absence_query,
    _file_cohort_repository,
)


def _identity_blocked_master(tmp_path: Any, name: str, *, started_at: datetime) -> Any:
    """A versioned reserved-unbound master wedged on the deterministic site A branch.

    ``with_runtime_rows=False`` breaks the cohort runtime identity, so
    ``_accepted_submit_reconcile_job`` is False while ``file_forecast_cohort`` is
    True -- exactly the ``reconcile.py:1387-1401`` writer site that records
    ``identity_mismatch_blocked`` every pass without moving the row.
    """

    return _file_cohort_repository(
        tmp_path / name,
        created_at=started_at,
        member_count=1,
        with_runtime_rows=False,
    )


def test_reserved_unbound_identity_blocked_streak_releases_the_reservation(tmp_path: Any) -> None:
    """tasks 2.1 -- three consecutive blocked passes converge onto reservation_lost."""

    from services.orchestrator.reconcile import reconcile_reserved_unbound_jobs

    started_at = datetime(2026, 7, 12, tzinfo=UTC)
    repository = _identity_blocked_master(tmp_path, "streak", started_at=started_at)
    job_id = "job_cycle_gfs_2026071200_forecast_fixture_forecast"

    def run_pass() -> list[Any]:
        return reconcile_reserved_unbound_jobs(
            repository,
            comment_query=_authoritative_absence_query,
            accepted_submit_grace=timedelta(seconds=300),
            identity_blocked_streak_limit=3,
            now=lambda: started_at + timedelta(hours=1),
        )

    first = run_pass()[0]
    assert (first.action, first.status, first.identity_blocked_streak) == (
        "identity_mismatch_blocked",
        "reserved",
        1,
    )
    assert repository.get_pipeline_job(job_id)["identity_blocked_streak"] == 1

    second = run_pass()[0]
    assert (second.action, second.status, second.identity_blocked_streak) == (
        "identity_mismatch_blocked",
        "reserved",
        2,
    )
    assert repository.get_pipeline_job(job_id)["identity_blocked_streak"] == 2

    third = run_pass()[0]
    assert third.action == "identity_mismatch_released"
    assert third.status == "reservation_lost"
    assert third.reconciliation_decision == "identity_mismatch_released"
    assert third.reconciliation_source == "slurm_exact_comment"
    assert third.identity_blocked_streak == 3
    assert third.durable_write_count > 0

    released = repository.get_pipeline_job(job_id)
    assert released["status"] == "reservation_lost"
    assert released["reconciliation_decision"] == "identity_mismatch_released"
    assert released["submit_outcome"] == "submit_result_ambiguous"
    assert released["matched_slurm_job_id"] is None
    assert released["identity_blocked_streak"] == 3

    from services.orchestrator.file_orchestration_journal import FileOrchestrationJournalRepository

    reopened = FileOrchestrationJournalRepository(repository.root)
    assert reopened.get_pipeline_job(job_id) == released
    assert reopened.query_reserved_unbound_jobs() == []
    assert run_pass() == []


def _foreign_collision_query(_key: str, **_kwargs: Any) -> list[Any]:
    return [
        SacctRecord(
            "17667",
            "RUNNING",
            "nhms_forecast",
            comment="nhms_idem:cycle_gfs_2026071200_forecast_fixture:forecast",
            user="foreign",
            account="other",
        )
    ]


def test_identity_blocked_streak_resets_on_a_different_reconcile_outcome(tmp_path: Any) -> None:
    """tasks 2.2(a) -- the counter is consecutive, never cumulative."""

    from services.orchestrator.reconcile import (
        ReconcileQueryUnavailable,
        reconcile_reserved_unbound_jobs,
    )

    started_at = datetime(2026, 7, 12, tzinfo=UTC)
    repository = _file_cohort_repository(
        tmp_path / "reset",
        created_at=started_at,
        member_count=1,
        expected_user="scheduler",
        expected_account="account",
    )
    job_id = "job_cycle_gfs_2026071200_forecast_fixture_forecast"

    def run_pass(comment_query: Any) -> Any:
        return reconcile_reserved_unbound_jobs(
            repository,
            comment_query=comment_query,
            accepted_submit_grace=timedelta(seconds=300),
            identity_blocked_streak_limit=3,
            now=lambda: started_at + timedelta(hours=1),
        )[0]

    def unavailable(_key: str, **_kwargs: Any) -> None:
        raise ReconcileQueryUnavailable("sacct unavailable")

    assert run_pass(_foreign_collision_query).identity_blocked_streak == 1
    assert run_pass(_foreign_collision_query).identity_blocked_streak == 2

    interrupted = run_pass(unavailable)
    assert interrupted.action == "query_unavailable"
    assert interrupted.identity_blocked_streak is None
    assert repository.get_pipeline_job(job_id)["identity_blocked_streak"] == 0

    assert run_pass(_foreign_collision_query).identity_blocked_streak == 1
    assert run_pass(_foreign_collision_query).identity_blocked_streak == 2
    assert repository.get_pipeline_job(job_id)["status"] == "reserved"


def test_identity_blocked_streak_restarts_after_absence_retry_and_reclaim(
    tmp_path: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """tasks 2.2(b) -- a stale streak must not make one post-reclaim pass release."""

    from services.orchestrator import file_orchestration_journal as journal_module
    from services.orchestrator.reconcile import reconcile_reserved_unbound_jobs

    started_at = datetime(2026, 7, 12, tzinfo=UTC)
    repository = _file_cohort_repository(
        tmp_path / "reclaim",
        created_at=started_at,
        member_count=1,
        expected_user="scheduler",
        expected_account="account",
    )
    job_id = "job_cycle_gfs_2026071200_forecast_fixture_forecast"

    def run_pass(comment_query: Any) -> Any:
        return reconcile_reserved_unbound_jobs(
            repository,
            comment_query=comment_query,
            accepted_submit_grace=timedelta(seconds=300),
            identity_blocked_streak_limit=3,
            now=lambda: started_at + timedelta(hours=1),
        )[0]

    assert run_pass(_foreign_collision_query).identity_blocked_streak == 1
    assert run_pass(_foreign_collision_query).identity_blocked_streak == 2

    released = run_pass(_authoritative_absence_query)
    assert released.action == "absence_retry_permitted"
    attempt_one = repository.get_pipeline_job(job_id)
    assert attempt_one["reconciliation_decision"] == "absence_retry_permitted"
    assert attempt_one["identity_blocked_streak"] == 0

    reclaim_anchor = started_at + timedelta(hours=2)
    monkeypatch.setattr(journal_module, "_utcnow", lambda: reclaim_anchor)
    reclaimed = repository.reclaim_pipeline_job_reservation(
        {
            **attempt_one,
            "expected_submission_attempt": attempt_one["submission_attempt"],
            "expected_submission_attempt_started_at": attempt_one["submission_attempt_started_at"],
            "status": "reserved",
            "submission_attempt": 2,
            "submission_attempt_started_at": reclaim_anchor,
            "submit_outcome": None,
            "reconciliation_source": None,
            "reconciliation_decision": None,
            "matched_slurm_job_id": None,
        }
    )
    monkeypatch.undo()
    assert reclaimed is not None
    assert reclaimed["identity_blocked_streak"] == 0

    first_after_reclaim = reconcile_reserved_unbound_jobs(
        repository,
        comment_query=_foreign_collision_query,
        accepted_submit_grace=timedelta(seconds=300),
        identity_blocked_streak_limit=3,
        now=lambda: reclaim_anchor + timedelta(hours=1),
    )[0]

    assert first_after_reclaim.action == "identity_mismatch_blocked"
    assert first_after_reclaim.identity_blocked_streak == 1
    assert repository.get_pipeline_job(job_id)["status"] == "reserved"


def test_identity_blocked_release_grace_is_anchored_to_the_attempt_start(tmp_path: Any) -> None:
    """tasks 2.3(a) -- the counter's own writes must not postpone the exit."""

    from services.orchestrator.reconcile import reconcile_reserved_unbound_jobs

    started_at = datetime(2026, 7, 12, tzinfo=UTC)
    repository = _identity_blocked_master(tmp_path, "grace", started_at=started_at)
    job_id = "job_cycle_gfs_2026071200_forecast_fixture_forecast"
    grace = timedelta(seconds=3600)

    def run_pass(at: datetime) -> Any:
        return reconcile_reserved_unbound_jobs(
            repository,
            comment_query=_authoritative_absence_query,
            accepted_submit_grace=grace,
            identity_blocked_streak_limit=2,
            now=lambda: at,
        )[0]

    assert run_pass(started_at + timedelta(seconds=10)).identity_blocked_streak == 1
    held = run_pass(started_at + timedelta(seconds=20))
    assert held.action == "identity_mismatch_blocked"
    assert held.identity_blocked_streak == 2
    assert repository.get_pipeline_job(job_id)["status"] == "reserved"

    # updated_at has been refreshed by the counter writes; the release must
    # still fire at submission_attempt_started_at + grace.
    refreshed = repository.get_pipeline_job(job_id)
    assert refreshed["updated_at"] > refreshed["submission_attempt_started_at"]

    released = run_pass(started_at + grace)
    assert released.action == "identity_mismatch_released"
    assert released.identity_blocked_streak == 2
    assert repository.get_pipeline_job(job_id)["status"] == "reservation_lost"


@pytest.mark.parametrize("limit", [None, 0, -1])
def test_identity_blocked_release_stays_disabled_and_saturates_to_zero_writes(
    tmp_path: Any,
    limit: int | None,
) -> None:
    """tasks 2.3(b) -- a disabled exit preserves today's behaviour and stays quiet."""

    from services.orchestrator.file_orchestration_journal import FileOrchestrationJournalRepository
    from services.orchestrator.reconcile import reconcile_reserved_unbound_jobs

    started_at = datetime(2026, 7, 12, tzinfo=UTC)
    repository = _identity_blocked_master(tmp_path, f"disabled-{limit}", started_at=started_at)
    job_id = "job_cycle_gfs_2026071200_forecast_fixture_forecast"

    def run_pass() -> Any:
        return reconcile_reserved_unbound_jobs(
            repository,
            comment_query=_authoritative_absence_query,
            accepted_submit_grace=timedelta(seconds=300),
            identity_blocked_streak_limit=limit,
            now=lambda: started_at + timedelta(hours=1),
        )[0]

    first = run_pass()
    assert first.action == "identity_mismatch_blocked"
    assert first.identity_blocked_streak == 0

    steady = repository.get_pipeline_job(job_id)
    steady_files = {
        str(path.relative_to(repository.root)): path.read_bytes()
        for path in repository.root.rglob("*")
        if path.is_file()
    }
    for _ in range(3):
        repeat = run_pass()
        assert repeat.action == "identity_mismatch_blocked"
        assert repeat.durable_write_count == 0
        assert repeat.identity_blocked_streak == 0
    assert repository.get_pipeline_job(job_id) == steady
    assert {
        str(path.relative_to(repository.root)): path.read_bytes()
        for path in repository.root.rglob("*")
        if path.is_file()
    } == steady_files
    assert FileOrchestrationJournalRepository(repository.root).get_pipeline_job(job_id) == steady
    assert steady["status"] == "reserved"


def test_identity_blocked_release_cas_failure_keeps_the_blocked_outcome(tmp_path: Any) -> None:
    """tasks 2.3(c) -- a concurrently advanced attempt loses the release race."""

    from services.orchestrator.reconcile import reconcile_reserved_unbound_jobs

    started_at = datetime(2026, 7, 12, tzinfo=UTC)
    repository = _identity_blocked_master(tmp_path, "cas", started_at=started_at)
    job_id = "job_cycle_gfs_2026071200_forecast_fixture_forecast"

    class _ConcurrentAttemptStore:
        """Delegate everything, but race the release CAS against attempt 2."""

        def __init__(self, inner: Any) -> None:
            self._inner = inner

        def __getattr__(self, name: str) -> Any:
            return getattr(self._inner, name)

        def release_identity_blocked_reservation(self, job_id: str, **kwargs: Any) -> int:
            kwargs["expected_submission_attempt"] = int(kwargs["expected_submission_attempt"]) + 1
            return self._inner.release_identity_blocked_reservation(job_id, **kwargs)

    store = _ConcurrentAttemptStore(repository)

    def run_pass() -> Any:
        return reconcile_reserved_unbound_jobs(
            store,
            comment_query=_authoritative_absence_query,
            accepted_submit_grace=timedelta(seconds=300),
            identity_blocked_streak_limit=2,
            now=lambda: started_at + timedelta(hours=1),
        )[0]

    run_pass()
    blocked = run_pass()

    assert blocked.action == "identity_mismatch_blocked"
    assert blocked.identity_blocked_streak == 2
    persisted = repository.get_pipeline_job(job_id)
    assert persisted["status"] == "reserved"
    assert persisted["reconciliation_decision"] == "identity_mismatch_blocked"
    assert persisted["identity_blocked_streak"] == 2


def _journal_bytes(repository: Any) -> dict[str, bytes]:
    return {
        str(path.relative_to(repository.root)): path.read_bytes()
        for path in repository.root.rglob("*")
        if path.is_file()
    }


def test_release_identity_blocked_reservation_refuses_a_concurrently_bound_row(tmp_path: Any) -> None:
    """Direct CAS oracle -- the unbound gate is the last guard before abandoning a live job.

    Reconcile only ever calls the release API on a row it has just seen as
    reserved-unbound, so the gate itself needs a direct caller. ``expected_status``
    is set to the bound row's own status on purpose: the unbound check is then the
    only gate that can refuse the write.
    """

    from services.orchestrator.accepted_submit_identity import ACCEPTED_SUBMIT_CONTRACT_VERSION
    from services.orchestrator.reconcile import SacctRecord, reconcile_reserved_unbound_jobs

    started_at = datetime(2026, 7, 12, tzinfo=UTC)
    repository = _file_cohort_repository(tmp_path / "bound", created_at=started_at, member_count=1)
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
    assert reconcile_reserved_unbound_jobs(repository, comment_query=lambda _key: exact)[0].action == "bound"
    bound = repository.get_pipeline_job(job_id)
    assert bound["slurm_job_id"] == "17667"
    before_files = _journal_bytes(repository)

    assert repository.release_identity_blocked_reservation(
        job_id,
        accepted_submit_contract_version=ACCEPTED_SUBMIT_CONTRACT_VERSION,
        expected_submission_attempt=int(bound["submission_attempt"]),
        expected_submission_attempt_started_at=bound["submission_attempt_started_at"],
        expected_status=str(bound["status"]),
        identity_blocked_streak=3,
    ) == 0
    assert _journal_bytes(repository) == before_files
    assert repository.get_pipeline_job(job_id) == bound


def test_release_identity_blocked_reservation_refuses_a_row_that_left_reserved(tmp_path: Any) -> None:
    """Direct CAS oracle -- an absence-released (reclaimable) row must not be re-decided.

    ``absence_retry_permitted`` also parks the row on ``reservation_lost`` while
    unbound, with the same attempt and anchor. Only the expected-status gate keeps
    the release API from overwriting that reclaimable decision with the
    non-reclaimable ``identity_mismatch_released`` one.
    """

    from services.orchestrator.accepted_submit_identity import ACCEPTED_SUBMIT_CONTRACT_VERSION
    from services.orchestrator.reconcile import reconcile_reserved_unbound_jobs

    started_at = datetime(2026, 7, 12, tzinfo=UTC)
    repository = _file_cohort_repository(tmp_path / "advanced", created_at=started_at, member_count=1)
    job_id = "job_cycle_gfs_2026071200_forecast_fixture_forecast"
    permitted = reconcile_reserved_unbound_jobs(
        repository,
        comment_query=_authoritative_absence_query,
        accepted_submit_grace=timedelta(seconds=300),
        identity_blocked_streak_limit=3,
        now=lambda: started_at + timedelta(hours=1),
    )[0]
    assert permitted.action == "absence_retry_permitted"
    advanced = repository.get_pipeline_job(job_id)
    assert advanced["status"] == "reservation_lost"
    assert advanced["reconciliation_decision"] == "absence_retry_permitted"
    assert advanced["slurm_job_id"] in (None, "")
    before_files = _journal_bytes(repository)

    assert repository.release_identity_blocked_reservation(
        job_id,
        accepted_submit_contract_version=ACCEPTED_SUBMIT_CONTRACT_VERSION,
        expected_submission_attempt=int(advanced["submission_attempt"]),
        expected_submission_attempt_started_at=advanced["submission_attempt_started_at"],
        identity_blocked_streak=3,
    ) == 0
    assert _journal_bytes(repository) == before_files
    assert repository.get_pipeline_job(job_id) == advanced


def test_release_identity_blocked_reservation_refuses_a_mismatched_attempt_anchor(tmp_path: Any) -> None:
    """Direct CAS oracle -- a reclaimed attempt (new anchor, same number) loses the race."""

    from services.orchestrator.accepted_submit_identity import ACCEPTED_SUBMIT_CONTRACT_VERSION

    started_at = datetime(2026, 7, 12, tzinfo=UTC)
    repository = _identity_blocked_master(tmp_path, "anchor", started_at=started_at)
    job_id = "job_cycle_gfs_2026071200_forecast_fixture_forecast"
    reserved = repository.get_pipeline_job(job_id)
    before_files = _journal_bytes(repository)

    assert repository.release_identity_blocked_reservation(
        job_id,
        accepted_submit_contract_version=ACCEPTED_SUBMIT_CONTRACT_VERSION,
        expected_submission_attempt=int(reserved["submission_attempt"]),
        expected_submission_attempt_started_at=started_at + timedelta(hours=2),
        identity_blocked_streak=3,
    ) == 0
    assert repository.release_identity_blocked_reservation(
        job_id,
        accepted_submit_contract_version=ACCEPTED_SUBMIT_CONTRACT_VERSION,
        expected_submission_attempt=int(reserved["submission_attempt"]) + 1,
        expected_submission_attempt_started_at=reserved["submission_attempt_started_at"],
        identity_blocked_streak=3,
    ) == 0
    assert _journal_bytes(repository) == before_files
    assert repository.get_pipeline_job(job_id) == reserved


def test_identity_mismatch_released_row_is_a_non_reclaimable_terminal(tmp_path: Any) -> None:
    """Runbook oracle -- the released idempotency key can never be revived."""

    from services.orchestrator.reconcile import reconcile_reserved_unbound_jobs

    started_at = datetime(2026, 7, 12, tzinfo=UTC)
    repository = _identity_blocked_master(tmp_path, "terminal", started_at=started_at)
    job_id = "job_cycle_gfs_2026071200_forecast_fixture_forecast"
    released = reconcile_reserved_unbound_jobs(
        repository,
        comment_query=_authoritative_absence_query,
        accepted_submit_grace=timedelta(seconds=300),
        identity_blocked_streak_limit=1,
        now=lambda: started_at + timedelta(hours=1),
    )[0]
    assert released.action == "identity_mismatch_released"

    terminal = repository.get_pipeline_job(job_id)

    # The cycle chain's reclaim shortcut only accepts the decisions
    # {absence_retry_permitted, operator_verified_absence}, so the released row
    # resumes as a terminal instead of re-submitting its key.
    from services.orchestrator import chain as _chain  # import order: chain owns the cycle mixin
    from services.orchestrator.chain_forecast_orchestrator_cycle import (
        _verified_accepted_submit_forecast_retry,
    )

    del _chain
    assert _verified_accepted_submit_forecast_retry(terminal) is False
    assert repository.reclaim_pipeline_job_reservation(
        {
            **terminal,
            "expected_submission_attempt": terminal["submission_attempt"],
            "expected_submission_attempt_started_at": terminal["submission_attempt_started_at"],
            "status": "reserved",
            "submission_attempt": 2,
            "submission_attempt_started_at": started_at + timedelta(hours=2),
            "submit_outcome": None,
            "reconciliation_source": None,
            "reconciliation_decision": None,
            "matched_slurm_job_id": None,
            "identity_blocked_streak": 0,
        }
    ) is None
    assert repository.get_pipeline_job(job_id) == terminal

    # Liveness: a retry-suffixed key is a different reservation and still opens.
    from services.orchestrator.reservation import slurm_comment_for

    retry_key = "cycle_gfs_2026071200_forecast_fixture:forecast:retry_1"
    retry_record = {
        **terminal,
        "job_id": f"{job_id}_retry_1",
        "idempotency_key": retry_key,
        "slurm_comment": slurm_comment_for(retry_key),
        "status": "reserved",
        "submission_attempt": 1,
        "submission_attempt_started_at": started_at + timedelta(hours=2),
        "submit_outcome": None,
        "reconciliation_source": None,
        "reconciliation_decision": None,
        "matched_slurm_job_id": None,
        "identity_blocked_streak": 0,
    }
    from services.orchestrator.accepted_submit_identity import forecast_cohort_digest

    retry_record["cohort_digest"] = forecast_cohort_digest(retry_record)
    assert repository.reserve_pipeline_job(retry_record) is not None
