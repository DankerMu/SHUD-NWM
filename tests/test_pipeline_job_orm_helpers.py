"""Pin the #3262 ORM helpers to literal ``pipeline_job`` outcomes.

R5-1 proved each helper equal to the ``PipelineStore`` write method it replaced
(``create_job`` / ``reserve_job`` / ``reclaim_reservation`` /
``bind_reservation``) by running both on the same inputs. R5-2 deleted those
methods, so every case now asserts the outcome that paired run recorded: the
returned ``None`` or row (all columns) and every row left behind. Each case runs
on both helper surfaces, the module functions and ``OrmPipelineStore`` (the
store double the reconcile suites drive production code with), each against a
fresh SQLite database with the production ``ops`` schema.

``utc_now`` is patched to one fixed instant in the helper module, so
``submitted_at`` and an explicitly refreshed ``updated_at`` (``"patched_now"``)
compare exactly. ``created_at`` and a default-only ``updated_at`` come from the
column default (bound at import, so not patchable) and compare NULL vs non-NULL.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator
from datetime import UTC, datetime
from typing import Any

import pytest
from sqlalchemy import create_engine, event, select
from sqlalchemy.orm import Session

import tests.pipeline_job_orm_helpers as orm_helpers
from services.orchestrator.persistence import Base, PipelineJob, PipelineStore
from tests.pipeline_job_orm_helpers import (
    OrmPipelineStore,
    orm_bind_reservation,
    orm_reclaim_reservation,
    orm_reserve_job,
    seed_pipeline_job,
)

FIXED_NOW = datetime(2026, 7, 1, 12, 0, tzinfo=UTC)
EARLIER = datetime(2026, 6, 30, 6, 0, tzinfo=UTC)
KEY = "cycle_gfs_2026050100:convert"

# How ``_snapshot`` renders the two instants above (naive UTC).
FIXED_NOW_NAIVE = datetime(2026, 7, 1, 12, 0)
EARLIER_NAIVE = datetime(2026, 6, 30, 6, 0)
PATCHED_NOW = "patched_now"


def _session() -> Session:
    engine = create_engine("sqlite://", future=True)

    @event.listens_for(engine, "connect")
    def _attach_ops_schema(dbapi_connection: Any, _record: Any) -> None:
        dbapi_connection.execute("ATTACH DATABASE ':memory:' AS ops")

    Base.metadata.create_all(engine)
    return Session(engine)


def _naive(value: datetime) -> datetime:
    return value.astimezone(UTC).replace(tzinfo=None) if value.tzinfo is not None else value


def _snapshot(job: PipelineJob, *, exact_updated_at: bool) -> dict[str, Any]:
    row: dict[str, Any] = {}
    for column in PipelineJob.__table__.columns:
        value = getattr(job, column.key)
        if column.key == "created_at" or (column.key == "updated_at" and not exact_updated_at):
            value = value is not None
        elif column.key == "updated_at":
            value = PATCHED_NOW if value is not None and _naive(value) == _naive(FIXED_NOW) else value is not None
        elif isinstance(value, datetime):
            value = _naive(value)
        row[column.key] = value
    return row


def _rows(session: Session, *, exact_updated_at: bool) -> list[dict[str, Any]]:
    session.expire_all()
    jobs = session.scalars(select(PipelineJob).order_by(PipelineJob.job_id))
    return [_snapshot(job, exact_updated_at=exact_updated_at) for job in jobs]


def _result(job: PipelineJob | None, *, exact_updated_at: bool) -> dict[str, Any] | None:
    return None if job is None else _snapshot(job, exact_updated_at=exact_updated_at)


def _prefill(session: Session, rows: list[dict[str, Any]]) -> None:
    """Put pre-existing rows in place without the helper under test."""

    for row in rows:
        session.add(PipelineJob(**row))
    session.commit()


# Every ``ops.pipeline_job`` column as ``_snapshot`` renders an all-default row.
_ROW_BASE: dict[str, Any] = {
    "array_task_id": None,
    "candidate_id": None,
    "created_at": True,
    "cycle_id": None,
    "error_code": None,
    "error_message": None,
    "exit_code": None,
    "finished_at": None,
    "idempotency_key": None,
    "job_id": None,
    "job_type": None,
    "log_uri": None,
    "manual_retry_marker": False,
    "model_id": None,
    "retry_count": 0,
    "run_id": None,
    "slurm_job_id": None,
    "stage": None,
    "started_at": None,
    "status": None,
    "submitted_at": None,
    "updated_at": True,
}


def _job(**columns: Any) -> dict[str, Any]:
    """A literal expected row: ``_ROW_BASE`` with the given columns replaced."""

    unknown = set(columns) - set(_ROW_BASE)
    assert not unknown, unknown
    return {**_ROW_BASE, **columns}


def _expect_rows(session: Session, rows: list[dict[str, Any]], *, exact_updated_at: bool) -> None:
    assert _rows(session, exact_updated_at=exact_updated_at) == rows


def _check(
    store: Any,
    operation: Callable[[Any], PipelineJob | None],
    *,
    result: dict[str, Any] | None,
    rows: list[dict[str, Any]],
    prefill: list[dict[str, Any]] | None = None,
    exact_updated_at: bool = True,
) -> None:
    """Run ``operation`` on ``store``; assert the literal result and the rows left."""

    _prefill(store.session, [dict(row) for row in prefill or []])
    assert _result(operation(store), exact_updated_at=exact_updated_at) == result
    _expect_rows(store.session, rows, exact_updated_at=exact_updated_at)


class _HelperFunctions:
    """The module functions behind the method names ``OrmPipelineStore`` exposes."""

    def __init__(self, session: Session) -> None:
        self.session = session

    def create_job(self, **kwargs: Any) -> PipelineJob:
        return seed_pipeline_job(self.session, **kwargs)

    def reserve_job(self, **kwargs: Any) -> PipelineJob | None:
        return orm_reserve_job(self.session, **kwargs)

    def reclaim_reservation(self, key: str, **kwargs: Any) -> PipelineJob | None:
        return orm_reclaim_reservation(self.session, key, **kwargs)

    def bind_reservation(self, key: str, **kwargs: Any) -> PipelineJob | None:
        return orm_bind_reservation(self.session, key, **kwargs)


_HELPER_SURFACES: dict[str, Callable[[Session], Any]] = {
    "functions": _HelperFunctions,
    "orm_store": OrmPipelineStore,
}


@pytest.fixture(params=sorted(_HELPER_SURFACES))
def store(request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch) -> Iterator[Any]:
    monkeypatch.setattr(orm_helpers, "utc_now", lambda: FIXED_NOW)
    surface = _HELPER_SURFACES[request.param](_session())
    try:
        yield surface
    finally:
        surface.session.close()


# --- seed shapes (seed_pipeline_job, formerly create_job) --------------------

_SEED_SHAPES: dict[str, dict[str, Any]] = {
    # tests/test_pipeline_persistence.py, tests/test_retry.py, the inflight store.
    "bound_default_commit": dict(
        job_id="job_1",
        run_id="run_1",
        cycle_id="gfs_2026050100",
        job_type="download_source_cycle",
        slurm_job_id="123",
        model_id="model_a",
        stage="download",
        status="succeeded",
    ),
    # tests/test_orchestration_chain.py / test_chain_cross_stage_state.py / test_retry_mint_floor.py.
    "keyed_cycle_job": dict(
        job_id="job_cycle_gfs_2026050100_convert",
        run_id="cycle_gfs_2026050100",
        cycle_id="gfs_2026050100",
        job_type="convert_canonical",
        slurm_job_id="6051",
        model_id=None,
        stage="convert",
        status="failed",
        idempotency_key=KEY,
    ),
    "keyed_unbound_reservation_lost": dict(
        job_id="job_cycle_gfs_2026050100_forecast_model_0_forecast",
        run_id="cycle_gfs_2026050100_forecast_model_0",
        cycle_id="gfs_2026050100",
        job_type="run_shud_forecast_array",
        slurm_job_id=None,
        model_id="model_0",
        stage="forecast",
        status="reservation_lost",
        idempotency_key="cycle_gfs_2026050100_forecast_model_0:forecast",
    ),
    # StoreBackedCycleRepository.upsert_pipeline_job, test_monitoring_api.py,
    # test_retry_cancel_consistency.py, test_retry_route_fail_closed.py.
    "flush_only": dict(
        job_id="job_flush",
        run_id="run_1",
        cycle_id="cycle_1",
        job_type="run_shud_analysis",
        slurm_job_id="slurm_1",
        model_id="model_a",
        stage="run",
        status="failed",
        commit=False,
    ),
    # The optional parameters no current site passes, so the helper cannot drift on them.
    "every_optional_column": dict(
        job_id="job_full",
        run_id="run_full",
        cycle_id="cycle_full",
        job_type="run_shud_forecast_array",
        slurm_job_id=None,
        model_id="model_f",
        stage="forecast",
        status="reserved",
        retry_count=2,
        manual_retry_marker=True,
        idempotency_key="run_full:forecast",
        candidate_id="cand_full",
        submitted_at=EARLIER,
    ),
    "explicit_null_submitted_at": dict(
        job_id="job_null_submit",
        run_id=None,
        cycle_id=None,
        job_type="forcing",
        slurm_job_id=None,
        model_id=None,
        stage=None,
        submitted_at=None,
    ),
}

# shape -> (the row written, whether it survives ``session.rollback()``, i.e. was committed).
_SEED_EXPECTED: dict[str, tuple[dict[str, Any], bool]] = {
    "bound_default_commit": (
        _job(
            job_id="job_1",
            run_id="run_1",
            cycle_id="gfs_2026050100",
            job_type="download_source_cycle",
            slurm_job_id="123",
            model_id="model_a",
            stage="download",
            status="succeeded",
            submitted_at=FIXED_NOW_NAIVE,
        ),
        True,
    ),
    "keyed_cycle_job": (
        _job(
            job_id="job_cycle_gfs_2026050100_convert",
            run_id="cycle_gfs_2026050100",
            cycle_id="gfs_2026050100",
            job_type="convert_canonical",
            slurm_job_id="6051",
            stage="convert",
            status="failed",
            idempotency_key="cycle_gfs_2026050100:convert",
            submitted_at=FIXED_NOW_NAIVE,
        ),
        True,
    ),
    "keyed_unbound_reservation_lost": (
        _job(
            job_id="job_cycle_gfs_2026050100_forecast_model_0_forecast",
            run_id="cycle_gfs_2026050100_forecast_model_0",
            cycle_id="gfs_2026050100",
            job_type="run_shud_forecast_array",
            model_id="model_0",
            stage="forecast",
            status="reservation_lost",
            idempotency_key="cycle_gfs_2026050100_forecast_model_0:forecast",
            submitted_at=FIXED_NOW_NAIVE,
        ),
        True,
    ),
    "flush_only": (
        _job(
            job_id="job_flush",
            run_id="run_1",
            cycle_id="cycle_1",
            job_type="run_shud_analysis",
            slurm_job_id="slurm_1",
            model_id="model_a",
            stage="run",
            status="failed",
            submitted_at=FIXED_NOW_NAIVE,
        ),
        False,
    ),
    "every_optional_column": (
        _job(
            job_id="job_full",
            run_id="run_full",
            cycle_id="cycle_full",
            job_type="run_shud_forecast_array",
            model_id="model_f",
            stage="forecast",
            status="reserved",
            retry_count=2,
            manual_retry_marker=True,
            idempotency_key="run_full:forecast",
            candidate_id="cand_full",
            submitted_at=EARLIER_NAIVE,
        ),
        True,
    ),
    "explicit_null_submitted_at": (
        _job(job_id="job_null_submit", job_type="forcing", status="pending"),
        True,
    ),
}


@pytest.mark.parametrize("shape", sorted(_SEED_SHAPES))
def test_seed_pipeline_job_writes_the_row_create_job_wrote(store: Any, shape: str) -> None:
    written, committed = _SEED_EXPECTED[shape]
    _check(
        store,
        lambda s: s.create_job(**_SEED_SHAPES[shape]),
        result=written,
        rows=[written],
        exact_updated_at=False,
    )

    # commit=True leaves a committed row; commit=False only a flushed one.
    store.session.rollback()
    _expect_rows(store.session, [written] if committed else [], exact_updated_at=False)


def test_one_seed_row_equals_a_reserve_then_bind(store: Any) -> None:
    """tests/test_orchestration_chain.py's prior-pass row: reserve_job + bind_reservation(running)."""

    prior_pass_row = _job(
        job_id="prior_pass_job",
        run_id="cycle_gfs_2026050100",
        cycle_id="gfs_2026050100",
        job_type="convert_canonical",
        slurm_job_id="90099",
        stage="convert",
        status="running",
        idempotency_key="cycle_gfs_2026050100:convert",
        submitted_at=FIXED_NOW_NAIVE,
    )
    common = dict(
        job_id="prior_pass_job",
        run_id="cycle_gfs_2026050100",
        cycle_id="gfs_2026050100",
        job_type="convert_canonical",
        model_id=None,
        stage="convert",
    )
    reserved_then_bound = type(store)(_session())
    try:
        store.create_job(**common, slurm_job_id="90099", status="running", idempotency_key=KEY)
        _expect_rows(store.session, [prior_pass_row], exact_updated_at=False)

        reserved_then_bound.reserve_job(**common, idempotency_key=KEY)
        reserved_then_bound.bind_reservation(KEY, slurm_job_id="90099", status="running")
        _expect_rows(reserved_then_bound.session, [prior_pass_row], exact_updated_at=False)
    finally:
        reserved_then_bound.session.close()


