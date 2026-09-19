"""``_clear_issue_126_rows`` must bound its guarded-hypertable DELETEs (#1640, #1654).

TimescaleDB refuses a DELETE with no time predicate on a hypertable that holds
any compressed chunk — **even when zero rows match**. The teardown helper hits
guarded hypertables: ``hydro.river_timeseries``, and — from #1991's 000061 —
BOTH ``met.forcing_station_timeseries`` (narrow) and
``met.forcing_station_timeseries_legacy``. Each probes the window its own
identity predicate covers, skips the DELETE when nothing matched, and otherwise
bounds the DELETE to the probed window.

Which forcing relations exist depends on how far the caller migrated: several
suites pin the ledger at ``through="000058"`` / ``"000059"`` for river's sake, so
the helper discovers them from ``information_schema`` and picks the predicate by
the presence of ``forcing_version_id`` rather than by the table's name — before
000061 the canonical name IS the text-column table.

Driven by a recording fake cursor rather than a live database: the oracle here is
the emitted ``execute`` sequence, which is exactly what TimescaleDB would reject.
The real-DB teardown receipt is the ``-m integration`` lane on node-27.

The fake returns **dict** rows because the only production caller of this helper
(:func:`tests.integration_helpers.seed_issue_126_data`) opens its connection with
``RealDictCursor``; a tuple-row fake would verify a shape the real cursor never
produces.
"""

from __future__ import annotations

import re
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
#: #1991 task 7.3: 000061 renames the fact table and gives the canonical name to
#: the narrow key/enum table, so from that migration on there are TWO guarded
#: forcing hypertables and a version's rows live in exactly one of them.
FORCING_LEGACY_TABLE = "met.forcing_station_timeseries_legacy"

#: ``information_schema`` answers, as ``(table_name, has forcing_version_id)``.
#: The helper decides the per-table predicate by that COLUMN, not by the name:
#: before 000061 the canonical name IS the text-column table, and several suites
#: pin the ledger there on purpose.
POST_EXPAND_CATALOG: tuple[tuple[str, bool], ...] = (
    ("forcing_station_timeseries", False),
    ("forcing_station_timeseries_legacy", True),
)
PRE_EXPAND_CATALOG: tuple[tuple[str, bool], ...] = (("forcing_station_timeseries", True),)


def _mentions(sql: str, table: str) -> bool:
    """``met.forcing_station_timeseries`` must NOT match ``…_legacy``.

    Plain ``in`` prefix-matches the renamed sibling, which would make every
    per-table assertion below count the wrong statements.
    """
    return re.search(re.escape(table) + r"(?![A-Za-z0-9_])", sql) is not None


class _RecordingCursor:
    """Records every ``execute`` and answers probes by the table they name."""

    def __init__(
        self,
        windows: dict[str, tuple[Any, Any] | None],
        catalog: tuple[tuple[str, bool], ...] = POST_EXPAND_CATALOG,
    ) -> None:
        self._windows = windows
        self._catalog = catalog
        self.calls: list[tuple[str, tuple[Any, ...]]] = []
        self._pending: dict[str, Any] | None = None
        self._pending_rows: list[dict[str, Any]] = []

    def execute(self, sql: str, params: tuple[Any, ...] = ()) -> None:
        self.calls.append((sql, tuple(params)))
        self._pending = None
        self._pending_rows = []
        if "information_schema.columns" in sql:
            self._pending_rows = [
                {"table_name": name, "has_text_version": has_text} for name, has_text in self._catalog
            ]
            return
        if "min(valid_time)" not in sql:
            return
        # Longest name first: `met.forcing_station_timeseries` is a prefix of
        # `met.forcing_station_timeseries_legacy`.
        for table in sorted(self._windows, key=len, reverse=True):
            if _mentions(sql, table):
                window = self._windows[table]
                if window is None:
                    self._pending = {"valid_time_min": None, "valid_time_max": None}
                else:
                    self._pending = {"valid_time_min": window[0], "valid_time_max": window[1]}
                return
        raise AssertionError(f"probe against an unconfigured table: {sql}")

    def fetchone(self) -> dict[str, Any] | None:
        return self._pending

    def fetchall(self) -> list[dict[str, Any]]:
        return self._pending_rows

    def __enter__(self) -> _RecordingCursor:
        return self

    def __exit__(self, *_exc: object) -> None:
        return None


class _FakeConnection:
    def __init__(self, cursor: _RecordingCursor) -> None:
        self._cursor = cursor

    def cursor(self) -> _RecordingCursor:
        return self._cursor


