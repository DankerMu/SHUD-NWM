"""#2694 / #2699 on a real PostgreSQL: the met-station inventory, both branches.

``PsycopgForecastStore.list_met_stations`` used to JOIN ``met.met_station`` to
``met.interp_weight`` and DISTINCT the result. It now filters
``met.met_station`` by membership in an uncorrelated scalar-array subquery. Two
things are proved here that the recording-cursor suite
(``tests/test_list_search_contract.py``) cannot:

* RESULT EQUIVALENCE. Every case runs the store against seeded rows and
  compares ids, order and ``total_count`` with (a) the pre-change JOIN+DISTINCT
  statement, frozen below as a literal, and (b) the answer worked out by hand
  from ``STATIONS`` -- so the two forms agreeing on a wrong answer still fails.
* PLAN SHAPE. ``met.interp_weight`` is reached only inside InitPlans, i.e. it
  is scanned once per statement and never on the inner side of a join. That is
  the property that makes the cost independent of stale row estimates; it does
  NOT reproduce node-27's 84 s plan (that needs node-27's statistics and stays
  a deferred receipt).

#2699 changed the other branch (``basin_version_id`` without ``model_id``): it
lists the stations of the model used by the basin version's latest displayable
forecast run instead of the ``active_flag`` ones. There is no pre-change form to
compare with -- the old answer is the bug -- so ``BASIN_CASES`` are checked
against the hand-worked answer only, each under its own set of
``hydro.hydro_run`` rows, and the plan-shape test gains the basin-only
statements (``hydro.hydro_run`` is reached only inside an InitPlan as well).

Run against a disposable database (never production), e.g. a local
``timescale/timescaledb-ha:pg15-latest`` container:

    NHMS_RUN_INTEGRATION=1 NHMS_INTEGRATION_DATABASE_URL=... \\
        uv run pytest -q -rs tests/test_met_station_model_filter_integration.py

SILENT-SKIP TRAP: without both variables every case skips; read the passed count.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from datetime import timedelta
from typing import Any

import psycopg2
import pytest
from psycopg2.extras import Json, RealDictCursor, execute_values

from packages.common.forecast_store import PsycopgForecastStore, _escape_like
from tests.integration_helpers import (
    BASIN_ID,
    BASIN_VERSION_ID,
    CYCLE_TIME,
    ISSUE_126_PREFIX,
    MESH_VERSION_ID,
    MODEL_ID,
    RIVER_NETWORK_VERSION_ID,
    SOURCE_ID,
    apply_migrations_from_zero,
    seed_issue_126_data,
)

pytestmark = pytest.mark.integration

PREFIX = f"{ISSUE_126_PREFIX}_2694"
OTHER_BASIN_VERSION_ID = f"{PREFIX}_other_basin_v1"
#: ``MODEL_ID`` is model A (seeded by ``seed_issue_126_data``).
MODEL_B = f"{PREFIX}_model_b"
MODEL_WITHOUT_WEIGHTS = f"{PREFIX}_model_without_weights"

S1_ALL_VARIABLES = f"{PREFIX}_s1_all_variables"
S2_SHARED = f"{PREFIX}_s2_shared_by_two_models"
S3_DUPLICATE_WEIGHTS = f"{PREFIX}_s3_duplicate_weights"
S4_OTHER_BASIN = f"{PREFIX}_s4_other_basin"
S5_MODEL_B_ONLY = f"{PREFIX}_s5_model_b_only"
S6_NO_WEIGHTS = f"{PREFIX}_s6_no_weights"
S7_INACTIVE_ONE_VARIABLE = f"{PREFIX}_s7_inactive_one_variable"

#: station -> (basin version, active_flag, {model: variables with a weight row}).
#: Every (model, station, variable) below gets ``WEIGHT_ROWS_PER_VARIABLE`` rows
#: (distinct grid cells), so every member station has duplicate weight rows and
#: the old form really needed its DISTINCT.
STATIONS: dict[str, tuple[str, bool, dict[str, tuple[str, ...]]]] = {
    S1_ALL_VARIABLES: (BASIN_VERSION_ID, True, {MODEL_ID: ("PRCP", "TEMP", "RH")}),
    # RH only under model B: a coverage filter that leaked across models would
    # count three variables for model A here.
    S2_SHARED: (BASIN_VERSION_ID, True, {MODEL_ID: ("PRCP", "TEMP"), MODEL_B: ("PRCP", "TEMP", "RH")}),
    S3_DUPLICATE_WEIGHTS: (BASIN_VERSION_ID, True, {MODEL_ID: ("PRCP", "TEMP")}),
    S4_OTHER_BASIN: (OTHER_BASIN_VERSION_ID, True, {MODEL_ID: ("PRCP", "TEMP", "RH")}),
    S5_MODEL_B_ONLY: (BASIN_VERSION_ID, True, {MODEL_B: ("PRCP", "TEMP", "RH")}),
    S6_NO_WEIGHTS: (BASIN_VERSION_ID, True, {}),
    # The model branch ignores active_flag (direct-grid variants stay inactive).
    S7_INACTIVE_ONE_VARIABLE: (BASIN_VERSION_ID, False, {MODEL_ID: ("PRCP",)}),
}
WEIGHT_ROWS_PER_VARIABLE = 3

#: (case id, list_met_stations filters, expected page ids in order, expected total_count).
CASES: tuple[tuple[str, dict[str, Any], list[str], int], ...] = (
    (
        "model and basin",
        {"model_id": MODEL_ID, "basin_version_id": BASIN_VERSION_ID},
        [S1_ALL_VARIABLES, S2_SHARED, S3_DUPLICATE_WEIGHTS, S7_INACTIVE_ONE_VARIABLE],
        4,
    ),
    (
        "model only spans basins",
        {"model_id": MODEL_ID, "basin_version_id": None},
        [S1_ALL_VARIABLES, S2_SHARED, S3_DUPLICATE_WEIGHTS, S4_OTHER_BASIN, S7_INACTIVE_ONE_VARIABLE],
        5,
    ),
    (
        "model only, limit 1 (the readonly route smoke call)",
        {"model_id": MODEL_ID, "basin_version_id": None, "limit": 1},
        [S1_ALL_VARIABLES],
        5,
    ),
    (
        "second model sees the shared station",
        {"model_id": MODEL_B, "basin_version_id": BASIN_VERSION_ID},
        [S2_SHARED, S5_MODEL_B_ONLY],
        2,
    ),
    (
        "model in a basin where it has one station",
        {"model_id": MODEL_ID, "basin_version_id": OTHER_BASIN_VERSION_ID},
        [S4_OTHER_BASIN],
        1,
    ),
    ("model without weights", {"model_id": MODEL_WITHOUT_WEIGHTS, "basin_version_id": BASIN_VERSION_ID}, [], 0),
    ("model without weights, no basin", {"model_id": MODEL_WITHOUT_WEIGHTS, "basin_version_id": None}, [], 0),
    ("unknown model", {"model_id": f"{PREFIX}_no_such_model", "basin_version_id": BASIN_VERSION_ID}, [], 0),
    (
        "pagination window",
        {"model_id": MODEL_ID, "basin_version_id": BASIN_VERSION_ID, "limit": 2, "offset": 1},
        [S2_SHARED, S3_DUPLICATE_WEIGHTS],
        4,
    ),
    (
        "offset past the end",
        {"model_id": MODEL_ID, "basin_version_id": BASIN_VERSION_ID, "offset": 4},
        [],
        4,
    ),
    (
        "two-variable coverage drops the station carrying one of them",
        {"model_id": MODEL_ID, "basin_version_id": BASIN_VERSION_ID, "variables": "PRCP,TEMP"},
        [S1_ALL_VARIABLES, S2_SHARED, S3_DUPLICATE_WEIGHTS],
        3,
    ),
    (
        "three-variable coverage does not borrow another model's variable",
        {"model_id": MODEL_ID, "basin_version_id": BASIN_VERSION_ID, "variables": "PRCP,TEMP,RH"},
        [S1_ALL_VARIABLES],
        1,
    ),
    (
        "three-variable coverage, model only",
        {"model_id": MODEL_ID, "basin_version_id": None, "variables": "PRCP,TEMP,RH"},
        [S1_ALL_VARIABLES, S4_OTHER_BASIN],
        2,
    ),
    (
        "coverage nobody satisfies",
        {"model_id": MODEL_ID, "basin_version_id": BASIN_VERSION_ID, "variables": "wind"},
        [],
        0,
    ),
    (
        "coverage on a model without weights",
        {"model_id": MODEL_WITHOUT_WEIGHTS, "basin_version_id": BASIN_VERSION_ID, "variables": "PRCP"},
        [],
        0,
    ),
    (
        "search on station id",
        {"model_id": MODEL_ID, "basin_version_id": BASIN_VERSION_ID, "search": "shared_by"},
        [S2_SHARED],
        1,
    ),
    (
        "search and coverage together",
        {"model_id": MODEL_ID, "basin_version_id": None, "search": "_s", "variables": "PRCP,TEMP,RH", "limit": 1},
        [S1_ALL_VARIABLES],
        2,
    ),
)

#: What the basin-only branch returns while model A / model B is the model of
#: the latest displayable run of ``BASIN_VERSION_ID`` -- worked out by hand from
#: ``STATIONS``. Model A's list has the INACTIVE S7 and lacks the active S5
#: (other model), S6 (no model) and S4 (model A, other basin version).
MODEL_A_STATIONS = [S1_ALL_VARIABLES, S2_SHARED, S3_DUPLICATE_WEIGHTS, S7_INACTIVE_ONE_VARIABLE]
MODEL_B_STATIONS = [S2_SHARED, S5_MODEL_B_ONLY]

#: A run added for one basin-only case:
#: (run id suffix, model, hours after the seeded run's cycle or None for a NULL
#: cycle_time, status, run_type, basin version).
#: ``seed_issue_126_data`` already holds one ``parsed`` forecast run of
#: ``MODEL_ID`` / ``BASIN_VERSION_ID`` at ``CYCLE_TIME`` (plus a 2025 hindcast),
#: so with no extra run model A is current; ``OTHER_BASIN_VERSION_ID`` has none.
_Run = tuple[str, str, int | None, str, str, str]


def _run(
    suffix: str,
    model_id: str,
    hours: int | None,
    status: str = "succeeded",
    run_type: str = "forecast",
    basin_version_id: str = BASIN_VERSION_ID,
) -> _Run:
    return (suffix, model_id, hours, status, run_type, basin_version_id)


#: (case id, extra runs, list_met_stations filters, expected page ids in order, expected total_count).
#: ``basin_version_id`` defaults to ``BASIN_VERSION_ID``; ``model_id`` is always None.
BASIN_CASES: tuple[tuple[str, tuple[_Run, ...], dict[str, Any], list[str], int], ...] = (
    ("seeded parsed run only: model A, whatever active_flag says", (), {}, MODEL_A_STATIONS, 4),
    ("newer succeeded run of model B wins", (_run("newer", MODEL_B, 6),), {}, MODEL_B_STATIONS, 2),
    ("newer published run of model B wins", (_run("newer", MODEL_B, 6, "published"),), {}, MODEL_B_STATIONS, 2),
    ("older run of model B loses", (_run("older", MODEL_B, -6),), {}, MODEL_A_STATIONS, 4),
    # The pair of run ids differs only in the trailing lowercase letter, so the
    # order does not depend on the database collation.
    (
        "equal cycle_time: model B holds the higher run_id",
        (_run("tie_a", MODEL_ID, 6), _run("tie_b", MODEL_B, 6)),
        {},
        MODEL_B_STATIONS,
        2,
    ),
    (
        "equal cycle_time: model A holds the higher run_id",
        (_run("tie_a", MODEL_B, 6), _run("tie_b", MODEL_ID, 6)),
        {},
        MODEL_A_STATIONS,
        4,
    ),
    ("newer failed run is ignored", (_run("newer", MODEL_B, 6, "failed"),), {}, MODEL_A_STATIONS, 4),
    ("newer superseded run is ignored", (_run("newer", MODEL_B, 6, "superseded"),), {}, MODEL_A_STATIONS, 4),
    ("newer running run is ignored", (_run("newer", MODEL_B, 6, "running"),), {}, MODEL_A_STATIONS, 4),
    (
        "newer non-forecast runs are ignored",
        (_run("hindcast", MODEL_B, 6, "parsed", "hindcast"), _run("analysis", MODEL_B, 12, "succeeded", "analysis")),
        {},
        MODEL_A_STATIONS,
        4,
    ),
    # ORDER BY cycle_time DESC sorts NULL first: without the IS NOT NULL
    # predicate this run would be "the latest".
    ("a forecast run without cycle_time is ignored", (_run("no_cycle", MODEL_B, None),), {}, MODEL_A_STATIONS, 4),
    (
        "a newer run of model B in another basin version does not move this one",
        (_run("elsewhere", MODEL_B, 6, basin_version_id=OTHER_BASIN_VERSION_ID),),
        {},
        MODEL_A_STATIONS,
        4,
    ),
    # S4 is active and carries model A weights, in a basin version with no run.
    ("no forecast run at all: empty", (), {"basin_version_id": OTHER_BASIN_VERSION_ID}, [], 0),
    (
        "no displayable run: empty",
        (
            _run("failed", MODEL_ID, 6, "failed", basin_version_id=OTHER_BASIN_VERSION_ID),
            _run("hindcast", MODEL_ID, 6, "parsed", "hindcast", OTHER_BASIN_VERSION_ID),
        ),
        {"basin_version_id": OTHER_BASIN_VERSION_ID},
        [],
        0,
    ),
    (
        "the other basin version lists its own station of the same model, not this one's",
        (_run("elsewhere", MODEL_ID, 6, basin_version_id=OTHER_BASIN_VERSION_ID),),
        {"basin_version_id": OTHER_BASIN_VERSION_ID},
        [S4_OTHER_BASIN],
        1,
    ),
    (
        "the current model has no station in the basin version: empty",
        (_run("elsewhere", MODEL_WITHOUT_WEIGHTS, 6),),
        {},
        [],
        0,
    ),
    ("the variables filter is not applied without model_id", (), {"variables": "PRCP,TEMP,RH"}, MODEL_A_STATIONS, 4),
    ("search composes", (), {"search": "shared_by"}, [S2_SHARED], 1),
    (
        "search cannot bring back a station of the older model",
        (_run("newer", MODEL_B, 6),),
        {"search": "all_variables"},
        [],
        0,
    ),
    ("search under the newer model", (_run("newer", MODEL_B, 6),), {"search": "_s"}, MODEL_B_STATIONS, 2),
    ("pagination window", (), {"limit": 2, "offset": 1}, [S2_SHARED, S3_DUPLICATE_WEIGHTS], 4),
    ("pagination under the newer model", (_run("newer", MODEL_B, 6),), {"limit": 1, "offset": 1}, [S5_MODEL_B_ONLY], 2),
    ("offset past the end", (), {"offset": 4}, [], 4),
)


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
            cursor.execute(
                """
                INSERT INTO core.basin_version (
                    basin_version_id, basin_id, version_label, geom, active_flag, source_uri, checksum
                )
                VALUES (
                    %s, %s, 'v2694', ST_Multi(ST_MakeEnvelope(113.0, 29.0, 116.0, 32.0, 4490)),
                    false, 'integration://basin-2694', 'basin-2694-sha'
                )
                """,
                (OTHER_BASIN_VERSION_ID, BASIN_ID),
            )
            for model_id in (MODEL_B, MODEL_WITHOUT_WEIGHTS):
                # Inactive: one active model per basin version is a unique index.
                cursor.execute(
                    """
                    INSERT INTO core.model_instance (
                        model_id, basin_version_id, river_network_version_id, mesh_version_id,
                        calibration_version_id, shud_code_version, model_package_uri,
                        active_flag, lifecycle_state, resource_profile
                    )
                    VALUES (%s, %s, %s, %s, 'calib-v1', 'shud-v1', 's3://nhms/models/it2694/package/',
                            false, 'inactive', %s)
                    """,
                    (model_id, BASIN_VERSION_ID, RIVER_NETWORK_VERSION_ID, MESH_VERSION_ID, Json({})),
                )
            for index, (station_id, (basin_version_id, active_flag, _weights)) in enumerate(STATIONS.items()):
                cursor.execute(
                    """
                    INSERT INTO met.met_station (
                        station_id, basin_version_id, station_name, geom, elevation_m, active_flag
                    )
                    VALUES (%s, %s, %s, ST_SetSRID(ST_MakePoint(%s, %s), 4490), 400.0, %s)
                    """,
                    (station_id, basin_version_id, f"#2694 station {index}", 110.1 + index / 10, 30.1, active_flag),
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
                    (SOURCE_ID, "it2694_grid", model_id, station_id, variable, f"it2694_cell_{cell}", 1.0, "idw")
                    for station_id, (_basin, _active, weights) in STATIONS.items()
                    for model_id, variables in weights.items()
                    for variable in variables
                    for cell in range(WEIGHT_ROWS_PER_VARIABLE)
                ],
            )
    finally:
        connection.close()


def _insert_runs(connection: Any, runs: tuple[_Run, ...]) -> list[str]:
    """Add forecast-run rows for one basin-only case; returns their run ids."""
    rows = [
        (
            f"{PREFIX}_run_{suffix}",
            run_type,
            "it2694_scenario",
            model_id,
            basin_version_id,
            SOURCE_ID,
            None if hours is None else CYCLE_TIME + timedelta(hours=hours),
            CYCLE_TIME,
            CYCLE_TIME + timedelta(hours=1),
            status,
            "s3://nhms/runs/it2694/input/manifest.json",
        )
        for suffix, model_id, hours, status, run_type, basin_version_id in runs
    ]
    if rows:
        with connection.cursor() as cursor:
            execute_values(
                cursor,
                """
                INSERT INTO hydro.hydro_run (
                    run_id, run_type, scenario_id, model_id, basin_version_id, source_id,
                    cycle_time, start_time, end_time, status, run_manifest_uri
                )
                VALUES %s
                """,
                rows,
            )
    return [row[0] for row in rows]


def _delete_runs(connection: Any, run_ids: list[str]) -> None:
    # Only the rows a case added: the seeded runs are referenced by other tables.
    with connection.cursor() as cursor:
        cursor.execute("DELETE FROM hydro.hydro_run WHERE run_id = ANY(%s)", (run_ids,))


def _pre_2694_rows(connection: Any, filters: dict[str, Any]) -> tuple[list[str], int]:
    """The JOIN + DISTINCT statements as they were before #2694, frozen here.

    Copied from ``packages/common/forecast_store.py`` at ``5372c7c2d`` (the
    ``model_id`` branch of ``list_met_stations``); only the SELECT list is cut
    down to ``station_id``. Do not "tidy" it towards the new form: it is the
    oracle.
    """
    from_sql = """
        FROM met.met_station ms
        JOIN met.interp_weight iw ON iw.station_id = ms.station_id
    """
    clauses = ["iw.model_id = %s"]
    params: list[Any] = [filters["model_id"]]
    if filters["basin_version_id"] is not None:
        clauses.append("ms.basin_version_id = %s")
        params.append(filters["basin_version_id"])
    search = (filters.get("search") or "").strip()
    if search:
        like_pattern = f"%{_escape_like(search)}%"
        clauses.append("(ms.station_id ILIKE %s ESCAPE '\\' OR COALESCE(ms.station_name, '') ILIKE %s ESCAPE '\\')")
        params.extend([like_pattern, like_pattern])
    variables = [token for token in (filters.get("variables") or "").split(",") if token]
    if variables:
        clauses.append(
            "ms.station_id IN ("
            "SELECT station_id FROM met.interp_weight "
            "WHERE model_id = %s AND variable = ANY(%s) "
            "GROUP BY station_id "
            "HAVING COUNT(DISTINCT variable) = %s)"
        )
        params.extend([filters["model_id"], variables, len(variables)])
    where = f"WHERE {' AND '.join(clauses)}"
    with connection.cursor() as cursor:
        cursor.execute(f"SELECT COUNT(DISTINCT ms.station_id) AS total_count {from_sql} {where}", tuple(params))
        total_count = int(cursor.fetchone()["total_count"])
        cursor.execute(
            f"SELECT DISTINCT ms.station_id {from_sql} {where} ORDER BY ms.station_id LIMIT %s OFFSET %s",
            (*params, filters.get("limit", 500), filters.get("offset", 0)),
        )
        return [row["station_id"] for row in cursor.fetchall()], total_count


def _list(store: PsycopgForecastStore, filters: dict[str, Any]) -> dict[str, Any]:
    return store.list_met_stations(**{"limit": 500, "offset": 0, **filters})


def test_model_filter_returns_the_pre_change_stations_in_order(throwaway_database_url: str) -> None:
    """Old JOIN+DISTINCT == new InitPlan-array == the hand-worked answer, per case."""
    _seed(throwaway_database_url)
    store = PsycopgForecastStore(throwaway_database_url)
    connection = _connect(throwaway_database_url)
    failures = []
    try:
        for case_id, filters, expected_ids, expected_total in CASES:
            result = _list(store, filters)
            new = ([item["station_id"] for item in result["items"]], result["total_count"])
            old = _pre_2694_rows(connection, filters)
            if old != (expected_ids, expected_total):
                failures.append(f"{case_id}: the pre-change form returned {old}, seed expects {expected_ids}")
            if new != (expected_ids, expected_total):
                failures.append(f"{case_id}: the store returned {new}, expected {(expected_ids, expected_total)}")
    finally:
        connection.close()
    assert not failures, "\n".join(failures)


def test_basin_list_is_the_stations_of_the_latest_displayable_run(throwaway_database_url: str) -> None:
    """#2699: the basin-only branch against the hand-worked answer, per set of runs."""
    _seed(throwaway_database_url)
    store = PsycopgForecastStore(throwaway_database_url)
    connection = _connect(throwaway_database_url)
    failures = []
    try:
        for case_id, runs, filters, expected_ids, expected_total in BASIN_CASES:
            run_ids = _insert_runs(connection, runs)
            try:
                result = _list(store, {"model_id": None, "basin_version_id": BASIN_VERSION_ID, **filters})
            finally:
                _delete_runs(connection, run_ids)
            got = ([item["station_id"] for item in result["items"]], result["total_count"])
            if got != (expected_ids, expected_total):
                failures.append(f"{case_id}: the store returned {got}, expected {(expected_ids, expected_total)}")
            if result["filters"]["available"]["variables"] is not False or "variables" in result["filters"]["applied"]:
                failures.append(f"{case_id}: the variables filter must stay unavailable, got {result['filters']}")
    finally:
        connection.close()
    assert not failures, "\n".join(failures)