# --- reserve (orm_reserve_job, formerly reserve_job) -------------------------

_RESERVE = dict(
    job_id="job_reserve",
    run_id="run_1",
    cycle_id="cycle_1",
    job_type="forcing",
    model_id="model_1",
    stage="forcing",
    idempotency_key=KEY,
)


def _existing(**overrides: Any) -> dict[str, Any]:
    row = dict(
        job_id="job_existing",
        run_id="run_existing",
        cycle_id="cycle_existing",
        job_type="forcing",
        slurm_job_id=None,
        model_id="model_existing",
        stage="forcing",
        status="reserved",
        idempotency_key=KEY,
        candidate_id=None,
        submitted_at=None,
    )
    row.update(overrides)
    return row


def test_reserve_win_inserts_the_reserved_unbound_row(store: Any) -> None:
    won = _job(
        job_id="job_reserve",
        run_id="run_1",
        cycle_id="cycle_1",
        job_type="forcing",
        model_id="model_1",
        stage="forcing",
        status="reserved",
        idempotency_key="cycle_gfs_2026050100:convert",
        candidate_id="cand_1",
    )
    _check(store, lambda s: s.reserve_job(**_RESERVE, candidate_id="cand_1"), result=won, rows=[won])


def test_reserve_passes_a_caller_status_through(store: Any) -> None:
    pending = _job(
        job_id="job_reserve",
        run_id="run_1",
        cycle_id="cycle_1",
        job_type="forcing",
        model_id="model_1",
        stage="forcing",
        status="pending",
        idempotency_key="cycle_gfs_2026050100:convert",
    )
    _check(store, lambda s: s.reserve_job(**_RESERVE, status="pending"), result=pending, rows=[pending])


