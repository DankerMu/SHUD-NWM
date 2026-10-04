"""#2713 real-DB proof: the advisory fence turns the chunk-DDL / ingest deadlock into a wait.

Oracle: a node-27 throwaway database on TimescaleDB 2.10.2 (``timescaledb_210``);
CI's ``not timescaledb_210`` lane never runs these. Each test builds its own
throwaway compressed hypertables under the production names
(``hydro.river_timeseries``, ``met.forcing_station_timeseries``) so the real
fence keys and the real runner seams apply, with the production-like shape that
matters here: the FK columns are the compression ``segmentby`` columns and
reference plain authority tables (``hydro.hydro_run``, ``met.met_station``).

* river red   -- no fence on either side: the 2026-10-04 interleaving
                 (probe, compress reaches its AccessExclusive upgrade, DELETE on
                 a different chunk window) must end in exactly one ``40P01``;
* river green -- both sides fenced through the real seams: no ``40P01``,
                 compression waits before copying anything, the lock queue
                 refuses new writers, and the writer's rows match its intent;
* forcing     -- an FK-referenced write taken BEFORE the fence is still a hazard
                 with a blocking fence; fence-first is not;
* retention   -- the real ``_default_drop_chunk`` against a parser-shaped
                 transaction: no ``40P01``.

Where TimescaleDB 2.10.2 does not reach an interleaving, the test skips with the
observed ``pg_locks`` in the reason (``pytest -rs``) instead of faking a red, as
design D5 requires.
"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import psycopg2
import pytest

from packages.common.timeseries_compression_fence import (
    FenceContended,
    acquire_compression_fence,
    fence_key,
    release_compression_fence,
    try_ingest_fence,
)
from scripts import node27_timeseries_compression as compression
from scripts import node27_timeseries_retention as retention

pytestmark = [pytest.mark.integration, pytest.mark.timescaledb_210]

_SESSION_TIMEOUT_MS = 30_000
_POLL_SECONDS = 20.0
_DEADLOCK = "40P01"
_DAY0 = datetime(2026, 9, 20, tzinfo=UTC)
_HISTORY_DAYS = 6  # history chunks: _DAY0 .. _DAY0 + 5 d, one per day
_NEW_DAY = _DAY0 + timedelta(days=12)  # the writer's window, a chunk of its own

_SCHEMA_SQL = f"""
CREATE EXTENSION IF NOT EXISTS timescaledb;
CREATE SCHEMA hydro;
CREATE SCHEMA met;
CREATE TABLE hydro.hydro_run (run_key integer PRIMARY KEY, status text NOT NULL DEFAULT 'succeeded');
CREATE TABLE hydro.river_timeseries (
    valid_time timestamptz NOT NULL,
    run_key integer NOT NULL REFERENCES hydro.hydro_run(run_key),
    river_segment_key integer NOT NULL,
    variable_e text NOT NULL,
    value double precision NOT NULL,
    PRIMARY KEY (run_key, river_segment_key, variable_e, valid_time)
);
SELECT create_hypertable('hydro.river_timeseries', 'valid_time', chunk_time_interval => INTERVAL '1 day');
ALTER TABLE hydro.river_timeseries SET (
    timescaledb.compress,
    timescaledb.compress_segmentby = 'run_key, river_segment_key',
    timescaledb.compress_orderby = 'variable_e, valid_time'
);
CREATE TABLE met.met_station (station_key integer PRIMARY KEY, station_id text NOT NULL UNIQUE);
CREATE TABLE met.forcing_station_timeseries (
    valid_time timestamptz NOT NULL,
    station_key integer NOT NULL REFERENCES met.met_station(station_key),
    variable_e text NOT NULL,
    value double precision NOT NULL,
    PRIMARY KEY (station_key, variable_e, valid_time)
);
SELECT create_hypertable('met.forcing_station_timeseries', 'valid_time', chunk_time_interval => INTERVAL '1 day');
ALTER TABLE met.forcing_station_timeseries SET (
    timescaledb.compress,
    timescaledb.compress_segmentby = 'station_key',
    timescaledb.compress_orderby = 'variable_e, valid_time'
);
INSERT INTO hydro.hydro_run (run_key) VALUES (1), (2);
INSERT INTO met.met_station (station_key, station_id) VALUES (1, 'st_1'), (2, 'st_2');
INSERT INTO hydro.river_timeseries
SELECT ts, 1, seg, 'q_down', seg + extract(epoch FROM ts) / 1e9
FROM generate_series('{_DAY0.isoformat()}'::timestamptz,
                     '{(_DAY0 + timedelta(days=_HISTORY_DAYS)).isoformat()}'::timestamptz
                         - INTERVAL '1 hour', INTERVAL '1 hour') AS ts,
     generate_series(1, 20) AS seg;
