"""#2516 D3 on a real PostgreSQL + TimescaleDB: the fenced narrow station legs.

Both narrow station legs -- the QHH latest-product one
(``forecast_store._LATEST_PRODUCT_STATION_SOURCE_TEMPLATES``) and the
display-coverage one (``display_coverage._STATION_SAMPLE_ROWS_TEMPLATES``) -- are
executed standalone under one literal ``candidate_runs`` row, next to master's
frozen narrow leg (``tests/station_membership_fence_oracle.py``), over seeded
narrow forcing rows: two stations with fact rows for all six MVP variables, and
``met.interp_weight`` membership for FOUR variables per station (different
sets), so the membership ``EXISTS`` really removes rows and a probe that does
not seek on ``variable`` sees four index entries per (model, station, source).
The expected row counts are worked out by hand from that seed.

The plan test asks the seeded planner whether the fence turns the probe into a
correlated SubPlan whose ``interp_weight`` Index Cond includes ``variable``; see
its docstring for exactly what it proves and what stays node-27's 3.4 evidence.

Run against a disposable database (never production), e.g. node-27's or a local
PG 15 / TimescaleDB 2.10 container:

    mkdir -p /home/nwm/tmp && export TMPDIR=/home/nwm/tmp
    NHMS_RUN_INTEGRATION=1 NHMS_INTEGRATION_DATABASE_URL=... \\
        uv run pytest -q tests/test_station_membership_fence_integration.py

SILENT-SKIP TRAP: without both variables every case skips; read the passed count.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from typing import Any

import psycopg2
import psycopg2.errors
import pytest
from psycopg2.extras import RealDictCursor, execute_values

from packages.common import display_coverage, forecast_store
from packages.common.forcing_store_routing import FORCING_NARROW_INSERT_TEMPLATE
from tests.integration_helpers import (
    BASIN_VERSION_ID,
    FORCING_VERSION_ID,
    ISSUE_126_PREFIX,
    MODEL_ID,
    SOURCE_ID,
    VALID_TIME_1,
    VALID_TIME_2,
    apply_migrations_from_zero,
    seed_issue_126_data,
)
from tests.station_membership_fence_oracle import (
    pre_2516_leg,
    rendered_narrow_leg,
    standalone_leg_parameters,
    standalone_leg_statement,
)

pytestmark = pytest.mark.integration

STATION_A = f"{ISSUE_126_PREFIX}_fence_station_a"
STATION_B = f"{ISSUE_126_PREFIX}_fence_station_b"
#: Membership: four interpolation variables per station, three shared; no station
#: has ``Press``, so its fact rows are all filtered out.
MEMBERSHIP = {
    STATION_A: ("PRCP", "TEMP", "RH", "wind"),
    STATION_B: ("PRCP", "TEMP", "Rn", "wind"),
}
UNITS = {"PRCP": "mm/day", "TEMP": "degC", "RH": "0-1", "wind": "m/s", "Rn": "W/m2", "Press": "Pa"}
MVP = list(forecast_store.MVP_STATION_VARIABLES)

NEW_LEGS = {
    "forecast_store.latest_product_station_source": forecast_store._LATEST_PRODUCT_STATION_SOURCE_TEMPLATES,
    "display_coverage.station_sample_rows": display_coverage._STATION_SAMPLE_ROWS_TEMPLATES,
}


def _connect(database_url: str) -> Any:
    connection = psycopg2.connect(database_url, cursor_factory=RealDictCursor)
    connection.autocommit = True
    return connection


def _seed(database_url: str) -> None:
    apply_migrations_from_zero(database_url)
    seed_issue_126_data(database_url)
    connection = _connect(database_url)
    try:
        with connection.cursor() as cursor:
            for index, station_id in enumerate(MEMBERSHIP):
                cursor.execute(
                    """
                    INSERT INTO met.met_station (station_id, basin_version_id, station_name, geom, elevation_m)
                    VALUES (%s, %s, %s, ST_SetSRID(ST_MakePoint(%s, %s), 4490), 400.0)
                    """,
                    (station_id, BASIN_VERSION_ID, f"#2516 fence station {index}", 110.1 + index / 10, 30.1),
                )
            execute_values(
                cursor,
                """
                INSERT INTO met.interp_weight (
                    source_id, grid_id, model_id, station_id, variable, grid_cell_id, weight, method
                )
                VALUES %s
                """,
                [
                    (SOURCE_ID, "it126_grid", MODEL_ID, station_id, variable, "it126_cell", 1.0, "idw")
                    for station_id, variables in MEMBERSHIP.items()
                    for variable in variables
                ],
            )
            cursor.execute(
                "SELECT forcing_version_key FROM met.forcing_version WHERE forcing_version_id = %s",
                (FORCING_VERSION_ID,),
            )
            forcing_version_key = cursor.fetchone()["forcing_version_key"]
            cursor.execute(
                "SELECT station_id, station_key FROM met.met_station WHERE station_id = ANY(%s)",
                (list(MEMBERSHIP),),
            )
            station_keys = {row["station_id"]: row["station_key"] for row in cursor.fetchall()}
            # ALL six variables have fact rows on BOTH stations; membership alone
            # decides which survive.
            execute_values(
                cursor,
                """
                INSERT INTO met.forcing_station_timeseries (
                    forcing_version_key, station_key, valid_time, variable_e, value,
                    unit_e, quality_flag_e, native_resolution
                )
                VALUES %s
                """,
                [
                    (
                        forcing_version_key,
                        station_keys[station_id],
                        valid_time,
                        variable,
                        float(10 * index + hour),
                        UNITS[variable],
                        "ok",
                        "1h",
                    )
                    for station_id in MEMBERSHIP
                    for index, variable in enumerate(MVP)
                    for hour, valid_time in enumerate((VALID_TIME_1, VALID_TIME_2))
                ],
                template=FORCING_NARROW_INSERT_TEMPLATE,
            )
    finally:
        connection.close()


def _rows(connection: Any, leg_sql: str, *, variables: list[str], scan_pinned: bool) -> list[dict[str, Any]]:
    with connection.cursor() as cursor:
        cursor.execute(
            standalone_leg_statement(leg_sql),
            standalone_leg_parameters(
                variables=variables,
                run_id=f"{ISSUE_126_PREFIX}_forecast_run",
                model_id=MODEL_ID,
                forcing_version_id=FORCING_VERSION_ID,
                basin_version_id=BASIN_VERSION_ID,
                source_id=SOURCE_ID.upper(),
                display_start_time=VALID_TIME_1,
                display_end_time=VALID_TIME_2,
                scan_pinned=scan_pinned,
            ),
        )
        return [dict(row) for row in cursor.fetchall()]


def _explain(connection: Any, leg_sql: str, *, scan_pinned: bool) -> dict[str, Any]:
    with connection.cursor() as cursor:
        cursor.execute(
            "EXPLAIN (ANALYZE, BUFFERS, FORMAT JSON) " + standalone_leg_statement(leg_sql),
            standalone_leg_parameters(
                variables=MVP,
                run_id=f"{ISSUE_126_PREFIX}_forecast_run",
                model_id=MODEL_ID,
                forcing_version_id=FORCING_VERSION_ID,
                basin_version_id=BASIN_VERSION_ID,
                source_id=SOURCE_ID.upper(),
                display_start_time=VALID_TIME_1,
                display_end_time=VALID_TIME_2,
                scan_pinned=scan_pinned,
            ),
        )
        return cursor.fetchone()["QUERY PLAN"][0]["Plan"]


def _walk(node: dict[str, Any]) -> Iterator[dict[str, Any]]:
    yield node
    for child in node.get("Plans") or []:
        yield from _walk(child)


def _interp_weight_scans(plan: dict[str, Any]) -> list[dict[str, Any]]:
    return [node for node in _walk(plan) if node.get("Relation Name") == "interp_weight"]


def _assert_variable_seeking_subplan(plan: dict[str, Any]) -> None:
    """The membership probe is a correlated SubPlan seeking on all four index columns."""
    (scan,) = _interp_weight_scans(plan)
    summary = json.dumps({key: scan.get(key) for key in ("Node Type", "Parent Relationship", "Index Cond")})
    assert scan.get("Parent Relationship") == "SubPlan", summary
    assert scan.get("Index Name") == "interp_weight_qhh_latest_membership_idx", summary
    index_cond = scan.get("Index Cond") or ""
    assert "(variable = (fst.variable_e)::text)" in index_cond, summary
    assert "(station_id = ms.station_id)" in index_cond, summary
    assert "lower(source_id)" in index_cond, summary
    assert "model_id" in index_cond, summary
    # Nothing is re-checked after the seek (four membership entries per model,
    # station and source; the variable picks one), and the probe runs once per
    # joined fact row (two stations x six variables x two valid times).
    assert not scan.get("Filter"), summary
    assert scan["Actual Rows"] <= 1, summary
    assert scan["Actual Loops"] == len(MEMBERSHIP) * len(MVP) * 2, summary


def test_both_fenced_narrow_legs_return_the_pre_change_rows(throwaway_database_url: str) -> None:
    """tasks.md 2.2: old (master narrow) == new (fenced) on both legs, rows hand-counted."""
    _seed(throwaway_database_url)
    connection = _connect(throwaway_database_url)
    try:
        old_leg = pre_2516_leg()
        failures = []
        #: (bound variables, expected row count from MEMBERSHIP x two valid times)
        cases = (
            (MVP, (4 + 4) * 2),
            (["PRCP"], (1 + 1) * 2),
            (["RH", "Rn"], (1 + 1) * 2),
            (["wind"], (1 + 1) * 2),
            (["Press"], 0),
            (["RH", "RH"], (1 + 0) * 2),
            (MVP + ["PRCP", "TEMP"], (4 + 4) * 2),
        )
        for scan_pinned in (False, True):
            for variables, expected_count in cases:
                old = _rows(connection, old_leg, variables=variables, scan_pinned=scan_pinned)
                where = (variables, scan_pinned)
                if len(old) != expected_count:
                    failures.append(f"old leg {where}: {len(old)} rows, expected {expected_count}")
                for key, pair in NEW_LEGS.items():
                    new = _rows(connection, rendered_narrow_leg(pair), variables=variables, scan_pinned=scan_pinned)
                    if new != old:
                        failures.append(f"{key} {where}: {len(new)} rows differ from the old leg's {len(old)}")
        assert not failures, "\n".join(failures)
        # Non-vacuity: the membership EXISTS really filtered (16 of the 24 fact
        # rows in window survive the full list), and the rows are the members'.
        for key, pair in NEW_LEGS.items():
            survivors = _rows(connection, rendered_narrow_leg(pair), variables=MVP, scan_pinned=False)
            assert {(row["station_id"], row["variable"]) for row in survivors} == {
                (station_id, variable) for station_id, variables in MEMBERSHIP.items() for variable in variables
            }, key
    finally:
        connection.close()


def test_the_fence_makes_the_membership_probe_seek_on_variable(throwaway_database_url: str) -> None:
    """tasks.md 2.2 plan test -- the seeded planner DOES reproduce it (PG 15.2 / TimescaleDB 2.10.2).

    What this proves on the seed: master's narrow leg lets the planner pull the
    EXISTS up into a join (the ``interp_weight`` scan is not a SubPlan and its
    Index Cond has no ``variable``), while the fenced legs run it as a correlated
    SubPlan whose Index Cond on ``interp_weight_qhh_latest_membership_idx``
    carries ``variable = (fst.variable_e)::text`` and ``station_id``, one row per
    loop. The seed is tiny, so a sequential scan is cheaper than any index; the
    session disables seqscan so the question asked is which index conditions the
    planner CAN use, not which access path is cheapest on eight rows. The
    buffer win itself (node 326648 -> 275464 hit, statement 517524 -> 466284)
    is node-27's 3.4 evidence, not this seed's.
    """
    _seed(throwaway_database_url)
    connection = _connect(throwaway_database_url)
    try:
        with connection.cursor() as cursor:
            cursor.execute("ANALYZE met.interp_weight")
            cursor.execute("ANALYZE met.met_station")
            cursor.execute("ANALYZE met.forcing_version")
            cursor.execute("SET enable_seqscan = off")
        for scan_pinned in (False, True):
            old_plan = _explain(connection, pre_2516_leg(), scan_pinned=scan_pinned)
            (old_scan,) = _interp_weight_scans(old_plan)
            assert old_scan.get("Parent Relationship") != "SubPlan", old_scan
            assert "variable" not in (old_scan.get("Index Cond") or ""), old_scan
            # The assertion below bites: master's plan fails it.
            with pytest.raises(AssertionError):
                _assert_variable_seeking_subplan(old_plan)
            for pair in NEW_LEGS.values():
                new_plan = _explain(connection, rendered_narrow_leg(pair), scan_pinned=scan_pinned)
                _assert_variable_seeking_subplan(new_plan)
    finally:
        connection.close()


def test_an_unknown_bound_variable_raises_the_same_enum_error_as_before(throwaway_database_url: str) -> None:
    """The fence changes no cast: an unknown variable is still 22P02 on every leg."""
    _seed(throwaway_database_url)
    connection = _connect(throwaway_database_url)
    try:
        legs = {"pre_2516": pre_2516_leg(), **{key: rendered_narrow_leg(pair) for key, pair in NEW_LEGS.items()}}
        codes = {}
        for key, leg_sql in legs.items():
            with pytest.raises(psycopg2.errors.InvalidTextRepresentation) as raised:
                _rows(connection, leg_sql, variables=["PRCP", "not_a_forcing_variable"], scan_pinned=False)
            codes[key] = (type(raised.value), raised.value.pgcode)
        assert set(codes.values()) == {(psycopg2.errors.InvalidTextRepresentation, "22P02")}, codes
        # The fact side really was non-empty for this pin.
        assert _rows(connection, legs["pre_2516"], variables=["PRCP"], scan_pinned=False)
    finally:
        connection.close()