_NEXT_RESERVED = _job(
    job_id="job_next",
    run_id="run_1",
    cycle_id="cycle_1",
    job_type="forcing",
    model_id="model_1",
    stage="forcing",
    status="reserved",
    idempotency_key="next:key",
)


@pytest.mark.parametrize(
    ("existing", "kept"),
    [
        pytest.param(
            _existing(),
            _job(
                job_id="job_existing",
                run_id="run_existing",
                cycle_id="cycle_existing",
                job_type="forcing",
                model_id="model_existing",
                stage="forcing",
                status="reserved",
                idempotency_key="cycle_gfs_2026050100:convert",
            ),
            id="same_idempotency_key",
        ),
        pytest.param(
            _existing(job_id="job_reserve", idempotency_key="other:key"),
            _job(
                job_id="job_reserve",
                run_id="run_existing",
                cycle_id="cycle_existing",
                job_type="forcing",
                model_id="model_existing",
                stage="forcing",
                status="reserved",
                idempotency_key="other:key",
            ),
            id="same_job_id",
        ),
    ],
)
def test_reserve_conflict_returns_none_and_keeps_the_session_usable(
    store: Any, existing: dict[str, Any], kept: dict[str, Any]
) -> None:
    _check(store, lambda s: s.reserve_job(**_RESERVE), prefill=[existing], result=None, rows=[kept])

    # The savepoint rolled back only the loser's insert; the session still writes.
    _check(
        store,
        lambda s: s.reserve_job(**{**_RESERVE, "job_id": "job_next", "idempotency_key": "next:key"}),
        result=_NEXT_RESERVED,
        rows=sorted([kept, _NEXT_RESERVED], key=lambda row: row["job_id"]),
    )


