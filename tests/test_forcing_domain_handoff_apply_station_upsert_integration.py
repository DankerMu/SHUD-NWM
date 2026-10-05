"""#2300: the handoff apply's station writer against a real PostgreSQL.

``_upsert_met_stations`` must leave an existing compatible ``met.met_station``
row physically unwritten, insert a missing one inactive, and fail the whole
apply on an incompatible one. "Physically unwritten" is a heap property the
unit suite's fake connection cannot show, so it is read here from the row's
``ctid`` and ``xmin``. ``xmax`` is never compared: a row lock sets it.

Cases 1-5 run the FULL ``apply_forcing_domain_handoff(envelope, connection=...)``
over the checked-in ``complete`` handoff fixture, each apply in its own
committed transaction (inside one transaction ``xmin`` would be the applying
transaction's own id either way). Case 6 calls ``_upsert_met_stations`` on a
real cursor directly, because the pre-check rejects that row first.
"""

from __future__ import annotations

import copy
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import psycopg2
import pytest
from psycopg2.extras import Json, RealDictCursor

from packages.common import forcing_domain_handoff_apply as apply_module
from packages.common.forcing_domain_handoff import parse_forcing_domain_handoff_path
from tests.integration_helpers import apply_migrations_from_zero

pytestmark = pytest.mark.integration

_CASE_ROOT = Path(__file__).parent / "fixtures" / "forcing_domain_handoff" / "complete"
_RUN_ID = "fcst_gfs_2026062012_basins_qhh_shud"
BASIN_VERSION_ID = "basins_qhh_v2026_06"
MODEL_ID = "basins_qhh_shud"
FORCING_VERSION_ID = "forc_gfs_2026062012_basins_qhh_shud"
STATION_1 = "qhh_forc_001"
STATION_2 = "qhh_forc_002"
EXPECTED_COUNTS = {
    "met.forcing_version": 1,
    "met.met_station": 2,
    "met.forcing_station_timeseries": 8,
    "met.interp_weight": 4,
}

# Every column of the row plus its physical address. `ctid` moves and `xmin`
# changes whenever a new tuple version is written, whatever the SET assigned.
_STATION_SNAPSHOT_SQL = """
    SELECT station_id,
           ctid::text AS ctid,
           xmin::text AS xmin,
           station_key,
           basin_version_id,
           station_name,
           ST_AsEWKT(geom) AS geom,
           elevation_m,
           station_role,
           active_flag,
           properties_json,
           grid_snapshot_id,
           created_at
    FROM met.met_station
    ORDER BY station_id
"""


@pytest.fixture()
def handoff_connection(throwaway_database_url: str) -> Iterator[Any]:
    apply_migrations_from_zero(throwaway_database_url)
    connection = psycopg2.connect(throwaway_database_url)
    try:
        with connection.cursor() as cursor:
            _seed_parents(cursor)
        connection.commit()
        yield connection
    finally:
        connection.close()


def _seed_parents(cursor: Any) -> None:
    """The rows the handoff's four target tables reference by foreign key."""
    cursor.execute(
        "INSERT INTO core.basin (basin_id, basin_name, basin_group, description)"
        " VALUES ('qhh', 'QHH', 'integration', 'issue 2300')"
    )
    cursor.execute(
        """
        INSERT INTO core.basin_version (
            basin_version_id, basin_id, version_label, geom, active_flag, source_uri, checksum
        )
        VALUES (
            %s, 'qhh', 'v1', ST_Multi(ST_MakeEnvelope(99.0, 37.0, 102.0, 40.0, 4490)),
            true, 'integration://basin', 'basin-sha'
        )
        """,
        (BASIN_VERSION_ID,),
    )
    cursor.execute(
        """
        INSERT INTO core.river_network_version (
            river_network_version_id, basin_version_id, version_label, segment_count, source_uri, checksum
        )
        VALUES ('it2300_rnv', %s, 'v1', 0, 'integration://river-network', 'rnv-sha')
        """,
        (BASIN_VERSION_ID,),
    )
    cursor.execute(
        """
        INSERT INTO core.mesh_version (mesh_version_id, basin_version_id, version_label, mesh_uri, checksum)
        VALUES ('it2300_mesh', %s, 'v1', 'integration://mesh', 'mesh-sha')
        """,
        (BASIN_VERSION_ID,),
    )
    cursor.execute(
        """
        INSERT INTO core.model_instance (
            model_id, basin_version_id, river_network_version_id, mesh_version_id,
            calibration_version_id, shud_code_version, model_package_uri, active_flag, lifecycle_state
        )
        VALUES (%s, %s, 'it2300_rnv', 'it2300_mesh', 'calib', 'shud', 'integration://package/', true, 'active')
        """,
        (MODEL_ID, BASIN_VERSION_ID),
    )
    cursor.execute(
        """
        INSERT INTO met.data_source (
            source_id, source_name, source_type, status, native_format, adapter_name, config_json
        )
        VALUES ('gfs', 'GFS Integration', 'forecast', 'mock', 'netcdf', 'gfs', '{}')
        ON CONFLICT (source_id) DO NOTHING
        """
    )