def _run(
    windows: dict[str, tuple[Any, Any] | None],
    catalog: tuple[tuple[str, bool], ...] = POST_EXPAND_CATALOG,
) -> _RecordingCursor:
    cursor = _RecordingCursor(windows, catalog)
    _clear_issue_126_rows(_FakeConnection(cursor))
    return cursor


def _statements(cursor: _RecordingCursor, needle: str, *, kind: str) -> list[tuple[str, tuple[Any, ...]]]:
    return [call for call in cursor.calls if _mentions(call[0], needle) and call[0].lstrip().startswith(kind)]


def test_river_delete_is_bounded_to_the_probed_window() -> None:
    """T6: rows present -> the DELETE carries both bounds, taken from the probe."""
    cursor = _run(
        {
            RIVER_TABLE: (VALID_TIME_1, VALID_TIME_2),
            FORCING_TABLE: (VALID_TIME_1, VALID_TIME_2),
            FORCING_LEGACY_TABLE: (VALID_TIME_1, VALID_TIME_2),
        }
    )

    probes = _statements(cursor, RIVER_TABLE, kind="SELECT")
    assert len(probes) == 1
    assert probes[0][1] == (FORECAST_RUN_ID, HINDCAST_RUN_ID)
    # Pin the aliases in the SQL text. The fake answers a probe by matching the
    # table name and returns keys it chose itself, so it would stay green if the
    # aliases were swapped in the SQL while the Python still read the right
    # keys — under a real RealDictCursor that yields max as the lower bound and
    # a backwards, row-leaking DELETE window.
    assert "min(valid_time) AS valid_time_min" in probes[0][0]
    assert "max(valid_time) AS valid_time_max" in probes[0][0]
    # What remains uncovered here is the fake-vs-real cursor gap — psycopg2
    # raises ProgrammingError on an out-of-sequence fetchone(), which the
    # helper's ``cursor.fetchone() or {}`` would absorb. That is closed by the
    # node-27 ``-m integration`` receipt, not by this file.

    deletes = _statements(cursor, RIVER_TABLE, kind="DELETE")
    assert len(deletes) == 1
    sql, params = deletes[0]
    assert "valid_time >= %s" in sql
    assert "valid_time <= %s" in sql
    assert params == (FORECAST_RUN_ID, HINDCAST_RUN_ID, VALID_TIME_1, VALID_TIME_2)


def test_forcing_delete_is_bounded_to_the_probed_window() -> None:
    """T6: same for the second guarded hypertable."""
    cursor = _run(
        {
            RIVER_TABLE: (VALID_TIME_1, VALID_TIME_2),
            FORCING_TABLE: (VALID_TIME_1, VALID_TIME_2),
            FORCING_LEGACY_TABLE: (VALID_TIME_1, VALID_TIME_2),
        }
    )

    probes = _statements(cursor, FORCING_TABLE, kind="SELECT")
    assert len(probes) == 1
    assert probes[0][1] == (f"{ISSUE_126_PREFIX}%",)
    # Same alias pin, same mutation, as for the river probe above.
    assert "min(valid_time) AS valid_time_min" in probes[0][0]
    assert "max(valid_time) AS valid_time_max" in probes[0][0]

    deletes = _statements(cursor, FORCING_TABLE, kind="DELETE")
    assert len(deletes) == 1
    sql, params = deletes[0]
    assert "valid_time >= %s" in sql
    assert "valid_time <= %s" in sql
    assert params == (f"{ISSUE_126_PREFIX}%", VALID_TIME_1, VALID_TIME_2)


def test_the_probe_precedes_the_delete_and_the_hydro_run_deletion() -> None:
    """The river probe resolves ``run_key`` through ``hydro_run``, deleted later."""
    cursor = _run(
        {
            RIVER_TABLE: (VALID_TIME_1, VALID_TIME_2),
            FORCING_TABLE: (VALID_TIME_1, VALID_TIME_2),
            FORCING_LEGACY_TABLE: (VALID_TIME_1, VALID_TIME_2),
        }
    )
    order = [index for index, (sql, _) in enumerate(cursor.calls) if RIVER_TABLE in sql]
    run_delete = next(index for index, (sql, _) in enumerate(cursor.calls) if "DELETE FROM hydro.hydro_run" in sql)

    # Identify the two statements by content, not by their position in the list:
    # the DELETE's bounds come from the probe's result, so emitting the DELETE
    # first would read a window that was never measured.
    probes = [index for index, (sql, _) in enumerate(cursor.calls) if RIVER_TABLE in sql and "min(valid_time)" in sql]
    deletes = [index for index, (sql, _) in enumerate(cursor.calls) if f"DELETE FROM {RIVER_TABLE}" in sql]
    assert len(probes) == 1 and len(deletes) == 1
    assert probes[0] < deletes[0]
    assert max(order) < run_delete


