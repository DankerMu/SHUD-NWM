"""Real-database proof for the #1341 read-path surrogate-key switch.

Run with the repo's standard opt-in against a throwaway database:

    NHMS_RUN_INTEGRATION=1 NHMS_INTEGRATION_DATABASE_URL=... uv run pytest -q \
        tests/test_river_ts_read_path_surrogate_keys_integration.py

``throwaway_database_url`` (tests/conftest.py) creates and drops a
uniquely-named database per TEST, so nothing here can touch a live one.

Scope: the questions a text-substring pin cannot answer.

* **Field identity.** Each switched query is executed side by side with the
  text-era query it replaced — the pre-change SQL is embedded verbatim below
  as the oracle — over rows that carry BOTH the text columns and the surrogate
  keys. Every projected field, including the ``feature_id`` concatenation and
  the geometry, must match.
* **The NULL-key exclusion.** Rows written before #1340 carry NULL keys and
  are invisible to key-filtered reads. That is a designed consequence with a
  retention deadline, not an accident, so it is asserted as an explicit
  contract: the text oracle sees the legacy row, the switched query does not,
  and the difference is exactly that row.
* **Zoom-split consistency.** The national tile reads the fact table through
  two UNION ALL legs selected by zoom. The same national identity must have
  the same NULL-key visibility at z<9 and at z>=9 — the failure a half-done
  switch produces.
* **Per-segment payload, not just shared constants.** Every national row
  carries the same run/network strings, so identity-only assertions cannot
  distinguish a correct key join from one that pairs a segment's geometry with
  another segment's value. The seeded segments have distinct values and
  distinct geometries and are compared one by one against the seed constants
  and ``core.river_segment``.
* **Degradation, not error.** An unknown ``run_id`` and an out-of-vocabulary
  ``variable`` must both return the empty result the text predicates returned.
  The ``enum_range`` matcher is proved to be load-bearing by showing the cast
  it replaced really does raise on the same literal.
* **What a coverage refresh does to an all-legacy run.** It refuses (#1446).
  The key-scan emptiness is the #1341 contract; destroying coverage the text
  era already materialized never was, so the upsert now skips a populated row
  whose fresh scan is empty unless the caller forces it. Pinned here on a real
  database because the guard IS a SQL clause -- ``force`` adaptation and the
  conditional ``DO UPDATE ... WHERE`` cannot be checked by a fake cursor.
"""

from __future__ import annotations

import hashlib
import math
from collections.abc import Callable, Mapping
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import mapbox_vector_tile
import psycopg2
import pytest
from psycopg2.extras import RealDictCursor
from sqlalchemy import text
from sqlalchemy.orm import Session

from apps.api.errors import ApiError
from apps.api.routes import hydro_display
from apps.api.routes.hydro_display import _require_hydro_mvt_source_identity
from packages.common.display_coverage import (
    DisplayCoverageRefreshRefused,
    _refresh,
    refresh_run_display_coverage,
)
from packages.common.river_ts_render import render_river_ts_sql
from services.tiles.mvt import (
    _valid_times_any_source_template,
    _valid_times_named_source_template,
    postgis_tile_sql,
    valid_times_for_layer,
)
from tests.integration_helpers import (
    apply_migrations_from_zero,
    insert_river_timeseries_dual_written,
    sqlalchemy_engine,
)
from tests.integration_helpers import (
    post_expand_forecast_database as post_expand_forecast_database,
)

pytestmark = pytest.mark.integration

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


def _frozen_national_source_body() -> str:
    frozen = (Path(__file__).parent / "fixtures/hydro_national_mvt_pre_store_c21bacf9.sql").read_bytes()
    assert hashlib.sha256(frozen).hexdigest() == "d18c89af633838df8be0d84e3bff02df3e01d35ef21935f9bb59de0dc1f8e8b4"
    sql = frozen.decode()
    opener = "source_rows AS NOT MATERIALIZED ("
    body = sql.split(opener, 1)[1].split("source_identity_stats AS (", 1)[0].rstrip()
    assert body.endswith("),")
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
    apply_migrations_from_zero(throwaway_database_url)
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


def _prepare_hydro_stores(
    session: Session,
    prepare: Callable[[Mapping[str, str]], None],
    store: str,
    run_id: str = _KEYED_RUN_ID,
) -> None:
    # Release all baseline reads before the separate connection renames tables.
    session.rollback()
    opposite_run = _LEGACY_RUN_ID if run_id == _KEYED_RUN_ID else _KEYED_RUN_ID
    prepare({
        run_id: store,
        opposite_run: "narrow" if store == "legacy" else "legacy",
        _ALL_LEGACY_RUN_ID: "legacy",
    })
    # Only this exact-time MVT oracle restores decoy times. Forecast's shared
    # poison remains unchanged, and NULL-key historical rows are never updated.
    for table, authoritative_store in (
        ("river_timeseries", "legacy"),
        ("river_timeseries_legacy", "narrow"),
    ):
        session.execute(text(
            f"UPDATE hydro.{table} ts SET valid_time = ts.valid_time - INTERVAL '30 minutes' "
            "FROM hydro.hydro_run h WHERE h.run_key = ts.run_key "
            "AND h.timeseries_store = :store"
        ), {"store": authoritative_store})
    session.commit()


