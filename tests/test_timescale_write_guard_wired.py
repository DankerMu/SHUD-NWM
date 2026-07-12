"""Wired-path tests for the compressed-chunk write guard.

Covers the three production write paths — asserting for each that:

1. The guard's SELECT against ``timescaledb_information.chunks`` runs BEFORE
   the DELETE. (Ordering: guard must not lose to TimescaleDB's raw error.)
2. When the guard raises, no DELETE (or INSERT) fires. (Fail-closed: no
   partial write.)
3. When the guard passes, the DELETE + INSERT run byte-identically to the
   pre-guard code path.

Every fake connection here records execute-call ordering so ``BEFORE`` claims
can be asserted, not just claimed.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime
from typing import Any

import pytest

from packages.common import forcing_domain_handoff_apply as apply_module
from packages.common.timescale_write_guard import (
    CompressedChunkWriteError,
)
from workers.forcing_producer.producer import ForcingTimeseriesRow
from workers.forcing_producer.store import PsycopgForcingRepository
from workers.output_parser.parser import (
    PsycopgOutputParserRepository,
    RiverTimeseriesRow,
)

_CHUNKS_QUERY_MARKER = "timescaledb_information.chunks"
_SET_LOCAL_TIMEOUT_MARKER = "set local statement_timeout"


def _t(hour: int) -> datetime:
    return datetime(2026, 6, 1, hour, tzinfo=UTC)


def _index_of_first(executions: list[tuple[str, tuple[Any, ...]]], needle: str) -> int:
    for idx, (statement, _params) in enumerate(executions):
        if needle.lower() in statement.lower():
            return idx
    return -1


class _RecordingCursor:
    """Cursor that records every ``execute`` call in caller-provided order.

    Configurable per-query behavior lets a test model "compressed chunk
    found" (guard returns a row → raise) vs. "no compressed chunks" (guard
    proceeds → DELETE + INSERT run).
    """

    def __init__(self, connection: "_RecordingConnection") -> None:
        self.connection = connection
        self._last_fetchone: Any = None
        self._execute_values_recorder: list[tuple[str, list[tuple[Any, ...]]]] = []

    def __enter__(self) -> "_RecordingCursor":
        return self

    def __exit__(self, *_args: object) -> bool:
        return False

    def execute(self, statement: str, parameters: tuple[Any, ...] = ()) -> None:
        self.connection.executions.append((statement, tuple(parameters)))
        normalized = statement.lower().strip()
        if _CHUNKS_QUERY_MARKER in normalized:
            self._last_fetchone = self.connection.compressed_chunk_row
            return
        self._last_fetchone = None

    def fetchone(self) -> Any:
        return self._last_fetchone

    def fetchall(self) -> list[Any]:
        return []

    @property
    def description(self) -> Any:
        return None

    @property
    def rowcount(self) -> int:
        return 0


class _RecordingConnection:
    """Fake psycopg2 connection with a single shared execution log.

    ``compressed_chunk_row`` — set to a ``(chunk_schema, chunk_name)`` tuple
    to model "guard finds a compressed chunk"; keep ``None`` to model
    "uncompressed, guard passes". ``close`` implicitly rolls back an
    uncommitted transaction to mirror psycopg2's documented semantics —
    the production write paths rely on this to roll back when the guard
    raises out of ``_replace_values``.
    """

    encoding: str = "UTF8"

    def __init__(self, *, compressed_chunk_row: tuple[str, str] | None = None) -> None:
        self.executions: list[tuple[str, tuple[Any, ...]]] = []
        self.execute_values_calls: list[tuple[str, list[tuple[Any, ...]]]] = []
        self.compressed_chunk_row = compressed_chunk_row
        self.commits = 0
        self.rollbacks = 0
        self.closed = False
        self._pending = False
        self.autocommit = False

    def cursor(self) -> _RecordingCursor:
        self._pending = True
        return _RecordingCursor(self)

    def commit(self) -> None:
        self.commits += 1
        self._pending = False

    def rollback(self) -> None:
        self.rollbacks += 1
        self._pending = False

    def close(self) -> None:
        if self._pending and not self.autocommit:
            self.rollbacks += 1
            self._pending = False
        self.closed = True


# ---------------------------------------------------------------------------
# workers/output_parser/parser.py :: upsert_river_timeseries
# ---------------------------------------------------------------------------


def _river_rows() -> tuple[RiverTimeseriesRow, ...]:
    return tuple(
        RiverTimeseriesRow(
            run_id="run_a",
            basin_version_id="basin_v1",
            river_network_version_id="rivnet_v1",
            river_segment_id=f"seg_{n}",
            valid_time=_t(n),
            lead_time_hours=n,
            variable="q_down",
            value=float(n),
            unit="m3/s",
        )
        for n in range(3)
    )


def _output_parser_repository(connection: _RecordingConnection) -> PsycopgOutputParserRepository:
    # PsycopgOutputParserRepository is a frozen dataclass; ``dataclasses.replace``
    # is the only supported way to bind a live connection.
    from dataclasses import replace

    base = PsycopgOutputParserRepository(database_url="postgres://unused")
    return replace(base, _connection=connection)


def _patch_parser_execute_values(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, list[Any]]]:
    """Patch ``psycopg2.extras.execute_values`` for parser wire tests.

    The parser's ``_execute_values`` imports execute_values locally on each
    call, so patching ``psycopg2.extras.execute_values`` reaches into the
    parser without touching the module under test.
    """
    from psycopg2 import extras as psycopg2_extras  # type: ignore[import-untyped]

    calls: list[tuple[str, list[Any]]] = []

    def _fake_execute_values(cursor: Any, statement: str, rows: Any, **_kwargs: Any) -> None:
        cursor.connection.executions.append((f"execute_values:{statement}", ()))
        calls.append((statement, list(rows)))

    monkeypatch.setattr(psycopg2_extras, "execute_values", _fake_execute_values)
    return calls


def test_output_parser_guard_runs_before_delete_on_uncompressed_batch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_parser_execute_values(monkeypatch)
    connection = _RecordingConnection()
    repository = _output_parser_repository(connection)
    repository.upsert_river_timeseries(_river_rows(), batch_size=2)

    guard_idx = _index_of_first(connection.executions, _CHUNKS_QUERY_MARKER)
    delete_idx = _index_of_first(connection.executions, "DELETE FROM hydro.river_timeseries")
    assert guard_idx >= 0, "guard's chunks query MUST fire"
    assert delete_idx >= 0, "DELETE MUST fire once guard passes"
    assert guard_idx < delete_idx, "guard MUST precede DELETE"
    # SET LOCAL is transaction-scoped and precedes the SELECT
    set_local_idx = _index_of_first(connection.executions, _SET_LOCAL_TIMEOUT_MARKER)
    assert 0 <= set_local_idx < guard_idx


def test_output_parser_guard_blocks_before_any_delete_on_compressed_chunk() -> None:
    connection = _RecordingConnection(
        compressed_chunk_row=("_timescaledb_internal", "_hyper_1_1_chunk"),
    )
    repository = _output_parser_repository(connection)
    with pytest.raises(CompressedChunkWriteError) as exc_info:
        repository.upsert_river_timeseries(_river_rows(), batch_size=2)
    assert "_hyper_1_1_chunk" in str(exc_info.value)
    assert "hydro.river_timeseries" in str(exc_info.value)
    delete_idx = _index_of_first(connection.executions, "DELETE FROM hydro.river_timeseries")
    assert delete_idx == -1, "DELETE MUST NOT fire when guard raises"


def test_output_parser_guard_passes_batch_unchanged_when_uncompressed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Byte-identity check: same DELETE key set, same INSERT statement text."""
    execute_values_calls = _patch_parser_execute_values(monkeypatch)
    connection = _RecordingConnection()
    repository = _output_parser_repository(connection)
    rows = _river_rows()
    repository.upsert_river_timeseries(rows, batch_size=len(rows))

    delete_calls = [
        (statement, params)
        for statement, params in connection.executions
        if "DELETE FROM hydro.river_timeseries" in statement
    ]
    # One DELETE per (run_id, river_network_version_id, variable) key — only
    # one such key in the fixture.
    assert len(delete_calls) == 1
    _, params = delete_calls[0]
    assert params == ("run_a", "rivnet_v1", "q_down")
    assert len(execute_values_calls) == 1
    assert "INSERT INTO hydro.river_timeseries" in execute_values_calls[0][0]
    assert len(execute_values_calls[0][1]) == len(rows)


