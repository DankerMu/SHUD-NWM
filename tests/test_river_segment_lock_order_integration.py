"""In-place river-segment writers take the parent network row lock first (#2157).

Two production transactions rewrite EXISTING ``core.river_segment`` rows: the
``UPDATE`` in ``_backfill_output_segment_geometry`` and the output-segment
upsert of ``qhh_production_bootstrap.py::_seed_output_segment_rows``. The import
transaction locks the parent ``core.river_network_version`` row first (it
``UPDATE``s it) and the backfill ends by bumping that same row, so a backfill
that wrote segments before touching the parent formed an ABBA pair with it.
After #2157 every in-place writer takes the parent row lock (``SELECT ... FOR
NO KEY UPDATE``) before its first segment statement.

These are two-session interleavings against a per-test throwaway database
(``throwaway_database_url``). Each step waits for the other session's state in
``pg_stat_activity`` / ``pg_locks`` before the next one, with bounded polling.
Nothing can hang: ``PGOPTIONS`` gives every connection this test opens -- the
ones ``seed_qhh_output_segments`` opens itself included -- a ``lock_timeout``
far above the server's ``deadlock_timeout``, every thread join is bounded, and
the ``finally`` blocks cancel the background backend, roll back and close every
connection. The test module imports only symbols that exist before the fix
(the parent lock is spelled as its SQL), so it runs unchanged against the
pre-change source for the red leg.

File name carries ``integration`` so ``.github/workflows/ci.yml``'s ``database``
filter opens the real-DB lane (which runs the whole tree under ``-m
integration``); no selector row is needed, same as
``tests/test_backfill_geometry_network_scope_integration.py``.
"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import psycopg2
import psycopg2.errors
import pytest
from psycopg2.extras import Json, RealDictCursor

from tests.integration_helpers import apply_migrations_from_zero, psycopg_connection
from workers.model_registry.basins_registry_import import _backfill_output_segment_geometry
from workers.model_registry.qhh_production_bootstrap import seed_qhh_output_segments

pytestmark = pytest.mark.integration

_PREFIX = "it2157"
MODEL_ID = f"{_PREFIX}_model"
BASIN_ID = f"{_PREFIX}_basin"
BASIN_VERSION_ID = f"{_PREFIX}_basin_v1"
NETWORK_ID = f"{_PREFIX}_rnv"
MESH_ID = f"{_PREFIX}_mesh"
OUTPUT_COUNT = 2
OUTPUT_IDS = tuple(f"{MODEL_ID}_shud_riv_{index:06d}" for index in range(1, OUTPUT_COUNT + 1))
REACH_TYPES = {1: 3, 2: 5}

# Far above the server's deadlock_timeout (1 s default), so a real deadlock is
# reported as `deadlock detected`, never masked as a lock timeout.
_PGOPTIONS = "-c lock_timeout=20000 -c statement_timeout=60000"
_POLL_SECONDS = 15.0
_JOIN_SECONDS = 45.0

_PARENT_LOCK_SQL = """
    SELECT 1 FROM core.river_network_version
    WHERE river_network_version_id = %s
    FOR NO KEY UPDATE