# --- reclaim (orm_reclaim_reservation, formerly reclaim_reservation) ---------

_RECLAIM_IDENTITY = dict(
    run_id="run_new",
    cycle_id="cycle_new",
    model_id="model_new",
    stage="stage_new",
    candidate_id="cand_new",
)


def _dead(status: str, **overrides: Any) -> dict[str, Any]:
    return _existing(
        status=status,
        run_id=None,
        cycle_id="cycle_kept",
        model_id=None,
        stage="stage_kept",
        candidate_id=None,
        array_task_id=4,
        submitted_at=EARLIER,
        started_at=EARLIER,
        finished_at=EARLIER,
        exit_code=1,
        error_code="SLURM_RESERVATION_LOST",
        error_message="lost",
        **overrides,
    )


@pytest.mark.parametrize("status", ["submission_failed", "reservation_lost"])
def test_reclaim_takes_a_dead_reservation_back_and_fills_only_null_identity(store: Any, status: str) -> None:
    reclaimed = _job(
        job_id="job_existing",
        run_id="run_new",
        cycle_id="cycle_kept",
        job_type="forcing",
        model_id="model_new",
        stage="stage_kept",
        status="reserved",
        idempotency_key="cycle_gfs_2026050100:convert",
        candidate_id="cand_new",
        updated_at=PATCHED_NOW,
    )
    _check(
        store,
        lambda s: s.reclaim_reservation(KEY, job_id="ignored_job_id", **_RECLAIM_IDENTITY),
        prefill=[_dead(status)],
        result=reclaimed,
        rows=[reclaimed],
    )