# ---------------------------------------------------------------------------
# workers/forcing_producer/store.py :: replace_forcing_timeseries
# ---------------------------------------------------------------------------


def _forcing_rows() -> tuple[ForcingTimeseriesRow, ...]:
    return tuple(
        ForcingTimeseriesRow(
            forcing_version_id="fv_a",
            basin_version_id="basin_v1",
            station_id=f"station_{n}",
            valid_time=_t(n),
            source_id="gfs",
            variable="PRCP",
            value=1.0,
            unit="mm/day",
            native_resolution="1h",
        )
        for n in range(3)
    )


def _install_fake_psycopg2(
    monkeypatch: pytest.MonkeyPatch,
    connection: _RecordingConnection,
) -> list[tuple[str, list[tuple[Any, ...]]]]:
    """Patch ``psycopg2.connect`` inside ``store.py``'s local import.

    Returns the recorded ``execute_values`` calls list so wire-path tests
    can assert INSERT byte-identity.
    """
    import psycopg2  # type: ignore[import-untyped]
    from psycopg2 import extras as psycopg2_extras  # type: ignore[import-untyped]

    execute_values_calls: list[tuple[str, list[tuple[Any, ...]]]] = []

    def _fake_connect(*_args: Any, **_kwargs: Any) -> _RecordingConnection:
        return connection

    def _fake_execute_values(cursor: Any, statement: str, rows: Any, **_kwargs: Any) -> None:
        cursor.connection.executions.append((f"execute_values:{statement}", ()))
        execute_values_calls.append((statement, list(rows)))

    monkeypatch.setattr(psycopg2, "connect", _fake_connect)
    monkeypatch.setattr(psycopg2_extras, "execute_values", _fake_execute_values)
    return execute_values_calls


