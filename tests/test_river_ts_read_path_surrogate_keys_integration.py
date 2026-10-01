"""Real-database proof for the #1341 read-path surrogate-key switch.

Run with the repo's standard opt-in against a throwaway database:

    NHMS_RUN_INTEGRATION=1 NHMS_INTEGRATION_DATABASE_URL=... uv run pytest -q \
        tests/test_river_ts_read_path_surrogate_keys_integration.py \
        tests/test_river_ts_read_path_surrogate_keys_coverage_integration.py

#2490 split the proof into those two partitions (pure move); the seed, the
``seeded`` fixture and the text-era oracles live in
``tests/river_ts_read_path_surrogate_keys_integration_helpers.py``.

``throwaway_database_url`` (tests/conftest.py) creates and drops a
uniquely-named database per TEST, so nothing here can touch a live one.

Scope: the questions a text-substring pin cannot answer.

* **Field identity.** Each switched query is executed side by side with the
  text-era query it replaced — the pre-change SQL is embedded verbatim below
  as the oracle — over rows that carry BOTH the text columns and the surrogate
  keys. Every projected field, including the ``feature_id`` concatenation and
  the geometry, must match.
* **One store.** #1342's contract (task 6.3) deleted
  ``hydro.hydro_run.timeseries_store`` and the routing readers, so nothing here
  is parametrized over a store any more; the cases that existed only to
  discriminate between the two physical tables are gone with it, and the
  planner assertion that replaced them requires the legacy hypertable to be
  absent from every live caller's plan. The three frozen pre-transition SQL
  snapshots two of these tests diffed against were part of the deleted marker
  corpus; their properties are asserted against the seed instead, which is the
  stronger oracle anyway.
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

from collections.abc import Callable, Mapping
from datetime import UTC, timedelta
from typing import Any

import mapbox_vector_tile
import psycopg2
import pytest
from psycopg2.extras import RealDictCursor
from sqlalchemy import text

from apps.api.errors import ApiError
from apps.api.routes import hydro_display
from apps.api.routes.hydro_display import _require_hydro_mvt_source_identity
from packages.common.display_coverage import (
    _refresh,
    refresh_run_display_coverage,
)
from packages.common.river_ts_render import render_river_ts_sql
from services.tiles import mvt as mvt_module
from services.tiles.mvt import (
    _valid_times_any_source_template,
    _valid_times_named_source_template,
    postgis_tile_sql,
    valid_times_for_layer,
)
from tests.integration_helpers import (
    insert_river_timeseries_dual_written,
)
from tests.integration_helpers import (
    post_expand_forecast_database as post_expand_forecast_database,
)
from tests.river_ts_read_path_surrogate_keys_integration_helpers import (
    _ALL_LEGACY_RUN_ID,
    _BASIN_ID,
    _BASIN_VERSION_ID,
    _KEYED_RUN_ID,
    _LEGACY_ONLY_TIME,
    _LEGACY_RUN_ID,
    _LEGACY_SEGMENT,
    _NARROW_STORE_MAP,
    _NATIONAL_TILE_PARAMS,
    _NETWORK_ID,
    _SEGMENT_LAT,
    _SEGMENT_LON,
    _SEGMENTS,
    _T0,
    _T1,
    _TEXT_ERA_HYDRO_SOURCE_CTE,
    _TEXT_ERA_VALID_TIMES_SQL,
    _VARIABLE,
    _assert_all_legacy_preconditions,
    _coverage,
    _expand,
    _hydro_source_query,
    _identity_params,
    _national_source_query,
    _rows,
    _source_cte_body,
    _tile_xy,
)
from tests.river_ts_read_path_surrogate_keys_integration_helpers import (
    seeded as seeded,
)

pytestmark = pytest.mark.integration


def test_hydro_tile_source_rows_are_field_identical_to_the_text_era_query(
    seeded: Any, post_expand_forecast_database: Callable[[Mapping[str, str]], None],
) -> None:
    _url, session = seeded
    params = _identity_params(_KEYED_RUN_ID)

    oracle = _rows(session, _hydro_source_query(_TEXT_ERA_HYDRO_SOURCE_CTE), params)
    _expand(session, post_expand_forecast_database)
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


def test_hydro_tile_produces_one_mvt_result_and_the_seeded_decoded_payload(
    seeded: Any, post_expand_forecast_database: Callable[[Mapping[str, str]], None],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """One row, three features, every property compared against the seed.

    Its pre-transition oracle was the frozen ``hydro_mvt_pre_store_f33441a2.sql``
    snapshot, deleted with #1342's marker corpus (task 6.3). Nothing is lost:
    the expectations below are the seed constants, not a recorded rendering, so
    they are a stronger oracle than a byte-frozen copy of the old statement —
    and ``test_hydro_tile_source_rows_are_field_identical_to_the_text_era_query``
    still runs the inline text-era CTE side by side over the same rows.
    """
    _url, session = seeded
    monkeypatch.setenv("NHMS_ENABLE_LIVE_POSTGIS_MVT", "true")
    assert _tile_xy(100.0, 38.0, 9) == (398, 197)
    params = _identity_params(_KEYED_RUN_ID, _T0)
    bind = hydro_display._postgis_tile_params(params, z=9, x=398, y=197, layer="hydro")
    _expand(session, post_expand_forecast_database)
    facts = _rows(session,
        "SELECT ts.value FROM hydro.river_timeseries ts "
        "WHERE ts.run_key = (SELECT run_key FROM hydro.hydro_run WHERE run_id = :run_id) "
        "AND ts.valid_time = :valid_time ORDER BY ts.value", params)
    assert [row["value"] for row in facts] == [10, 20, 30]
    columns = _rows(session,
        "SELECT column_name FROM information_schema.columns "
        "WHERE table_schema = 'hydro' AND table_name = 'river_timeseries'", {})
    assert not {"run_id", "river_network_version_id", "river_segment_id", "variable"} & {
        row["column_name"] for row in columns
    }
    result = _rows(session, postgis_tile_sql("hydro"), bind)
    assert len(result) == 1
    assert result[0]["source_identity_count"] == 1
    assert result[0]["feature_count"] == len(_SEGMENTS)
    assert result[0]["invalid_property_count"] == 0
    actual = mapbox_vector_tile.decode(bytes(result[0]["tile"]))
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
    assert mapbox_vector_tile.decode(consumed) == actual


@pytest.mark.parametrize("case", ("unknown", "oov", "off-tile", "non-finite", "budget"))
def test_hydro_real_consumer_preserves_failure_and_empty_outcomes(
    seeded: Any, post_expand_forecast_database: Callable[[Mapping[str, str]], None],
    case: str, monkeypatch: pytest.MonkeyPatch,
) -> None:
    _url, session = seeded
    monkeypatch.setenv("NHMS_ENABLE_LIVE_POSTGIS_MVT", "true")
    _expand(session, post_expand_forecast_database)
    params = _identity_params(_KEYED_RUN_ID)
    x, y = (0, 0) if case == "off-tile" else (398, 197)
    if case == "unknown":
        params["run_id"] = "run-does-not-exist"
    elif case == "oov":
        params["variable"] = "not_a_river_variable"
    elif case == "non-finite":
        session.execute(text(
            "UPDATE hydro.river_timeseries ts SET value = 'NaN'::double precision "
            "FROM core.river_segment rs WHERE rs.river_segment_key = ts.river_segment_key "
            "AND rs.river_segment_id = 'seg-a' AND ts.valid_time = :valid_time "
            "AND ts.run_key = (SELECT run_key FROM hydro.hydro_run WHERE run_id = :run_id)"
        ), params)
    elif case == "budget":
        # Re-targeted by #2165 (design D4 contract change, not a loosening): the
        # bind and the 413 predicate now read `services.tiles.mvt.feature_limit`,
        # which reads `MVT_MAX_FEATURES` from its own module, so that is where the
        # lowered budget has to land. The assertions below are unchanged.
        monkeypatch.setattr(mvt_module, "MVT_MAX_FEATURES", 2)
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


def test_valid_times_named_identity_branch_is_field_identical_to_the_text_era_query(
    seeded: Any, post_expand_forecast_database: Callable[[Mapping[str, str]], None],
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
    _expand(session, post_expand_forecast_database)
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
    active_raw = render_river_ts_sql(_valid_times_named_source_template("narrow"), "narrow").sql
    assert {row["valid_time"] for row in _rows(session, active_raw, params)} == {_T0, _T1}
    assert _rows(session, active_raw, params | {"variable": "not_a_river_variable"}) == []


def test_any_identity_valid_times_are_distinct_and_limited_across_runs(
    seeded: Any, post_expand_forecast_database: Callable[[Mapping[str, str]], None],
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
    _expand(session, post_expand_forecast_database)

    # The two runs contribute overlapping but unequal instant sets, so a
    # DISTINCT that dropped one run's rows, or a limit applied per run rather
    # than to the combined set, would show up in the discovery below.
    for run_id, expected in (
        (_KEYED_RUN_ID, {_T0, _T1, t4}),
        (_LEGACY_RUN_ID, {_T0, t3, t4}),
    ):
        actual = {
            row["valid_time"] for row in _rows(
                session,
                "SELECT ts.valid_time FROM hydro.river_timeseries ts "
                "JOIN hydro.hydro_run h ON h.run_key = ts.run_key "
                "WHERE h.run_id = :run_id",
                {"run_id": run_id},
            )
        }
        assert actual == expected

    discovery = valid_times_for_layer(session, "discharge", limit=3)
    assert discovery.valid_times == [
        "2026-06-01T01:00:00Z", "2026-06-01T03:00:00Z", "2026-06-01T04:00:00Z",
    ]
    assert discovery.limit == 3
    assert discovery.observed_count == 4
    assert discovery.truncated is True
    active_raw = render_river_ts_sql(_valid_times_any_source_template("narrow"), "narrow").sql
    assert {row["valid_time"] for row in _rows(session, active_raw, {"variable": _VARIABLE})} == {
        _T0, _T1, t3, t4,
    }
    assert _rows(session, active_raw, {"variable": "not_a_river_variable"}) == []


def test_existence_probe_accepts_the_seeded_identity_and_404s_on_unknown_ones(
    seeded: Any, post_expand_forecast_database: Callable[[Mapping[str, str]], None],
) -> None:
    _url, session = seeded
    _expand(session, post_expand_forecast_database)

    # Non-vacuity: the probe really does have the requested instants to find.
    times = {
        row["valid_time"] for row in _rows(
            session,
            "SELECT ts.valid_time FROM hydro.river_timeseries ts "
            "JOIN hydro.hydro_run h ON h.run_key = ts.run_key "
            "WHERE h.run_id = :run_id",
            {"run_id": _KEYED_RUN_ID},
        )
    }
    assert times == {_T0, _T1}

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


def test_unknown_identity_and_out_of_vocabulary_variable_return_empty_not_error(
    seeded: Any, post_expand_forecast_database: Callable[[Mapping[str, str]], None],
) -> None:
    _url, session = seeded

    _expand(session, post_expand_forecast_database)
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


def test_null_key_legacy_rows_are_invisible_to_the_switched_reads(
    seeded: Any, post_expand_forecast_database: Callable[[Mapping[str, str]], None],
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
    _expand(session, post_expand_forecast_database)
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


def test_national_legs_agree_on_null_key_visibility_across_the_zoom_split(
    seeded: Any, post_expand_forecast_database: Callable[[Mapping[str, str]], None],
) -> None:
    """One national identity, two zoom branches, one visibility answer.

    z>=9 reads through ``typed_values`` and z<9 through ``untyped_ranked``. The
    legacy segment carries the largest value and no stream class, so at z=5 its
    PERCENT_RANK is 1.0 and it clears the cutoff: had the untyped leg kept the
    text predicates, it would appear at z=5 and vanish at z=9.

    The pre-transition national snapshot this used to diff against went with
    #1342's marker corpus (task 6.3). The property under test never needed it:
    it is an agreement between the two live legs and the seed, both asserted
    directly below.
    """
    _url, session = seeded

    detail_x, detail_y = _tile_xy(_SEGMENT_LON, _SEGMENT_LAT, 9)
    overview_x, overview_y = _tile_xy(_SEGMENT_LON, _SEGMENT_LAT, 5)
    _expand(session, post_expand_forecast_database)
    query = _national_source_query(_source_cte_body("hydro-national"))
    detail = _rows(session, query, _NATIONAL_TILE_PARAMS | {
        "valid_time": _T0, "z": 9, "x": detail_x, "y": detail_y,
    })
    overview = _rows(session, query, _NATIONAL_TILE_PARAMS | {
        "valid_time": _T0, "z": 5, "x": overview_x, "y": overview_y,
    })

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


def test_national_rows_carry_the_right_measurement_and_geometry_per_segment(
    seeded: Any, post_expand_forecast_database: Callable[[Mapping[str, str]], None],
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

    detail_x, detail_y = _tile_xy(_SEGMENT_LON, _SEGMENT_LAT, 9)
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
    _expand(session, post_expand_forecast_database)
    query = _national_source_query(_source_cte_body("hydro-national"))
    detail = _rows(session, query, _NATIONAL_TILE_PARAMS | {
        "valid_time": _T0, "z": 9, "x": detail_x, "y": detail_y,
    })

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


@pytest.mark.parametrize("all_runs", (False, True), ids=("named", "all-runs"))
def test_display_coverage_river_rollup_counts_keyed_rows_and_skips_null_key_rows(
    seeded: Any,
    post_expand_forecast_database: Callable[[Mapping[str, str]], None],
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
    post_expand_forecast_database(_NARROW_STORE_MAP)
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
