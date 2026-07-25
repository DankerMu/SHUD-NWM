"""Per-run coverage refresh: scan-pushdown prefetch contract.

The per-run refresh MUST bind the run's scalar identity/window as pushdown
predicates into the hypertable sample CTEs (planner cannot push CTE-join
equalities into the chunk scans; without pushdown one refresh seq-scans both
hypertables end to end). Requirements covered:

* eligible run -> header prefetch runs first, main SQL gets scan_* bound to
  the header values, run_id returned;
* non-eligible run (no header row) -> [] without executing the heavy SQL;
* run_id=None (all-runs SQL contract) -> no header prefetch, scan_* all NULL;
* the pushdown predicates exist in the SQL text for BOTH sample CTEs.
"""

from __future__ import annotations

from typing import Any

from packages.common import display_coverage

_HEADER_ROW = {
    "run_id": "run-1",
    "forcing_version_id": "fv-1",
    "basin_version_id": "bv-1",
    "river_network_version_id": "rnv-1",
    "source_id_lower": "gfs",
    "display_start_time": "2026-07-12T00:00:00Z",
    "display_end_time": "2026-07-17T00:00:00Z",
}


class _Cursor:
    def __init__(self, header_rows: list[dict[str, Any]]) -> None:
        self.header_rows = header_rows
        self.executed: list[tuple[str, Any]] = []
        self._pending: list[dict[str, Any]] = []

    def execute(self, sql: str, params: Any = None) -> None:
        self.executed.append((sql, params))
        if sql is display_coverage._SCAN_HEADER_SQL:
            self._pending = list(self.header_rows)
        elif sql is display_coverage._REFRESH_SQL:
            self._pending = [{"run_id": "run-1"}]
        else:
            self._pending = []

    def fetchall(self) -> list[dict[str, Any]]:
        return self._pending

    def __enter__(self) -> "_Cursor":
        return self

    def __exit__(self, *args: Any) -> None:
        return None


class _Connection:
    def __init__(self, header_rows: list[dict[str, Any]]) -> None:
        self.cursor_obj = _Cursor(header_rows)

    def cursor(self, cursor_factory: Any = None) -> _Cursor:
        return self.cursor_obj


def _executed_sqls(connection: _Connection) -> list[str]:
    return [sql for sql, _params in connection.cursor_obj.executed]


def test_eligible_run_binds_header_values_as_scan_pushdown() -> None:
    connection = _Connection([_HEADER_ROW])

    refreshed = display_coverage._refresh(connection, "run-1")

    assert refreshed == ["run-1"]
    sqls = _executed_sqls(connection)
    assert display_coverage._SCAN_HEADER_SQL in sqls
    assert display_coverage._REFRESH_SQL in sqls
    _sql, params = connection.cursor_obj.executed[-1]
    assert params["scan_run_id"] == "run-1"
    assert params["scan_forcing_version_id"] == "fv-1"
    assert params["scan_basin_version_id"] == "bv-1"
    assert params["scan_river_network_version_id"] == "rnv-1"
    assert params["scan_source_id_lower"] == "gfs"
    assert params["scan_display_start"] == _HEADER_ROW["display_start_time"]
    assert params["scan_display_end"] == _HEADER_ROW["display_end_time"]


def test_non_eligible_run_skips_heavy_query() -> None:
    connection = _Connection([])

    refreshed = display_coverage._refresh(connection, "run-missing")

    assert refreshed == []
    sqls = _executed_sqls(connection)
    assert display_coverage._SCAN_HEADER_SQL in sqls
    assert display_coverage._REFRESH_SQL not in sqls


def test_all_runs_mode_disables_pushdown() -> None:
    connection = _Connection([_HEADER_ROW])

    display_coverage._refresh(connection, None)

    sqls = _executed_sqls(connection)
    assert display_coverage._SCAN_HEADER_SQL not in sqls
    _sql, params = connection.cursor_obj.executed[-1]
    for key in display_coverage._SCAN_PARAM_KEYS:
        assert params[key] is None


def test_pushdown_predicates_present_in_both_sample_ctes() -> None:
    sql = display_coverage._REFRESH_SQL
    assert "fst.forcing_version_id = %(scan_forcing_version_id)s" in sql
    assert "LOWER(fst.source_id) = %(scan_source_id_lower)s" in sql
    assert "fst.valid_time >= %(scan_display_start)s" in sql
    assert "rt.run_id = %(scan_run_id)s" in sql
    assert "rt.river_network_version_id = %(scan_river_network_version_id)s" in sql
    assert "rt.valid_time <= %(scan_display_end)s" in sql
