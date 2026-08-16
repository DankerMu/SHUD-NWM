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
from tests.test_sql_shape_helpers import (
    FORBIDDEN_TEXT_FACT_COLUMNS,
    SANCTIONED_TEXT_PUSHDOWN_COLUMNS,
    outer_predicates,
    text_fact_columns,
)

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
    """Both sample CTEs keep their scan pushdown; the river one now pushes keys.

    Re-pinned by issue #1341, then again by its round-1 hybrid amendment. The
    ``scan_*`` parameters keep their text semantics (they are still the run's
    scalar text identity, prefetched by the header query). The row-selection
    authority is now the surrogate key, resolved inside the query; the text
    conjunct beside it is the transitional compressed-chunk pushdown aid, kept
    only for the columns compression actually segments by. Dropping either half
    seq-scans a 730M-row hypertable — the key half loses the index, the text
    half loses compressed-chunk pushdown — so both are pinned literally.
    """
    sql = display_coverage._REFRESH_SQL
    assert "fst.forcing_version_id = %(scan_forcing_version_id)s" in sql
    assert "LOWER(fst.source_id) = %(scan_source_id_lower)s" in sql
    assert "fst.valid_time >= %(scan_display_start)s" in sql
    assert "rt.run_key = (SELECT run_key FROM hydro.hydro_run" in sql
    assert "WHERE run_id = %(scan_run_id)s)" in sql
    assert "rt.river_network_version_key = (SELECT river_network_version_key" in sql
    assert "= %(scan_river_network_version_id)s)" in sql
    assert "rt.valid_time <= %(scan_display_end)s" in sql

    outer = outer_predicates(sql)

    # Positive half: every sanctioned text aid sits in the SAME conjunction as
    # its key/enum counterpart. Asserted as one substring each, because two
    # independent `in` checks also pass when the pair is split across the query
    # and the "strict no-op" argument no longer holds.
    assert "WHERE rt.variable = 'q_down' AND rt.variable_e = 'q_down'::hydro.river_variable" in outer
    assert "(rt.run_id = %(scan_run_id)s AND rt.run_key = )" in outer
    assert (
        "(rt.river_network_version_id = %(scan_river_network_version_id)s AND rt.river_network_version_key = )"
    ) in outer

    # Negative half: the aids are bounded to the sanctioned three, and nothing
    # joins the fact table on a text column (a join equality buys no
    # compressed-chunk pushdown anyway, so it would be cost without benefit).
    assert text_fact_columns(sql, "rt") == set(SANCTIONED_TEXT_PUSHDOWN_COLUMNS)
    for forbidden in FORBIDDEN_TEXT_FACT_COLUMNS:
        assert f"rt.{forbidden}" not in outer, forbidden
    assert "cr.run_id = rt.run_id" not in outer
    assert "cr.river_network_version_id = rt.river_network_version_id" not in outer