def test_model_filter_ignores_the_forecast_runs(throwaway_database_url: str) -> None:
    """A newer run of model B moves the basin list and nothing in the ``model_id`` branch."""
    _seed(throwaway_database_url)
    store = PsycopgForecastStore(throwaway_database_url)
    connection = _connect(throwaway_database_url)
    failures = []
    try:
        _insert_runs(connection, (_run("newer", MODEL_B, 6),))
        basin_list = _list(store, {"model_id": None, "basin_version_id": BASIN_VERSION_ID})
        assert [item["station_id"] for item in basin_list["items"]] == MODEL_B_STATIONS
        for case_id, filters, expected_ids, expected_total in CASES:
            result = _list(store, filters)
            got = ([item["station_id"] for item in result["items"]], result["total_count"])
            if got != (expected_ids, expected_total):
                failures.append(f"{case_id}: the store returned {got}, expected {(expected_ids, expected_total)}")
    finally:
        connection.close()
    assert not failures, "\n".join(failures)


def test_basin_list_rows_keep_the_station_response_shape(throwaway_database_url: str) -> None:
    """The inactive station of the current model comes back as a full inventory row."""
    _seed(throwaway_database_url)
    result = _list(
        PsycopgForecastStore(throwaway_database_url), {"model_id": None, "basin_version_id": BASIN_VERSION_ID}
    )
    assert result["total_count"] == 4
    assert result["filters"]["applied"] == {}
    (inactive,) = [item for item in result["items"] if item["station_id"] == S7_INACTIVE_ONE_VARIABLE]
    assert inactive["basin_version_id"] == BASIN_VERSION_ID
    assert inactive["name"] == "#2694 station 6"
    assert inactive["longitude"] == pytest.approx(110.7)
    assert inactive["latitude"] == pytest.approx(30.1)
    assert inactive["elevation"] == 400.0
    assert inactive["station_role"] == "forcing_proxy"


