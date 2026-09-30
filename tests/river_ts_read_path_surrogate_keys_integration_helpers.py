"""Shared seed, fixture and text-era oracles of the #1341 read-path surrogate-key proof.

Non-collectible support module for the two partitions of that proof (#2490
split, pure move): ``tests/test_river_ts_read_path_surrogate_keys_integration.py``
(the field-identity, NULL-key and zoom-split cases, plus the module docstring
that states the scope) and
``tests/test_river_ts_read_path_surrogate_keys_coverage_integration.py`` (the
#1446 coverage-refresh guard and the live-caller plan cases). The
``integration`` in this file name keeps it inside the ci.yml ``database``
filter glob ``tests/*integration*.py``.
"""

from __future__ import annotations

import math
from collections.abc import Callable, Mapping
from datetime import UTC, datetime, timedelta
from typing import Any

import psycopg2
import pytest
from psycopg2.extras import RealDictCursor
from sqlalchemy import text
from sqlalchemy.orm import Session

from services.tiles.mvt import (
    postgis_tile_sql,
)
from tests.integration_helpers import (
    apply_migrations_from_zero,
    sqlalchemy_engine,
)

_BASIN_ID = "basin-1341"
_BASIN_VERSION_ID = "bv-1341"
_NETWORK_ID = "rnv-1341"
_MODEL_ID = "model-1341"
_VARIABLE = "q_down"

# Two runs on the same network. `_KEYED_RUN_ID` is entirely dual-written and
# carries the field-identity comparisons; `_LEGACY_RUN_ID` additionally holds
# rows in the pre-#1340 shape (text columns only, NULL keys) and carries the
# exclusion contract. It also has the later cycle_time and the only
# run_display_coverage row, so the national layer deterministically selects it.
_KEYED_RUN_ID = "run-1341-keyed"
_LEGACY_RUN_ID = "run-1341-legacy"
# A run with NOTHING but pre-#1340 rows, plus the coverage row the text era
# already materialized for it. It exists to pin what a coverage refresh does to
# such a run after the switch: since #1446 it is REFUSED rather than zeroed,
# and `force` is the only way to zero it.
# Its cycle_time is the earliest of the three so the national layer's
# DISTINCT ON still selects `_LEGACY_RUN_ID` and the zoom-split test is
# unaffected.
_ALL_LEGACY_RUN_ID = "run-1341-all-legacy"

_T0 = datetime(2026, 6, 1, tzinfo=UTC)
_T1 = _T0 + timedelta(hours=1)
_LEGACY_ONLY_TIME = _T0 + timedelta(hours=2)
_ALL_LEGACY_CYCLE = _T0 - timedelta(hours=1)

# seg-a is the only segment with a source stream class, so at z<9 it is the
# typed_values leg's row; seg-b/seg-c have none and go through untyped_ranked.
# seg-legacy has none either AND the largest value, so its PERCENT_RANK is 1.0
# and it clears every low-zoom cutoff — if the untyped leg had kept the text
# predicates it would render at z=5 while staying absent at z=9.
_SEGMENTS: tuple[tuple[str, float | None, float], ...] = (
    ("seg-a", 5.0, 30.0),
    ("seg-b", None, 20.0),
    ("seg-c", None, 10.0),
)
_LEGACY_SEGMENT = ("seg-legacy", None, 90.0)

_SEGMENT_LON = 100.0
_SEGMENT_LAT = 38.0