_KEYED_LIVE = dict(
    job_id="job_existing",
    run_id="run_existing",
    cycle_id="cycle_existing",
    job_type="forcing",
    model_id="model_existing",
    stage="forcing",
    idempotency_key="cycle_gfs_2026050100:convert",
)


@pytest.mark.parametrize(
    ("existing", "kept"),
    [
        pytest.param(_existing(status="reserved"), _job(**_KEYED_LIVE, status="reserved"), id="live_reserved"),
        pytest.param(
            _existing(status="submitted", slurm_job_id="701"),
            _job(**_KEYED_LIVE, status="submitted", slurm_job_id="701"),
            id="live_submitted",
        ),
        pytest.param(
            _existing(status="running", slurm_job_id="702"),
            _job(**_KEYED_LIVE, status="running", slurm_job_id="702"),
            id="live_running",
        ),
        pytest.param(_existing(status="pending"), _job(**_KEYED_LIVE, status="pending"), id="keyed_pending"),
        pytest.param(
            _dead("reservation_lost", slurm_job_id="703"),
            _job(
                job_id="job_existing",
                cycle_id="cycle_kept",
                job_type="forcing",
                slurm_job_id="703",
                array_task_id=4,
                stage="stage_kept",
                status="reservation_lost",
                idempotency_key="cycle_gfs_2026050100:convert",
                submitted_at=EARLIER_NAIVE,
                started_at=EARLIER_NAIVE,
                finished_at=EARLIER_NAIVE,
                exit_code=1,
                error_code="SLURM_RESERVATION_LOST",
                error_message="lost",
            ),
            id="dead_status_but_bound",
        ),
    ],
)
def test_reclaim_refuses_a_live_or_bound_row(store: Any, existing: dict[str, Any], kept: dict[str, Any]) -> None:
    _check(
        store,
        lambda s: s.reclaim_reservation(KEY, job_id=existing["job_id"], **_RECLAIM_IDENTITY),
        prefill=[existing],
        result=None,
        rows=[kept],
    )