@pytest.mark.parametrize("store", ("legacy", "narrow"))
def test_hydro_tile_source_rows_are_field_identical_to_the_text_era_query(
    seeded: Any, post_expand_forecast_database: Callable[[Mapping[str, str]], None], store: str,
) -> None:
    _url, session = seeded
    params = _identity_params(_KEYED_RUN_ID)

    oracle = _rows(session, _hydro_source_query(_TEXT_ERA_HYDRO_SOURCE_CTE), params)
    _prepare_hydro_stores(session, post_expand_forecast_database, store)
    switched = _rows(session, _hydro_source_query(_source_cte_body("hydro")), params)

    assert len(oracle) == len(_SEGMENTS), "seed must produce rows for the oracle to be meaningful"
    assert switched == oracle
    # Spot-check the wire values themselves, so an oracle that silently went
    # empty on both sides cannot pass.
    assert [row["feature_id"] for row in switched] == [
        f"{_NETWORK_ID}::{segment_id}" for segment_id, _type, _value in _SEGMENTS
    ]
    assert {row["run_id"] for row in switched} == {_KEYED_RUN_ID}
    assert {row["variable"] for row in switched} == {_VARIABLE}
    assert {row["unit"] for row in switched} == {"m3/s"}
    assert {row["quality_flag"] for row in switched} == {"ok"}
    assert {row["basin_version_id"] for row in switched} == {_BASIN_VERSION_ID}


# Frozen at f33441a2dafd910375aab087f61257327055611c, never regenerated.
_FROZEN_HYDRO_SQL = Path(__file__).parent / "fixtures/hydro_mvt_pre_store_f33441a2.sql"
_FROZEN_HYDRO_SHA256 = "bf284b1f7d6532d5f45a26b490b8f23093b303c962a1adf524292557b169fd68"


@pytest.mark.parametrize("store", ("legacy", "narrow"))
def test_hydro_store_union_preserves_one_mvt_result_and_decoded_payload(
    seeded: Any, post_expand_forecast_database: Callable[[Mapping[str, str]], None],
    store: str, monkeypatch: pytest.MonkeyPatch,
) -> None:
    _url, session = seeded
    monkeypatch.setenv("NHMS_ENABLE_LIVE_POSTGIS_MVT", "true")
    assert _tile_xy(100.0, 38.0, 9) == (398, 197)
    params = _identity_params(_KEYED_RUN_ID, _T0)
    bind = hydro_display._postgis_tile_params(params, z=9, x=398, y=197, layer="hydro")
    frozen = _FROZEN_HYDRO_SQL.read_bytes()
    assert hashlib.sha256(frozen).hexdigest() == _FROZEN_HYDRO_SHA256
    baseline = _rows(session, frozen.decode(), bind)
    assert len(baseline) == 1
    decoded = mapbox_vector_tile.decode(bytes(baseline[0]["tile"]))
    _prepare_hydro_stores(session, post_expand_forecast_database, store)
    for physical_store, table in (("legacy", "river_timeseries_legacy"), ("narrow", "river_timeseries")):
        facts = _rows(session,
            f"SELECT ts.value FROM hydro.{table} ts "
            "WHERE ts.run_key = (SELECT run_key FROM hydro.hydro_run WHERE run_id = :run_id) "
            "AND ts.valid_time = :valid_time ORDER BY ts.value", params)
        assert [row["value"] for row in facts] == (
            [10, 20, 30] if physical_store == store else [10010, 10020, 10030]
        )
    columns = _rows(session,
        "SELECT column_name FROM information_schema.columns "
        "WHERE table_schema = 'hydro' AND table_name = 'river_timeseries'", {})
    assert not {"run_id", "river_network_version_id", "river_segment_id", "variable"} & {
        row["column_name"] for row in columns
    }
    result = _rows(session, postgis_tile_sql("hydro"), bind)
    assert len(result) == 1
    assert {key: value for key, value in result[0].items() if key != "tile"} == {
        key: value for key, value in baseline[0].items() if key != "tile"
    }
    actual = mapbox_vector_tile.decode(bytes(result[0]["tile"]))
    assert actual == decoded
    features = actual["hydro"]["features"]
    assert [feature["properties"]["segment_id"] for feature in features] == ["seg-a", "seg-b", "seg-c"]
    assert [feature["properties"]["value"] for feature in features] == [30, 20, 10]
    for feature, segment in zip(features, ("seg-a", "seg-b", "seg-c"), strict=True):
        assert feature["properties"] == {
            "feature_id": f"{_NETWORK_ID}::{segment}", "segment_id": segment,
            "river_segment_id": segment, "river_network_version_id": _NETWORK_ID,
            "basin_version_id": _BASIN_VERSION_ID, "value": {"seg-a": 30, "seg-b": 20, "seg-c": 10}[segment],
            "unit": "m3/s", "quality_flag": "ok", "run_id": _KEYED_RUN_ID,
            "variable": "q_down", "valid_time": "2026-06-01T00:00:00Z",
        }
        assert feature["geometry"]["type"] == "LineString"
    consumed = hydro_display._fetch_postgis_tile_bytes(session, "hydro", params, z=9, x=398, y=197)
    assert consumed == bytes(result[0]["tile"])
    assert mapbox_vector_tile.decode(consumed) == decoded