# The hydro-layer source CTE exactly as it stood before #1341 (text predicates,
# two-column segment join, text columns projected straight out of the fact
# table). This is the oracle: it is not derived from the code under test.
_TEXT_ERA_HYDRO_SOURCE_CTE = """
    SELECT (ts.river_network_version_id || '::' || ts.river_segment_id) AS feature_id,
           ts.river_segment_id AS segment_id,
           ts.river_segment_id,
           ts.river_network_version_id,
           ts.basin_version_id,
           ts.value, ts.unit,
           ts.quality_flag,
           ts.run_id, ts.variable,
           to_char(ts.valid_time AT TIME ZONE 'UTC', 'YYYY-MM-DD"T"HH24:MI:SS"Z"') AS valid_time,
           rs.geom
    FROM hydro.river_timeseries ts
    JOIN core.river_segment rs
      ON rs.river_segment_id = ts.river_segment_id
     AND rs.river_network_version_id = ts.river_network_version_id
    WHERE ts.run_id = :run_id
      AND ts.basin_version_id = :basin_version_id
      AND ts.river_network_version_id = :river_network_version_id
      AND ts.variable = :variable
      AND ts.valid_time = :valid_time
"""

# valid_times_for_layer's named-identity branch as it stood before #1341.
_TEXT_ERA_VALID_TIMES_SQL = """
    SELECT DISTINCT valid_time
    FROM hydro.river_timeseries
    WHERE run_id = :run_id
      AND basin_version_id = :basin_version_id
      AND river_network_version_id = :river_network_version_id
      AND variable = :variable
    ORDER BY valid_time DESC
    LIMIT :limit
"""

_HYDRO_SOURCE_PROJECTION = (
    "feature_id",
    "segment_id",
    "river_segment_id",
    "river_network_version_id",
    "basin_version_id",
    "value",
    "unit",
    "quality_flag",
    "run_id",
    "variable",
    "valid_time",
)


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _tile_xy(longitude: float, latitude: float, zoom: int) -> tuple[int, int]:
    """Slippy-map tile containing a lon/lat, so no tile index is a magic number."""
    scale = 2**zoom
    x = int((longitude + 180.0) / 360.0 * scale)
    radians = math.radians(latitude)
    y = int((1.0 - math.asinh(math.tan(radians)) / math.pi) / 2.0 * scale)
    return min(x, scale - 1), min(y, scale - 1)


def _source_cte_body(layer: str) -> str:
    """The layer's ``source_rows`` CTE body, lifted out of the production SQL."""
    sql = postgis_tile_sql(layer)
    opener = "source_rows AS NOT MATERIALIZED ("
    start = sql.index(opener) + len(opener)
    end = sql.index("source_identity_stats AS (", start)
    body = sql[start:end].rstrip()
    assert body.endswith("),"), body[-80:]
    return body[:-2]


def _hydro_source_query(cte_body: str) -> str:
    return f"""
        WITH source_rows AS (
        {cte_body}
        )
        SELECT {", ".join(_HYDRO_SOURCE_PROJECTION)}, ST_AsEWKT(geom) AS geom_wkt
        FROM source_rows
        ORDER BY river_network_version_id, river_segment_id
    """


def _national_source_query(cte_body: str) -> str:
    return f"""
        WITH bounds AS (
            SELECT ST_TileEnvelope(:z, :x, :y) AS geom_3857
        ),
        source_rows AS (
        {cte_body}
        )
        SELECT feature_id, segment_id, river_segment_id, river_network_version_id,
               basin_version_id, basin_id, value, unit, quality_flag, run_id,
               variable, valid_time, ST_AsEWKT(geom) AS geom_wkt
        FROM source_rows
        WHERE geom IS NOT NULL
        ORDER BY river_network_version_id, river_segment_id
    """


def _rows(session: Session, sql: str, params: dict[str, Any]) -> list[dict[str, Any]]:
    return [dict(row) for row in session.execute(text(sql), params).mappings().all()]


def _segment_geom_sql(index: int) -> str:
    """A short line near (100E, 38N); distinct per segment so geometry is comparable."""
    lon = _SEGMENT_LON + index * 0.01
    return (
        "ST_Multi(ST_SetSRID(ST_GeomFromText("
        f"'LINESTRING({lon} {_SEGMENT_LAT}, {lon + 0.005} {_SEGMENT_LAT + 0.005})'), 4490))"
    )


