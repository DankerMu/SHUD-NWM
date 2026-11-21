"""Pin the #3262 ORM helpers to the ``PipelineStore`` write methods they replace.

Every case runs the same inputs against two fresh SQLite databases with the
production ``ops`` schema: one through the original ``PipelineStore`` method,
one through ``tests.pipeline_job_orm_helpers``. Both must return the same
result (``None`` or a row with the same columns) and leave the same rows.

``utc_now`` is patched to one fixed instant in both modules, so ``submitted_at``
and an explicitly refreshed ``updated_at`` compare exactly. ``created_at`` and a
default-only ``updated_at`` come from the column default (bound at import, so
not patchable) and compare NULL vs non-NULL.

R5-2 deletes or rewrites this module when the original methods are deleted.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator
from datetime import UTC, datetime
from typing import Any

import pytest
from sqlalchemy import create_engine, event, select
from sqlalchemy.orm import Session

import services.orchestrator.persistence as persistence
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
            value = "patched_now" if value is not None and _naive(value) == _naive(FIXED_NOW) else value is not None
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
    """Put pre-existing rows in place with neither code path under test."""

    for row in rows:
        session.add(PipelineJob(**row))
    session.commit()


class _HelperFunctions:
    """The module functions behind the same method names as ``PipelineStore``."""

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
def pair(request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch) -> Iterator[tuple[PipelineStore, Any]]:
    monkeypatch.setattr(persistence, "utc_now", lambda: FIXED_NOW)
    monkeypatch.setattr(orm_helpers, "utc_now", lambda: FIXED_NOW)
    original = PipelineStore(_session())
    helper = _HELPER_SURFACES[request.param](_session())
    try:
        yield original, helper
    finally:
        original.session.close()
        helper.session.close()


def _assert_same(
    pair: tuple[PipelineStore, Any],
    operation: Callable[[Any], PipelineJob | None],
    *,
    prefill: list[dict[str, Any]] | None = None,
    exact_updated_at: bool = True,
) -> dict[str, Any] | None:
    original, helper = pair
    _prefill(original.session, [dict(row) for row in prefill or []])
    _prefill(helper.session, [dict(row) for row in prefill or []])
    expected = _result(operation(original), exact_updated_at=exact_updated_at)
    actual = _result(operation(helper), exact_updated_at=exact_updated_at)
    assert actual == expected
    assert _rows(helper.session, exact_updated_at=exact_updated_at) == _rows(
        original.session, exact_updated_at=exact_updated_at
    )
    return expected


# --- seed shapes (create_job) ----------------------------------------------

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


@pytest.mark.parametrize("shape", sorted(_SEED_SHAPES))
def test_seed_pipeline_job_writes_the_row_create_job_wrote(pair: tuple[PipelineStore, Any], shape: str) -> None:
    kwargs = _SEED_SHAPES[shape]
    _assert_same(pair, lambda store: store.create_job(**kwargs), exact_updated_at=False)

    # commit=True leaves a committed row; commit=False only a flushed one.
    original, helper = pair
    original.session.rollback()
    helper.session.rollback()
    survived = _rows(original.session, exact_updated_at=False)
    assert _rows(helper.session, exact_updated_at=False) == survived
    assert bool(survived) is kwargs.get("commit", True)


def test_one_seed_row_equals_a_reserve_then_bind(monkeypatch: pytest.MonkeyPatch) -> None:
    """tests/test_orchestration_chain.py's prior-pass row: reserve_job + bind_reservation(running)."""

    monkeypatch.setattr(persistence, "utc_now", lambda: FIXED_NOW)
    monkeypatch.setattr(orm_helpers, "utc_now", lambda: FIXED_NOW)
    original = PipelineStore(_session())
    seeded = _session()
    common = dict(
        job_id="prior_pass_job",
        run_id="cycle_gfs_2026050100",
        cycle_id="gfs_2026050100",
        job_type="convert_canonical",
        model_id=None,
        stage="convert",
    )
    try:
        original.reserve_job(**common, idempotency_key=KEY)
        original.bind_reservation(KEY, slurm_job_id="90099", status="running")
        seed_pipeline_job(seeded, **common, slurm_job_id="90099", status="running", idempotency_key=KEY)

        assert _rows(seeded, exact_updated_at=False) == _rows(original.session, exact_updated_at=False)
    finally:
        original.session.close()
        seeded.close()


# --- reserve_job ------------------------------------------------------------

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


def test_reserve_win_inserts_the_reserved_unbound_row(pair: tuple[PipelineStore, Any]) -> None:
    won = _assert_same(pair, lambda store: store.reserve_job(**_RESERVE, candidate_id="cand_1"))

    assert won is not None and won["status"] == "reserved" and won["submitted_at"] is None


def test_reserve_passes_a_caller_status_through(pair: tuple[PipelineStore, Any]) -> None:
    _assert_same(pair, lambda store: store.reserve_job(**_RESERVE, status="pending"))


@pytest.mark.parametrize(
    "existing",
    [
        pytest.param(_existing(), id="same_idempotency_key"),
        pytest.param(_existing(job_id="job_reserve", idempotency_key="other:key"), id="same_job_id"),
    ],
)
def test_reserve_conflict_returns_none_and_keeps_the_session_usable(
    pair: tuple[PipelineStore, Any], existing: dict[str, Any]
) -> None:
    lost = _assert_same(pair, lambda store: store.reserve_job(**_RESERVE), prefill=[existing])
    assert lost is None

    # The savepoint rolled back only the loser's insert; the session still writes.
    _assert_same(
        pair,
        lambda store: store.reserve_job(**{**_RESERVE, "job_id": "job_next", "idempotency_key": "next:key"}),
    )


