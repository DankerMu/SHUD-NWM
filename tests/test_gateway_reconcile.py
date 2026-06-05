"""Tests for restart reconcile-by-identity of in-flight Slurm jobs.

Reconcile MUST read job ids from the durable ``pipeline_job`` table (not gateway
memory), verify candidate identity via ``sacct``, and never resubmit a
still-running or already-terminal candidate.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import create_engine, event
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from services.orchestrator.persistence import Base, PipelineJob, PipelineStore
from services.orchestrator.reconcile import (
    RECONCILE_UNVERIFIED_STATUS,
    SacctRecord,
    reconcile_inflight_jobs,
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


def _make_inflight_job(
    store: PipelineStore,
    *,
    job_id: str,
    slurm_job_id: str,
    stage: str = "run_shud_forecast_array",
    status: str = "running",
    run_id: str = "run_1",
    model_id: str = "model_1",
) -> None:
    store.create_job(
        job_id=job_id,
        run_id=run_id,
        cycle_id="cycle_1",
        job_type=stage,
        slurm_job_id=slurm_job_id,
        model_id=model_id,
        stage=stage,
        status=status,
    )


def _fake_sacct(records: dict[str, SacctRecord | None]):
    """Fake sacct querier backed by a dict; ``None`` => unknown to accounting."""

    def _query(slurm_job_id: str) -> SacctRecord | None:
        return records.get(str(slurm_job_id))

    return _query


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


def test_reconcile_unknown_to_accounting_is_unverified_not_resubmitted() -> None:
    store = _store()
    _make_inflight_job(store, job_id="job_unknown", slurm_job_id="3003")

    # sacct knows nothing about this job id.
    sacct = _fake_sacct({"3003": None})

    outcomes = reconcile_inflight_jobs(store, sacct_query=sacct)

    assert outcomes[0].action == "unverified"
    assert store.get_job("job_unknown").status == RECONCILE_UNVERIFIED_STATUS


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

    outcomes = reconcile_reserved_unbound_jobs(store.store, comment_query=_comment_query)

    # Reconcile queried accounting (did not re-submit) and marked it typed.
    assert submit_attempts == [key]
    assert outcomes[0].action == "reservation_lost"
    state = store.query_candidate_state(key)
    assert state["status"] == RESERVATION_LOST_STATUS
    assert state["slurm_job_id"] is None
    # At most one row for this key ever existed.
    rows = [j for j in store.store.session.query(PipelineJob).all() if j.idempotency_key == key]
    assert len(rows) == 1
