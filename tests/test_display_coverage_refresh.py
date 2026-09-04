"""Per-run coverage refresh: scan-pushdown prefetch and overwrite-guard contract.

The per-run refresh MUST bind the run's scalar identity/window as pushdown
predicates into the hypertable sample CTEs (planner cannot push CTE-join
equalities into the chunk scans; without pushdown one refresh seq-scans both
hypertables end to end). Requirements covered:

* eligible run -> header prefetch runs first, main SQL gets scan_* bound to
  the header values, run_id returned;
* non-eligible run (no header row) -> empty outcome without executing the heavy
  SQL, and NOT a refusal even when the run still owns a populated row;
* run_id=None (all-runs SQL contract) -> no header prefetch, scan_* all NULL,
  and no refusal classification (it protects, it does not classify);
* the pushdown predicates exist in the SQL text for BOTH sample CTEs.

Issue #1446 adds the overwrite guard to the same write point, so this module
also covers it: a candidate that reached the upsert and did not come back in
``RETURNING`` was skipped by the guard, which ``refresh_run_display_coverage``
turns into ``DisplayCoverageRefreshRefused`` after a rollback, and ``force``
bypasses.
"""

from __future__ import annotations

from typing import Any

import pytest

from packages.common import display_coverage
from tests.test_sql_shape_helpers import (
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

_RETURNED_RUN_1 = [{"run_id": "run-1"}]
# What the guard clause produces for a refused run: the statement ran, the row
# was skipped, so RETURNING yields nothing.
_RETURNED_NOTHING: list[dict[str, Any]] = []


class _Cursor:
    def __init__(
        self,
        header_rows: list[dict[str, Any]],
        refresh_rows: list[dict[str, Any]],
        existing_segment_count: int | None,
    ) -> None:
        self.header_rows = header_rows
        self.refresh_rows = refresh_rows
        self.existing_segment_count = existing_segment_count
        self.executed: list[tuple[str, Any]] = []
        self._pending: list[dict[str, Any]] = []

    def execute(self, sql: str, params: Any = None) -> None:
        self.executed.append((sql, params))
        if sql is display_coverage._SCAN_HEADER_SQL:
            self._pending = list(self.header_rows)
        elif sql is display_coverage._REFRESH_SQL:
            self._pending = list(self.refresh_rows)
        elif sql is display_coverage._EXISTING_SEGMENT_COUNT_SQL:
            self._pending = (
                [] if self.existing_segment_count is None else [{"segment_count": self.existing_segment_count}]
            )
        else:
            self._pending = []

    def fetchall(self) -> list[dict[str, Any]]:
        return self._pending

    def fetchone(self) -> dict[str, Any] | None:
        return self._pending[0] if self._pending else None

    def __enter__(self) -> "_Cursor":
        return self

    def __exit__(self, *args: Any) -> None:
        return None


class _Connection:
    def __init__(
        self,
        header_rows: list[dict[str, Any]],
        refresh_rows: list[dict[str, Any]] | None = None,
        existing_segment_count: int | None = None,
    ) -> None:
        self.cursor_obj = _Cursor(
            header_rows,
            _RETURNED_RUN_1 if refresh_rows is None else refresh_rows,
            existing_segment_count,
        )
        self.commits = 0
        self.rollbacks = 0

    def cursor(self, cursor_factory: Any = None) -> _Cursor:
        return self.cursor_obj

    def commit(self) -> None:
        self.commits += 1

    def rollback(self) -> None:
        self.rollbacks += 1


def _executed_sqls(connection: _Connection) -> list[str]:
    return [sql for sql, _params in connection.cursor_obj.executed]


def test_eligible_run_binds_header_values_as_scan_pushdown() -> None:
    connection = _Connection([_HEADER_ROW])

    outcome = display_coverage._refresh(connection, "run-1")

    assert outcome == display_coverage.RefreshOutcome(["run-1"], [])
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
    # Unforced is the default on every existing caller: the guard is live.
    assert params["force"] is False


def test_non_eligible_run_skips_heavy_query() -> None:
    connection = _Connection([])

    outcome = display_coverage._refresh(connection, "run-missing")

    assert outcome == display_coverage.RefreshOutcome([], [])
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
    assert params["force"] is False


# ---------------------------------------------------------------------------
# #1446 overwrite guard
# ---------------------------------------------------------------------------


def test_refresh_sql_skips_a_populated_row_when_the_fresh_scan_is_empty() -> None:
    """The guard is the upsert's own conditional DO UPDATE ... WHERE.

    Asserted on the statement text because no caller-side read-then-write
    exists to assert on: a fake cursor cannot evaluate the clause, and the
    clause is the entire protection. The three disjuncts are the whole
    contract -- force bypasses, a non-empty fresh scan always wins, and a row
    that already reads 0 is rewritten as before (so first refreshes and
    genuinely empty runs are untouched).
    """
    sql = display_coverage._REFRESH_SQL

    guard = sql.split("refreshed_at = EXCLUDED.refreshed_at")[-1]
    assert "%(force)s" in guard
    assert "EXCLUDED.segment_count > 0" in guard
    assert "hydro.run_display_coverage.segment_count = 0" in guard
    # The guard sits between the SET list and RETURNING -- i.e. it gates the
    # UPDATE, not the INSERT and not the returned projection.
    assert guard.index("%(force)s") < guard.index("RETURNING run_id")


def test_candidate_absent_from_returning_is_reported_as_refused() -> None:
    """Refusal is decided from the statement's own outcome, nothing else.

    The run reached the upsert (it had a header row) and the upsert returned
    no row for it. Because `coverage` is built FROM candidate_runs with LEFT
    JOINs, a candidate always produces exactly one upsert row -- so "absent
    from RETURNING" can only mean the guard skipped it.
    """
    connection = _Connection([_HEADER_ROW], refresh_rows=_RETURNED_NOTHING)

    outcome = display_coverage._refresh(connection, "run-1")

    assert outcome == display_coverage.RefreshOutcome([], ["run-1"])
    assert display_coverage._REFRESH_SQL in _executed_sqls(connection)


def test_legacy_populated_run_is_refused_with_its_stored_count_and_rolled_back() -> None:
    connection = _Connection([_HEADER_ROW], refresh_rows=_RETURNED_NOTHING, existing_segment_count=12)

    with pytest.raises(display_coverage.DisplayCoverageRefreshRefused) as excinfo:
        display_coverage.refresh_run_display_coverage(connection, "run-1")

    refusal = excinfo.value
    assert refusal.run_id == "run-1"
    assert refusal.existing_segment_count == 12
    assert refusal.advice == display_coverage._REFUSAL_ADVICE
    # One stderr line is the CLI's contract (tests/test_node27_refresh_coverage_cli.py);
    # it holds only while the advice itself carries no newline.
    assert "\n" not in display_coverage._REFUSAL_ADVICE
    assert "run-1" in str(refusal)
    # Nothing written, and the transaction is left clean for the caller.
    assert connection.rollbacks == 1
    assert connection.commits == 0


def test_force_bypasses_the_guard_and_commits() -> None:
    # With force the DB would perform the zeroing, so the statement returns the
    # run -- the library must pass the flag through and treat it as a refresh.
    connection = _Connection([_HEADER_ROW], refresh_rows=_RETURNED_RUN_1)

    assert display_coverage.refresh_run_display_coverage(connection, "run-1", force=True) is True

    _sql, params = connection.cursor_obj.executed[-1]
    assert params["force"] is True
    assert connection.commits == 1
    assert connection.rollbacks == 0


def test_non_candidate_with_an_old_populated_row_is_not_a_refusal() -> None:
    """The distinction the early return has to preserve.

    A run that no longer matches the candidate query never reaches the upsert,
    so nothing was skipped and nothing is owed an operator alarm -- it stays
    the pre-existing "no coverage row" outcome (False, no raise), which the
    autopipeline records as `no_coverage_row`.
    """
    connection = _Connection([], existing_segment_count=12)

    assert display_coverage.refresh_run_display_coverage(connection, "run-missing") is False

    assert connection.commits == 1
    assert connection.rollbacks == 0
    assert display_coverage._EXISTING_SEGMENT_COUNT_SQL not in _executed_sqls(connection)


def test_all_runs_form_protects_without_classifying_refusals() -> None:
    """`_refresh(conn, None)` runs no header query, so it has no candidate set.

    Guarded rows are simply not returned (the protection is in the statement),
    but there is no run id to charge a refusal to -- so `refused` is empty by
    contract, and the two runs the upsert did write come back as refreshed.
    """
    connection = _Connection(
        [_HEADER_ROW],
        refresh_rows=[{"run_id": "run-1"}, {"run_id": "run-3"}],
    )

    outcome = display_coverage._refresh(connection, None)

    assert outcome.refreshed == ["run-1", "run-3"]
    assert outcome.refused == []


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
    # #1980 re-pin: the `variable` aid moved off the `WHERE` line onto its own
    # marked `AND` line, and the two guards' aids moved inside `OR (` with the
    # marker above them (hence the space after the bracket once comments are
    # stripped). Same conjunctions, same truth table, same fold-away on NULL.
    assert "WHERE rt.variable_e = 'q_down'::hydro.river_variable AND rt.variable = 'q_down'" in outer
    assert "( rt.run_id = %(scan_run_id)s AND rt.run_key = )" in outer
    assert (
        "( rt.river_network_version_id = %(scan_river_network_version_id)s AND rt.river_network_version_key = )"
    ) in outer

    # Negative half: the aids are bounded to the sanctioned three, and nothing
    # joins the fact table on a text column (a join equality buys no
    # compressed-chunk pushdown anyway, so it would be cost without benefit).
    #
    # The set equality carries the whole forbidden-column half on its own:
    # `text_fact_columns` scans the same `outer` text for every column in
    # SANCTIONED | FORBIDDEN under word boundaries, so any `rt.basin_version_id`
    # / `rt.river_segment_id` / `rt.unit` / `rt.quality_flag` reference widens
    # the left side and fails. A bare `f"rt.{forbidden}" not in outer` loop
    # beside it adds nothing and actively false-reds: "rt.unit" is a prefix of
    # the legitimate `rt.unit_e`, and it would also fail on correct post-#1342
    # code for the same reason.
    assert text_fact_columns(sql, "rt") == set(SANCTIONED_TEXT_PUSHDOWN_COLUMNS)
    assert "cr.run_id = rt.run_id" not in outer
    assert "cr.river_network_version_id = rt.river_network_version_id" not in outer