@pytest.mark.parametrize("store", ("legacy", "narrow"))
@pytest.mark.parametrize("case", ("unknown", "oov", "off-tile", "non-finite", "budget"))
def test_hydro_routed_real_consumer_preserves_failure_and_empty_outcomes(
    seeded: Any, post_expand_forecast_database: Callable[[Mapping[str, str]], None],
    store: str, case: str, monkeypatch: pytest.MonkeyPatch,
) -> None:
    _url, session = seeded
    monkeypatch.setenv("NHMS_ENABLE_LIVE_POSTGIS_MVT", "true")
    _prepare_hydro_stores(session, post_expand_forecast_database, store)
    params = _identity_params(_KEYED_RUN_ID)
    x, y = (0, 0) if case == "off-tile" else (398, 197)
    if case == "unknown":
        params["run_id"] = "run-does-not-exist"
    elif case == "oov":
        params["variable"] = "not_a_river_variable"
    elif case == "non-finite":
        table = "river_timeseries_legacy" if store == "legacy" else "river_timeseries"
        session.execute(text(
            f"UPDATE hydro.{table} ts SET value = 'NaN'::double precision "
            "FROM core.river_segment rs WHERE rs.river_segment_key = ts.river_segment_key "
            "AND rs.river_segment_id = 'seg-a' AND ts.valid_time = :valid_time "
            "AND ts.run_key = (SELECT run_key FROM hydro.hydro_run WHERE run_id = :run_id)"
        ), params)
    elif case == "budget":
        monkeypatch.setattr(hydro_display, "MVT_MAX_FEATURES", 2)
    bind = hydro_display._postgis_tile_params(params, z=9, x=x, y=y, layer="hydro")
    result = _rows(session, postgis_tile_sql("hydro"), bind)
    assert len(result) == 1
    row = result[0]
    if case == "off-tile":
        assert row["source_identity_count"] == 1
        assert row["feature_count"] == 0
        assert hydro_display._fetch_postgis_tile_bytes(session, "hydro", params, z=9, x=x, y=y) == b""
        return
    if case in {"unknown", "oov"}:
        assert row["source_identity_count"] == 0
        expected = (424, "MVT_LIVE_POSTGIS_UNAVAILABLE")
    elif case == "non-finite":
        assert row["invalid_property_count"] == 1
        assert row["invalid_properties"] == "value"
        expected = (500, "MVT_TILE_CONTRACT_INVALID")
    else:
        assert bind["feature_limit"] == 2
        assert row["feature_count"] == 3
        expected = (413, "MVT_TILE_BUDGET_EXCEEDED")
    with pytest.raises(ApiError) as raised:
        hydro_display._fetch_postgis_tile_bytes(session, "hydro", params, z=9, x=x, y=y)
    assert (raised.value.status_code, raised.value.code) == expected


@pytest.mark.parametrize("store", ("legacy", "narrow"))
def test_valid_times_named_identity_branch_is_field_identical_to_the_text_era_query(
    seeded: Any, post_expand_forecast_database: Callable[[Mapping[str, str]], None], store: str,
) -> None:
    _url, session = seeded
    params = {
        "run_id": _KEYED_RUN_ID,
        "basin_version_id": _BASIN_VERSION_ID,
        "river_network_version_id": _NETWORK_ID,
        "variable": _VARIABLE,
        "limit": 100,
    }

    oracle = [row["valid_time"] for row in _rows(session, _TEXT_ERA_VALID_TIMES_SQL, params)]
    session.rollback()
    post_expand_forecast_database({
        _KEYED_RUN_ID: store,
        _LEGACY_RUN_ID: "narrow" if store == "legacy" else "legacy",
        _ALL_LEGACY_RUN_ID: "legacy",
    })
    switched = valid_times_for_layer(
        session,
        "discharge",
        run_id=_KEYED_RUN_ID,
        basin_version_id=_BASIN_VERSION_ID,
        river_network_version_id=_NETWORK_ID,
    )

    assert oracle == [_T1, _T0]
    assert switched.observed_count == len(oracle)
    # ``_valid_time_discovery`` formats and sorts ascending, so the switched
    # answer must be the oracle's rows put through exactly that transform.
    assert switched.valid_times == sorted(
        value.astimezone(UTC).isoformat().replace("+00:00", "Z") for value in oracle
    )
    assert switched.valid_times == ["2026-06-01T00:00:00Z", "2026-06-01T01:00:00Z"]
    active_raw = render_river_ts_sql(_valid_times_named_source_template(store), store).sql
    assert {row["valid_time"] for row in _rows(session, active_raw, params)} == {_T0, _T1}
    assert _rows(session, active_raw, params | {"variable": "not_a_river_variable"}) == []


