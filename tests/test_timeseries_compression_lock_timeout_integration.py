"""#3380 real-DB proof: the compress statement's ``lock_timeout`` releases the readers queued behind it.

Oracle: a node-27 throwaway database on TimescaleDB 2.10.2 (``timescaledb_210``), run
through ``scripts/node27_integration_pg.py run --``; anywhere else these tests skip or
run exactly as ``tests/test_timeseries_compression_fence_integration.py`` does, whose
throwaway hypertables and helpers they reuse.

Measured on 2026-11-25 on that instance: after rewriting the data, ``compress_chunk``
asks for AccessExclusiveLock on the uncompressed chunk. An open transaction holding
ACCESS SHARE on the chunk makes that request wait, and a reader arriving later queues
behind the waiting request. With ``lock_timeout`` set, the statement ends with 55P03,
the chunk stays uncompressed and the queued reader returns. This drives the real
``_default_compress_chunk`` through that interleaving; every wait is bounded.
"""

from __future__ import annotations

import time
from collections.abc import Callable, Iterator
from typing import Any

import pytest

from scripts import node27_timeseries_compression as compression
from tests.test_timeseries_compression_fence_integration import (
    _SCHEMA_SQL,
    _SESSION_TIMEOUT_MS,
    _Chunk,
    _chunks,
    _compression_row,
    _connect,
    _is_compressed,
    _locks,
    _poll,
    _Worker,
)

pytestmark = [pytest.mark.integration, pytest.mark.timescaledb_210]

_HYPERTABLE = "hydro.river_timeseries"
_LOCK_TIMEOUT_MS = 5_000
_JOIN_SECONDS = _SESSION_TIMEOUT_MS / 1000 + 30


@pytest.fixture()
def lock_db(throwaway_database_url: str) -> Iterator[str]:
    connection = _connect(throwaway_database_url)
    try:
        with connection.cursor() as cursor:
            cursor.execute(_SCHEMA_SQL)
        connection.commit()
    finally:
        connection.close()
    yield throwaway_database_url


def _compress(database_url: str, chunk: _Chunk) -> Callable[[], int]:
    """The real runner seam with a 5 s lock timeout (60 s budget, 5 s fence wait)."""
    return lambda: compression._default_compress_chunk(
        database_url,
        _compression_row(_HYPERTABLE, chunk),
        compress_timeout_ms=60_000,
        fence_wait_ms=5_000,
        lock_timeout_ms=_LOCK_TIMEOUT_MS,
    )


def _queued_on(rows: list[tuple[Any, ...]], chunk: _Chunk) -> set[tuple[int, str]]:
    """(pid, mode) of every ungranted relation lock on ``chunk`` in a ``_locks`` snapshot."""
    return {(row[0], row[6]) for row in rows if row[1] == "relation" and row[2] == chunk.qualified and not row[7]}


def _timed_count(connection: Any) -> tuple[int, float]:
    started = time.monotonic()
    with connection.cursor() as cursor:
        cursor.execute(f"SELECT count(*) FROM {_HYPERTABLE}")
        (count,) = cursor.fetchone()
    return count, time.monotonic() - started


def test_a_lock_timeout_ends_the_compress_wait_and_releases_the_queued_reader(
    lock_db: str, record_property: Callable[[str, Any], None]
) -> None:
    chunk = _chunks(lock_db, _HYPERTABLE)[0]
    blocker, reader, observer = _connect(lock_db), _connect(lock_db), _connect(lock_db)
    reader.autocommit = True
    compress_worker: _Worker | None = None
    reader_worker: _Worker | None = None
    try:
        # 1. The blocker reads the chunk and keeps its transaction (ACCESS SHARE) open.
        with blocker.cursor() as cursor:
            cursor.execute(f"SELECT count(*) FROM {chunk.qualified}")
            cursor.fetchone()

        # 2. The compress call reaches the AccessExclusiveLock request at the end of
        #    its transaction: the locks it takes at the start do not conflict with a reader.
        compress_worker = _Worker(_compress(lock_db, chunk))
        compress_worker.start()
        waiting = _poll(
            lambda: [pid for pid, mode in _queued_on(_locks(observer), chunk) if mode == "AccessExclusiveLock"]
        )
        at_wait = _locks(observer)
        record_property("lock_timeout_pg_locks_at_compress_wait", repr(at_wait))
        assert waiting, f"compress_chunk never waited for AccessExclusiveLock on {chunk.qualified}: {at_wait!r}"
        compress_pid = waiting[0]

        # 3. Only now a reader starts; one pg_locks snapshot shows it queued behind the
        #    compress request that is still waiting, i.e. before the timeout fired.
        with reader.cursor() as cursor:
            cursor.execute("SELECT pg_backend_pid()")
            (reader_pid,) = cursor.fetchone()
        reader_worker = _Worker(lambda: _timed_count(reader))
        reader_worker.start()

        def both_queued() -> list[tuple[Any, ...]]:
            rows = _locks(observer)
            queued = _queued_on(rows, chunk)
            if {(compress_pid, "AccessExclusiveLock"), (reader_pid, "AccessShareLock")} <= queued:
                return rows
            return []

        queued_snapshot = _poll(both_queued, seconds=_LOCK_TIMEOUT_MS / 1000)
        record_property("lock_timeout_pg_locks_reader_queued", repr(queued_snapshot))
        assert queued_snapshot, (
            "the reader was not seen queued behind the waiting compress request before the lock timeout; "
            f"pg_locks={_locks(observer)!r}"
        )

        # 4. The compress call gives up with RelationLockContended; the reader returns,
        #    well inside its own statement timeout.
        compress_worker.outcome()
        assert isinstance(compress_worker.error, compression.RelationLockContended), repr(compress_worker.error)
        reader_worker.outcome()
        assert reader_worker.error is None, repr(reader_worker.error)
        _count, reader_seconds = reader_worker.result
        record_property("lock_timeout_reader_seconds", reader_seconds)
        assert reader_seconds < _LOCK_TIMEOUT_MS / 1000 + 5 < _SESSION_TIMEOUT_MS / 1000

        # 5. Nothing was compressed.
        assert not _is_compressed(lock_db, chunk)

        # 6. Once the blocker ends, the same call compresses the chunk.
        blocker.rollback()
        assert isinstance(_compress(lock_db, chunk)(), int)
        assert _is_compressed(lock_db, chunk)
    finally:
        # End the blocker first so no worker can stay parked on a lock, then bounded joins.
        try:
            blocker.rollback()
        except Exception:  # noqa: BLE001 - cleanup must reach the joins and closes
            pass
        for worker in (compress_worker, reader_worker):
            if worker is not None:
                worker.join(timeout=_JOIN_SECONDS)
        for connection in (blocker, reader, observer):
            connection.close()