def _seed(database_url: str) -> None:
    connection = psycopg2.connect(database_url, cursor_factory=RealDictCursor)
    connection.autocommit = True
    try:
        with connection.cursor() as cursor:
            cursor.execute(
                "INSERT INTO core.basin (basin_id, basin_name) VALUES (%s, %s)",
                (_BASIN_ID, "Basin 1341"),
            )
            cursor.execute(
                """
                INSERT INTO core.basin_version
                    (basin_version_id, basin_id, version_label, geom, active_flag)
                VALUES (%s, %s, 'v1',
                        ST_SetSRID(ST_GeomFromText(
                            'MULTIPOLYGON(((99 37, 99 39, 101 39, 101 37, 99 37)))'), 4490),
                        true)
                """,
                (_BASIN_VERSION_ID, _BASIN_ID),
            )
            cursor.execute(
                """
                INSERT INTO core.river_network_version
                    (river_network_version_id, basin_version_id, version_label, segment_count)
                VALUES (%s, %s, 'v1', %s)
                """,
                (_NETWORK_ID, _BASIN_VERSION_ID, len(_SEGMENTS)),
            )
            for index, (segment_id, stream_type, _value) in enumerate((*_SEGMENTS, _LEGACY_SEGMENT)):
                properties = "{}" if stream_type is None else f'{{"Type": {stream_type}}}'
                cursor.execute(
                    f"""
                    INSERT INTO core.river_segment
                        (river_segment_id, river_network_version_id, segment_order,
                         geom, properties_json)
                    VALUES (%s, %s, %s, {_segment_geom_sql(index)}, %s::jsonb)
                    """,
                    (segment_id, _NETWORK_ID, index, properties),
                )
            cursor.execute(
                """
                INSERT INTO core.model_instance
                    (model_id, basin_version_id, river_network_version_id, mesh_version_id,
                     calibration_version_id, shud_code_version, model_package_uri,
                     active_flag, lifecycle_state)
                VALUES (%s, %s, %s, 'mesh-1341', 'cal-1341', '1.0', 's3://nhms/model',
                        true, 'active')
                """,
                (_MODEL_ID, _BASIN_VERSION_ID, _NETWORK_ID),
            )
            for run_id, cycle_time in (
                (_KEYED_RUN_ID, _T0),
                (_LEGACY_RUN_ID, _T1),
                (_ALL_LEGACY_RUN_ID, _ALL_LEGACY_CYCLE),
            ):
                cursor.execute(
                    """
                    INSERT INTO hydro.hydro_run
                        (run_id, run_type, scenario_id, model_id, basin_version_id, cycle_time,
                         start_time, end_time, status, run_manifest_uri)
                    VALUES (%s, 'forecast', 'sc', %s, %s, %s, %s, %s, 'succeeded', 's3://nhms/manifest')
                    """,
                    (run_id, _MODEL_ID, _BASIN_VERSION_ID, cycle_time, _T0, _LEGACY_ONLY_TIME),
                )

            # Dual-written rows: the seven surrogate columns come from the
            # authority rows themselves, which is what the #1340 writer does.
            for run_id in (_KEYED_RUN_ID, _LEGACY_RUN_ID):
                valid_times = (_T0, _T1) if run_id == _KEYED_RUN_ID else (_T0,)
                for segment_id, _stream_type, value in _SEGMENTS:
                    for lead, valid_time in enumerate(valid_times):
                        cursor.execute(
                            """
                            INSERT INTO hydro.river_timeseries (
                                run_id, basin_version_id, river_network_version_id,
                                river_segment_id, valid_time, lead_time_hours,
                                variable, value, unit, quality_flag,
                                run_key, basin_version_key, river_network_version_key,
                                river_segment_key, variable_e, unit_e, quality_flag_e)
                            SELECT h.run_id, bv.basin_version_id, rnv.river_network_version_id,
                                   rs.river_segment_id, %(valid_time)s, %(lead)s,
                                   'q_down', %(value)s, 'm3/s', 'ok',
                                   h.run_key, bv.basin_version_key, rnv.river_network_version_key,
                                   rs.river_segment_key,
                                   'q_down'::hydro.river_variable,
                                   'm3/s'::hydro.river_unit,
                                   'ok'::hydro.river_quality_flag
                            FROM hydro.hydro_run h,
                                 core.basin_version bv,
                                 core.river_network_version rnv,
                                 core.river_segment rs
                            WHERE h.run_id = %(run_id)s
                              AND bv.basin_version_id = %(basin_version_id)s
                              AND rnv.river_network_version_id = %(river_network_version_id)s
                              AND rs.river_network_version_id = %(river_network_version_id)s
                              AND rs.river_segment_id = %(segment_id)s
                            """,
                            {
                                "run_id": run_id,
                                "basin_version_id": _BASIN_VERSION_ID,
                                "river_network_version_id": _NETWORK_ID,
                                "segment_id": segment_id,
                                "valid_time": valid_time,
                                "lead": lead,
                                "value": value,
                            },
                        )

            # Legacy rows: pre-#1340 shape, every surrogate column left NULL.
            legacy_segment_id, _legacy_type, legacy_value = _LEGACY_SEGMENT
            for lead, valid_time in enumerate((_T0, _LEGACY_ONLY_TIME)):
                cursor.execute(
                    """
                    INSERT INTO hydro.river_timeseries (
                        run_id, basin_version_id, river_network_version_id, river_segment_id,
                        valid_time, lead_time_hours, variable, value, unit, quality_flag)
                    VALUES (%s, %s, %s, %s, %s, %s, 'q_down', %s, 'm3/s', 'ok')
                    """,
                    (
                        _LEGACY_RUN_ID,
                        _BASIN_VERSION_ID,
                        _NETWORK_ID,
                        legacy_segment_id,
                        valid_time,
                        lead,
                        legacy_value,
                    ),
                )

            # `_ALL_LEGACY_RUN_ID`: every row in the pre-#1340 shape, for all
            # three ordinary segments at both hours. Nothing about it is
            # key-visible.
            for segment_id, _stream_type, value in _SEGMENTS:
                for lead, valid_time in enumerate((_T0, _T1)):
                    cursor.execute(
                        """
                        INSERT INTO hydro.river_timeseries (
                            run_id, basin_version_id, river_network_version_id, river_segment_id,
                            valid_time, lead_time_hours, variable, value, unit, quality_flag)
                        VALUES (%s, %s, %s, %s, %s, %s, 'q_down', %s, 'm3/s', 'ok')
                        """,
                        (
                            _ALL_LEGACY_RUN_ID,
                            _BASIN_VERSION_ID,
                            _NETWORK_ID,
                            segment_id,
                            valid_time,
                            lead,
                            value,
                        ),
                    )

            # `_LEGACY_RUN_ID` is display-covered and has the latest
            # cycle_time, so the national layer's DISTINCT ON deterministically
            # selects the run that owns the NULL-key rows — the interesting
            # case for the zoom split. `_ALL_LEGACY_RUN_ID` also carries a
            # coverage row, materialized in the text era from its (then
            # readable) rows; the refresh test asserts what happens to it now.
            cursor.executemany(
                """
                INSERT INTO hydro.run_display_coverage
                    (run_id, segment_count, river_sample_count,
                     river_valid_time_start, river_valid_time_end,
                     min_lead_time_hours, max_lead_time_hours)
                VALUES (%s, %s, %s, %s, %s, %s, %s)
                """,
                (
                    (_LEGACY_RUN_ID, len(_SEGMENTS), len(_SEGMENTS), _T0, _LEGACY_ONLY_TIME, 0, 0),
                    (_ALL_LEGACY_RUN_ID, len(_SEGMENTS), len(_SEGMENTS) * 2, _T0, _T1, 0, 1),
                ),
            )
    finally:
        connection.close()