def test_no_delete_at_all_when_nothing_matches() -> None:
    """T7: the skip path — an unbounded DELETE is not the only failure mode.

    TimescaleDB rejects the unbounded DELETE even for zero matching rows, so
    "emit it bounded to a NULL window" would be just as broken. Nothing may be
    emitted against either hypertable except the probe.
    """
    cursor = _run({RIVER_TABLE: None, FORCING_TABLE: None, FORCING_LEGACY_TABLE: None})

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
    cursor = _run({RIVER_TABLE: (VALID_TIME_1, VALID_TIME_2), FORCING_TABLE: None, FORCING_LEGACY_TABLE: None})

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

    cursor = _run(
        {
            RIVER_TABLE: (wider_min, wider_max),
            FORCING_TABLE: (wider_min, wider_max),
            FORCING_LEGACY_TABLE: (wider_min, wider_max),
        }
    )

    river_params = _statements(cursor, RIVER_TABLE, kind="DELETE")[0][1]
    assert river_params == (FORECAST_RUN_ID, HINDCAST_RUN_ID, wider_min, wider_max)

    forcing_params = _statements(cursor, FORCING_TABLE, kind="DELETE")[0][1]
    assert forcing_params == (f"{ISSUE_126_PREFIX}%", wider_min, wider_max)


def test_both_forcing_stores_are_cleaned_under_their_own_predicates() -> None:
    """#1991: after 000061 a version's rows are in exactly one of two tables.

    Cleaning only the canonical name leaks every legacy-routed fixture row into
    the next test on a session-scoped database, and cleaning both under the SAME
    predicate is not possible: the narrow table has no ``forcing_version_id``.
    """
    cursor = _run(
        {
            RIVER_TABLE: (VALID_TIME_1, VALID_TIME_2),
            FORCING_TABLE: (VALID_TIME_1, VALID_TIME_2),
            FORCING_LEGACY_TABLE: (VALID_TIME_1, VALID_TIME_2),
        }
    )

    narrow_delete = _statements(cursor, FORCING_TABLE, kind="DELETE")
    legacy_delete = _statements(cursor, FORCING_LEGACY_TABLE, kind="DELETE")
    assert len(narrow_delete) == len(legacy_delete) == 1

    # The narrow table carries no text version column, so its predicate resolves
    # the surrogate key through `met.forcing_version`; the legacy one is the
    # unchanged text LIKE. Both still bind exactly the prefix and the two bounds.
    assert "forcing_version_key IN (SELECT forcing_version_key FROM met.forcing_version" in narrow_delete[0][0]
    assert "forcing_version_id LIKE %s" in legacy_delete[0][0]
    assert "forcing_version_key" not in legacy_delete[0][0]
    assert narrow_delete[0][1] == legacy_delete[0][1] == (f"{ISSUE_126_PREFIX}%", VALID_TIME_1, VALID_TIME_2)


def test_a_pre_expand_catalog_cleans_one_table_with_the_text_predicate() -> None:
    """The ``through="000058"`` / ``"000059"`` suites must keep working.

    Their catalogs have never seen 000061: one forcing fact table, under the
    canonical name, with text columns. Deciding by the migration ledger — or by
    assuming the rename happened — would emit the narrow predicate against a
    table that has no ``forcing_version_key``.
    """
    cursor = _run(
        {RIVER_TABLE: (VALID_TIME_1, VALID_TIME_2), FORCING_TABLE: (VALID_TIME_1, VALID_TIME_2)},
        PRE_EXPAND_CATALOG,
    )

    assert not any(_mentions(sql, FORCING_LEGACY_TABLE) for sql, _ in cursor.calls)
    deletes = _statements(cursor, FORCING_TABLE, kind="DELETE")
    assert len(deletes) == 1
    assert "forcing_version_id LIKE %s" in deletes[0][0]
    assert "forcing_version_key" not in deletes[0][0]
    assert deletes[0][1] == (f"{ISSUE_126_PREFIX}%", VALID_TIME_1, VALID_TIME_2)
