"""Latest-QHH authoritative fallback: scan-pushdown prefetch contract (#1120).

The coverage-cache miss path recomputes the deep coverage CTEs at request time.
Without literal scan predicates the planner cannot exclude a single chunk of
``met.forcing_station_timeseries`` / ``hydro.river_timeseries`` (the CTE join
equalities and the correlated ``cr.display_*`` window columns are opaque to it),
which is the shape that measured 633s before 62824a45 fixed the refresh path.
Requirements covered here:

* a scan header statement runs FIRST, built from the same candidate_runs SQL
  constant as the heavy statement — no hand-copied second CTE;
* the heavy statement carries NULL-guarded ``scan_*`` predicates in BOTH sample
  CTEs and pins candidate_runs to the header's ``run_id``;
* an empty header returns ``[]`` and the heavy statement is never executed;
* the header's scalars are bound into the heavy statement's named parameters;
* the whole fallback really executes against a cursor that rejects any
  placeholder-style/parameter-container mismatch (a text assertion cannot see a
  dict degraded to a tuple of key names — this can);
* the fast path and the unavailable-context query keep positional parameters;
* the run_id pinning stays sound only while the candidate limit is 1.
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from typing import Any

import pytest

from packages.common import forecast_store
from packages.common.forecast_store import (
    QHH_LATEST_CANDIDATE_LIMIT,
    QHH_LATEST_SEARCH_LIMIT,
    PsycopgForecastStore,
)
from tests.test_sql_shape_helpers import outer_predicates

_NAMED_PLACEHOLDER = re.compile(r"%\(([a-zA-Z_][a-zA-Z0-9_]*)\)s")
_POSITIONAL_PLACEHOLDER = re.compile(r"%s")

_HEADER_ROW = {
    "run_id": "qhh_gfs_2026050700",
    "forcing_version_id": "forc_qhh_gfs_2026050700",
    "basin_version_id": "basins_qhh_vbasins",
    "river_network_version_id": "basins_qhh_rivnet_vbasins",
    "source_id_lower": "gfs",
    "display_start_time": datetime(2026, 5, 7, tzinfo=UTC),
    "display_end_time": datetime(2026, 5, 14, tzinfo=UTC),
}

_CANDIDATE_ROW = {"run_id": "qhh_gfs_2026050700", "station_count": 386, "segment_count": 1633}


class BindingCheckedCursor:
    """A cursor that fails the way psycopg2 fails on a binding mismatch.

    psycopg2 fills ``%s`` from a sequence and ``%(name)s`` from a mapping, and
    refuses a mix. Passing a dict where the statement uses ``%s`` (or a tuple
    where it uses ``%(name)s`` — what ``tuple(parameters)`` would silently
    produce) is a production-only crash that text assertions never see.
    """

    def __init__(self, *, header_rows: list[dict[str, Any]], coverage_table: bool = False) -> None:
        self.header_rows = header_rows
        self.coverage_table = coverage_table
        self.executed: list[tuple[str, Any]] = []
        self._pending: list[dict[str, Any]] = []

    def execute(self, statement: str, parameters: Any = None) -> None:
        self._check_binding(statement, parameters)
        self.executed.append((statement, parameters))
        if "to_regclass(" in statement:
            self._pending = [{"reg": "hydro.run_display_coverage" if self.coverage_table else None}]
        elif self._is_header(statement):
            self._pending = list(self.header_rows)
        else:
            self._pending = [dict(_CANDIDATE_ROW)]

    @staticmethod
    def _is_header(statement: str) -> bool:
        return "LOWER(source_id) AS source_id_lower" in statement and "station_sample_rows" not in statement

    @staticmethod
    def _check_binding(statement: str, parameters: Any) -> None:
        named = set(_NAMED_PLACEHOLDER.findall(statement))
        positional = len(_POSITIONAL_PLACEHOLDER.findall(statement))
        if named and positional:
            raise AssertionError("statement mixes %s and %(name)s placeholders")
        if named:
            if not isinstance(parameters, Mapping):
                raise AssertionError(
                    f"statement uses named placeholders {sorted(named)} but got {type(parameters).__name__}"
                )
            missing = named - set(parameters)
            if missing:
                raise AssertionError(f"named placeholders not bound: {sorted(missing)}")
            return
        if positional:
            if isinstance(parameters, Mapping) or not isinstance(parameters, Sequence):
                raise AssertionError(
                    f"statement uses positional placeholders but got {type(parameters).__name__}"
                )
            if len(parameters) != positional:
                raise AssertionError(f"expected {positional} positional parameters, got {len(parameters)}")

    def fetchall(self) -> list[dict[str, Any]]:
        rows, self._pending = self._pending, []
        return rows

    def fetchone(self) -> dict[str, Any]:
        rows = self.fetchall()
        return rows[0] if rows else {}


def _store() -> PsycopgForecastStore:
    return PsycopgForecastStore("postgresql://unit-test")


def _fallback_statements(cursor: BindingCheckedCursor) -> list[str]:
    return [statement for statement, _params in cursor.executed if "to_regclass(" not in statement]


def _run_fallback(
    cursor: BindingCheckedCursor,
    *,
    identity: Any = None,
) -> list[dict[str, Any]]:
    return _store()._fetch_latest_qhh_display_candidates(
        cursor,
        basin_id="basins_qhh",
        source_id="GFS",
        identity=identity,
    )


def test_header_runs_first_and_reuses_the_shared_candidate_sql_constant() -> None:
    cursor = BindingCheckedCursor(header_rows=[dict(_HEADER_ROW)])

    _run_fallback(cursor)

    header_sql, heavy_sql = _fallback_statements(cursor)
    assert "LOWER(source_id) AS source_id_lower" in header_sql
    assert "station_sample_rows" in heavy_sql
    # Both statements embed the SAME rendered constant (the heavy one only adds
    # the run_id pin), so the two candidate_runs CTEs cannot drift apart.
    assert forecast_store._qhh_latest_candidate_runs_sql(identity_sql="", pin_scan_run_id=False) in header_sql
    assert forecast_store._qhh_latest_candidate_runs_sql(identity_sql="", pin_scan_run_id=True) in heavy_sql
    # The header must stay cheap: no hypertable is named in it.
    assert "met.forcing_station_timeseries" not in header_sql
    assert "hydro.river_timeseries" not in header_sql


def test_scan_pushdown_predicates_present_in_both_sample_ctes() -> None:
    cursor = BindingCheckedCursor(header_rows=[dict(_HEADER_ROW)])

    _run_fallback(cursor)

    _header_sql, heavy_sql = _fallback_statements(cursor)
    station_cte = heavy_sql[heavy_sql.index("station_sample_rows AS") : heavy_sql.index("river_sample_rows AS")]
    river_cte = heavy_sql[heavy_sql.index("river_sample_rows AS") :]
    assert "(%(scan_forcing_version_id)s IS NULL" in station_cte
    assert "fst.forcing_version_id = %(scan_forcing_version_id)s" in station_cte
    assert "fst.basin_version_id = %(scan_basin_version_id)s" in station_cte
    assert "LOWER(fst.source_id) = %(scan_source_id_lower)s" in station_cte
    assert "fst.valid_time >= %(scan_display_start)s" in station_cte
    assert "fst.valid_time <= %(scan_display_end)s" in station_cte
    # The river leg filters on the surrogate keys since #1442. Each scan_* guard
    # keeps its text binding — resolved to a key by an authority sub-select — so
    # a NULL binding still folds the guard away and an unknown text value still
    # yields the empty scan. run_id / river_network_version_id additionally keep
    # their text conjunct as the transitional compressed-chunk pushdown aid;
    # basin_version_id does not (it is not a segmentby column).
    #
    # Pinned WHOLE-GUARD rather than as separate "the escape is somewhere" /
    # "the predicate is somewhere" assertions: split that way, an escape in one
    # guard and a predicate in another satisfy the pair, and the fold-away
    # property this suite exists for could be lost with nothing red. The
    # comparison runs on `outer_predicates` — key-resolution sub-selects
    # stripped (hence the `= )` tails), comments removed, whitespace collapsed —
    # so re-indenting the CTE does not break it.
    river_outer = outer_predicates(river_cte)
    assert "AND (%(scan_run_id)s IS NULL OR (rt.run_id = %(scan_run_id)s AND rt.run_key = ))" in river_outer
    assert (
        "AND (%(scan_river_network_version_id)s IS NULL "
        "OR (rt.river_network_version_id = %(scan_river_network_version_id)s "
        "AND rt.river_network_version_key = ))"
    ) in river_outer
    assert "AND (%(scan_basin_version_id)s IS NULL OR rt.basin_version_key = )" in river_outer
    assert "AND (%(scan_display_start)s IS NULL OR rt.valid_time >= %(scan_display_start)s)" in river_outer
    assert "AND (%(scan_display_end)s IS NULL OR rt.valid_time <= %(scan_display_end)s)" in river_outer
    # basin_version_id is resolved, never compared as a fact predicate.
    assert "rt.basin_version_id = %(scan_basin_version_id)s" not in river_cte
    # Non-vacuity for the tails the stripper removed.
    assert "rt.run_key = (SELECT run_key FROM hydro.hydro_run" in river_cte
    assert "rt.basin_version_key = (SELECT basin_version_key FROM core.basin_version" in river_cte
    assert "rt.river_network_version_key = (SELECT river_network_version_key" in river_cte


def test_heavy_statement_pins_candidate_runs_to_the_header_run_id() -> None:
    cursor = BindingCheckedCursor(header_rows=[dict(_HEADER_ROW)])

    _run_fallback(cursor)

    header_sql, heavy_sql = _fallback_statements(cursor)
    candidate_cte = heavy_sql[: heavy_sql.index("station_sample_rows AS")]
    assert "AND h.run_id = %(scan_run_id)s" in candidate_cte
    # The header cannot pin itself: it is what resolves the run_id.
    assert "AND h.run_id = %(scan_run_id)s" not in header_sql


def test_header_scalars_are_bound_as_the_heavy_statement_scan_parameters() -> None:
    cursor = BindingCheckedCursor(header_rows=[dict(_HEADER_ROW)])

    _run_fallback(cursor)

    _statement, parameters = cursor.executed[-1]
    assert parameters["scan_run_id"] == _HEADER_ROW["run_id"]
    assert parameters["scan_forcing_version_id"] == _HEADER_ROW["forcing_version_id"]
    assert parameters["scan_basin_version_id"] == _HEADER_ROW["basin_version_id"]
    assert parameters["scan_river_network_version_id"] == _HEADER_ROW["river_network_version_id"]
    assert parameters["scan_source_id_lower"] == _HEADER_ROW["source_id_lower"]
    assert parameters["scan_display_start"] == _HEADER_ROW["display_start_time"]
    assert parameters["scan_display_end"] == _HEADER_ROW["display_end_time"]
    assert parameters["basin_id"] == "basins_qhh"
    assert parameters["source_id"] == "GFS"
    assert parameters["candidate_limit"] == QHH_LATEST_SEARCH_LIMIT


def test_empty_header_returns_no_rows_without_executing_the_heavy_statement() -> None:
    cursor = BindingCheckedCursor(header_rows=[])

    rows = _run_fallback(cursor)

    assert rows == []
    statements = _fallback_statements(cursor)
    assert len(statements) == 1
    assert "station_sample_rows" not in statements[0]


def test_strict_identity_binds_named_parameters_through_the_shared_template() -> None:
    identity = forecast_store._QhhLatestStrictIdentity(
        source_id="GFS",
        run_id="qhh_gfs_2026050700",
        cycle_time=datetime(2026, 5, 7, tzinfo=UTC),
        model_id="basins_qhh_shud",
    )
    cursor = BindingCheckedCursor(header_rows=[dict(_HEADER_ROW)])

    _run_fallback(cursor, identity=identity)

    for statement, parameters in cursor.executed[1:]:
        assert "AND h.run_id = %(identity_run_id)s" in statement
        assert "AND h.cycle_time = %(identity_cycle_time)s" in statement
        assert "AND h.model_id = %(identity_model_id)s" in statement
        assert parameters["identity_run_id"] == "qhh_gfs_2026050700"
        assert parameters["identity_cycle_time"] == identity.cycle_time
        assert parameters["identity_model_id"] == "basins_qhh_shud"
        assert parameters["candidate_limit"] == 1


def test_fallback_rows_are_returned_from_the_heavy_statement() -> None:
    cursor = BindingCheckedCursor(header_rows=[dict(_HEADER_ROW)])

    rows = _run_fallback(cursor)

    assert rows == [dict(_CANDIDATE_ROW)]


def test_fast_path_and_unavailable_context_keep_positional_parameters() -> None:
    fast_cursor = BindingCheckedCursor(header_rows=[dict(_HEADER_ROW)], coverage_table=True)
    _store()._fetch_latest_qhh_display_candidates_fast(
        fast_cursor,
        basin_id="basins_qhh",
        source_id="GFS",
        identity_sql="",
        identity_params=(),
        candidate_limit=QHH_LATEST_SEARCH_LIMIT,
    )
    context_cursor = BindingCheckedCursor(header_rows=[])
    _store()._fetch_latest_qhh_display_unavailable_context(
        context_cursor,
        basin_id="basins_qhh",
        source_id="GFS",
    )

    for cursor in (fast_cursor, context_cursor):
        statement, parameters = cursor.executed[-1]
        assert isinstance(parameters, tuple)
        assert not _NAMED_PLACEHOLDER.findall(statement)


def test_binding_checked_cursor_rejects_a_mapping_degraded_to_a_tuple() -> None:
    # Guards the guard: without this, the fake cursor could pass anything and
    # the fallback tests above would prove nothing about parameter binding.
    cursor = BindingCheckedCursor(header_rows=[])

    with pytest.raises(AssertionError, match="named placeholders"):
        cursor.execute("SELECT %(basin_id)s", ("basin_id",))
    with pytest.raises(AssertionError, match="positional placeholders"):
        cursor.execute("SELECT %s", {"basin_id": "basins_qhh"})


def test_candidate_limit_invariant_keeps_run_id_pinning_sound() -> None:
    assert QHH_LATEST_SEARCH_LIMIT == 1, (
        "The fallback pins its heavy statement to ONE header run_id "
        "(display-coverage-residual-debt design D1.5). Raising "
        "QHH_LATEST_SEARCH_LIMIT would silently truncate every extra candidate: "
        "redesign the fallback as a per-candidate loop first."
    )
    assert QHH_LATEST_CANDIDATE_LIMIT == QHH_LATEST_SEARCH_LIMIT
