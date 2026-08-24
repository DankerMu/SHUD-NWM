"""Grace guard for confirmed-but-young absence (slurmdbd propagation lag),
including the created_at fallback when updated_at is NULL.
"""

from __future__ import annotations

from datetime import (
    UTC,
    datetime,
    timedelta,
)
from typing import Any

from services.orchestrator.persistence import PipelineJob
from services.orchestrator.reconcile import SacctRecord
from tests.gateway_reconcile_helpers import (
    _store_repo,
    _StoreRepo,
)

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

    from datetime import UTC, datetime

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

    from datetime import UTC, datetime

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