def _envelope() -> dict[str, Any]:
    envelope = parse_forcing_domain_handoff_path(
        _CASE_ROOT / "object-store" / "runs" / _RUN_ID / "input" / "forcing_domain_handoff.json",
        object_store_root=_CASE_ROOT / "object-store",
    )
    assert envelope["available"] is True
    return copy.deepcopy(envelope)


def _apply(connection: Any, envelope: dict[str, Any]) -> dict[str, Any]:
    """One apply in its own transaction; the helper commits or rolls back."""
    return apply_module.apply_forcing_domain_handoff(envelope, connection=connection)


def _station_snapshot(connection: Any) -> dict[str, dict[str, Any]]:
    with connection.cursor(cursor_factory=RealDictCursor) as cursor:
        cursor.execute(_STATION_SNAPSHOT_SQL)
        rows = {row["station_id"]: dict(row) for row in cursor.fetchall()}
    connection.commit()
    return rows


def _table_counts(connection: Any) -> dict[str, int]:
    counts: dict[str, int] = {}
    with connection.cursor() as cursor:
        for table in EXPECTED_COUNTS:
            cursor.execute(f"SELECT count(*) FROM {table}")
            counts[table] = cursor.fetchone()[0]
    connection.commit()
    return counts


def _execute_committed(connection: Any, statement: str, parameters: tuple[Any, ...] = ()) -> None:
    with connection.cursor() as cursor:
        cursor.execute(statement, parameters)
        assert cursor.rowcount == 1, statement
    connection.commit()


def _conflict_codes(report: dict[str, Any]) -> set[str]:
    return {reason["code"] for reason in report["unavailable_reasons"]}


def test_second_apply_leaves_station_ctid_and_xmin_unchanged(handoff_connection: Any) -> None:
    first = _apply(handoff_connection, _envelope())
    assert first["status"] == "applied"
    assert first["row_counts"] == EXPECTED_COUNTS
    before = _station_snapshot(handoff_connection)
    assert set(before) == {STATION_1, STATION_2}

    second = _apply(handoff_connection, _envelope())

    assert second["status"] == "applied"
    assert second["row_counts"] == EXPECTED_COUNTS
    assert _station_snapshot(handoff_connection) == before


def test_incompatible_existing_station_conflicts_and_stays_unwritten(handoff_connection: Any) -> None:
    # Station 1 exists under another name with the same role: no branch of the
    # identity predicate accepts it. Station 2 does not exist yet.
    _execute_committed(
        handoff_connection,
        """
        INSERT INTO met.met_station (
            station_id, basin_version_id, station_name, geom, elevation_m, station_role, active_flag, properties_json
        )
        VALUES (%s, %s, 'another station', ST_SetSRID(ST_MakePoint(100.125, 38.25), 4490),
                3280.0, 'forcing_grid', true, %s)
        """,
        (STATION_1, BASIN_VERSION_ID, Json({"owner": "someone else"})),
    )
    before = _station_snapshot(handoff_connection)

    report = _apply(handoff_connection, _envelope())

    assert report["status"] == "failed"
    assert report["writes_performed"] is False
    assert _conflict_codes(report) == {apply_module.REASON_APPLY_STATION_CONFLICT}
    assert _station_snapshot(handoff_connection) == before
    assert _table_counts(handoff_connection) == {
        "met.forcing_version": 0,
        "met.met_station": 1,
        "met.forcing_station_timeseries": 0,
        "met.interp_weight": 0,
    }


def test_fresh_station_is_inserted_inactive(handoff_connection: Any) -> None:
    envelope = _envelope()
    # The payload says active; the INSERT template's literal `false` decides.
    assert {row["active_flag"] for row in envelope["parsed"]["met.met_station"]} == {True}

    report = _apply(handoff_connection, envelope)

    assert report["status"] == "applied"
    stored = _station_snapshot(handoff_connection)
    assert {station_id: row["active_flag"] for station_id, row in stored.items()} == {
        STATION_1: False,
        STATION_2: False,
    }
    assert stored[STATION_1]["station_name"] == "QHH forcing station 001"
    assert stored[STATION_1]["geom"] == "SRID=4490;POINT(100.125 38.25)"
    assert stored[STATION_1]["elevation_m"] == 3280.0
    assert stored[STATION_1]["station_role"] == "forcing_grid"
    assert stored[STATION_1]["properties_json"] == {
        "forcing_filename": "X100.125Y38.25.csv",
        "source": "qhh.tsd.forc",
        "model_id": MODEL_ID,
    }


