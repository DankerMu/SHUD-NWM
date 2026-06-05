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