def test_model_filter_rows_keep_the_station_response_shape(throwaway_database_url: str) -> None:
    """One row per station with the inventory columns, despite 3 weight rows per variable."""
    _seed(throwaway_database_url)
    result = _list(
        PsycopgForecastStore(throwaway_database_url),
        {"model_id": MODEL_ID, "basin_version_id": BASIN_VERSION_ID, "variables": "PRCP,TEMP"},
    )
    assert result["total_count"] == 3
    assert result["filters"]["applied"] == {"variables": ["PRCP", "TEMP"]}
    (shared,) = [item for item in result["items"] if item["station_id"] == S2_SHARED]
    assert shared["basin_version_id"] == BASIN_VERSION_ID
    assert shared["name"] == "#2694 station 1"
    assert shared["longitude"] == pytest.approx(110.2)
    assert shared["latitude"] == pytest.approx(30.1)
    assert shared["elevation"] == 400.0
    assert shared["station_role"] == "forcing_proxy"


class _RecordingCursor:
    """Passes every statement to the real cursor and remembers what was sent."""

    def __init__(self, cursor: Any, executed: list[tuple[str, Any]]) -> None:
        self._cursor = cursor
        self._executed = executed

    def execute(self, statement: str, parameters: Any = None) -> Any:
        self._executed.append((statement, parameters))
        return self._cursor.execute(statement, parameters)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._cursor, name)


