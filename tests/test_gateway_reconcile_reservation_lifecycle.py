"""Reservation lifecycle: reserve-before-submit, crash-window bind-by-
comment, dead-reservation reclaim, and reserve SQL conflict absorption.
"""

from __future__ import annotations

from typing import Any

from services.orchestrator.persistence import PipelineJob
from services.orchestrator.reconcile import SacctRecord
from tests.gateway_reconcile_helpers import (
    _past_grace_now,
    _store_repo,
    _StoreRepo,
)


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