@pytest.fixture()
def seeded(throwaway_database_url: str) -> Any:
    # Frozen text-era reader goldens need the historical catalog before expand.
    apply_migrations_from_zero(throwaway_database_url, through="000058")
    _seed(throwaway_database_url)
    engine = sqlalchemy_engine(throwaway_database_url)
    with Session(engine) as session:
        yield throwaway_database_url, session
    engine.dispose()


# The national ``source_rows`` CTE binds `(source, cycle)` since #2007. These
# cases are written for the legacy source-less semantics -- every candidate run
# eligible, newest cycle wins -- so they bind the NULL pass-through the legacy
# 5-segment route binds. Omitting the names is not an option: `text()` raises
# `StatementError: A value is required for bind parameter 'source'` before the
# statement reaches the driver.
_NATIONAL_TILE_PARAMS: dict[str, Any] = {"variable": _VARIABLE, "source": None, "cycle": None}


def _identity_params(run_id: str, valid_time: datetime = _T0, variable: str = _VARIABLE) -> dict[str, Any]:
    return {
        "run_id": run_id,
        "basin_version_id": _BASIN_VERSION_ID,
        "river_network_version_id": _NETWORK_ID,
        "variable": variable,
        "valid_time": valid_time,
    }


# ---------------------------------------------------------------------------
# field identity
# ---------------------------------------------------------------------------