@pytest.mark.parametrize("store", ("legacy", "narrow"))
def test_any_identity_valid_times_combine_stores_before_distinct_and_limit(
    seeded: Any, post_expand_forecast_database: Callable[[Mapping[str, str]], None], store: str,
) -> None:
    url, session = seeded
    t3 = _T0 + timedelta(hours=3)
    t4 = _T0 + timedelta(hours=4)
    session.rollback()
    with psycopg2.connect(url) as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                "UPDATE hydro.hydro_run SET end_time = %s WHERE run_id IN (%s, %s)",
                (t4, _KEYED_RUN_ID, _LEGACY_RUN_ID),
            )
            assert cursor.rowcount == 2
            insert_river_timeseries_dual_written(cursor, [
                (
                    run_id, _BASIN_VERSION_ID, _NETWORK_ID, _SEGMENTS[0][0],
                    valid_time, hour, _VARIABLE, _SEGMENTS[0][2], "m3/s", "ok",
                )
                for run_id, valid_time, hour in (
                    (_KEYED_RUN_ID, t4, 4),
                    (_LEGACY_RUN_ID, t3, 3),
                    (_LEGACY_RUN_ID, t4, 4),
                )
            ])
    session.rollback()
    opposite = "narrow" if store == "legacy" else "legacy"
    post_expand_forecast_database({
        _KEYED_RUN_ID: store,
        _LEGACY_RUN_ID: opposite,
        _ALL_LEGACY_RUN_ID: "legacy",
    })

    # Read both physical sides independently: wrong-store copies must exist
    # and stay half an hour away, so DISTINCT cannot hide missing authority.
    for run_id, authoritative_store, expected in (
        (_KEYED_RUN_ID, store, {_T0, _T1, t4}),
        (_LEGACY_RUN_ID, opposite, {_T0, t3, t4}),
    ):
        for physical_store, table in (
            ("legacy", "river_timeseries_legacy"), ("narrow", "river_timeseries"),
        ):
            actual = {
                row["valid_time"] for row in _rows(
                    session,
                    f"SELECT ts.valid_time FROM hydro.{table} ts "
                    "JOIN hydro.hydro_run h ON h.run_key = ts.run_key "
                    "WHERE h.run_id = :run_id",
                    {"run_id": run_id},
                )
            }
            assert actual == (
                expected if physical_store == authoritative_store
                else {value + timedelta(minutes=30) for value in expected}
            )

    discovery = valid_times_for_layer(session, "discharge", limit=3)
    assert discovery.valid_times == [
        "2026-06-01T01:00:00Z", "2026-06-01T03:00:00Z", "2026-06-01T04:00:00Z",
    ]
    assert discovery.limit == 3
    assert discovery.observed_count == 4
    assert discovery.truncated is True
    for active_store, expected in ((store, {_T0, _T1, t4}), (opposite, {_T0, t3, t4})):
        active_raw = render_river_ts_sql(_valid_times_any_source_template(active_store), active_store).sql
        assert {row["valid_time"] for row in _rows(session, active_raw, {"variable": _VARIABLE})} == expected
        assert _rows(session, active_raw, {"variable": "not_a_river_variable"}) == []


@pytest.mark.parametrize("store", ("legacy", "narrow"))
def test_existence_probe_accepts_the_seeded_identity_and_404s_on_unknown_ones(
    seeded: Any, post_expand_forecast_database: Callable[[Mapping[str, str]], None], store: str,
) -> None:
    _url, session = seeded
    session.rollback()
    opposite = "narrow" if store == "legacy" else "legacy"
    post_expand_forecast_database({
        _KEYED_RUN_ID: store,
        _LEGACY_RUN_ID: opposite,
        _ALL_LEGACY_RUN_ID: "legacy",
    })
    metadata = hydro_display._run_row(session, _KEYED_RUN_ID)
    assert metadata.get("timeseries_store") == store
    mapped_store = metadata["timeseries_store"]

    # Keep the helper's +30 minute wrong-store facts: an opposite-store probe
    # must miss the requested instant rather than accidentally accepting it.
    for physical_store, table in (
        ("legacy", "river_timeseries_legacy"), ("narrow", "river_timeseries"),
    ):
        times = {
            row["valid_time"] for row in _rows(
                session,
                f"SELECT ts.valid_time FROM hydro.{table} ts "
                "JOIN hydro.hydro_run h ON h.run_key = ts.run_key "
                "WHERE h.run_id = :run_id",
                {"run_id": _KEYED_RUN_ID},
            )
        }
        assert times == (
            {_T0, _T1} if physical_store == store
            else {_T0 + timedelta(minutes=30), _T1 + timedelta(minutes=30)}
        )

    _require_hydro_mvt_source_identity(
        session,
        run_id=_KEYED_RUN_ID,
        variable=_VARIABLE,
        valid_time=_T0,
        basin_version_id=_BASIN_VERSION_ID,
        river_network_version_id=_NETWORK_ID,
        timeseries_store=mapped_store,
    )

    for unknown in (
        {"run_id": "run-does-not-exist"},
        {"basin_version_id": "bv-does-not-exist"},
        {"river_network_version_id": "rnv-does-not-exist"},
        {"variable": "not_a_river_variable"},
    ):
        arguments = {
            "run_id": _KEYED_RUN_ID,
            "variable": _VARIABLE,
            "valid_time": _T0,
            "basin_version_id": _BASIN_VERSION_ID,
            "river_network_version_id": _NETWORK_ID,
            "timeseries_store": mapped_store,
            **unknown,
        }
        with pytest.raises(ApiError) as raised:
            _require_hydro_mvt_source_identity(session, **arguments)
        assert raised.value.status_code == 404, unknown
        assert raised.value.code == "MVT_SOURCE_IDENTITY_NOT_FOUND"
        assert raised.value.details == {
            "layer_id": "discharge" if arguments["variable"] == _VARIABLE else "hydro:not_a_river_variable",
            "run_id": arguments["run_id"],
            "variable": arguments["variable"],
            "valid_time": "2026-06-01T00:00:00Z",
            "basin_version_id": arguments["basin_version_id"],
            "river_network_version_id": arguments["river_network_version_id"],
        }


