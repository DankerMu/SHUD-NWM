"""``_clear_issue_126_rows`` must bound its guarded-hypertable DELETEs (#1640, #1654).

TimescaleDB refuses a DELETE with no time predicate on a hypertable that holds
any compressed chunk — **even when zero rows match**. Two statements in the
teardown helper hit guarded hypertables: ``hydro.river_timeseries`` and
``met.forcing_station_timeseries``. Both now probe the window their own identity
predicate covers, skip the DELETE when nothing matched, and otherwise bound the
DELETE to the probed window.

Driven by a recording fake cursor rather than a live database: the oracle here is
the emitted ``execute`` sequence, which is exactly what TimescaleDB would reject.
The real-DB teardown receipt is the ``-m integration`` lane on node-27.

The fake returns **dict** rows because the only production caller of this helper
(:func:`tests.integration_helpers.seed_issue_126_data`) opens its connection with
``RealDictCursor``; a tuple-row fake would verify a shape the real cursor never
produces.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from tests.integration_helpers import (
    FORECAST_RUN_ID,
    HINDCAST_RUN_ID,
    ISSUE_126_PREFIX,
    VALID_TIME_1,
    VALID_TIME_2,
    _clear_issue_126_rows,
)

RIVER_TABLE = "hydro.river_timeseries"
FORCING_TABLE = "met.forcing_station_timeseries"


class _RecordingCursor:
    """Records every ``execute`` and answers probes by the table they name."""

    def __init__(self, windows: dict[str, tuple[Any, Any] | None]) -> None:
        self._windows = windows
        self.calls: list[tuple[str, tuple[Any, ...]]] = []
        self._pending: dict[str, Any] | None = None

    def execute(self, sql: str, params: tuple[Any, ...] = ()) -> None:
        self.calls.append((sql, tuple(params)))
        self._pending = None
        if "min(valid_time)" not in sql:
            return
        for table, window in self._windows.items():
            if table in sql:
                if window is None:
                    self._pending = {"valid_time_min": None, "valid_time_max": None}
                else:
                    self._pending = {"valid_time_min": window[0], "valid_time_max": window[1]}
                return
        raise AssertionError(f"probe against an unconfigured table: {sql}")

    def fetchone(self) -> dict[str, Any] | None:
        return self._pending

    def __enter__(self) -> _RecordingCursor:
        return self

    def __exit__(self, *_exc: object) -> None:
        return None


class _FakeConnection:
    def __init__(self, cursor: _RecordingCursor) -> None:
        self._cursor = cursor

    def cursor(self) -> _RecordingCursor:
        return self._cursor


def _run(windows: dict[str, tuple[Any, Any] | None]) -> _RecordingCursor:
    cursor = _RecordingCursor(windows)
    _clear_issue_126_rows(_FakeConnection(cursor))
    return cursor


def _statements(cursor: _RecordingCursor, needle: str, *, kind: str) -> list[tuple[str, tuple[Any, ...]]]:
    return [call for call in cursor.calls if needle in call[0] and call[0].lstrip().startswith(kind)]


def test_river_delete_is_bounded_to_the_probed_window() -> None:
    """T6: rows present -> the DELETE carries both bounds, taken from the probe."""
    cursor = _run({RIVER_TABLE: (VALID_TIME_1, VALID_TIME_2), FORCING_TABLE: (VALID_TIME_1, VALID_TIME_2)})

    probes = _statements(cursor, RIVER_TABLE, kind="SELECT")
    assert len(probes) == 1
    assert probes[0][1] == (FORECAST_RUN_ID, HINDCAST_RUN_ID)

    deletes = _statements(cursor, RIVER_TABLE, kind="DELETE")
    assert len(deletes) == 1
    sql, params = deletes[0]
    assert "valid_time >= %s" in sql
    assert "valid_time <= %s" in sql
    assert params == (FORECAST_RUN_ID, HINDCAST_RUN_ID, VALID_TIME_1, VALID_TIME_2)


def test_forcing_delete_is_bounded_to_the_probed_window() -> None:
    """T6: same for the second guarded hypertable."""
    cursor = _run({RIVER_TABLE: (VALID_TIME_1, VALID_TIME_2), FORCING_TABLE: (VALID_TIME_1, VALID_TIME_2)})

    probes = _statements(cursor, FORCING_TABLE, kind="SELECT")
    assert len(probes) == 1
    assert probes[0][1] == (f"{ISSUE_126_PREFIX}%",)

    deletes = _statements(cursor, FORCING_TABLE, kind="DELETE")
    assert len(deletes) == 1
    sql, params = deletes[0]
    assert "valid_time >= %s" in sql
    assert "valid_time <= %s" in sql
    assert params == (f"{ISSUE_126_PREFIX}%", VALID_TIME_1, VALID_TIME_2)


def test_the_probe_precedes_the_delete_and_the_hydro_run_deletion() -> None:
    """The river probe resolves ``run_key`` through ``hydro_run``, deleted later."""
    cursor = _run({RIVER_TABLE: (VALID_TIME_1, VALID_TIME_2), FORCING_TABLE: (VALID_TIME_1, VALID_TIME_2)})
    order = [index for index, (sql, _) in enumerate(cursor.calls) if RIVER_TABLE in sql]
    run_delete = next(index for index, (sql, _) in enumerate(cursor.calls) if "DELETE FROM hydro.hydro_run" in sql)

    assert order == sorted(order)
    assert max(order) < run_delete


def test_no_delete_at_all_when_nothing_matches() -> None:
    """T7: the skip path — an unbounded DELETE is not the only failure mode.

    TimescaleDB rejects the unbounded DELETE even for zero matching rows, so
    "emit it bounded to a NULL window" would be just as broken. Nothing may be
    emitted against either hypertable except the probe.
    """
    cursor = _run({RIVER_TABLE: None, FORCING_TABLE: None})

    emitted = [sql for sql, _ in cursor.calls]
    assert not any(f"DELETE FROM {RIVER_TABLE}" in sql for sql in emitted)
    assert not any(f"DELETE FROM {FORCING_TABLE}" in sql for sql in emitted)
    assert len(_statements(cursor, RIVER_TABLE, kind="SELECT")) == 1
    assert len(_statements(cursor, FORCING_TABLE, kind="SELECT")) == 1
    # The rest of the teardown still runs.
    assert any("DELETE FROM hydro.hydro_run" in sql for sql in emitted)
    assert any("DELETE FROM core.basin " in sql for sql in emitted)


def test_the_two_tables_decide_independently() -> None:
    """The mixed case, which is the normal one on the session-scoped database.

    ``river_timeseries`` is seeded there; ``forcing_station_timeseries`` never is
    (`tests/integration_helpers.py` seeds no stations), so one table skips while
    the other deletes.
    """
    cursor = _run({RIVER_TABLE: (VALID_TIME_1, VALID_TIME_2), FORCING_TABLE: None})

    assert len(_statements(cursor, RIVER_TABLE, kind="DELETE")) == 1
    assert not any(f"DELETE FROM {FORCING_TABLE}" in sql for sql, _ in cursor.calls)


def test_the_bound_follows_the_table_not_the_fixture_constants() -> None:
    """T8 / design D2: a row seeded outside ``VALID_TIME_1..2`` is still deleted.

    Hard-coding the seeder's two timestamps would make teardown silently leak the
    third row into the next test on a session-scoped database.
    """
    wider_min = datetime(2020, 1, 1, tzinfo=UTC)
    wider_max = datetime(2031, 12, 31, 23, tzinfo=UTC)
    assert wider_min < VALID_TIME_1 and wider_max > VALID_TIME_2

    cursor = _run({RIVER_TABLE: (wider_min, wider_max), FORCING_TABLE: (wider_min, wider_max)})

    river_params = _statements(cursor, RIVER_TABLE, kind="DELETE")[0][1]
    assert river_params == (FORECAST_RUN_ID, HINDCAST_RUN_ID, wider_min, wider_max)

    forcing_params = _statements(cursor, FORCING_TABLE, kind="DELETE")[0][1]
    assert forcing_params == (f"{ISSUE_126_PREFIX}%", wider_min, wider_max)