"""


def _seed_network(database_url: str) -> None:
    """One network: a model referencing it, two source reaches, two fillable output rows.

    Output rows carry NULL geom and no ``Type`` -- the committed "fillable" state
    both interleavings need -- keyed exactly as ``_seed_output_segment_rows``
    keys them, so the seed's upsert conflicts on (and row-locks) these rows.
    """
    apply_migrations_from_zero(database_url)
    with psycopg_connection(database_url) as connection, connection.cursor() as cursor:
        cursor.execute(
            "INSERT INTO core.basin (basin_id, basin_name, basin_group, description) VALUES (%s, %s, %s, %s)",
            (BASIN_ID, "Issue 2157 Basin", "integration", "Lock-order basin."),
        )
        cursor.execute(
            """
            INSERT INTO core.basin_version (
                basin_version_id, basin_id, version_label, geom, active_flag, source_uri, checksum
            )
            VALUES (%s, %s, 'v1', ST_Multi(ST_MakeEnvelope(109.0, 29.0, 112.0, 32.0, 4490)),
                    true, 'integration://basin', 'basin-sha')
            """,
            (BASIN_VERSION_ID, BASIN_ID),
        )
        cursor.execute(
            """
            INSERT INTO core.river_network_version (
                river_network_version_id, basin_version_id, version_label, segment_count, source_uri, checksum
            )
            VALUES (%s, %s, 'v1', %s, 'integration://river-network', 'rnv-sha')
            """,
            (NETWORK_ID, BASIN_VERSION_ID, OUTPUT_COUNT),
        )
        cursor.execute(
            """
            INSERT INTO core.mesh_version (mesh_version_id, basin_version_id, version_label, mesh_uri)
            VALUES (%s, %s, 'v1', 'integration://mesh')
            """,
            (MESH_ID, BASIN_VERSION_ID),
        )
        cursor.execute(
            """
            INSERT INTO core.model_instance (
                model_id, basin_version_id, river_network_version_id, mesh_version_id,
                calibration_version_id, shud_code_version, model_package_uri
            )
            VALUES (%s, %s, %s, %s, 'cal-v1', 'shud-test', 'integration://package/')
            """,
            (MODEL_ID, BASIN_VERSION_ID, NETWORK_ID, MESH_ID),
        )
        for index, stream_type in REACH_TYPES.items():
            cursor.execute(
                """
                INSERT INTO core.river_segment (
                    river_segment_id, river_network_version_id, segment_order, length_m, geom, properties_json
                )
                VALUES (%s, %s, %s, %s, ST_Multi(ST_GeomFromText(%s, 4490)), %s)
                """,
                (
                    f"{MODEL_ID}_reach_{index:06d}",
                    NETWORK_ID,
                    index,
                    1000.0 * index,
                    f"LINESTRING(110.{index} 30.0, 110.{index} 30.5)",
                    Json({"iRiv": str(index), "Type": stream_type}),
                ),
            )
        for index, output_id in enumerate(OUTPUT_IDS, start=1):
            cursor.execute(
                """
                INSERT INTO core.river_segment (
                    river_segment_id, river_network_version_id, segment_order, geom, properties_json
                )
                VALUES (%s, %s, %s, NULL, %s)
                """,
                (
                    output_id,
                    NETWORK_ID,
                    OUTPUT_COUNT + index,
                    Json({"shud_output_river": True, "shud_riv_index": index}),
                ),
            )


def _write_sp_riv(root: Path) -> Path:
    path = root / f"{_PREFIX}.sp.riv"
    rows = "\n".join(f"{index} 0 1 0.001 100 0" for index in range(1, OUTPUT_COUNT + 1))
    path.write_text(f"{OUTPUT_COUNT} 6\nIndex Down Type Slope Length BC\n{rows}\n", encoding="utf-8")
    return path


def _connect(database_url: str, *, autocommit: bool = False) -> Any:
    connection = psycopg2.connect(database_url, cursor_factory=RealDictCursor)
    connection.autocommit = autocommit
    return connection


def _pid(connection: Any) -> int:
    with connection.cursor() as cursor:
        cursor.execute("SELECT pg_backend_pid() AS pid")
        pid = int(cursor.fetchone()["pid"])
    if not connection.autocommit:
        connection.commit()
    return pid


def _generation(database_url: str) -> int:
    with psycopg_connection(database_url) as connection, connection.cursor() as cursor:
        cursor.execute(
            "SELECT geometry_generation FROM core.river_network_version WHERE river_network_version_id = %s",
            (NETWORK_ID,),
        )
        return int(cursor.fetchone()["geometry_generation"])


def _output_state(database_url: str) -> list[tuple[str, bool, Any]]:
    with psycopg_connection(database_url) as connection, connection.cursor() as cursor:
        cursor.execute(
            """
            SELECT river_segment_id, geom IS NOT NULL AS has_geom, stream_type
            FROM core.river_segment
            WHERE river_network_version_id = %s AND river_segment_id = ANY(%s)
            ORDER BY river_segment_id
            """,
            (NETWORK_ID, list(OUTPUT_IDS)),
        )
        return [(row["river_segment_id"], row["has_geom"], row["stream_type"]) for row in cursor.fetchall()]


class _Background(threading.Thread):
    """Run one session's work off the main thread; keep its result or its error."""

    def __init__(self, work: Callable[[], Any]) -> None:
        super().__init__(daemon=True)
        self._work = work
        self.result: Any = None
        self.error: BaseException | None = None

    def run(self) -> None:
        try:
            self.result = self._work()
        except BaseException as error:  # noqa: BLE001 - surfaced by the test body
            self.error = error