# Every seeded run, mapped to the one store the read path still has. #1342's
# contract (task 6.3) deleted the routing, so there is nothing left to
# parametrize; naming all three runs keeps the helper's decoy poison off the
# rows these tests read, which is what the old store mapping used to arrange.
_NARROW_STORE_MAP: Mapping[str, str] = {
    _KEYED_RUN_ID: "narrow",
    _LEGACY_RUN_ID: "narrow",
    _ALL_LEGACY_RUN_ID: "narrow",
}


def _expand(session: Session, prepare: Callable[[Mapping[str, str]], None]) -> None:
    # Release all baseline reads before the separate connection renames tables.
    session.rollback()
    prepare(_NARROW_STORE_MAP)


_COVERAGE_COLUMNS_SQL = """
    SELECT segment_count, river_sample_count,
           river_valid_time_start, river_valid_time_end,
           min_lead_time_hours, max_lead_time_hours, refreshed_at
    FROM hydro.run_display_coverage WHERE run_id = %s
"""


def _coverage(connection: Any, run_id: str) -> dict[str, Any] | None:
    with connection.cursor() as cursor:
        cursor.execute(_COVERAGE_COLUMNS_SQL, (run_id,))
        row = cursor.fetchone()
    return None if row is None else dict(row)


def _assert_all_legacy_preconditions(connection: Any) -> dict[str, Any]:
    """The seed really is the hazard shape: populated row, zero keyed rows."""
    before = _coverage(connection, _ALL_LEGACY_RUN_ID)

    # Independent expectation: the text era saw all three segments at both
    # hours, and that is exactly what the seeded row holds.
    assert before["segment_count"] == len(_SEGMENTS)
    assert before["river_sample_count"] == len(_SEGMENTS) * 2
    assert before["river_valid_time_start"] == _T0
    assert before["river_valid_time_end"] == _T1

    # And the rows really are still in the table, text-readable — so an empty
    # fresh scan is the key switch, not missing data.
    with connection.cursor() as cursor:
        cursor.execute(
            """
            SELECT COUNT(*) AS n, COUNT(run_key) AS keyed
            FROM hydro.river_timeseries_legacy WHERE run_id = %s
            """,
            (_ALL_LEGACY_RUN_ID,),
        )
        rows = dict(cursor.fetchone())
    assert rows == {"n": len(_SEGMENTS) * 2, "keyed": 0}
    return before