# ---------------------------------------------------------------------------
# empty, never an error
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("store", ("legacy", "narrow"))
def test_unknown_identity_and_out_of_vocabulary_variable_return_empty_not_error(
    seeded: Any, post_expand_forecast_database: Callable[[Mapping[str, str]], None], store: str,
) -> None:
    _url, session = seeded

    _prepare_hydro_stores(session, post_expand_forecast_database, store)
    assert (
        valid_times_for_layer(
            session,
            "discharge",
            run_id="run-does-not-exist",
            basin_version_id=_BASIN_VERSION_ID,
            river_network_version_id=_NETWORK_ID,
        ).valid_times
        == []
    )
    query = _hydro_source_query(_source_cte_body("hydro"))

    assert _rows(session, query, _identity_params("run-does-not-exist")) == []
    assert _rows(session, query, _identity_params(_KEYED_RUN_ID, variable="not_a_river_variable")) == []


def test_the_rejected_enum_cast_really_would_have_raised_on_that_literal(seeded: Any) -> None:
    """Non-vacuity for the test above: the OOV literal is genuinely uncastable.

    Without this, "OOV returns empty" would also pass for a query whose enum
    matcher happened to be dead code.
    """
    _url, session = seeded

    with pytest.raises(Exception) as raised:
        session.execute(text("SELECT 'not_a_river_variable'::hydro.river_variable")).all()
    assert "not_a_river_variable" in str(raised.value)
    session.rollback()

    assert (
        session.execute(
            text(
                "SELECT count(*) FROM unnest(enum_range(NULL::hydro.river_variable)) e "
                "WHERE e::text = 'not_a_river_variable'"
            )
        ).scalar()
        == 0
    )


# ---------------------------------------------------------------------------
# the NULL-key exclusion contract
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("store", ("legacy", "narrow"))
def test_null_key_legacy_rows_are_invisible_to_the_switched_reads(
    seeded: Any, post_expand_forecast_database: Callable[[Mapping[str, str]], None], store: str,
) -> None:
    """Excluded by design, with the text oracle proving the rows are really there."""
    _url, session = seeded
    params = _identity_params(_LEGACY_RUN_ID)

    oracle = _rows(session, _hydro_source_query(_TEXT_ERA_HYDRO_SOURCE_CTE), params)


    # Same exclusion at the valid-time discovery surface: the legacy-only hour
    # exists in the table and is not advertised.
    text_era_times = [
        row["valid_time"]
        for row in _rows(
            session,
            _TEXT_ERA_VALID_TIMES_SQL,
            {
                "run_id": _LEGACY_RUN_ID,
                "basin_version_id": _BASIN_VERSION_ID,
                "river_network_version_id": _NETWORK_ID,
                "variable": _VARIABLE,
                "limit": 100,
            },
        )
    ]
    assert text_era_times == [_LEGACY_ONLY_TIME, _T0]
    _prepare_hydro_stores(session, post_expand_forecast_database, store, _LEGACY_RUN_ID)
    discovery = valid_times_for_layer(
        session,
        "discharge",
        run_id=_LEGACY_RUN_ID,
        basin_version_id=_BASIN_VERSION_ID,
        river_network_version_id=_NETWORK_ID,
    )
    assert discovery.valid_times == ["2026-06-01T00:00:00Z"]
    switched = _rows(session, _hydro_source_query(_source_cte_body("hydro")), params)
    switched_segments = [row["river_segment_id"] for row in switched]
    oracle_segments = [row["river_segment_id"] for row in oracle]
    assert set(oracle_segments) - set(switched_segments) == {_LEGACY_SEGMENT[0]}
    assert switched_segments == [segment_id for segment_id, _type, _value in _SEGMENTS]


