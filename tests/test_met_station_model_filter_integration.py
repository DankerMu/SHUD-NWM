"""#2694 on a real PostgreSQL: the met-station inventory's ``model_id`` branch.

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

Run against a disposable database (never production), e.g. a local
``timescale/timescaledb-ha:pg15-latest`` container:

    NHMS_RUN_INTEGRATION=1 NHMS_INTEGRATION_DATABASE_URL=... \\
        uv run pytest -q -rs tests/test_met_station_model_filter_integration.py

SILENT-SKIP TRAP: without both variables every case skips; read the passed count.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

import psycopg2
import pytest
from psycopg2.extras import Json, RealDictCursor, execute_values

from packages.common.forecast_store import PsycopgForecastStore, _escape_like
from tests.integration_helpers import (
    BASIN_ID,
    BASIN_VERSION_ID,
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


def _interp_weight_scans(node: dict[str, Any], under_init_plan: bool = False) -> Iterator[tuple[dict[str, Any], bool]]:
    under_init_plan = under_init_plan or node.get("Parent Relationship") == "InitPlan"
    if node.get("Relation Name") == "interp_weight":
        yield node, under_init_plan
    for child in node.get("Plans") or []:
        yield from _interp_weight_scans(child, under_init_plan)


@pytest.mark.parametrize(
    ("filters", "expected_scans"),
    (
        ({"model_id": MODEL_ID, "basin_version_id": BASIN_VERSION_ID}, 1),
        ({"model_id": MODEL_ID, "basin_version_id": None, "limit": 1}, 1),
        ({"model_id": MODEL_ID, "basin_version_id": BASIN_VERSION_ID, "variables": "PRCP,TEMP"}, 2),
    ),
    ids=("model-and-basin", "model-only-limit-1", "variable-coverage"),
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
            scans = list(_interp_weight_scans(plan))
            summary = [(scan.get("Node Type"), scan.get("Actual Loops"), under) for scan, under in scans]
            assert len(scans) == expected_scans, (statement, summary)
            assert all(under_init_plan for _scan, under_init_plan in scans), (statement, summary)
            assert all(scan["Actual Loops"] == 1 for scan, _under in scans), (statement, summary)
    finally:
        connection.close()