# --- reclaim_reservation ----------------------------------------------------

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
def test_reclaim_takes_a_dead_reservation_back_and_fills_only_null_identity(
    pair: tuple[PipelineStore, Any], status: str
) -> None:
    reclaimed = _assert_same(
        pair,
        lambda store: store.reclaim_reservation(KEY, job_id="ignored_job_id", **_RECLAIM_IDENTITY),
        prefill=[_dead(status)],
    )

    assert reclaimed is not None
    assert (reclaimed["status"], reclaimed["run_id"], reclaimed["cycle_id"]) == ("reserved", "run_new", "cycle_kept")
    assert reclaimed["updated_at"] == "patched_now"


@pytest.mark.parametrize(
    "existing",
    [
        pytest.param(_existing(status="reserved"), id="live_reserved"),
        pytest.param(_existing(status="submitted", slurm_job_id="701"), id="live_submitted"),
        pytest.param(_existing(status="running", slurm_job_id="702"), id="live_running"),
        pytest.param(_existing(status="pending"), id="keyed_pending"),
        pytest.param(_dead("reservation_lost", slurm_job_id="703"), id="dead_status_but_bound"),
    ],
)
def test_reclaim_refuses_a_live_or_bound_row(pair: tuple[PipelineStore, Any], existing: dict[str, Any]) -> None:
    refused = _assert_same(
        pair,
        lambda store: store.reclaim_reservation(KEY, job_id=existing["job_id"], **_RECLAIM_IDENTITY),
        prefill=[existing],
    )

    assert refused is None


def test_reclaim_adopts_an_unkeyed_pending_row_named_by_job_id(pair: tuple[PipelineStore, Any]) -> None:
    pending = _existing(status="pending", idempotency_key=None, run_id=None, submitted_at=EARLIER)
    adopted = _assert_same(
        pair,
        lambda store: store.reclaim_reservation(KEY, job_id="job_existing", **_RECLAIM_IDENTITY),
        prefill=[pending],
    )

    assert adopted is not None
    assert (adopted["idempotency_key"], adopted["status"], adopted["submitted_at"]) == (KEY, "reserved", None)


@pytest.mark.parametrize(
    ("job_id", "existing"),
    [
        pytest.param(None, [], id="no_row_no_job_id"),
        pytest.param("", [_existing(status="pending", idempotency_key=None)], id="empty_job_id"),
        pytest.param("job_missing", [], id="job_id_without_row"),
        pytest.param("job_existing", [_existing(status="pending", idempotency_key="other:key")], id="row_keyed"),
        pytest.param(
            "job_existing",
            [_existing(status="pending", idempotency_key=None, slurm_job_id="704")],
            id="row_bound",
        ),
        pytest.param("job_existing", [_existing(status="failed", idempotency_key=None)], id="row_not_pending"),
    ],
)
def test_reclaim_without_a_keyed_row_refuses_everything_but_an_adoptable_row(
    pair: tuple[PipelineStore, Any], job_id: str | None, existing: list[dict[str, Any]]
) -> None:
    refused = _assert_same(
        pair,
        lambda store: store.reclaim_reservation(KEY, job_id=job_id, **_RECLAIM_IDENTITY),
        prefill=existing,
    )

    assert refused is None


# --- bind_reservation -------------------------------------------------------


@pytest.mark.parametrize(
    ("existing", "kwargs"),
    [
        pytest.param(_existing(), dict(slurm_job_id="801"), id="reserved_unbound"),
        pytest.param(_existing(), dict(slurm_job_id="802", status="running", array_task_id=3), id="status_and_task"),
        pytest.param(_existing(array_task_id=5), dict(slurm_job_id="803"), id="keeps_array_task_id"),
        pytest.param(_existing(submitted_at=EARLIER), dict(slurm_job_id="804"), id="keeps_submitted_at"),
    ],
)
def test_bind_stamps_the_unbound_reservation(
    pair: tuple[PipelineStore, Any], existing: dict[str, Any], kwargs: dict[str, Any]
) -> None:
    bound = _assert_same(pair, lambda store: store.bind_reservation(KEY, **kwargs), prefill=[existing])

    assert bound is not None and bound["slurm_job_id"] == kwargs["slurm_job_id"]
    assert bound["updated_at"] == "patched_now"


@pytest.mark.parametrize(
    "existing",
    [
        pytest.param([_existing(status="submitted", slurm_job_id="900")], id="already_bound"),
        pytest.param([], id="missing_row"),
        pytest.param([_existing(idempotency_key="other:key")], id="other_key_only"),
    ],
)
def test_bind_is_a_noop_when_missing_or_already_bound(
    pair: tuple[PipelineStore, Any], existing: list[dict[str, Any]]
) -> None:
    noop = _assert_same(
        pair,
        lambda store: store.bind_reservation(KEY, slurm_job_id="999", status="running"),
        prefill=existing,
    )

    assert noop is None


# --- OrmPipelineStore surface ------------------------------------------------


def test_orm_store_is_not_a_pipeline_store_and_keeps_only_surviving_reads() -> None:
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