def _wait_for_lock_wait(observer: Any, background: _Background, pid_of: Callable[[], int | None]) -> int:
    """Poll until the background session's backend is waiting on a heavyweight lock."""
    deadline = time.monotonic() + _POLL_SECONDS
    while time.monotonic() < deadline:
        if not background.is_alive():
            raise AssertionError(
                f"the background session finished before it waited on a lock: "
                f"result={background.result!r} error={background.error!r}"
            )
        pid = pid_of()
        if pid is not None:
            with observer.cursor() as cursor:
                cursor.execute("SELECT wait_event_type FROM pg_stat_activity WHERE pid = %s", (pid,))
                row = cursor.fetchone()
            if row is not None and row["wait_event_type"] == "Lock":
                return pid
        time.sleep(0.05)
    raise AssertionError(f"the background session never waited on a lock within {_POLL_SECONDS}s")


def _is_lock_waiting(observer: Any, pid: int) -> bool:
    with observer.cursor() as cursor:
        cursor.execute("SELECT wait_event_type FROM pg_stat_activity WHERE pid = %s", (pid,))
        row = cursor.fetchone()
    return row is not None and row["wait_event_type"] == "Lock"


def _holds_segment_write_lock(observer: Any, pid: int) -> bool:
    """Has this backend written ``core.river_segment`` in its open transaction?

    Row locks are not listed in ``pg_locks`` once granted; the table-level
    ``RowExclusiveLock`` every INSERT/UPDATE takes (and holds to transaction
    end) is, so it is the observable proof that the upsert already ran.
    """
    with observer.cursor() as cursor:
        cursor.execute(
            """
            SELECT 1 FROM pg_locks
            WHERE pid = %s AND granted
              AND relation = 'core.river_segment'::regclass
              AND mode = 'RowExclusiveLock'
            """,
            (pid,),
        )
        return cursor.fetchone() is not None


@contextmanager
def _sessions(database_url: str, count: int) -> Iterator[list[Any]]:
    connections = [_connect(database_url) for _ in range(count)]
    try:
        yield connections
    finally:
        for connection in connections:
            try:
                connection.rollback()
            except psycopg2.Error:
                pass
            connection.close()


def _stop(observer: Any, background: _Background, pid: int | None) -> None:
    """Never leave a blocked backend or a live thread behind a failed test."""
    if background.is_alive() and pid is not None:
        with observer.cursor() as cursor:
            cursor.execute("SELECT pg_cancel_backend(%s)", (pid,))
    background.join(timeout=_JOIN_SECONDS)
    assert not background.is_alive(), "background session did not stop"


