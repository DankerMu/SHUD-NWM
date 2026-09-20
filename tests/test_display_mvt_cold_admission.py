"""Cold MVT admission keeps reserved pool capacity for hot tiles and catalog.

The shared MVT boundary serves all six tile routes; this suite drives the
national river route and `GET /api/v1/layers` over FastAPI. Producers hold a
real SQLAlchemy QueuePool checkout until an event releases them, so starvation
and cleanup are observed as HTTP/pool behaviour rather than helper echoes.
"""

from __future__ import annotations

import threading
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, event, text
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session
from sqlalchemy.pool import QueuePool

from apps.api import main
from apps.api.errors import ApiError
from apps.api.routes import hydro_display
from services.tiles.mvt import TileInput, TileResponse, cache_key

_RIVER_URL = "/api/v1/tiles/river-network-national/3/6/3.pbf"
_LAYERS_URL = "/api/v1/layers"
_BUSY_CODE = "MVT_COLD_GENERATION_BUSY"
_REQUEST_ID = "req-cold-admission"


class _CheckoutTracker:
    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.checkouts = 0
        self.releases = 0
        self.checked_out = 0

    def snapshot(self) -> tuple[int, int, int]:
        with self.lock:
            return self.checkouts, self.releases, self.checked_out


def _attach_checkout_tracker(engine: Engine) -> _CheckoutTracker:
    tracker = _CheckoutTracker()

    @event.listens_for(engine, "checkout")
    def _on_checkout(_dbapi_connection: Any, _connection_record: Any, _connection_proxy: Any) -> None:
        with tracker.lock:
            tracker.checkouts += 1
            tracker.checked_out += 1

    @event.listens_for(engine, "checkin")
    def _on_checkin(_dbapi_connection: Any, _connection_record: Any) -> None:
        with tracker.lock:
            tracker.releases += 1
            tracker.checked_out -= 1

    return tracker


def _sqlite_engine(pool_size: int, max_overflow: int) -> Engine:
    engine = create_engine(
        "sqlite://",
        future=True,
        poolclass=QueuePool,
        pool_size=pool_size,
        max_overflow=max_overflow,
        pool_timeout=0.2,
        connect_args={"check_same_thread": False},
    )
    with engine.begin() as connection:
        connection.execute(text("SELECT 1"))
    return engine


def _reset_cold_gate() -> None:
    hydro_display.__dict__["_DISPLAY_POOL_CONFIGURATION"] = None
    hydro_display.__dict__["_COLD_GATE"] = None
    hydro_display.__dict__["_COLD_GATE_LIMIT"] = None
    hydro_display._engine.cache_clear()