def test_existing_active_station_stays_active_across_reapply(handoff_connection: Any) -> None:
    assert _apply(handoff_connection, _envelope())["status"] == "applied"
    # The cutover's flip, which the apply does not own.
    _execute_committed(
        handoff_connection,
        "UPDATE met.met_station SET active_flag = true WHERE station_id = %s",
        (STATION_1,),
    )
    before = _station_snapshot(handoff_connection)
    assert before[STATION_1]["active_flag"] is True

    envelope = _envelope()
    for row in envelope["parsed"]["met.met_station"]:
        row["active_flag"] = False
    report = _apply(handoff_connection, envelope)

    assert report["status"] == "applied"
    after = _station_snapshot(handoff_connection)
    assert after[STATION_1]["active_flag"] is True
    assert after[STATION_2]["active_flag"] is False
    assert after == before


def test_sub_tolerance_elevation_drift_is_rejected_by_the_sql_predicate_alone(handoff_connection: Any) -> None:
    assert _apply(handoff_connection, _envelope())["status"] == "applied"
    # 5e-10 is under STATION_COORDINATE_TOLERANCE (1e-9), so the Python
    # pre-check accepts the row; the SQL `IS NOT DISTINCT FROM` does not.
    drifted_elevation = 3280.0000000005
    assert 0 < abs(drifted_elevation - 3280.0) < apply_module.STATION_COORDINATE_TOLERANCE
    _execute_committed(
        handoff_connection,
        "UPDATE met.met_station SET elevation_m = %s WHERE station_id = %s",
        (drifted_elevation, STATION_1),
    )
    before = _station_snapshot(handoff_connection)
    assert before[STATION_1]["elevation_m"] == drifted_elevation
    counts_before = _table_counts(handoff_connection)

    envelope = _envelope()
    stations, reasons = apply_module._prepare_apply_rows(envelope)
    assert reasons == []
    with handoff_connection.cursor() as cursor:
        # Does not raise: the pre-check lets this row through.
        apply_module._verify_existing_station_rows(cursor, stations["stations"])
    handoff_connection.rollback()

    report = _apply(handoff_connection, envelope)

    assert report["status"] == "failed"
    assert report["writes_performed"] is False
    assert _conflict_codes(report) == {apply_module.REASON_APPLY_STATION_CONFLICT}
    assert _station_snapshot(handoff_connection) == before
    assert _table_counts(handoff_connection) == counts_before


def test_undecidable_predicate_is_a_conflict_in_the_station_writer(handoff_connection: Any) -> None:
    # A `direct_grid_cache` row WITHOUT the `direct_grid` key: the name differs,
    # so only the direct-grid branch can accept it, and there
    # `properties_json ->> 'direct_grid' = 'true'` is NULL while every other
    # term is true. The whole predicate is NULL, not false.
    station_id = "dg-it2300::cell:1"
    binding = {"forcing_filename": "station_00001.csv", "shud_forcing_index": 1}
    _execute_committed(
        handoff_connection,
        """
        INSERT INTO met.met_station (
            station_id, basin_version_id, station_name, geom, elevation_m, station_role, active_flag, properties_json
        )
        VALUES (%s, %s, 'Direct-grid station 1', ST_SetSRID(ST_MakePoint(100.125, 38.25), 4490),
                3280.0, 'direct_grid_cache', false, %s)
        """,
        (station_id, BASIN_VERSION_ID, Json(binding)),
    )
    before = _station_snapshot(handoff_connection)
    incoming = {
        "station_id": station_id,
        "basin_version_id": BASIN_VERSION_ID,
        "station_name": "QHH forcing station 1",
        "longitude": 100.125,
        "latitude": 38.25,
        "elevation_m": 3280.0,
        "station_role": "forcing_grid",
        "properties_json": dict(binding),
    }

    # RealDictCursor here, plain tuples in the full applies above: the writer
    # reads its RETURNING rows under both row shapes.
    with handoff_connection.cursor(cursor_factory=RealDictCursor) as cursor:
        with pytest.raises(apply_module.ForcingDomainHandoffApplyError) as raised:
            apply_module._upsert_met_stations(cursor, [incoming])
    handoff_connection.rollback()

    assert raised.value.reason["code"] == apply_module.REASON_APPLY_STATION_CONFLICT
    assert raised.value.reason["table"] == "met.met_station"
    assert _station_snapshot(handoff_connection) == before