def _store_statements(
    monkeypatch: pytest.MonkeyPatch, database_url: str, filters: dict[str, Any]
) -> list[tuple[str, Any]]:
    """The statements ``list_met_stations`` actually sends, with their binds."""
    executed: list[tuple[str, Any]] = []
    real_transaction = PsycopgForecastStore._transaction

    @contextmanager
    def recording_transaction(self: PsycopgForecastStore) -> Iterator[Any]:
        with real_transaction(self) as cursor:
            yield _RecordingCursor(cursor, executed)

    monkeypatch.setattr(PsycopgForecastStore, "_transaction", recording_transaction)
    _list(PsycopgForecastStore(database_url), filters)
    monkeypatch.undo()
    return executed


def _relation_scans(
    node: dict[str, Any], relation: str, under_init_plan: bool = False
) -> Iterator[tuple[dict[str, Any], bool]]:
    """Every plan node reading ``relation``, with whether it sits under an InitPlan."""
    under_init_plan = under_init_plan or node.get("Parent Relationship") == "InitPlan"
    if node.get("Relation Name") == relation:
        yield node, under_init_plan
    for child in node.get("Plans") or []:
        yield from _relation_scans(child, relation, under_init_plan)


@pytest.mark.parametrize(
    ("filters", "expected_scans"),
    (
        ({"model_id": MODEL_ID, "basin_version_id": BASIN_VERSION_ID}, 1),
        ({"model_id": MODEL_ID, "basin_version_id": None, "limit": 1}, 1),
        ({"model_id": MODEL_ID, "basin_version_id": BASIN_VERSION_ID, "variables": "PRCP,TEMP"}, 2),
        # #2699: the basin-only branch reads the current model's stations once.
        ({"model_id": None, "basin_version_id": BASIN_VERSION_ID}, 1),
    ),
    ids=("model-and-basin", "model-only-limit-1", "variable-coverage", "basin-only"),
)
def test_interp_weight_is_only_scanned_inside_init_plans(
    throwaway_database_url: str,
    monkeypatch: pytest.MonkeyPatch,
    filters: dict[str, Any],
    expected_scans: int,
) -> None:
    """Governing invariant 1: no join against ``met.interp_weight``, COUNT and page alike.

    An InitPlan runs once per statement whatever the planner believes about row
    counts, which a join's inner side does not. Asserted on the plan of the
    exact statements and binds the store sent.

    The basin-only branch (#2699) looks up the latest displayable run first:
    ``hydro.hydro_run`` is reached only inside an InitPlan too, and the
    ``model_id`` branch does not read it at all.
    """
    _seed(throwaway_database_url)
    statements = _store_statements(monkeypatch, throwaway_database_url, filters)
    assert len(statements) == 2, [statement for statement, _ in statements]
    connection = _connect(throwaway_database_url)
    try:
        for statement, parameters in statements:
            with connection.cursor() as cursor:
                cursor.execute("EXPLAIN (ANALYZE, FORMAT JSON) " + statement, parameters)
                plan = cursor.fetchone()["QUERY PLAN"][0]["Plan"]
            scans = list(_relation_scans(plan, "interp_weight"))
            summary = [(scan.get("Node Type"), scan.get("Actual Loops"), under) for scan, under in scans]
            assert len(scans) == expected_scans, (statement, summary)
            assert all(under_init_plan for _scan, under_init_plan in scans), (statement, summary)
            assert all(scan["Actual Loops"] == 1 for scan, _under in scans), (statement, summary)
            run_scans = list(_relation_scans(plan, "hydro_run"))
            run_summary = [(scan.get("Node Type"), scan.get("Actual Loops"), under) for scan, under in run_scans]
            assert bool(run_scans) is (filters["model_id"] is None), (statement, run_summary)
            assert all(under_init_plan for _scan, under_init_plan in run_scans), (statement, run_summary)
    finally:
        connection.close()