@pytest.fixture
def isolate_gate(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    _reset_cold_gate()
    monkeypatch.setenv("NHMS_DISPLAY_DB_POOL_SIZE", "3")
    monkeypatch.setenv("NHMS_DISPLAY_DB_MAX_OVERFLOW", "0")
    monkeypatch.setenv("NHMS_DISPLAY_MVT_COLD_LIMIT", "2")
    try:
        yield
    finally:
        _reset_cold_gate()


def _tile(source_id: str) -> TileInput:
    return TileInput(
        layer_id="river-network",
        source_id=source_id,
        source_version="generation-a",
        valid_time=None,
        z=3,
        x=6,
        y=3,
        variant_id="national",
    )


def _tile_response(tile: TileInput, data: bytes = b"pbf", cache_status: str = "miss") -> TileResponse:
    return TileResponse(
        data=data,
        checksum="checksum",
        etag='W/"etag"',
        cache_key=cache_key(tile),
        cache_status=cache_status,
        layer_id=tile.layer_id,
    )


def _occupy(engine: Engine, tile: TileInput, produce: Any) -> Any:
    with Session(engine) as session:
        session.execute(text("SELECT 1"))
        return hydro_display._cached_or_generated_mvt_response(session, tile, produce)


def test_saturated_distinct_cold_requests_return_503_while_hot_and_layers_stay_200(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Any, isolate_gate: None
) -> None:
    monkeypatch.setenv("NHMS_MVT_FILE_CACHE_DIR", str(tmp_path))
    engine = _sqlite_engine(pool_size=3, max_overflow=0)
    tracker = _attach_checkout_tracker(engine)
    started = threading.Barrier(3)
    release = threading.Event()
    third_started = threading.Event()
    results: dict[str, Any] = {}
    errors: dict[str, BaseException] = {}
    cold_coordinates = {(5, 3), (6, 3)}
    extra_coordinates = {(7, 3)}

    def fake_read(session: Session, tile: TileInput) -> TileResponse | None:
        if (tile.x, tile.y) == (4, 3):
            return _tile_response(tile, b"hot", cache_status="hit")
        session.execute(text("SELECT 1"))
        return None

    def fake_build(session: Session, tile: TileInput, data: bytes) -> TileResponse:
        session.execute(text("SELECT 1"))
        return _tile_response(tile, data)

    def fake_fetch(
        session: Session, _layer: str, _params: dict[str, Any], *, z: int, x: int, y: int
    ) -> bytes:
        session.execute(text("SELECT 1"))
        if (x, y) in cold_coordinates:
            started.wait(timeout=5)
            release.wait()
        elif (x, y) in extra_coordinates:
            third_started.set()
            release.wait()
        return f"cold-{z}-{x}-{y}".encode()

    def identity_digest(session: Session) -> str:
        session.execute(text("SELECT 1"))
        return "generation-a"

    def display_ready(session: Session) -> None:
        session.execute(text("SELECT 1"))
        return None

    monkeypatch.setattr(hydro_display, "read_cached_tile_response", fake_read)
    monkeypatch.setattr(hydro_display, "build_raw_tile_response", fake_build)
    monkeypatch.setattr(hydro_display, "_fetch_postgis_tile_bytes", fake_fetch)
    monkeypatch.setattr(hydro_display, "national_river_network_source_version", identity_digest)
    monkeypatch.setattr(hydro_display, "display_ready_run", display_ready)
    monkeypatch.setattr(hydro_display, "display_catalog_cached", lambda _request, _key, loader: loader())

    def session_dep() -> Iterator[Session]:
        with Session(engine) as session:
            yield session

    app = main.create_app()
    app.dependency_overrides[hydro_display.get_hydro_display_session] = session_dep

    def issue(name: str, path: str) -> None:
        try:
            with TestClient(app, raise_server_exceptions=False) as client:
                results[name] = client.get(path)
        except BaseException as exc:
            errors[name] = exc

    cold_threads = [
        threading.Thread(target=issue, args=("cold-a", "/api/v1/tiles/river-network-national/3/5/3.pbf")),
        threading.Thread(target=issue, args=("cold-b", "/api/v1/tiles/river-network-national/3/6/3.pbf")),
    ]
    extra_thread = threading.Thread(
        target=issue, args=("extra", "/api/v1/tiles/river-network-national/3/7/3.pbf")
    )
    started_threads: list[threading.Thread] = []
    try:
        for thread in cold_threads:
            thread.start()
            started_threads.append(thread)
        started.wait(timeout=5)
        extra_thread.start()
        started_threads.append(extra_thread)
        for _ in range(100):
            if "extra" in results or "extra" in errors or third_started.is_set():
                break
            extra_thread.join(timeout=0.05)

        with TestClient(app, raise_server_exceptions=False) as client:
            hot = client.get("/api/v1/tiles/river-network-national/3/4/3.pbf")
            layers = client.get(_LAYERS_URL)

        # The pre-admission baseline starts the third producer, monopolizes the
        # real three-slot pool and makes this public catalog request time out.
        # The candidate rejects it before the producer, leaving the third slot.
        assert hot.status_code == 200
        assert layers.status_code == 200
        assert "extra" in results
        assert errors == {}
        extra = results["extra"]
        assert extra.status_code == 503
        assert extra.json()["error"]["code"] == _BUSY_CODE
        assert extra.headers["Retry-After"] == "1"
        assert extra.headers["Cache-Control"] == "no-store"
        _checkouts, _releases, checked_out = tracker.snapshot()
        assert checked_out == 2
    finally:
        release.set()
        for thread in started_threads:
            thread.join(timeout=5)
        app.dependency_overrides.clear()
        engine.dispose()

    assert all(not thread.is_alive() for thread in (*cold_threads, extra_thread))
    assert errors == {}
    assert results["cold-a"].status_code == results["cold-b"].status_code == 200


def test_same_key_waiter_does_not_hold_a_checkout(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Any, isolate_gate: None
) -> None:
    monkeypatch.setenv("NHMS_MVT_FILE_CACHE_DIR", str(tmp_path))
    engine = _sqlite_engine(pool_size=3, max_overflow=0)
    tracker = _attach_checkout_tracker(engine)
    first_entered = threading.Event()
    follower_entered_lock = threading.Event()
    release = threading.Event()
    stored: TileResponse | None = None
    generations = 0
    state_lock = threading.Lock()
    lock_attempts = 0
    lock_attempts_lock = threading.Lock()
    outcomes: dict[str, Any] = {}
    errors: dict[str, BaseException] = {}
    original_tile_generation_lock = hydro_display.tile_generation_lock

    @contextmanager
    def observed_tile_generation_lock(tile_input: TileInput) -> Iterator[None]:
        nonlocal lock_attempts
        with lock_attempts_lock:
            lock_attempts += 1
            if lock_attempts == 2:
                # This runs only after the follower has passed admission and
                # immediately before it blocks on the real file lock below.
                follower_entered_lock.set()
        with original_tile_generation_lock(tile_input):
            yield

    def fake_read(session: Session, tile: TileInput) -> TileResponse | None:
        session.execute(text("SELECT 1"))
        return stored

    def fake_build(session: Session, tile: TileInput, data: bytes) -> TileResponse:
        nonlocal stored
        session.execute(text("SELECT 1"))
        stored = _tile_response(tile, data)
        return stored

    def produce() -> bytes:
        nonlocal generations
        with state_lock:
            generations += 1
        first_entered.set()
        release.wait()
        return b"pbf"

    def unexpected_producer() -> bytes:
        raise AssertionError("same-key follower must receive the holder's cached tile")

    monkeypatch.setattr(hydro_display, "read_cached_tile_response", fake_read)
    monkeypatch.setattr(hydro_display, "build_raw_tile_response", fake_build)
    monkeypatch.setattr(hydro_display, "tile_generation_lock", observed_tile_generation_lock)
    tile = _tile("same-key")

    def request(name: str, producer: Any) -> None:
        try:
            outcomes[name] = _occupy(engine, tile, producer)
        except BaseException as exc:
            errors[name] = exc

    holder_thread = threading.Thread(target=request, args=("holder", produce))
    waiter_thread = threading.Thread(target=request, args=("follower", unexpected_producer))
    started_threads: list[threading.Thread] = []
    try:
        holder_thread.start()
        started_threads.append(holder_thread)
        assert first_entered.wait(timeout=5)
        waiter_thread.start()
        started_threads.append(waiter_thread)
        assert follower_entered_lock.wait(timeout=5)
        # The holder owns the only checkout while producing. The follower has
        # passed admission, rolled back, and is blocked on the real same-key
        # file lock, so it cannot have returned or failed yet.
        assert waiter_thread.is_alive()
        assert stored is None
        assert tracker.snapshot()[2] == 1

        # The holder and its follower consume both cold permits, but only the
        # holder holds a database checkout. A distinct key is rejected rather
        # than joining the lock queue or borrowing a reserved connection.
        with pytest.raises(ApiError) as excinfo:
            _occupy(engine, _tile("distinct-key"), unexpected_producer)
        assert excinfo.value.status_code == 503
        assert excinfo.value.code == _BUSY_CODE
        assert tracker.snapshot()[2] == 1
    finally:
        release.set()
        for thread in started_threads:
            thread.join(timeout=5)
        engine.dispose()

    assert not holder_thread.is_alive()
    assert not waiter_thread.is_alive()
    assert errors == {}
    assert set(outcomes) == {"holder", "follower"}
    assert outcomes["holder"].body == outcomes["follower"].body == b"pbf"
    assert generations == 1
    assert stored is not None and stored.data == b"pbf"


def test_producer_exception_releases_permit_and_checkout(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Any, isolate_gate: None
) -> None:
    monkeypatch.setenv("NHMS_MVT_FILE_CACHE_DIR", str(tmp_path))
    # One permit makes the recovery request prove the failed producer returned
    # it; a leaked permit under the fixture's cap of two would still admit one.
    monkeypatch.setenv("NHMS_DISPLAY_MVT_COLD_LIMIT", "1")
    engine = _sqlite_engine(pool_size=3, max_overflow=0)
    tracker = _attach_checkout_tracker(engine)

    def fake_read(session: Session, tile: TileInput) -> TileResponse | None:
        session.execute(text("SELECT 1"))
        return None

    def fake_build(session: Session, tile: TileInput, data: bytes) -> TileResponse:
        raise AssertionError("producer failure must not reach cache write")

    def producer_fails() -> bytes:
        raise RuntimeError("producer failed")

    monkeypatch.setattr(hydro_display, "read_cached_tile_response", fake_read)
    monkeypatch.setattr(hydro_display, "build_raw_tile_response", fake_build)
    try:
        # Keep the failing Session open while observing pool events. Closing it
        # first would hide a missing rollback in the production finally block.
        with Session(engine) as failed_session:
            with pytest.raises(RuntimeError, match="producer failed"):
                hydro_display._cached_or_generated_mvt_response(failed_session, _tile("boom"), producer_fails)
            assert tracker.snapshot()[2] == 0

        def fake_build_ok(session: Session, tile: TileInput, data: bytes) -> TileResponse:
            session.execute(text("SELECT 1"))
            return _tile_response(tile, data)

        monkeypatch.setattr(hydro_display, "build_raw_tile_response", fake_build_ok)
        recovered = _occupy(engine, _tile("recovered"), lambda: b"recovered")
        assert recovered.body == b"recovered"
    finally:
        engine.dispose()



@pytest.mark.parametrize("raise_on", (1, 2), ids=("initial-probe", "under-lock-probe"))
def test_cache_probe_exception_releases_permit_and_checkout(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Any, isolate_gate: None, raise_on: int
) -> None:
    monkeypatch.setenv("NHMS_MVT_FILE_CACHE_DIR", str(tmp_path))
    # At the under-lock probe this turns recovery into a permit-release proof.
    monkeypatch.setenv("NHMS_DISPLAY_MVT_COLD_LIMIT", "1")
    engine = _sqlite_engine(pool_size=3, max_overflow=0)
    tracker = _attach_checkout_tracker(engine)
    state = {"calls": 0, "raising": True}

    def fake_read(session: Session, tile: TileInput) -> TileResponse | None:
        session.execute(text("SELECT 1"))
        state["calls"] += 1
        if state["raising"] and state["calls"] == raise_on:
            raise RuntimeError("cache probe failed")
        return None

    def fake_build(session: Session, tile: TileInput, data: bytes) -> TileResponse:
        session.execute(text("SELECT 1"))
        return _tile_response(tile, data)

    monkeypatch.setattr(hydro_display, "read_cached_tile_response", fake_read)
    monkeypatch.setattr(hydro_display, "build_raw_tile_response", fake_build)
    try:
        # As above, assert before Session.__exit__ can conceal a leaked checkout.
        with Session(engine) as failed_session:
            with pytest.raises(RuntimeError, match="cache probe failed"):
                hydro_display._cached_or_generated_mvt_response(
                    failed_session, _tile(f"probe-{raise_on}"), lambda: b"unused"
                )
            assert tracker.snapshot()[2] == 0

        state["raising"] = False
        recovered = _occupy(engine, _tile(f"recovered-{raise_on}"), lambda: b"recovered")
        assert recovered.body == b"recovered"
    finally:
        engine.dispose()


def test_checkout_returns_before_permit_becomes_available(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Any, isolate_gate: None
) -> None:
    monkeypatch.setenv("NHMS_MVT_FILE_CACHE_DIR", str(tmp_path))
    monkeypatch.setenv("NHMS_DISPLAY_DB_POOL_SIZE", "2")
    monkeypatch.setenv("NHMS_DISPLAY_DB_MAX_OVERFLOW", "0")
    monkeypatch.setenv("NHMS_DISPLAY_MVT_COLD_LIMIT", "1")
    engine = _sqlite_engine(pool_size=2, max_overflow=0)
    _attach_checkout_tracker(engine)
    final_checkin_started = threading.Event()
    allow_final_checkin = threading.Event()
    checkin_lock = threading.Lock()
    checkins = 0
    holder: dict[str, Any] = {}
    errors: list[BaseException] = []

    @event.listens_for(engine, "checkin")
    def pause_final_checkin(_dbapi_connection: Any, _connection_record: Any) -> None:
        nonlocal checkins
        with checkin_lock:
            checkins += 1
            is_final_checkin = checkins == 2
        if is_final_checkin:
            final_checkin_started.set()
            allow_final_checkin.wait()

    def fake_read(session: Session, tile: TileInput) -> TileResponse | None:
        session.execute(text("SELECT 1"))
        return None

    def fake_build(session: Session, tile: TileInput, data: bytes) -> TileResponse:
        session.execute(text("SELECT 1"))
        return _tile_response(tile, data)

    def generate_holder() -> None:
        try:
            with Session(engine) as session:
                holder["response"] = hydro_display._cached_or_generated_mvt_response(
                    session, _tile("ordering-holder"), lambda: b"holder"
                )
        except BaseException as exc:
            errors.append(exc)

    monkeypatch.setattr(hydro_display, "read_cached_tile_response", fake_read)
    monkeypatch.setattr(hydro_display, "build_raw_tile_response", fake_build)
    holder_thread = threading.Thread(target=generate_holder)
    try:
        holder_thread.start()
        assert final_checkin_started.wait(timeout=5)

        # The holder has reached the real checkin callback but has not returned
        # from rollback. The spare pool connection lets this contender reach
        # admission; it must still see the holder's permit as unavailable.
        with Session(engine) as contender_session:
            with pytest.raises(ApiError) as excinfo:
                hydro_display._cached_or_generated_mvt_response(
                    contender_session, _tile("ordering-contender"), lambda: b"unexpected"
                )
        assert excinfo.value.status_code == 503
        assert excinfo.value.code == _BUSY_CODE
    finally:
        allow_final_checkin.set()
        holder_thread.join(timeout=5)
        engine.dispose()

    assert not holder_thread.is_alive()
    assert errors == []
    assert holder["response"].body == b"holder"


def test_capacity_one_admits_zero_cold_work(monkeypatch: pytest.MonkeyPatch) -> None:
    _reset_cold_gate()
    engine: Engine | None = None
    try:
        monkeypatch.setenv("NHMS_DISPLAY_DB_POOL_SIZE", "1")
        monkeypatch.setenv("NHMS_DISPLAY_DB_MAX_OVERFLOW", "0")
        monkeypatch.delenv("NHMS_DISPLAY_MVT_COLD_LIMIT", raising=False)
        assert hydro_display._effective_cold_limit() == 0
        monkeypatch.setenv("NHMS_DISPLAY_MVT_COLD_LIMIT", "8")
        assert hydro_display._effective_cold_limit() == 0
        engine = _sqlite_engine(pool_size=1, max_overflow=0)

        def fake_read(session: Session, tile: TileInput) -> TileResponse | None:
            session.execute(text("SELECT 1"))
            return None

        monkeypatch.setattr(hydro_display, "read_cached_tile_response", fake_read)
        with pytest.raises(ApiError) as excinfo:
            _occupy(engine, _tile("cold"), lambda: b"unused")
        assert excinfo.value.status_code == 503
        assert excinfo.value.code == _BUSY_CODE
    finally:
        if engine is not None:
            engine.dispose()
        _reset_cold_gate()


def test_requested_cold_limit_is_clamped_below_capacity(monkeypatch: pytest.MonkeyPatch) -> None:
    _reset_cold_gate()
    try:
        monkeypatch.setenv("NHMS_DISPLAY_DB_POOL_SIZE", "8")
        monkeypatch.setenv("NHMS_DISPLAY_DB_MAX_OVERFLOW", "8")
        monkeypatch.setenv("NHMS_DISPLAY_MVT_COLD_LIMIT", "16")
        assert hydro_display._effective_cold_limit() == 15
        monkeypatch.setenv("NHMS_DISPLAY_MVT_COLD_LIMIT", "8")
        assert hydro_display._effective_cold_limit() == 8
        monkeypatch.setenv("NHMS_DISPLAY_MVT_COLD_LIMIT", "not-an-int")
        assert hydro_display._effective_cold_limit() == 8
        monkeypatch.setenv("NHMS_DISPLAY_MVT_COLD_LIMIT", "-1")
        assert hydro_display._effective_cold_limit() == 8
        monkeypatch.delenv("NHMS_DISPLAY_MVT_COLD_LIMIT", raising=False)
        assert hydro_display._effective_cold_limit() == 8
    finally:
        _reset_cold_gate()



def test_concurrent_first_use_bounds_admitted_cold_generation(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Any
) -> None:
    _reset_cold_gate()
    engine: Engine | None = None
    release = threading.Event()
    threads: list[threading.Thread] = []
    producer_names: list[str] = []
    results: dict[str, Any] = {}
    errors: dict[str, BaseException] = {}
    try:
        monkeypatch.setenv("NHMS_MVT_FILE_CACHE_DIR", str(tmp_path))
        monkeypatch.setenv("NHMS_DISPLAY_DB_POOL_SIZE", "3")
        monkeypatch.setenv("NHMS_DISPLAY_DB_MAX_OVERFLOW", "0")
        monkeypatch.setenv("NHMS_DISPLAY_MVT_COLD_LIMIT", "2")
        engine = _sqlite_engine(pool_size=3, max_overflow=0)
        tracker = _attach_checkout_tracker(engine)
        start = threading.Barrier(5)
        settled = threading.Event()
        state_lock = threading.Lock()
        arrived = 0
        active = 0
        max_active = 0
        rejected: list[ApiError] = []

        def mark_arrived() -> None:
            nonlocal arrived
            with state_lock:
                arrived += 1
                if arrived == 4:
                    settled.set()

        def fake_read(session: Session, tile: TileInput) -> TileResponse | None:
            session.execute(text("SELECT 1"))
            return None

        def fake_build(session: Session, tile: TileInput, data: bytes) -> TileResponse:
            session.execute(text("SELECT 1"))
            return _tile_response(tile, data)

        def request(name: str) -> None:
            def produce() -> bytes:
                nonlocal active, max_active
                with state_lock:
                    active += 1
                    max_active = max(max_active, active)
                    producer_names.append(name)
                mark_arrived()
                release.wait()
                with state_lock:
                    active -= 1
                return name.encode()

            try:
                start.wait(timeout=5)
                results[name] = _occupy(engine, _tile(f"first-use-{name}"), produce)
            except ApiError as exc:
                with state_lock:
                    rejected.append(exc)
                mark_arrived()
            except BaseException as exc:
                with state_lock:
                    errors[name] = exc
                mark_arrived()

        monkeypatch.setattr(hydro_display, "read_cached_tile_response", fake_read)
        monkeypatch.setattr(hydro_display, "build_raw_tile_response", fake_build)
        threads = [threading.Thread(target=request, args=(f"cold-{index}",)) for index in range(4)]
        for thread in threads:
            thread.start()
        start.wait(timeout=5)
        assert settled.wait(timeout=5)
        with state_lock:
            assert errors == {}
            assert len(producer_names) == 2
            assert len(rejected) == 2
            assert active == max_active == 2
            assert {error.code for error in rejected} == {_BUSY_CODE}
        assert tracker.snapshot()[2] == 2
    finally:
        release.set()
        if engine is not None:
            for thread in threads:
                thread.join(timeout=5)
            engine.dispose()
        _reset_cold_gate()

    assert all(not thread.is_alive() for thread in threads)
    assert errors == {}
    assert len(results) == 2
    assert {response.body for response in results.values()} == {name.encode() for name in producer_names}


def test_busy_response_preserves_canonical_request_id_against_case_variants(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _reset_cold_gate()
    monkeypatch.setenv("NHMS_DISPLAY_DB_POOL_SIZE", "3")
    monkeypatch.setenv("NHMS_DISPLAY_DB_MAX_OVERFLOW", "0")
    monkeypatch.setenv("NHMS_DISPLAY_MVT_COLD_LIMIT", "0")
    engine = _sqlite_engine(pool_size=3, max_overflow=0)

    def fake_read(session: Session, tile: TileInput) -> TileResponse | None:
        session.execute(text("SELECT 1"))
        return None

    def identity_digest(session: Session) -> str:
        session.execute(text("SELECT 1"))
        return "generation-a"

    monkeypatch.setattr(hydro_display, "read_cached_tile_response", fake_read)
    monkeypatch.setattr(hydro_display, "national_river_network_source_version", identity_digest)

    def session_dep() -> Iterator[Session]:
        with Session(engine) as session:
            yield session

    app = main.create_app()
    app.dependency_overrides[hydro_display.get_hydro_display_session] = session_dep
    try:
        with TestClient(app, raise_server_exceptions=False) as client:
            response = client.get(_RIVER_URL, headers={"x-request-id": _REQUEST_ID})
    finally:
        app.dependency_overrides.clear()
        _reset_cold_gate()
        engine.dispose()
    assert response.status_code == 503
    assert response.json()["error"]["code"] == _BUSY_CODE
    assert response.json()["request_id"] == _REQUEST_ID
    assert response.headers["X-Request-ID"] == _REQUEST_ID
    assert response.headers["Retry-After"] == "1"
    assert response.headers["Cache-Control"] == "no-store"
    assert [name for name in response.headers if name.lower() == "x-request-id"] == ["x-request-id"]