def test_reclaim_adopts_an_unkeyed_pending_row_named_by_job_id(store: Any) -> None:
    pending = _existing(status="pending", idempotency_key=None, run_id=None, submitted_at=EARLIER)
    adopted = _job(
        job_id="job_existing",
        run_id="run_new",
        cycle_id="cycle_existing",
        job_type="forcing",
        model_id="model_existing",
        stage="forcing",
        status="reserved",
        idempotency_key="cycle_gfs_2026050100:convert",
        candidate_id="cand_new",
        updated_at=PATCHED_NOW,
    )
    _check(
        store,
        lambda s: s.reclaim_reservation(KEY, job_id="job_existing", **_RECLAIM_IDENTITY),
        prefill=[pending],
        result=adopted,
        rows=[adopted],
    )


_UNKEYED = dict(
    job_id="job_existing",
    run_id="run_existing",
    cycle_id="cycle_existing",
    job_type="forcing",
    model_id="model_existing",
    stage="forcing",
)


@pytest.mark.parametrize(
    ("job_id", "existing", "kept"),
    [
        pytest.param(None, [], [], id="no_row_no_job_id"),
        pytest.param(
            "",
            [_existing(status="pending", idempotency_key=None)],
            [_job(**_UNKEYED, status="pending")],
            id="empty_job_id",
        ),
        pytest.param("job_missing", [], [], id="job_id_without_row"),
        pytest.param(
            "job_existing",
            [_existing(status="pending", idempotency_key="other:key")],
            [_job(**_UNKEYED, status="pending", idempotency_key="other:key")],
            id="row_keyed",
        ),
        pytest.param(
            "job_existing",
            [_existing(status="pending", idempotency_key=None, slurm_job_id="704")],
            [_job(**_UNKEYED, status="pending", slurm_job_id="704")],
            id="row_bound",
        ),
        pytest.param(
            "job_existing",
            [_existing(status="failed", idempotency_key=None)],
            [_job(**_UNKEYED, status="failed")],
            id="row_not_pending",
        ),
    ],
)
def test_reclaim_without_a_keyed_row_refuses_everything_but_an_adoptable_row(
    store: Any, job_id: str | None, existing: list[dict[str, Any]], kept: list[dict[str, Any]]
) -> None:
    _check(
        store,
        lambda s: s.reclaim_reservation(KEY, job_id=job_id, **_RECLAIM_IDENTITY),
        prefill=existing,
        result=None,
        rows=kept,
    )


# --- bind (orm_bind_reservation, formerly bind_reservation) ------------------

_KEYED_EXISTING = dict(
    job_id="job_existing",
    run_id="run_existing",
    cycle_id="cycle_existing",
    job_type="forcing",
    model_id="model_existing",
    stage="forcing",
    idempotency_key="cycle_gfs_2026050100:convert",
)


@pytest.mark.parametrize(
    ("existing", "kwargs", "bound"),
    [
        pytest.param(
            _existing(),
            dict(slurm_job_id="801"),
            _job(
                **_KEYED_EXISTING,
                slurm_job_id="801",
                status="submitted",
                submitted_at=FIXED_NOW_NAIVE,
                updated_at=PATCHED_NOW,
            ),
            id="reserved_unbound",
        ),
        pytest.param(
            _existing(),
            dict(slurm_job_id="802", status="running", array_task_id=3),
            _job(
                **_KEYED_EXISTING,
                slurm_job_id="802",
                array_task_id=3,
                status="running",
                submitted_at=FIXED_NOW_NAIVE,
                updated_at=PATCHED_NOW,
            ),
            id="status_and_task",
        ),
        pytest.param(
            _existing(array_task_id=5),
            dict(slurm_job_id="803"),
            _job(
                **_KEYED_EXISTING,
                slurm_job_id="803",
                array_task_id=5,
                status="submitted",
                submitted_at=FIXED_NOW_NAIVE,
                updated_at=PATCHED_NOW,
            ),
            id="keeps_array_task_id",
        ),
        pytest.param(
            _existing(submitted_at=EARLIER),
            dict(slurm_job_id="804"),
            _job(
                **_KEYED_EXISTING,
                slurm_job_id="804",
                status="submitted",
                submitted_at=EARLIER_NAIVE,
                updated_at=PATCHED_NOW,
            ),
            id="keeps_submitted_at",
        ),
    ],
)
def test_bind_stamps_the_unbound_reservation(
    store: Any, existing: dict[str, Any], kwargs: dict[str, Any], bound: dict[str, Any]
) -> None:
    _check(store, lambda s: s.bind_reservation(KEY, **kwargs), prefill=[existing], result=bound, rows=[bound])