@pytest.mark.parametrize("store", ("legacy", "narrow"))
def test_national_legs_agree_on_null_key_visibility_across_the_zoom_split(
    seeded: Any, post_expand_forecast_database: Callable[[Mapping[str, str]], None], store: str,
) -> None:
    """One national identity, two zoom branches, one visibility answer.

    z>=9 reads through ``typed_values`` and z<9 through ``untyped_ranked``. The
    legacy segment carries the largest value and no stream class, so at z=5 its
    PERCENT_RANK is 1.0 and it clears the cutoff: had the untyped leg kept the
    text predicates, it would appear at z=5 and vanish at z=9.
    """
    _url, session = seeded
    query = _national_source_query(_frozen_national_source_body())

    detail_x, detail_y = _tile_xy(_SEGMENT_LON, _SEGMENT_LAT, 9)
    overview_x, overview_y = _tile_xy(_SEGMENT_LON, _SEGMENT_LAT, 5)
    detail = _rows(
        session,
        query,
        _NATIONAL_TILE_PARAMS | {"valid_time": _T0, "z": 9, "x": detail_x, "y": detail_y},
    )
    overview = _rows(
        session,
        query,
        _NATIONAL_TILE_PARAMS | {"valid_time": _T0, "z": 5, "x": overview_x, "y": overview_y},
    )
    baseline_detail, baseline_overview = detail, overview
    _prepare_hydro_stores(session, post_expand_forecast_database, store, _LEGACY_RUN_ID)
    query = _national_source_query(_source_cte_body("hydro-national"))
    detail = _rows(session, query, _NATIONAL_TILE_PARAMS | {
        "valid_time": _T0, "z": 9, "x": detail_x, "y": detail_y,
    })
    overview = _rows(session, query, _NATIONAL_TILE_PARAMS | {
        "valid_time": _T0, "z": 5, "x": overview_x, "y": overview_y,
    })
    assert detail == baseline_detail
    assert overview == baseline_overview

    detail_segments = {row["river_segment_id"] for row in detail}
    overview_segments = {row["river_segment_id"] for row in overview}

    assert detail_segments == {segment_id for segment_id, _type, _value in _SEGMENTS}
    # seg-a via the typed leg (stream class 5 clears the z5 threshold), seg-b
    # via the untyped leg (top of its network's value distribution).
    assert overview_segments == {"seg-a", "seg-b"}
    assert _LEGACY_SEGMENT[0] not in detail_segments
    assert _LEGACY_SEGMENT[0] not in overview_segments

    # The rows that do render still carry the restored text identity.
    for row in (*detail, *overview):
        assert row["run_id"] == _LEGACY_RUN_ID
        assert row["river_network_version_id"] == _NETWORK_ID
        assert row["basin_version_id"] == _BASIN_VERSION_ID
        assert row["basin_id"] == _BASIN_ID
        assert row["unit"] == "m3/s"
        assert row["quality_flag"] == "ok"
        assert row["variable"] == _VARIABLE
        assert row["feature_id"] == f"{_NETWORK_ID}::{row['river_segment_id']}"


@pytest.mark.parametrize("store", ("legacy", "narrow"))
def test_national_rows_carry_the_right_measurement_and_geometry_per_segment(
    seeded: Any, post_expand_forecast_database: Callable[[Mapping[str, str]], None], store: str,
) -> None:
    """Per-segment payload, not just the run/network constants both legs share.

    Identity fields are the same string on every national row, so asserting
    only those cannot tell a correct key join from one that pairs a segment's
    geometry with another segment's value — precisely the failure mode of
    swapping a two-column text join for a single surrogate key. The seed gives
    the three segments distinct values (30/20/10) and distinct geometries, so
    any mispairing shows up here.

    Oracles are independent of the code under test: values come from the seed
    constants, geometry straight from ``core.river_segment``. At z>=9 the layer
    emits ``rs.geom`` unmodified, so that comparison is exact; the z<9 branch
    simplifies, so only its value/identity payload is compared.
    """
    _url, session = seeded
    query = _national_source_query(_frozen_national_source_body())

    detail_x, detail_y = _tile_xy(_SEGMENT_LON, _SEGMENT_LAT, 9)
    detail = _rows(
        session,
        query,
        _NATIONAL_TILE_PARAMS | {"valid_time": _T0, "z": 9, "x": detail_x, "y": detail_y},
    )

    expected_values = {segment_id: value for segment_id, _type, value in _SEGMENTS}
    assert len(set(expected_values.values())) == len(expected_values), "values must be distinct to be diagnostic"
    authority_geometry = {
        row["river_segment_id"]: row["geom_wkt"]
        for row in _rows(
            session,
            """
            SELECT river_segment_id, ST_AsEWKT(geom) AS geom_wkt
            FROM core.river_segment
            WHERE river_network_version_id = :river_network_version_id
            """,
            {"river_network_version_id": _NETWORK_ID},
        )
    }
    assert len(set(authority_geometry.values())) == len(_SEGMENTS) + 1, "geometries must be distinct too"
    baseline_detail = detail
    baseline_later = _rows(session, query, _NATIONAL_TILE_PARAMS | {
        "valid_time": _T1, "z": 9, "x": detail_x, "y": detail_y,
    })
    assert baseline_later == []
    _prepare_hydro_stores(session, post_expand_forecast_database, store, _LEGACY_RUN_ID)
    query = _national_source_query(_source_cte_body("hydro-national"))
    detail = _rows(session, query, _NATIONAL_TILE_PARAMS | {
        "valid_time": _T0, "z": 9, "x": detail_x, "y": detail_y,
    })
    assert detail == baseline_detail

    assert len(detail) == len(_SEGMENTS)
    for row in detail:
        segment_id = row["river_segment_id"]
        assert float(row["value"]) == pytest.approx(expected_values[segment_id]), segment_id
        assert row["geom_wkt"] == authority_geometry[segment_id], segment_id
        assert row["valid_time"] == "2026-06-01T00:00:00Z", segment_id
        assert row["segment_id"] == segment_id

    # Non-vacuity for the valid_time predicate: the run the national layer
    # selects (`_LEGACY_RUN_ID`) has key-carrying rows only at _T0, while its
    # coverage window advertises _T1 as well. So the same query one hour later
    # must come back empty rather than repeating the _T0 payload.
    later = _rows(
        session,
        query,
        _NATIONAL_TILE_PARAMS | {"valid_time": _T1, "z": 9, "x": detail_x, "y": detail_y},
    )
    assert later == []


