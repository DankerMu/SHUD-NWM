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
* **Degradation, not error.** An unknown ``run_id`` and an out-of-vocabulary
  ``variable`` must both return the empty result the text predicates returned.
  The ``enum_range`` matcher is proved to be load-bearing by showing the cast
  it replaced really does raise on the same literal.
"""

from __future__ import annotations

import math
from datetime import UTC, datetime, timedelta
from typing import Any

import psycopg2
import pytest
from psycopg2.extras import RealDictCursor
from sqlalchemy import text
from sqlalchemy.orm import Session

from apps.api.errors import ApiError
from apps.api.routes.hydro_display import _require_hydro_mvt_source_identity
from packages.common.display_coverage import refresh_run_display_coverage
from services.tiles.mvt import postgis_tile_sql, valid_times_for_layer
from tests.integration_helpers import apply_migrations_from_zero, sqlalchemy_engine

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

_T0 = datetime(2026, 6, 1, tzinfo=UTC)
_T1 = _T0 + timedelta(hours=1)
_LEGACY_ONLY_TIME = _T0 + timedelta(hours=2)

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
               variable, valid_time
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
            for run_id, cycle_time in ((_KEYED_RUN_ID, _T0), (_LEGACY_RUN_ID, _T1)):
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

            # Only the legacy run is display-covered, so the national layer's
            # DISTINCT ON deterministically selects the run that owns the
            # NULL-key rows — the interesting case for the zoom split.
            cursor.execute(
                """
                INSERT INTO hydro.run_display_coverage
                    (run_id, segment_count, river_sample_count,
                     river_valid_time_start, river_valid_time_end)
                VALUES (%s, %s, %s, %s, %s)
                """,
                (_LEGACY_RUN_ID, len(_SEGMENTS), len(_SEGMENTS), _T0, _LEGACY_ONLY_TIME),
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


def test_hydro_tile_source_rows_are_field_identical_to_the_text_era_query(seeded: Any) -> None:
    _url, session = seeded
    params = _identity_params(_KEYED_RUN_ID)

    switched = _rows(session, _hydro_source_query(_source_cte_body("hydro")), params)
    oracle = _rows(session, _hydro_source_query(_TEXT_ERA_HYDRO_SOURCE_CTE), params)

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


def test_valid_times_named_identity_branch_is_field_identical_to_the_text_era_query(seeded: Any) -> None:
    _url, session = seeded
    params = {
        "run_id": _KEYED_RUN_ID,
        "basin_version_id": _BASIN_VERSION_ID,
        "river_network_version_id": _NETWORK_ID,
        "variable": _VARIABLE,
        "limit": 100,
    }

    oracle = [row["valid_time"] for row in _rows(session, _TEXT_ERA_VALID_TIMES_SQL, params)]
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


def test_existence_probe_accepts_the_seeded_identity_and_404s_on_unknown_ones(seeded: Any) -> None:
    _url, session = seeded

    _require_hydro_mvt_source_identity(
        session,
        run_id=_KEYED_RUN_ID,
        variable=_VARIABLE,
        valid_time=_T0,
        basin_version_id=_BASIN_VERSION_ID,
        river_network_version_id=_NETWORK_ID,
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
            **unknown,
        }
        with pytest.raises(ApiError) as raised:
            _require_hydro_mvt_source_identity(session, **arguments)
        assert raised.value.status_code == 404, unknown
        assert raised.value.code == "MVT_SOURCE_IDENTITY_NOT_FOUND"


# ---------------------------------------------------------------------------
# empty, never an error
# ---------------------------------------------------------------------------


def test_unknown_identity_and_out_of_vocabulary_variable_return_empty_not_error(seeded: Any) -> None:
    _url, session = seeded
    query = _hydro_source_query(_source_cte_body("hydro"))

    assert _rows(session, query, _identity_params("run-does-not-exist")) == []
    assert _rows(session, query, _identity_params(_KEYED_RUN_ID, variable="not_a_river_variable")) == []

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


def test_null_key_legacy_rows_are_invisible_to_the_switched_reads(seeded: Any) -> None:
    """Excluded by design, with the text oracle proving the rows are really there."""
    _url, session = seeded
    params = _identity_params(_LEGACY_RUN_ID)

    switched = _rows(session, _hydro_source_query(_source_cte_body("hydro")), params)
    oracle = _rows(session, _hydro_source_query(_TEXT_ERA_HYDRO_SOURCE_CTE), params)

    switched_segments = [row["river_segment_id"] for row in switched]
    oracle_segments = [row["river_segment_id"] for row in oracle]
    assert set(oracle_segments) - set(switched_segments) == {_LEGACY_SEGMENT[0]}
    assert switched_segments == [segment_id for segment_id, _type, _value in _SEGMENTS]

    # Same exclusion at the valid-time discovery surface: the legacy-only hour
    # exists in the table and is not advertised.
    discovery = valid_times_for_layer(
        session,
        "discharge",
        run_id=_LEGACY_RUN_ID,
        basin_version_id=_BASIN_VERSION_ID,
        river_network_version_id=_NETWORK_ID,
    )
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
    assert discovery.valid_times == ["2026-06-01T00:00:00Z"]


def test_national_legs_agree_on_null_key_visibility_across_the_zoom_split(seeded: Any) -> None:
    """One national identity, two zoom branches, one visibility answer.

    z>=9 reads through ``typed_values`` and z<9 through ``untyped_ranked``. The
    legacy segment carries the largest value and no stream class, so at z=5 its
    PERCENT_RANK is 1.0 and it clears the cutoff: had the untyped leg kept the
    text predicates, it would appear at z=5 and vanish at z=9.
    """
    _url, session = seeded
    query = _national_source_query(_source_cte_body("hydro-national"))

    detail_x, detail_y = _tile_xy(_SEGMENT_LON, _SEGMENT_LAT, 9)
    overview_x, overview_y = _tile_xy(_SEGMENT_LON, _SEGMENT_LAT, 5)
    detail = _rows(
        session,
        query,
        {"variable": _VARIABLE, "valid_time": _T0, "z": 9, "x": detail_x, "y": detail_y},
    )
    overview = _rows(
        session,
        query,
        {"variable": _VARIABLE, "valid_time": _T0, "z": 5, "x": overview_x, "y": overview_y},
    )

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


# ---------------------------------------------------------------------------
# display coverage
# ---------------------------------------------------------------------------


def test_display_coverage_river_rollup_counts_keyed_rows_and_skips_null_key_rows(seeded: Any) -> None:
    url, _session = seeded
    connection = psycopg2.connect(url, cursor_factory=RealDictCursor)
    try:
        assert refresh_run_display_coverage(connection, _KEYED_RUN_ID) is True
        with connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT segment_count, river_sample_count,
                       river_valid_time_start, river_valid_time_end,
                       min_lead_time_hours, max_lead_time_hours
                FROM hydro.run_display_coverage WHERE run_id = %s
                """,
                (_KEYED_RUN_ID,),
            )
            coverage = dict(cursor.fetchone())
    finally:
        connection.close()

    # Independent expectation from the seed: three segments x two hours, a
    # complete segment rectangle at both hours (rnv.segment_count is 3).
    assert coverage["segment_count"] == len(_SEGMENTS)
    assert coverage["river_sample_count"] == len(_SEGMENTS) * 2
    assert coverage["river_valid_time_start"] == _T0
    assert coverage["river_valid_time_end"] == _T1
    assert coverage["min_lead_time_hours"] == 0
    assert coverage["max_lead_time_hours"] == 1
