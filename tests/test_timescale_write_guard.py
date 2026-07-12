"""Unit tests for ``packages.common.timescale_write_guard``.

Covers the shared helper's requirement scenarios per the fixture in
``openspec/changes/tier-node27-timeseries-storage/design.md``:

* Empty batch short-circuit.
* Uncompressed batch allowed.
* Compressed chunk overlap raises the named error.
* Catalog lookup exceptions (``OperationalError``, ``QueryCanceled``, generic)
  fail-closed with ``CompressedChunkGuardError``.
* ``range_end`` exclusive boundary.
* ``SET LOCAL statement_timeout`` used, ``SET SESSION`` never used.
* DSN masking in error messages.
* ``HYPERTABLES_GUARDED`` matches the two production hypertables only.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest

from packages.common.timescale_write_guard import (
    HYPERTABLES_GUARDED,
    RUNBOOK_ANCHOR,
    CompressedChunkGuardError,
    CompressedChunkWriteError,
    check_batch_targets_uncompressed,
)


class _FakeCursor:
    """Records ``execute()`` calls and returns a caller-configured row.

    ``row`` is what the guard's ``fetchone()`` returns (a tuple, dict, or
    ``None``). ``raise_on_query`` optionally makes the second ``execute``
    raise, exercising the catalog-error path.
    """

    def __init__(
        self,
        *,
        row: Any = None,
        raise_on_query: BaseException | None = None,
        raise_on_timeout: BaseException | None = None,
    ) -> None:
        self.executed: list[tuple[str, tuple[Any, ...]]] = []
        self._row = row
        self._raise_on_query = raise_on_query
        self._raise_on_timeout = raise_on_timeout

    def execute(self, statement: str, parameters: tuple[Any, ...] = ()) -> None:
        self.executed.append((statement, parameters))
        if statement.startswith("SET LOCAL"):
            if self._raise_on_timeout is not None:
                raise self._raise_on_timeout
            return
        if self._raise_on_query is not None:
            raise self._raise_on_query

    def fetchone(self) -> Any:
        return self._row


class _FakeOperationalError(Exception):
    """Stand-in for ``psycopg2.OperationalError`` — the guard catches by base ``Exception``."""


class _FakeQueryCanceled(Exception):
    """Stand-in for ``psycopg2.errors.QueryCanceled``."""


def _t(hour: int) -> datetime:
    return datetime(2026, 6, 1, hour, tzinfo=UTC)


def test_empty_batch_short_circuits() -> None:
    cursor = _FakeCursor()
    check_batch_targets_uncompressed(
        cursor,
        hypertable_schema="hydro",
        hypertable_name="river_timeseries",
        valid_time_min=None,
        valid_time_max=None,
    )
    assert cursor.executed == []


def test_uncompressed_chunk_allows_write() -> None:
    cursor = _FakeCursor(row=None)
    check_batch_targets_uncompressed(
        cursor,
        hypertable_schema="hydro",
        hypertable_name="river_timeseries",
        valid_time_min=_t(0),
        valid_time_max=_t(23),
    )
    assert any(stmt.startswith("SET LOCAL statement_timeout") for stmt, _ in cursor.executed)
    assert any("timescaledb_information.chunks" in stmt for stmt, _ in cursor.executed)


def test_compressed_chunk_raises_named_error() -> None:
    cursor = _FakeCursor(row=("_timescaledb_internal", "_hyper_1_1_chunk"))
    with pytest.raises(CompressedChunkWriteError) as exc_info:
        check_batch_targets_uncompressed(
            cursor,
            hypertable_schema="hydro",
            hypertable_name="river_timeseries",
            valid_time_min=_t(0),
            valid_time_max=_t(23),
        )
    error = exc_info.value
    assert error.chunk_schema == "_timescaledb_internal"
    assert error.chunk_name == "_hyper_1_1_chunk"
    assert error.hypertable_schema == "hydro"
    assert error.hypertable_name == "river_timeseries"
    message = str(error)
    assert "_hyper_1_1_chunk" in message
    assert RUNBOOK_ANCHOR in message
    assert "#43-decompress-procedure" in message


def test_compressed_chunk_raises_from_dict_cursor_row() -> None:
    cursor = _FakeCursor(
        row={"chunk_schema": "_timescaledb_internal", "chunk_name": "_hyper_2_5_chunk"}
    )
    with pytest.raises(CompressedChunkWriteError) as exc_info:
        check_batch_targets_uncompressed(
            cursor,
            hypertable_schema="met",
            hypertable_name="forcing_station_timeseries",
            valid_time_min=_t(0),
            valid_time_max=_t(1),
        )
    assert exc_info.value.chunk_name == "_hyper_2_5_chunk"


def test_catalog_lookup_error_raises_guard_error() -> None:
    cursor = _FakeCursor(raise_on_query=_FakeOperationalError("connection reset"))
    with pytest.raises(CompressedChunkGuardError) as exc_info:
        check_batch_targets_uncompressed(
            cursor,
            hypertable_schema="hydro",
            hypertable_name="river_timeseries",
            valid_time_min=_t(0),
            valid_time_max=_t(23),
        )
    error = exc_info.value
    assert not isinstance(error, CompressedChunkWriteError)
    assert "hydro.river_timeseries" in str(error)
    # No DSN leakage
    assert "postgres://" not in str(error)
    assert "postgresql://" not in str(error)


def test_query_cancelled_raises_guard_error() -> None:
    cursor = _FakeCursor(raise_on_query=_FakeQueryCanceled("statement timeout"))
    with pytest.raises(CompressedChunkGuardError) as exc_info:
        check_batch_targets_uncompressed(
            cursor,
            hypertable_schema="met",
            hypertable_name="forcing_station_timeseries",
            valid_time_min=_t(0),
            valid_time_max=_t(23),
        )
    assert not isinstance(exc_info.value, CompressedChunkWriteError)
    assert "met.forcing_station_timeseries" in str(exc_info.value)


def test_boundary_range_end_exclusive_allows_write() -> None:
    """A batch whose ``valid_time_max`` equals a compressed chunk's ``range_end`` is allowed.

    The query filter ``range_start < %s AND range_end > %s`` binds
    ``(valid_time_max, valid_time_min)``. If the compressed chunk ends at
    ``valid_time_max`` (i.e. its ``range_end == valid_time_max``) then
    ``range_start < valid_time_max`` may hold but ``range_end > valid_time_min``
    also has to hold; the fake cursor returns ``None`` to model this — the
    real DB would filter it out via the exclusive semantic.
    """
    cursor = _FakeCursor(row=None)
    check_batch_targets_uncompressed(
        cursor,
        hypertable_schema="hydro",
        hypertable_name="river_timeseries",
        valid_time_min=_t(20),
        valid_time_max=_t(23),
    )
    # verify the query was bound with (max, min) — max first
    query_calls = [
        (stmt, params) for stmt, params in cursor.executed if "chunks" in stmt
    ]
    assert len(query_calls) == 1
    _, params = query_calls[0]
    assert params == ("hydro", "river_timeseries", _t(23), _t(20))


def test_set_local_not_set_session() -> None:
    """The guard MUST use SET LOCAL (transaction-scoped) and never SET SESSION.

    Also verifies the guard restores the session default statement_timeout
    after its catalog lookup so subsequent DELETE + INSERT are not clipped
    by the guard's short timeout — golden-path byte-identity preservation.
    """
    cursor = _FakeCursor(row=None)
    check_batch_targets_uncompressed(
        cursor,
        hypertable_schema="hydro",
        hypertable_name="river_timeseries",
        valid_time_min=_t(0),
        valid_time_max=_t(1),
    )
    session_statements = [stmt for stmt, _ in cursor.executed if stmt.startswith("SET SESSION")]
    assert session_statements == []
    local_statements = [
        stmt for stmt, _ in cursor.executed if stmt.startswith("SET LOCAL statement_timeout")
    ]
    # Two: one setting '5s' before the SELECT, one resetting to DEFAULT after.
    assert len(local_statements) == 2
    assert "'5s'" in local_statements[0]
    assert "DEFAULT" in local_statements[1]


def test_dsn_not_leaked_in_error() -> None:
    """If a catalog error carries a DSN, the guard's reason message masks it."""

    class _DsnBearingError(Exception):
        def __init__(self) -> None:
            super().__init__("psql://user:password@host:5432/db failed")

    cursor = _FakeCursor(raise_on_query=_DsnBearingError())
    with pytest.raises(CompressedChunkGuardError) as exc_info:
        check_batch_targets_uncompressed(
            cursor,
            hypertable_schema="hydro",
            hypertable_name="river_timeseries",
            valid_time_min=_t(0),
            valid_time_max=_t(1),
        )
    guard_message = str(exc_info.value)
    assert "password" not in guard_message
    # The type name IS exposed but not the DSN string; verify the guard's
    # own message never embeds URL credentials.
    assert "psql://user:password" not in guard_message


def test_hypertables_guarded_constant_only_covers_production_pair() -> None:
    """``HYPERTABLES_GUARDED`` is the drill-exemption assertion source."""
    assert HYPERTABLES_GUARDED == frozenset(
        {
            ("hydro", "river_timeseries"),
            ("met", "forcing_station_timeseries"),
        }
    )


def test_runbook_anchor_matches_expected_github_slug() -> None:
    """The anchor is caller-observable contract; guard drift breaks the runbook link."""
    assert RUNBOOK_ANCHOR.endswith("#43-decompress-procedure")
    assert RUNBOOK_ANCHOR == "docs/runbooks/tier-node27-timeseries-storage.md#43-decompress-procedure"


def test_timeout_error_before_query_also_wraps() -> None:
    """A ``SET LOCAL`` failure (e.g. transaction aborted) still fails closed."""
    cursor = _FakeCursor(raise_on_timeout=_FakeOperationalError("transaction aborted"))
    with pytest.raises(CompressedChunkGuardError):
        check_batch_targets_uncompressed(
            cursor,
            hypertable_schema="hydro",
            hypertable_name="river_timeseries",
            valid_time_min=_t(0),
            valid_time_max=_t(1),
        )