# ---------------------------------------------------------------------------
# display coverage
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("store", ("legacy", "narrow"))
@pytest.mark.parametrize("all_runs", (False, True), ids=("named", "all-runs"))
def test_display_coverage_river_rollup_counts_keyed_rows_and_skips_null_key_rows(
    seeded: Any,
    post_expand_forecast_database: Callable[[Mapping[str, str]], None],
    store: str,
    all_runs: bool,
) -> None:
    url, session = seeded
    session.rollback()
    # The national seed gives the second run a later cycle for route selection.
    # Coverage needs its original T0 facts inside the candidate window as well.
    with psycopg2.connect(url) as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                "UPDATE hydro.hydro_run SET cycle_time = %s WHERE run_id = %s",
                (_T0, _LEGACY_RUN_ID),
            )
            assert cursor.rowcount == 1
    post_expand_forecast_database({
        _KEYED_RUN_ID: store,
        _LEGACY_RUN_ID: "narrow" if store == "legacy" else "legacy",
        _ALL_LEGACY_RUN_ID: "legacy",
    })
    connection = psycopg2.connect(url, cursor_factory=RealDictCursor)
    try:
        protected = _assert_all_legacy_preconditions(connection)
        if all_runs:
            outcome = _refresh(connection, None)
            assert set(outcome.refreshed) == {_KEYED_RUN_ID, _LEGACY_RUN_ID}
            assert outcome.refused == []
            connection.commit()
        else:
            assert refresh_run_display_coverage(connection, _KEYED_RUN_ID) is True
        expected = {
            _KEYED_RUN_ID: {
                "segment_count": 3, "river_sample_count": 6,
                "river_valid_time_start": _T0, "river_valid_time_end": _T1,
                "min_lead_time_hours": 0, "max_lead_time_hours": 1,
            },
        }
        if all_runs:
            expected[_LEGACY_RUN_ID] = {
                "segment_count": 3, "river_sample_count": 3,
                "river_valid_time_start": _T0, "river_valid_time_end": _T0,
                "min_lead_time_hours": 0, "max_lead_time_hours": 0,
            }
        for run_id, fields in expected.items():
            actual = _coverage(connection, run_id)
            assert {key: actual[key] for key in fields} == fields
        assert _coverage(connection, _ALL_LEGACY_RUN_ID) == protected
    finally:
        connection.close()


_COVERAGE_COLUMNS_SQL = """
    SELECT segment_count, river_sample_count,
           river_valid_time_start, river_valid_time_end,
           min_lead_time_hours, max_lead_time_hours, refreshed_at
    FROM hydro.run_display_coverage WHERE run_id = %s
"""

_ZEROED_COVERAGE = {
    "segment_count": 0,
    "river_sample_count": 0,
    "river_valid_time_start": None,
    "river_valid_time_end": None,
    "min_lead_time_hours": None,
    "max_lead_time_hours": None,
}


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


def test_refreshing_coverage_for_an_all_null_key_run_is_refused_not_zeroed(
    seeded: Any, post_expand_forecast_database: Callable[[Mapping[str, str]], None],
) -> None:
    """#1446: the guard holds on a real database, against the real hazard shape.

    Re-scanning a run whose rows are all pre-#1340 does not merely fail to find
    them: ``coverage`` is built ``FROM candidate_runs`` with a LEFT JOIN to the
    river rollup, so the run still produces an upsert row — with
    ``COALESCE(..., 0)`` counts and NULL valid-time bounds. Before #1446 that
    row overwrote the correct values the text era had materialized, dropping
    the run out of latest-product readiness and off the national tile.

    The conditional ``DO UPDATE ... WHERE`` now skips it. This is the only test
    that executes that clause: the unit suite's cursor is a fake and cannot
    evaluate SQL.
    """
    url, _session = seeded
    _session.rollback()
    post_expand_forecast_database({_ALL_LEGACY_RUN_ID: "legacy"})
    connection = psycopg2.connect(url, cursor_factory=RealDictCursor)
    try:
        before = _assert_all_legacy_preconditions(connection)

        with pytest.raises(DisplayCoverageRefreshRefused) as excinfo:
            refresh_run_display_coverage(connection, _ALL_LEGACY_RUN_ID)

        assert excinfo.value.run_id == _ALL_LEGACY_RUN_ID
        assert excinfo.value.existing_segment_count == len(_SEGMENTS)

        # Whole-row skip (design D1): nothing moved, `refreshed_at` included —
        # which is what keeps the run stale and rescanned until #1408 heals it.
        assert _coverage(connection, _ALL_LEGACY_RUN_ID) == before
    finally:
        connection.close()