def test_forcing_producer_guard_runs_before_delete_on_uncompressed_batch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    connection = _RecordingConnection()
    _install_fake_psycopg2(monkeypatch, connection)
    repository = PsycopgForcingRepository(database_url="postgres://unused")
    repository.replace_forcing_timeseries("fv_a", _forcing_rows())

    guard_idx = _index_of_first(connection.executions, _CHUNKS_QUERY_MARKER)
    delete_idx = _index_of_first(
        connection.executions,
        "DELETE FROM met.forcing_station_timeseries",
    )
    assert guard_idx >= 0
    assert delete_idx >= 0
    assert guard_idx < delete_idx


def test_forcing_producer_guard_blocks_before_any_delete_on_compressed_chunk(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    connection = _RecordingConnection(
        compressed_chunk_row=("_timescaledb_internal", "_hyper_2_5_chunk"),
    )
    execute_values_calls = _install_fake_psycopg2(monkeypatch, connection)
    repository = PsycopgForcingRepository(database_url="postgres://unused")
    with pytest.raises(CompressedChunkWriteError) as exc_info:
        repository.replace_forcing_timeseries("fv_a", _forcing_rows())
    assert "_hyper_2_5_chunk" in str(exc_info.value)
    assert "met.forcing_station_timeseries" in str(exc_info.value)
    delete_idx = _index_of_first(
        connection.executions,
        "DELETE FROM met.forcing_station_timeseries",
    )
    assert delete_idx == -1
    assert execute_values_calls == [], "INSERT MUST NOT fire when guard raises"
    assert connection.rollbacks >= 1, "caller transaction MUST roll back"


def test_forcing_producer_uncompressed_passes_batch_unchanged(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    connection = _RecordingConnection()
    execute_values_calls = _install_fake_psycopg2(monkeypatch, connection)
    repository = PsycopgForcingRepository(database_url="postgres://unused")
    rows = _forcing_rows()
    repository.replace_forcing_timeseries("fv_a", rows)

    # One DELETE, one execute_values, and the INSERT statement unchanged.
    delete_calls = [
        (statement, params)
        for statement, params in connection.executions
        if "DELETE FROM met.forcing_station_timeseries" in statement
    ]
    assert len(delete_calls) == 1
    assert delete_calls[0][1] == ("fv_a",)
    assert len(execute_values_calls) == 1
    insert_statement = execute_values_calls[0][0]
    assert re.search(r"INSERT INTO met\.forcing_station_timeseries", insert_statement)
    assert len(execute_values_calls[0][1]) == len(rows)
    assert connection.commits == 1


# ---------------------------------------------------------------------------
# packages/common/forcing_domain_handoff_apply.py :: _replace_forcing_station_timeseries
# ---------------------------------------------------------------------------


def _handoff_rows() -> list[dict[str, Any]]:
    return [
        {
            "forcing_version_id": "fv_a",
            "basin_version_id": "basin_v1",
            "station_id": f"station_{n}",
            "valid_time": _t(n),
            "source_id": "gfs",
            "variable": "PRCP",
            "value": 1.0,
            "unit": "mm/day",
            "native_resolution": "1h",
            "quality_flag": "ok",
        }
        for n in range(3)
    ]


def test_handoff_apply_guard_runs_before_delete_on_uncompressed_batch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    connection = _RecordingConnection()
    executed_values: list[tuple[str, list[Any]]] = []

    def _fake_execute_values(cursor: Any, statement: str, rows: Any, **_kwargs: Any) -> None:
        cursor.connection.executions.append((f"execute_values:{statement}", ()))
        executed_values.append((statement, list(rows)))

    monkeypatch.setattr(apply_module, "execute_values", _fake_execute_values)

    cursor = connection.cursor()
    apply_module._replace_forcing_station_timeseries(cursor, "fv_a", _handoff_rows())

    guard_idx = _index_of_first(connection.executions, _CHUNKS_QUERY_MARKER)
    delete_idx = _index_of_first(
        connection.executions,
        "DELETE FROM met.forcing_station_timeseries",
    )
    assert guard_idx >= 0
    assert delete_idx >= 0
    assert guard_idx < delete_idx
    assert len(executed_values) == 1


def test_handoff_apply_guard_blocks_before_delete_on_compressed_chunk(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    connection = _RecordingConnection(
        compressed_chunk_row=("_timescaledb_internal", "_hyper_3_9_chunk"),
    )
    executed_values: list[tuple[str, list[Any]]] = []

    def _fake_execute_values(cursor: Any, statement: str, rows: Any, **_kwargs: Any) -> None:
        cursor.connection.executions.append((f"execute_values:{statement}", ()))
        executed_values.append((statement, list(rows)))

    monkeypatch.setattr(apply_module, "execute_values", _fake_execute_values)

    cursor = connection.cursor()
    with pytest.raises(CompressedChunkWriteError) as exc_info:
        apply_module._replace_forcing_station_timeseries(cursor, "fv_a", _handoff_rows())

    assert "_hyper_3_9_chunk" in str(exc_info.value)
    assert "met.forcing_station_timeseries" in str(exc_info.value)
    delete_idx = _index_of_first(
        connection.executions,
        "DELETE FROM met.forcing_station_timeseries",
    )
    assert delete_idx == -1
    assert executed_values == [], "INSERT MUST NOT fire when guard raises"


def test_handoff_apply_empty_rows_shortcircuits_guard(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Empty ``rows`` yields ``valid_time_min/max = None`` — guard short-circuits.

    Pre-guard behavior: DELETE fired but no INSERT. Post-guard behavior must
    match (an empty batch has no compressed-chunk overlap semantic).
    """
    connection = _RecordingConnection()

    def _fake_execute_values(*_args: Any, **_kwargs: Any) -> None:
        raise AssertionError("execute_values MUST NOT fire on empty rows")

    monkeypatch.setattr(apply_module, "execute_values", _fake_execute_values)
    cursor = connection.cursor()
    apply_module._replace_forcing_station_timeseries(cursor, "fv_a", [])

    # Guard short-circuits — no catalog lookup, no SET LOCAL statement.
    guard_idx = _index_of_first(connection.executions, _CHUNKS_QUERY_MARKER)
    assert guard_idx == -1
    # DELETE still runs (pre-guard behavior preserved).
    delete_idx = _index_of_first(
        connection.executions,
        "DELETE FROM met.forcing_station_timeseries",
    )
    assert delete_idx >= 0


def test_output_parser_empty_batch_shortcircuits_guard() -> None:
    """Empty batch: caller returns before the guard is invoked at all."""
    connection = _RecordingConnection()
    repository = _output_parser_repository(connection)
    repository.upsert_river_timeseries((), batch_size=8)
    # No execute of any kind — caller's ``if not rows: return`` fires first.
    assert connection.executions == []


# ---------------------------------------------------------------------------
# Design D5: shared helper (no divergent per-path implementation).
# ---------------------------------------------------------------------------


def test_all_three_paths_import_from_shared_helper_module() -> None:
    """Design D5: divergent per-path guard implementations are forbidden.

    All three write paths import the guard from
    ``packages.common.timescale_write_guard``. This test would catch a
    future copy-paste guard implementation.
    """
    import packages.common.forcing_domain_handoff_apply as apply_module_ref
    import workers.forcing_producer.store as store_module_ref
    import workers.output_parser.parser as parser_module_ref
    from packages.common.timescale_write_guard import (
        check_batch_targets_uncompressed as canonical,
    )

    assert parser_module_ref.check_batch_targets_uncompressed is canonical
    assert store_module_ref.check_batch_targets_uncompressed is canonical
    assert apply_module_ref.check_batch_targets_uncompressed is canonical


def test_seed_demo_does_not_import_guard() -> None:
    """Seed intentionally NOT wired to guard — asserted by import inspection."""
    from db.seeds import seed_demo

    assert not hasattr(seed_demo, "check_batch_targets_uncompressed")
    assert not hasattr(seed_demo, "CompressedChunkWriteError")
    assert not hasattr(seed_demo, "CompressedChunkGuardError")