@pytest.mark.parametrize(
    ("existing", "kept"),
    [
        pytest.param(
            [_existing(status="submitted", slurm_job_id="900")],
            [_job(**_KEYED_EXISTING, status="submitted", slurm_job_id="900")],
            id="already_bound",
        ),
        pytest.param([], [], id="missing_row"),
        pytest.param(
            [_existing(idempotency_key="other:key")],
            [_job(**{**_KEYED_EXISTING, "idempotency_key": "other:key"}, status="reserved")],
            id="other_key_only",
        ),
    ],
)
def test_bind_is_a_noop_when_missing_or_already_bound(
    store: Any, existing: list[dict[str, Any]], kept: list[dict[str, Any]]
) -> None:
    _check(
        store,
        lambda s: s.bind_reservation(KEY, slurm_job_id="999", status="running"),
        prefill=existing,
        result=None,
        rows=kept,
    )


# --- OrmPipelineStore surface ------------------------------------------------

# The methods PipelineStore keeps after R5-2 (display reads, cancel route, reconcile reads).
_PIPELINE_STORE_SURFACE = {
    "get_job",
    "insert_event",
    "query_candidate_state",
    "query_inflight_jobs",
    "query_jobs_by_cycle",
    "query_jobs_by_run",
    "query_reserved_unbound_jobs",
    "update_job_status",
}
# Gone from PipelineStore; OrmPipelineStore keeps them, re-implemented in ORM.
_DELETED_WRITE_METHODS = {"bind_reservation", "create_job", "reclaim_reservation", "reserve_job"}


def test_orm_store_is_not_a_pipeline_store_and_keeps_only_surviving_reads() -> None:
    assert {name for name in vars(PipelineStore) if not name.startswith("_")} == _PIPELINE_STORE_SURFACE
    assert not any(hasattr(PipelineStore, name) for name in _DELETED_WRITE_METHODS)
    assert {name for name in vars(OrmPipelineStore) if not name.startswith("_")} == (
        _PIPELINE_STORE_SURFACE | _DELETED_WRITE_METHODS
    )

    session = _session()
    try:
        store = OrmPipelineStore(session)
        seed_pipeline_job(
            session,
            job_id="job_r",
            run_id="run_r",
            cycle_id="cycle_r",
            job_type="forcing",
            slurm_job_id=None,
            model_id=None,
            stage="forcing",
            status="reserved",
            idempotency_key=KEY,
            submitted_at=None,
        )

        assert not isinstance(store, PipelineStore)
        assert [job.job_id for job in store.query_reserved_unbound_jobs()] == ["job_r"]
        assert store.query_candidate_state(KEY).job_id == "job_r"
        assert store.get_job("job_r") is not None
        assert store.update_job_status("job_r", "reservation_lost").status == "reservation_lost"
        assert [job.job_id for job in store.query_jobs_by_run("run_r")] == ["job_r"]
        assert [job.job_id for job in store.query_jobs_by_cycle("cycle_r")] == ["job_r"]
        assert store.query_inflight_jobs() == []
        assert store.insert_event(
            entity_type="pipeline_job",
            entity_id="job_r",
            event_type="status_change",
            status_from="reserved",
            status_to="reservation_lost",
        ).event_id == 1
        # A name PipelineStore never had stays absent, as the reconcile probes expect.
        assert getattr(store, "bind_forcing_submit_attempt", None) is None
    finally:
        session.close()