def test_forced_refresh_of_an_all_null_key_run_performs_the_zeroing(
    seeded: Any, post_expand_forecast_database: Callable[[Mapping[str, str]], None],
) -> None:
    """The escape hatch still works — and is the ONLY way to zero the row."""
    url, _session = seeded
    _session.rollback()
    post_expand_forecast_database({_ALL_LEGACY_RUN_ID: "legacy"})
    connection = psycopg2.connect(url, cursor_factory=RealDictCursor)
    try:
        before = _assert_all_legacy_preconditions(connection)

        assert refresh_run_display_coverage(connection, _ALL_LEGACY_RUN_ID, force=True) is True

        after = _coverage(connection, _ALL_LEGACY_RUN_ID)
        assert {key: after[key] for key in _ZEROED_COVERAGE} == _ZEROED_COVERAGE
        assert after["refreshed_at"] > before["refreshed_at"]
    finally:
        connection.close()


def test_an_existing_zero_row_is_still_rewritten_by_an_empty_scan(
    seeded: Any, post_expand_forecast_database: Callable[[Mapping[str, str]], None],
) -> None:
    """The guard's third disjunct: a row that already reads 0 is not protected.

    Chained after the force, because that is exactly the state that matters —
    once a run has been zeroed deliberately, ordinary refreshes must keep
    bumping its ``refreshed_at`` so it stops being reported stale forever.
    """
    url, _session = seeded
    _session.rollback()
    post_expand_forecast_database({_ALL_LEGACY_RUN_ID: "legacy"})
    connection = psycopg2.connect(url, cursor_factory=RealDictCursor)
    try:
        assert refresh_run_display_coverage(connection, _ALL_LEGACY_RUN_ID, force=True) is True
        zeroed = _coverage(connection, _ALL_LEGACY_RUN_ID)
        assert zeroed["segment_count"] == 0

        # No force this time: the row reads 0, so the guard lets it through.
        assert refresh_run_display_coverage(connection, _ALL_LEGACY_RUN_ID) is True

        after = _coverage(connection, _ALL_LEGACY_RUN_ID)
        assert {key: after[key] for key in _ZEROED_COVERAGE} == _ZEROED_COVERAGE
        assert after["refreshed_at"] > zeroed["refreshed_at"]
    finally:
        connection.close()


def test_first_refresh_with_no_existing_row_writes_zero(
    seeded: Any, post_expand_forecast_database: Callable[[Mapping[str, str]], None],
) -> None:
    """A first refresh is an INSERT and never reaches the DO UPDATE guard.

    The guard must not make an empty run unmaterializable: without a stored
    row there is nothing to protect, so the zero row is written exactly as
    before #1446.
    """
    url, _session = seeded
    _session.rollback()
    post_expand_forecast_database({_ALL_LEGACY_RUN_ID: "legacy"})
    connection = psycopg2.connect(url, cursor_factory=RealDictCursor)
    try:
        with connection.cursor() as cursor:
            cursor.execute(
                "DELETE FROM hydro.run_display_coverage WHERE run_id = %s",
                (_ALL_LEGACY_RUN_ID,),
            )
        connection.commit()
        assert _coverage(connection, _ALL_LEGACY_RUN_ID) is None

        assert refresh_run_display_coverage(connection, _ALL_LEGACY_RUN_ID) is True

        after = _coverage(connection, _ALL_LEGACY_RUN_ID)
        assert {key: after[key] for key in _ZEROED_COVERAGE} == _ZEROED_COVERAGE
    finally:
        connection.close()


def test_keyed_run_refresh_is_never_refused(
    seeded: Any, post_expand_forecast_database: Callable[[Mapping[str, str]], None],
) -> None:
    """Non-vacuity for the guard: a run whose scan finds segments is unaffected.

    Refreshed twice in a row — the second call is the one that would trip a
    guard written against "has an existing row" rather than against "the fresh
    scan is empty".
    """
    url, _session = seeded
    _session.rollback()
    post_expand_forecast_database({_ALL_LEGACY_RUN_ID: "legacy"})
    connection = psycopg2.connect(url, cursor_factory=RealDictCursor)
    try:
        assert refresh_run_display_coverage(connection, _KEYED_RUN_ID) is True
        first = _coverage(connection, _KEYED_RUN_ID)
        assert first["segment_count"] == len(_SEGMENTS)

        assert refresh_run_display_coverage(connection, _KEYED_RUN_ID) is True
        second = _coverage(connection, _KEYED_RUN_ID)
        assert second["segment_count"] == len(_SEGMENTS)
        assert second["refreshed_at"] > first["refreshed_at"]
    finally:
        connection.close()