INSERT INTO hydro.river_timeseries
SELECT ts, 2, seg, 'q_down', 0
FROM generate_series('{_NEW_DAY.isoformat()}'::timestamptz,
                     '{(_NEW_DAY + timedelta(hours=23)).isoformat()}'::timestamptz, INTERVAL '1 hour') AS ts,
     generate_series(1, 20) AS seg;
INSERT INTO met.forcing_station_timeseries
SELECT ts, st, 'PRCP', st
FROM generate_series('{_DAY0.isoformat()}'::timestamptz,
                     '{(_DAY0 + timedelta(days=_HISTORY_DAYS)).isoformat()}'::timestamptz
                         - INTERVAL '1 hour', INTERVAL '1 hour') AS ts,
     generate_series(1, 2) AS st;
"""

# The parser's replace chain, in its production order (workers/output_parser/parser.py
# upsert_river_timeseries): run-row lock, unbounded probe, bounded DELETE, INSERT.
_RUN_LOCK_SQL = "SELECT 1 FROM hydro.hydro_run WHERE run_key = 2 FOR UPDATE"
_RIVER_PROBE_SQL = (
    "SELECT 1 FROM hydro.river_timeseries WHERE run_key = 2 AND river_segment_key = 1 "
    "AND variable_e = 'q_down' LIMIT 1"
)
# Literal timestamps, the exact shape of the 2026-10-04 deadlocked statement.
_RIVER_DELETE_SQL = (
    "DELETE FROM hydro.river_timeseries WHERE run_key = 2 AND river_segment_key = 1 "
    f"AND variable_e = 'q_down' AND valid_time >= '{_NEW_DAY.isoformat()}'::timestamptz "
    f"AND valid_time <= '{(_NEW_DAY + timedelta(hours=23)).isoformat()}'::timestamptz"
)
_RIVER_INSERT_SQL = (
    "INSERT INTO hydro.river_timeseries (valid_time, run_key, river_segment_key, variable_e, value) "
    "SELECT ts, 2, 1, 'q_down', extract(hour FROM ts AT TIME ZONE 'UTC') + 0.5 "
    f"FROM generate_series('{_NEW_DAY.isoformat()}'::timestamptz, "
    f"'{(_NEW_DAY + timedelta(hours=23)).isoformat()}'::timestamptz, INTERVAL '1 hour') AS ts"
)
# The writer's intended rows for (run 2, segment 1): hour h carries h + 0.5. Written
# out here, not read back from the statement, as the no-compression baseline.
_RIVER_INTENT = [(_NEW_DAY + timedelta(hours=hour), hour + 0.5) for hour in range(24)]


def _connect(database_url: str) -> Any:
    connection = psycopg2.connect(database_url, options=f"-c statement_timeout={_SESSION_TIMEOUT_MS}")
    connection.autocommit = False
    return connection


@pytest.fixture()
def fence_db(throwaway_database_url: str) -> Iterator[str]:
    connection = _connect(throwaway_database_url)
    try:
        with connection.cursor() as cursor:
            cursor.execute(_SCHEMA_SQL)
        connection.commit()
    finally:
        connection.close()
    yield throwaway_database_url


@dataclass(frozen=True)
class _Chunk:
    schema: str
    name: str
    range_start: datetime
    range_end: datetime

    @property
    def qualified(self) -> str:
        return f"{self.schema}.{self.name}"


def _chunks(database_url: str, hypertable: str) -> list[_Chunk]:
    schema, name = hypertable.split(".")
    connection = _connect(database_url)
    try:
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT chunk_schema, chunk_name, range_start, range_end FROM timescaledb_information.chunks "
                "WHERE hypertable_schema = %s AND hypertable_name = %s ORDER BY range_start",
                (schema, name),
            )
            return [_Chunk(*row) for row in cursor.fetchall()]
    finally:
        connection.rollback()
        connection.close()


def _is_compressed(database_url: str, chunk: _Chunk) -> bool:
    connection = _connect(database_url)
    try:
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT is_compressed FROM timescaledb_information.chunks WHERE chunk_schema = %s AND chunk_name = %s",
                (chunk.schema, chunk.name),
            )
            row = cursor.fetchone()
            return bool(row and row[0])
    finally:
        connection.rollback()
        connection.close()


def _locks(observer: Any, *, pid: int | None = None) -> list[tuple[Any, ...]]:
    with observer.cursor() as cursor:
        cursor.execute(
            "SELECT pid, locktype, relation::regclass::text, classid, objid, objsubid, mode, granted "
            "FROM pg_locks WHERE (%s::int IS NULL OR pid = %s::int) AND pid <> pg_backend_pid() "
            "ORDER BY pid, locktype, mode",
            (pid, pid),
        )
        rows = cursor.fetchall()
    observer.rollback()
    return rows


def _poll(predicate: Callable[[], Any], *, seconds: float = _POLL_SECONDS) -> Any:
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        value = predicate()
        if value:
            return value
        time.sleep(0.05)
    return None


def _fence_waiters(observer: Any, hypertable: str) -> list[int]:
    """Pids waiting for the exclusive fence of ``hypertable``."""
    classid, objid = fence_key(hypertable)
    with observer.cursor() as cursor:
        cursor.execute(
            "SELECT pid FROM pg_locks WHERE locktype = 'advisory' AND classid = %s::oid "
            "AND objid = %s::bigint::oid AND objsubid = 2 AND mode = 'ExclusiveLock' AND NOT granted",
            (classid, objid & 0xFFFFFFFF),
        )
        pids = [row[0] for row in cursor.fetchall()]
    observer.rollback()
    return pids


class _Worker(threading.Thread):
    """Runs one chunk-DDL call on its own connection and keeps its outcome."""

    def __init__(self, target: Callable[[], Any]) -> None:
        super().__init__(daemon=True)
        self._target_call = target
        self.result: Any = None
        self.error: BaseException | None = None

    def run(self) -> None:
        try:
            self.result = self._target_call()
        except BaseException as error:  # noqa: BLE001 - reported to the test thread
            self.error = error

    def outcome(self) -> Any:
        self.join(timeout=_SESSION_TIMEOUT_MS / 1000 + 30)
        assert not self.is_alive(), "chunk-DDL worker did not finish within its statement bound"
        return self.result


def _pgcode(error: BaseException | None) -> str | None:
    return getattr(error, "pgcode", None)


def _compression_row(hypertable: str, chunk: _Chunk) -> compression.ChunkRow:
    schema, name = hypertable.split(".")
    return compression.ChunkRow(schema, name, chunk.schema, chunk.name, chunk.range_start, chunk.range_end, False)


def _fenced_compress(database_url: str, hypertable: str, chunk: _Chunk, *, fence_wait_ms: int) -> Callable[[], int]:
    """The real runner seam, with the production keyword-only budget binding."""
    return lambda: compression._default_compress_chunk(
        database_url,
        _compression_row(hypertable, chunk),
        compress_timeout_ms=60_000,
        fence_wait_ms=fence_wait_ms,
    )


def _raw_compress(database_url: str, chunk: _Chunk, pid_box: list[int]) -> Callable[[], None]:
    def call() -> None:
        connection = _connect(database_url)
        try:
            with connection.cursor() as cursor:
                cursor.execute("SELECT pg_backend_pid()")
                pid_box.append(cursor.fetchone()[0])
                cursor.execute("SELECT compress_chunk(%s::regclass)", (chunk.qualified,))
            connection.commit()
        finally:
            connection.close()

    return call


# ---------------------------------------------------------------------------
# River shape
# ---------------------------------------------------------------------------


def test_river_red_unfenced_probe_compress_delete_deadlocks(
    fence_db: str, record_property: Callable[[str, Any], None]
) -> None:
    history = _chunks(fence_db, "hydro.river_timeseries")[0]
    writer, observer = _connect(fence_db), _connect(fence_db)
    pid_box: list[int] = []
    try:
        with writer.cursor() as cursor:
            cursor.execute(_RIVER_PROBE_SQL)  # AccessShare on every chunk, held to commit
            cursor.fetchall()
        worker = _Worker(_raw_compress(fence_db, history, pid_box))
        worker.start()
        assert _poll(lambda: pid_box), "compress session never started"
        upgrade_waiting = _poll(
            lambda: any(
                row[2] == history.qualified and row[6] == "AccessExclusiveLock" and not row[7]
                for row in _locks(observer, pid=pid_box[0])
            )
        )
        snapshot = _locks(observer)
        if not upgrade_waiting:
            worker.outcome()
            pytest.skip(
                "river red unreachable on this TimescaleDB: compress_chunk never showed a waiting "
                f"AccessExclusiveLock on {history.qualified} behind the probe; pg_locks={snapshot!r}"
            )
        writer_error: BaseException | None = None
        try:
            with writer.cursor() as cursor:
                cursor.execute(_RIVER_DELETE_SQL)  # a DIFFERENT chunk window
        except psycopg2.Error as error:
            writer_error = error
        writer.rollback()
        worker.outcome()
    finally:
        writer.close()
        observer.close()

    codes = {"writer": _pgcode(writer_error), "compress": _pgcode(worker.error)}
    victims = [side for side, code in codes.items() if code == _DEADLOCK]
    record_property("river_red_pg_locks_at_upgrade_wait", repr(snapshot))
    record_property("river_red_outcome", repr(codes))
    if not victims:
        pytest.skip(
            "river red: the interleaving was reached (compress waited for AccessExclusiveLock) but "
            f"no 40P01 followed on this TimescaleDB; outcome={codes!r}; pg_locks={snapshot!r}"
        )
    assert len(victims) == 1, codes
    record_property("deadlock_victim", victims[0])


def test_river_green_fenced_compression_waits_before_copying_and_refuses_new_writers(
    fence_db: str, record_property: Callable[[str, Any], None]
) -> None:
    history = _chunks(fence_db, "hydro.river_timeseries")[0]
    writer, latecomer, observer = _connect(fence_db), _connect(fence_db), _connect(fence_db)
    try:
        with writer.cursor() as cursor:
            assert try_ingest_fence(cursor, "hydro.river_timeseries") is True  # first statement
            cursor.execute(_RUN_LOCK_SQL)
            cursor.execute(_RIVER_PROBE_SQL)
            cursor.fetchall()
        worker = _Worker(
            _fenced_compress(fence_db, "hydro.river_timeseries", history, fence_wait_ms=_SESSION_TIMEOUT_MS - 5_000)
        )
        worker.start()
        waiters = _poll(lambda: _fence_waiters(observer, "hydro.river_timeseries"))
        assert waiters, f"compression never queued on the fence; pg_locks={_locks(observer)!r}"
        compress_locks = _locks(observer, pid=waiters[0])
        record_property("river_green_compress_locks_while_queued", repr(compress_locks))
        # It waits holding NO relation lock: nothing on any chunk, nothing on the FK table.
        assert [row for row in compress_locks if row[1] == "relation"] == [], compress_locks
        # Lock-queue rule: a new shared try conflicts with the QUEUED exclusive request.
        with latecomer.cursor() as cursor:
            assert try_ingest_fence(cursor, "hydro.river_timeseries") is False
        latecomer.rollback()
        # A backend already holding the shared fence is granted it again.
        with writer.cursor() as cursor:
            assert try_ingest_fence(cursor, "hydro.river_timeseries") is True
            cursor.execute(_RIVER_DELETE_SQL)
            cursor.execute(_RIVER_INSERT_SQL)
        writer.commit()
        elapsed_ms = worker.outcome()
    finally:
        writer.close()
        latecomer.close()
        observer.close()

    assert worker.error is None, repr(worker.error)
    assert elapsed_ms > 0
    assert _is_compressed(fence_db, history)
    reader = _connect(fence_db)
    try:
        with reader.cursor() as cursor:
            cursor.execute(
                "SELECT valid_time, value FROM hydro.river_timeseries "
                "WHERE run_key = 2 AND river_segment_key = 1 ORDER BY valid_time"
            )
            assert cursor.fetchall() == _RIVER_INTENT
    finally:
        reader.close()


def test_a_held_fence_defers_a_new_writer_until_released(fence_db: str) -> None:
    holder, writer = _connect(fence_db), _connect(fence_db)
    holder.autocommit = True
    try:
        with holder.cursor() as cursor:
            acquire_compression_fence(cursor, "hydro.river_timeseries", 5_000)
        with writer.cursor() as cursor:
            assert try_ingest_fence(cursor, "hydro.river_timeseries") is False
            # Another hypertable's fence is independent.
            assert try_ingest_fence(cursor, "met.forcing_station_timeseries") is True
        writer.rollback()
        with holder.cursor() as cursor:
            release_compression_fence(cursor, "hydro.river_timeseries")
        with writer.cursor() as cursor:
            assert try_ingest_fence(cursor, "hydro.river_timeseries") is True
        writer.rollback()
    finally:
        holder.close()
        writer.close()


def test_a_contended_fence_defers_the_chunk_without_copying(fence_db: str) -> None:
    history = _chunks(fence_db, "hydro.river_timeseries")[0]
    writer = _connect(fence_db)
    try:
        with writer.cursor() as cursor:
            assert try_ingest_fence(cursor, "hydro.river_timeseries") is True
        with pytest.raises(FenceContended):
            _fenced_compress(fence_db, "hydro.river_timeseries", history, fence_wait_ms=500)()
    finally:
        writer.rollback()
        writer.close()
    assert not _is_compressed(fence_db, history)


# ---------------------------------------------------------------------------
# Forcing shape (FK-referenced table written in the same transaction)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("late_fence", ["blocking", "try"])
def test_forcing_fence_taken_after_an_fk_referenced_write(
    fence_db: str, late_fence: str, record_property: Callable[[str, Any], None]
) -> None:
    """D0.4 seed: does compress_chunk wait on a writer's lock on the FK-referenced table?

    If it does, a fence taken AFTER that write is a hazard: a blocking fence closes the
    cycle (exactly one 40P01); the production try-fence instead defers the writer and
    compression proceeds. If it does not, the test skips with the pg_locks evidence.
    """
    history = _chunks(fence_db, "met.forcing_station_timeseries")[0]
    writer, observer = _connect(fence_db), _connect(fence_db)
    try:
        with writer.cursor() as cursor:
            cursor.execute("INSERT INTO met.met_station (station_key, station_id) VALUES (3, 'st_3')")
        worker = _Worker(
            _fenced_compress(
                fence_db, "met.forcing_station_timeseries", history, fence_wait_ms=_SESSION_TIMEOUT_MS - 5_000
            )
        )
        worker.start()

        def compress_waits_on_station() -> list[tuple[Any, ...]]:
            return [row for row in _locks(observer) if row[2] == "met.met_station" and not row[7]]

        waiting = _poll(compress_waits_on_station, seconds=10.0)
        snapshot = _locks(observer)
        record_property("forcing_late_fence_pg_locks", repr(snapshot))
        if not waiting:
            writer.rollback()
            worker.outcome()
            pytest.skip(
                "D0.4: compress_chunk took no conflicting lock on FK-referenced met.met_station while a "
                f"writer held RowExclusive on it (compress outcome={worker.error!r}); pg_locks={snapshot!r}"
            )
        classid, objid = fence_key("met.forcing_station_timeseries")
        writer_error: BaseException | None = None
        try:
            with writer.cursor() as cursor:
                if late_fence == "blocking":
                    cursor.execute("SELECT pg_advisory_xact_lock_shared(%s, %s)", (classid, objid))
                else:
                    assert try_ingest_fence(cursor, "met.forcing_station_timeseries") is False
        except psycopg2.Error as error:
            writer_error = error
        writer.rollback()
        worker.outcome()
    finally:
        writer.close()
        observer.close()

    codes = {"writer": _pgcode(writer_error), "compress": _pgcode(worker.error)}
    record_property("forcing_late_fence_outcome", repr(codes))
    if late_fence == "blocking":
        assert sum(code == _DEADLOCK for code in codes.values()) == 1, codes
    else:
        assert codes == {"writer": None, "compress": None}
        assert _is_compressed(fence_db, history)


def test_forcing_fence_first_gives_no_cycle(fence_db: str) -> None:
    history = _chunks(fence_db, "met.forcing_station_timeseries")[0]
    writer, observer = _connect(fence_db), _connect(fence_db)
    try:
        with writer.cursor() as cursor:
            assert try_ingest_fence(cursor, "met.forcing_station_timeseries") is True
            cursor.execute("INSERT INTO met.met_station (station_key, station_id) VALUES (3, 'st_3')")
            cursor.execute("SELECT 1 FROM met.forcing_station_timeseries WHERE station_key = 3 LIMIT 1")
        worker = _Worker(
            _fenced_compress(
                fence_db, "met.forcing_station_timeseries", history, fence_wait_ms=_SESSION_TIMEOUT_MS - 5_000
            )
        )
        worker.start()
        assert _poll(lambda: _fence_waiters(observer, "met.forcing_station_timeseries"))
        with writer.cursor() as cursor:
            cursor.execute(
                "DELETE FROM met.forcing_station_timeseries WHERE station_key = 3 "
                f"AND valid_time >= '{_NEW_DAY.isoformat()}'::timestamptz "
                f"AND valid_time <= '{(_NEW_DAY + timedelta(hours=23)).isoformat()}'::timestamptz"
            )
            cursor.execute(
                "INSERT INTO met.forcing_station_timeseries (valid_time, station_key, variable_e, value) "
                f"VALUES ('{_NEW_DAY.isoformat()}'::timestamptz, 3, 'PRCP', 1.0)"
            )
        writer.commit()
        worker.outcome()
    finally:
        writer.close()
        observer.close()
    assert worker.error is None, repr(worker.error)
    assert _is_compressed(fence_db, history)


# ---------------------------------------------------------------------------
# Retention drop against a parser-shaped transaction (design D2b)
# ---------------------------------------------------------------------------


def test_retention_drop_waits_for_a_fenced_parser_transaction_without_deadlock(
    fence_db: str, tmp_path: Path, record_property: Callable[[str, Any], None]
) -> None:
    oldest = _chunks(fence_db, "hydro.river_timeseries")[0]
    config = retention.RetentionConfig(
        database_url=fence_db,
        window_days=21,
        per_tick_bound=1,
        receipt_path=tmp_path / "receipt.json",
        lock_path=tmp_path / "runner.lock",
        enforce=True,
        lock_timeout_ms=_SESSION_TIMEOUT_MS - 5_000,
    )
    chunk = retention.ChunkRow(
        "hydro", "river_timeseries", oldest.schema, oldest.name, oldest.range_start, oldest.range_end, False
    )
    writer, observer = _connect(fence_db), _connect(fence_db)
    try:
        with writer.cursor() as cursor:
            assert try_ingest_fence(cursor, "hydro.river_timeseries") is True  # first statement
            cursor.execute(_RUN_LOCK_SQL)  # RowShare on the FK-referenced hydro.hydro_run
        worker = _Worker(lambda: retention._default_drop_chunk(config, chunk))
        worker.start()
        waiters = _poll(lambda: _fence_waiters(observer, "hydro.river_timeseries"))
        assert waiters, f"the drop never queued on the fence; pg_locks={_locks(observer)!r}"
        drop_locks = _locks(observer, pid=waiters[0])
        record_property("retention_drop_locks_while_queued", repr(drop_locks))
        assert [row for row in drop_locks if row[1] == "relation"] == [], drop_locks
        with writer.cursor() as cursor:
            cursor.execute(_RIVER_DELETE_SQL)
            cursor.execute(_RIVER_INSERT_SQL)
        writer.commit()
        worker.outcome()
    finally:
        writer.close()
        observer.close()

    assert worker.error is None, repr(worker.error)
    assert oldest.qualified not in {c.qualified for c in _chunks(fence_db, "hydro.river_timeseries")}