def test_import_transaction_and_a_concurrent_backfill_serialize_on_the_parent_row(
    throwaway_database_url: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """B-2: A holds the parent row, B backfills, A then writes B's target segment.

    Before #2157 B read its candidates, row-locked the output segments with its
    UPDATE and only then waited on the parent row for the generation bump, so
    A's segment UPDATE closed the cycle: `deadlock detected`. After it, B waits
    on the parent row BEFORE touching any segment, so A's segment UPDATE
    returns while B is still waiting (the discriminant: "B waits" alone is true
    of both builds), A commits, and B then fills and bumps.
    """
    url = throwaway_database_url
    _seed_network(url)
    monkeypatch.setenv("PGOPTIONS", _PGOPTIONS)
    generation_before = _generation(url)
    observer = _connect(url, autocommit=True)
    b_pid: int | None = None
    try:
        with _sessions(url, 2) as (session_a, session_b):
            b_pid = _pid(session_b)

            def backfill_in_b() -> int:
                with session_b.cursor() as cursor:
                    updated = _backfill_output_segment_geometry(cursor, NETWORK_ID, only_missing=True)
                session_b.commit()
                return updated

            background = _Background(backfill_in_b)
            try:
                with session_a.cursor() as cursor_a:
                    # (1) A: the import transaction's first parent write.
                    cursor_a.execute(
                        "UPDATE core.river_network_version SET segment_count = segment_count "
                        "WHERE river_network_version_id = %s",
                        (NETWORK_ID,),
                    )
                    # (2) B: the backfill, until it waits on a lock.
                    background.start()
                    _wait_for_lock_wait(observer, background, lambda: b_pid)
                    # (3) A: the segment row B is about to fill.
                    cursor_a.execute(
                        "UPDATE core.river_segment SET length_m = length_m "
                        "WHERE river_segment_id = %s AND river_network_version_id = %s",
                        (OUTPUT_IDS[0], NETWORK_ID),
                    )
                    assert background.error is None, f"session B failed while A wrote its segment: {background.error}"
                    assert background.is_alive() and _is_lock_waiting(observer, b_pid), (
                        "A's segment UPDATE returned but B is no longer waiting on the parent row"
                    )
                # (4) A commits; B proceeds.
                session_a.commit()
                background.join(timeout=_JOIN_SECONDS)
            finally:
                _stop(observer, background, b_pid)
            assert background.error is None, f"session B failed after A committed: {background.error}"
            assert background.result == OUTPUT_COUNT, background.result
    finally:
        observer.close()

    assert _generation(url) == generation_before + 1
    assert _output_state(url) == [
        (OUTPUT_IDS[0], True, float(REACH_TYPES[1])),
        (OUTPUT_IDS[1], True, float(REACH_TYPES[2])),
    ]


def test_autopipeline_backfill_and_the_seed_serialize_on_the_parent_row(
    throwaway_database_url: str,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """B-2b: A holds the parent row (the backfill's entry lock), B seeds, A backfills.

    If only the backfill locked the parent row, B's upsert would row-lock the
    existing output rows and THEN wait on the parent row inside its trailing
    backfill; A's backfill UPDATE of those rows closes the cycle. With the seed
    taking the parent lock before its upsert, B waits holding no segment write
    lock, A's backfill completes and commits, and B then seeds and commits.
    """
    url = throwaway_database_url
    _seed_network(url)
    sp_riv = _write_sp_riv(tmp_path)
    monkeypatch.setenv("PGOPTIONS", _PGOPTIONS)
    generation_before = _generation(url)
    observer = _connect(url, autocommit=True)
    b_pid: int | None = None
    b_holds_segment_write_lock: bool | None = None
    a_updated: int | None = None
    try:
        with _sessions(url, 1) as (session_a,):
            a_pid = _pid(session_a)
            known = {a_pid, _pid(observer)}

            def seed_pid() -> int | None:
                with observer.cursor() as cursor:
                    cursor.execute(
                        """
                        SELECT pid FROM pg_stat_activity
                        WHERE datname = current_database() AND backend_type = 'client backend'
                          AND pid <> ALL(%s)
                        """,
                        (list(known),),
                    )
                    rows = cursor.fetchall()
                return int(rows[0]["pid"]) if len(rows) == 1 else None

            background = _Background(
                lambda: seed_qhh_output_segments(
                    database_url=url,
                    model_id=MODEL_ID,
                    sp_riv_path=sp_riv,
                    containment_root=tmp_path,
                )
            )
            try:
                with session_a.cursor() as cursor_a:
                    # (2) A: the parent-row lock the backfill takes on entry.
                    cursor_a.execute(_PARENT_LOCK_SQL, (NETWORK_ID,))
                    # (3) B: the seed, until it waits on a lock.
                    background.start()
                    b_pid = _wait_for_lock_wait(observer, background, seed_pid)
                    b_holds_segment_write_lock = _holds_segment_write_lock(observer, b_pid)
                    observed = f"B was waiting holding a core.river_segment write lock: {b_holds_segment_write_lock}"
                    # (4) A: the backfill over the committed fillable rows.
                    try:
                        a_updated = _backfill_output_segment_geometry(cursor_a, NETWORK_ID, only_missing=True)
                    except psycopg2.Error as error:
                        raise AssertionError(f"session A's backfill failed ({observed}): {error}") from error
                    assert background.error is None, (
                        f"session B failed during A's backfill ({observed}): {background.error}"
                    )
                session_a.commit()
                background.join(timeout=_JOIN_SECONDS)
            finally:
                _stop(observer, background, b_pid)
            assert background.error is None, f"session B (seed) failed after A committed: {background.error}"
    finally:
        observer.close()

    assert b_holds_segment_write_lock is False, "the seed wrote core.river_segment before taking the parent row lock"
    assert a_updated == OUTPUT_COUNT
    assert background.result["status"] == "seeded", background.result
    assert background.result["geometry_backfilled_count"] == OUTPUT_COUNT, background.result
    # A's backfill bumped once; the seed's unconditional backfill bumped again.
    assert _generation(url) == generation_before + 2
    assert _output_state(url) == [
        (OUTPUT_IDS[0], True, float(REACH_TYPES[1])),
        (OUTPUT_IDS[1], True, float(REACH_TYPES[2])),
    ]


def test_geometry_complete_tick_holds_the_parent_row_and_bumps_nothing(throwaway_database_url: str) -> None:
    """B-3 (AC3): the no-op tick takes the parent lock, returns 0, bumps nothing.

    Row locks are invisible in ``pg_locks`` once granted, so "it took the lock"
    is proven from a second session: while the tick's transaction is open, a
    ``FOR NO KEY UPDATE NOWAIT`` on the same parent row must fail with
    ``LockNotAvailable``. Before #2157 the tick exited early holding nothing.
    """
    url = throwaway_database_url
    _seed_network(url)
    with psycopg_connection(url) as connection, connection.cursor() as cursor:
        assert _backfill_output_segment_geometry(cursor, NETWORK_ID, only_missing=True) == OUTPUT_COUNT
    generation_complete = _generation(url)
    state_complete = _output_state(url)

    with _sessions(url, 2) as (session_1, session_2):
        with session_1.cursor() as cursor_1:
            assert _backfill_output_segment_geometry(cursor_1, NETWORK_ID, only_missing=True) == 0
        with session_2.cursor() as cursor_2, pytest.raises(psycopg2.errors.LockNotAvailable):
            cursor_2.execute(_PARENT_LOCK_SQL.replace("FOR NO KEY UPDATE", "FOR NO KEY UPDATE NOWAIT"), (NETWORK_ID,))
        session_1.commit()

    assert _generation(url) == generation_complete
    assert _output_state(url) == state_complete


def test_seed_shaped_backfill_still_rewrites_and_bumps_a_complete_network(throwaway_database_url: str) -> None:
    """AC4: ``only_missing=False`` (the seed path) keeps rewriting and bumping unconditionally."""
    url = throwaway_database_url
    _seed_network(url)
    with psycopg_connection(url) as connection, connection.cursor() as cursor:
        assert _backfill_output_segment_geometry(cursor, NETWORK_ID, only_missing=True) == OUTPUT_COUNT
    generation_complete = _generation(url)

    with psycopg_connection(url) as connection, connection.cursor() as cursor:
        assert _backfill_output_segment_geometry(cursor, NETWORK_ID) == OUTPUT_COUNT

    assert _generation(url) == generation_complete + 1
