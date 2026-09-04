"""424 semantics oracle for the ``hydro-national`` identity-existence probe (#1596).

Run with the repo's standard opt-in against a throwaway database:

    NHMS_RUN_INTEGRATION=1 NHMS_INTEGRATION_DATABASE_URL=... uv run pytest -q \
        tests/test_mvt_national_identity_probe_integration.py

``throwaway_database_url`` (tests/conftest.py) creates and drops a
uniquely-named database per TEST, so nothing here can touch a live one.

Why this file exists
--------------------

#1596 reshapes ``source_identity_stats`` into a per-identity ``CROSS JOIN
LATERAL`` probe so the compressed chunks stop being decompressed whole. The
acceptance standard is that the probe's 0/1 answer is unchanged, and the whole
repo had ZERO tests on the branch it drives: ``grep
MVT_LIVE_POSTGIS_UNAVAILABLE tests/`` matched nothing before this file. So the
cheap-looking alternative — answer existence from ``hydro.run_display_coverage``
alone and never touch the fact table — had no oracle that could reject it.

It must be rejected, and the middle case below is what rejects it: the coverage
window is a MIN/MAX over *complete* instants (packages/common/display_coverage.py
:439-456), not a per-instant bitmap. An instant inside the window with no rows
at all — an interior gap — is 424 today; a coverage-only probe would answer 1
and serve an empty 200 tile instead.

Two traps this file is written around (design D4):

* **The false green.** ``_require_live_postgis_mvt`` (hydro_display.py:490-498)
  and the probe's zero branch (:543-549) raise the SAME 424 with the SAME code,
  differing only in ``details``. ``set_integration_env`` does not set
  ``NHMS_ENABLE_LIVE_POSTGIS_MVT``, so without the explicit ``setenv`` below all
  three cases would pass against any probe whatsoever. Every 424 assertion
  therefore checks that ``details`` carries the tile coordinates and NOT
  ``required_env``.
* **Coverage that never materializes.** A coverage window only exists when the
  run is ``run_type='forecast'`` with a ``met.forcing_version`` row (the window
  is a GREATEST/LEAST against the forcing window — a missing row NULLs it away)
  and when the endpoint instants are *complete*: ``segment_count =
  expected_segment_count``, taken here from ``rnv.segment_count`` because the
  model instance declares no ``resource_profile`` override. The seed writes
  every segment at both endpoints for exactly that reason, and the interior-gap
  case asserts the materialized window before it asserts the 424.
"""

from __future__ import annotations

import math
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import psycopg2
import pytest
from fastapi.testclient import TestClient
from psycopg2.extras import RealDictCursor

from apps.api.main import app
from packages.common.display_coverage import refresh_run_display_coverage
from services.tiles.mvt import MVT_MEDIA_TYPE
from tests.integration_helpers import (
    apply_migrations_from_zero,
    insert_river_timeseries_dual_written,
    set_integration_env,
)

pytestmark = pytest.mark.integration

_PREFIX = "it1596"
_BASIN_ID = f"{_PREFIX}_basin"
_BASIN_VERSION_ID = f"{_PREFIX}_basin_v1"
_NETWORK_ID = f"{_PREFIX}_rnv_v1"
_MODEL_ID = f"{_PREFIX}_model"
_SOURCE_ID = "gfs"
_FORCING_VERSION_ID = f"{_PREFIX}_forcing_v1"
_RUN_ID = f"{_PREFIX}_forecast_run"
_VARIABLE = "q_down"
_LAYER_ID = "discharge"

_SEGMENT_IDS = (f"{_PREFIX}_seg_a", f"{_PREFIX}_seg_b")
_SEGMENT_LON = 100.0
_SEGMENT_LAT = 38.0

# Three instants, one per case, so the tile cache
# (``_cached_or_generated_mvt_response``) can never carry one case's answer
# into another. `_WINDOW_START` and `_WINDOW_END` are written complete; the hour
# between them is the interior gap and gets no rows at all.
_CYCLE_TIME = datetime(2026, 7, 1, tzinfo=UTC)
_WINDOW_START = _CYCLE_TIME
_GAP_TIME = _CYCLE_TIME + timedelta(hours=1)
_WINDOW_END = _CYCLE_TIME + timedelta(hours=2)

# #2007's second source. Production stores `gfs` lower-case and `IFS`
# UPPER-case (`SELECT DISTINCT source_id FROM hydro.hydro_run` on node-27
# returns exactly those two), so the upper-case spelling is the one that proves
# the `lower(h.source_id) = :source` match. A cycle of its own keeps the two
# identities independent of one another.
_IFS_SOURCE_ID = "IFS"
_IFS_FORCING_VERSION_ID = f"{_PREFIX}_forcing_ifs_v1"
_IFS_RUN_ID = f"{_PREFIX}_forecast_run_ifs"
_IFS_CYCLE_TIME = _CYCLE_TIME + timedelta(hours=6)
_IFS_WINDOW_START = _IFS_CYCLE_TIME
_IFS_WINDOW_END = _IFS_CYCLE_TIME + timedelta(hours=2)

_ZOOM = 9


def _tile_xy(longitude: float, latitude: float, zoom: int) -> tuple[int, int]:
    """Slippy-map tile containing a lon/lat, so no tile index is a magic number."""
    scale = 2**zoom
    x = int((longitude + 180.0) / 360.0 * scale)
    radians = math.radians(latitude)
    y = int((1.0 - math.asinh(math.tan(radians)) / math.pi) / 2.0 * scale)
    return min(x, scale - 1), min(y, scale - 1)


def _segment_geom_sql(index: int) -> str:
    """A short line near (100E, 38N), distinct per segment, inside the pinned tile."""
    lon = _SEGMENT_LON + index * 0.01
    return (
        "ST_Multi(ST_SetSRID(ST_GeomFromText("
        f"'LINESTRING({lon} {_SEGMENT_LAT}, {lon + 0.005} {_SEGMENT_LAT + 0.005})'), 4490))"
    )


def _seed(database_url: str) -> None:
    """One display-ready national identity, complete at both window endpoints.

    Deliberately does NOT refresh ``hydro.run_display_coverage``: the no-coverage
    case needs the table empty, and the two cases that need a window call
    ``_refresh_coverage`` themselves. Coverage is materialized by the production
    refresh rather than INSERTed by hand so the window under test is the one
    ``display_coverage`` really computes from these rows.
    """
    connection = psycopg2.connect(database_url, cursor_factory=RealDictCursor)
    connection.autocommit = True
    try:
        with connection.cursor() as cursor:
            cursor.execute(
                "INSERT INTO core.basin (basin_id, basin_name) VALUES (%s, %s)",
                (_BASIN_ID, "Basin 1596"),
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
            # segment_count is the `expected_segment_count` the coverage window
            # measures completeness against (no resource_profile override below,
            # so the COALESCE chain falls through to this column).
            cursor.execute(
                """
                INSERT INTO core.river_network_version
                    (river_network_version_id, basin_version_id, version_label, segment_count)
                VALUES (%s, %s, 'v1', %s)
                """,
                (_NETWORK_ID, _BASIN_VERSION_ID, len(_SEGMENT_IDS)),
            )
            for index, segment_id in enumerate(_SEGMENT_IDS):
                cursor.execute(
                    f"""
                    INSERT INTO core.river_segment
                        (river_segment_id, river_network_version_id, segment_order,
                         geom, properties_json)
                    VALUES (%s, %s, %s, {_segment_geom_sql(index)}, '{{"Type": 5}}'::jsonb)
                    """,
                    (segment_id, _NETWORK_ID, index),
                )
            cursor.execute(
                """
                INSERT INTO core.model_instance
                    (model_id, basin_version_id, river_network_version_id, mesh_version_id,
                     calibration_version_id, shud_code_version, model_package_uri,
                     active_flag, lifecycle_state)
                VALUES (%s, %s, %s, 'mesh-1596', 'cal-1596', '1.0', 's3://nhms/model',
                        true, 'active')
                """,
                (_MODEL_ID, _BASIN_VERSION_ID, _NETWORK_ID),
            )
            cursor.execute(
                """
                INSERT INTO met.data_source
                    (source_id, source_name, source_type, status, native_format, adapter_name)
                VALUES (%s, 'GFS 1596', 'forecast', 'mock', 'netcdf', 'gfs')
                """,
                (_SOURCE_ID,),
            )
            # Mandatory: display_start/display_end are GREATEST/LEAST against
            # this row's window, so without it the run's coverage window is NULL
            # and every case would collapse onto the no-coverage branch.
            cursor.execute(
                """
                INSERT INTO met.forcing_version
                    (forcing_version_id, model_id, source_id, cycle_time, start_time, end_time,
                     station_count, forcing_package_uri, checksum)
                VALUES (%s, %s, %s, %s, %s, %s, 1, 's3://nhms/forcing/1596/', 'forcing-sha')
                """,
                (
                    _FORCING_VERSION_ID,
                    _MODEL_ID,
                    _SOURCE_ID,
                    _CYCLE_TIME,
                    _WINDOW_START,
                    _WINDOW_END,
                ),
            )
            cursor.execute(
                """
                INSERT INTO hydro.hydro_run
                    (run_id, run_type, scenario_id, model_id, basin_version_id, forcing_version_id,
                     source_id, cycle_time, start_time, end_time, status, run_manifest_uri)
                VALUES (%s, 'forecast', 'sc', %s, %s, %s, %s, %s, %s, %s, 'parsed', 's3://nhms/manifest')
                """,
                (
                    _RUN_ID,
                    _MODEL_ID,
                    _BASIN_VERSION_ID,
                    _FORCING_VERSION_ID,
                    _SOURCE_ID,
                    _CYCLE_TIME,
                    _WINDOW_START,
                    _WINDOW_END,
                ),
            )
            # Every segment at both endpoints, nothing at `_GAP_TIME`.
            insert_river_timeseries_dual_written(
                cursor,
                [
                    (
                        _RUN_ID,
                        _BASIN_VERSION_ID,
                        _NETWORK_ID,
                        segment_id,
                        valid_time,
                        lead,
                        _VARIABLE,
                        100.0 + index,
                        "m3/s",
                        "ok",
                    )
                    for lead, valid_time in enumerate((_WINDOW_START, _WINDOW_END))
                    for index, segment_id in enumerate(_SEGMENT_IDS)
                ],
            )
    finally:
        connection.close()


def _seed_uppercase_ifs_run(database_url: str) -> None:
    """A second display-ready identity whose ``source_id`` is stored ``'IFS'``.

    Seeded only by the tests that need it, so the three pre-existing cases keep
    running against exactly the data they were written for.

    It needs its own ``met.data_source`` AND ``met.forcing_version`` rows, not
    just a ``hydro.hydro_run`` row: the display-coverage window is a
    GREATEST/LEAST against the forcing window, so a run without one materializes
    a NULL window and this case would fail on the no-coverage branch instead of
    on the thing it is testing.
    """
    connection = psycopg2.connect(database_url, cursor_factory=RealDictCursor)
    connection.autocommit = True
    try:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                INSERT INTO met.data_source
                    (source_id, source_name, source_type, status, native_format, adapter_name)
                VALUES (%s, 'IFS 2007', 'forecast', 'mock', 'netcdf', 'ifs')
                """,
                (_IFS_SOURCE_ID,),
            )
            cursor.execute(
                """
                INSERT INTO met.forcing_version
                    (forcing_version_id, model_id, source_id, cycle_time, start_time, end_time,
                     station_count, forcing_package_uri, checksum)
                VALUES (%s, %s, %s, %s, %s, %s, 1, 's3://nhms/forcing/2007-ifs/', 'forcing-sha-ifs')
                """,
                (
                    _IFS_FORCING_VERSION_ID,
                    _MODEL_ID,
                    _IFS_SOURCE_ID,
                    _IFS_CYCLE_TIME,
                    _IFS_WINDOW_START,
                    _IFS_WINDOW_END,
                ),
            )
            cursor.execute(
                """
                INSERT INTO hydro.hydro_run
                    (run_id, run_type, scenario_id, model_id, basin_version_id, forcing_version_id,
                     source_id, cycle_time, start_time, end_time, status, run_manifest_uri)
                VALUES (%s, 'forecast', 'sc', %s, %s, %s, %s, %s, %s, %s, 'parsed', 's3://nhms/manifest')
                """,
                (
                    _IFS_RUN_ID,
                    _MODEL_ID,
                    _BASIN_VERSION_ID,
                    _IFS_FORCING_VERSION_ID,
                    _IFS_SOURCE_ID,
                    _IFS_CYCLE_TIME,
                    _IFS_WINDOW_START,
                    _IFS_WINDOW_END,
                ),
            )
            insert_river_timeseries_dual_written(
                cursor,
                [
                    (
                        _IFS_RUN_ID,
                        _BASIN_VERSION_ID,
                        _NETWORK_ID,
                        segment_id,
                        valid_time,
                        lead,
                        _VARIABLE,
                        200.0 + index,
                        "m3/s",
                        "ok",
                    )
                    for lead, valid_time in enumerate((_IFS_WINDOW_START, _IFS_WINDOW_END))
                    for index, segment_id in enumerate(_SEGMENT_IDS)
                ],
            )
    finally:
        connection.close()


def _refresh_coverage(database_url: str, run_id: str = _RUN_ID) -> None:
    connection = psycopg2.connect(database_url, cursor_factory=RealDictCursor)
    try:
        assert refresh_run_display_coverage(connection, run_id) is True
    finally:
        connection.close()


def _query(database_url: str, sql: str, params: tuple[Any, ...]) -> list[dict[str, Any]]:
    connection = psycopg2.connect(database_url, cursor_factory=RealDictCursor)
    try:
        with connection.cursor() as cursor:
            cursor.execute(sql, params)
            return [dict(row) for row in cursor.fetchall()]
    finally:
        connection.close()


@pytest.fixture()
def national_tile(
    throwaway_database_url: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> Any:
    apply_migrations_from_zero(throwaway_database_url)
    _seed(throwaway_database_url)
    object_root = tmp_path / "object-store"
    set_integration_env(throwaway_database_url, object_root, monkeypatch)
    # Not set by set_integration_env, and its absence produces the very same
    # 424 code the probe produces — see the module docstring.
    monkeypatch.setenv("NHMS_ENABLE_LIVE_POSTGIS_MVT", "true")
    with TestClient(app) as client:
        yield throwaway_database_url, client


def _stamp(value: datetime) -> str:
    return value.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _request_tile(client: TestClient, valid_time: datetime) -> Any:
    """The legacy source-less route: also this file's regression oracle for it."""
    x, y = _tile_xy(_SEGMENT_LON, _SEGMENT_LAT, _ZOOM)
    return client.get(f"/api/v1/tiles/hydro-national/{_VARIABLE}/{_stamp(valid_time)}/{_ZOOM}/{x}/{y}.pbf")


def _request_identity_tile(client: TestClient, source: str, cycle: datetime, valid_time: datetime) -> Any:
    """The canonical `{source}/{cycle}` route (#2007)."""
    x, y = _tile_xy(_SEGMENT_LON, _SEGMENT_LAT, _ZOOM)
    return client.get(
        f"/api/v1/tiles/hydro-national/{source}/{_stamp(cycle)}/{_VARIABLE}"
        f"/{_stamp(valid_time)}/{_ZOOM}/{x}/{y}.pbf"
    )


def _assert_tile_carries_the_seeded_features(response: Any) -> None:
    assert response.status_code == 200, response.text
    assert response.headers["content-type"].startswith(MVT_MEDIA_TYPE)
    assert response.content, "a 200 with empty bytes would mean the probe answered 1 for nothing"
    for segment_id in _SEGMENT_IDS:
        assert segment_id.encode() in response.content, segment_id
    assert _NETWORK_ID.encode() in response.content


def _assert_probe_said_no_data(response: Any) -> None:
    """A 424 from the PROBE, not from live PostGIS being switched off.

    The two raise the same status and the same code; only ``details`` tells them
    apart (hydro_display.py:490-498 vs :543-549). Asserting the discriminator is
    what keeps this file from passing against a probe that was never reached.
    """
    assert response.status_code == 424, response.text
    body = response.json()
    assert body["error"]["code"] == "MVT_LIVE_POSTGIS_UNAVAILABLE"
    details = body["error"]["details"]
    x, y = _tile_xy(_SEGMENT_LON, _SEGMENT_LAT, _ZOOM)
    assert details == {"layer_id": _LAYER_ID, "z": _ZOOM, "x": x, "y": y}
    assert "required_env" not in details


def test_national_tile_is_424_when_no_display_ready_run_covers_the_instant(national_tile: Any) -> None:
    """The zero branch with no coverage row at all.

    The fact rows for this instant exist — the run is simply not display-ready,
    so the probe's discovery sub-select is empty and never touches the fact
    table. This is the branch that made an uncovered compressed instant cost 38
    seconds before #1596 and now costs nothing at all.
    """
    database_url, client = national_tile

    assert _query(database_url, "SELECT run_id FROM hydro.run_display_coverage", ()) == []
    rows_at_instant = _query(
        database_url,
        "SELECT COUNT(*) AS n FROM hydro.river_timeseries WHERE valid_time = %s",
        (_WINDOW_START,),
    )
    assert rows_at_instant[0]["n"] == len(_SEGMENT_IDS), "seed must have rows here, or the case proves nothing"

    _assert_probe_said_no_data(_request_tile(client, _WINDOW_START))


def test_national_tile_is_424_on_an_interior_coverage_window_gap(national_tile: Any) -> None:
    """The case that rejects answering existence from the coverage window.

    ``river_valid_time_start/end`` are a MIN/MAX over complete instants, so this
    instant is inside the advertised window while holding no rows at all. A
    coverage-only probe answers 1 here and serves an empty 200 tile; the
    fact-touching probe answers 0 and keeps the 424 the pre-change shape
    produced.
    """
    database_url, client = national_tile
    _refresh_coverage(database_url)

    coverage = _query(
        database_url,
        """
        SELECT segment_count, river_valid_time_start, river_valid_time_end
        FROM hydro.run_display_coverage WHERE run_id = %s
        """,
        (_RUN_ID,),
    )
    assert len(coverage) == 1
    assert coverage[0]["segment_count"] == len(_SEGMENT_IDS)
    assert coverage[0]["river_valid_time_start"] == _WINDOW_START
    assert coverage[0]["river_valid_time_end"] == _WINDOW_END
    # Non-vacuity, both halves: the instant is inside the window AND the fact
    # table really is empty there.
    assert coverage[0]["river_valid_time_start"] < _GAP_TIME < coverage[0]["river_valid_time_end"]
    gap_rows = _query(
        database_url,
        "SELECT COUNT(*) AS n FROM hydro.river_timeseries WHERE valid_time = %s",
        (_GAP_TIME,),
    )
    assert gap_rows[0]["n"] == 0

    _assert_probe_said_no_data(_request_tile(client, _GAP_TIME))


def test_national_tile_is_200_with_a_non_empty_mvt_when_the_instant_has_data(national_tile: Any) -> None:
    """The one branch of the probe: covered window, rows at that exact instant."""
    database_url, client = national_tile
    _refresh_coverage(database_url)

    response = _request_tile(client, _WINDOW_END)

    assert response.status_code == 200, response.text
    assert response.headers["content-type"].startswith(MVT_MEDIA_TYPE)
    assert response.content, "a 200 with empty bytes would mean the probe answered 1 for nothing"
    # MVT string values are stored as plain UTF-8 in the protobuf, so the seeded
    # identity is visible in the bytes without decoding the tile.
    for segment_id in _SEGMENT_IDS:
        assert segment_id.encode() in response.content, segment_id
    assert _NETWORK_ID.encode() in response.content


def test_national_identity_tile_is_424_for_the_source_without_a_run_at_that_cycle(national_tile: Any) -> None:
    """#2007's fail-closed case, and the only oracle that can see a half-bound query.

    Binding `(source, cycle)` in the `latest_runs` data CTE alone leaves the
    identity probe answering from ANY source's run: `source_identity_count`
    stays 1, the data CTE selects nothing, and the route serves an empty 200
    where the contract requires 424. Everything else in the repo passes under
    that bug, because a fake session never runs the SQL.
    """
    database_url, client = national_tile
    _refresh_coverage(database_url)

    assert _query(
        database_url,
        "SELECT source_id FROM hydro.hydro_run WHERE cycle_time = %s ORDER BY source_id",
        (_CYCLE_TIME,),
    ) == [{"source_id": _SOURCE_ID}], "only gfs may hold a run at this cycle, or the case proves nothing"

    _assert_tile_carries_the_seeded_features(
        _request_identity_tile(client, "gfs", _CYCLE_TIME, _WINDOW_END)
    )
    _assert_probe_said_no_data(_request_identity_tile(client, "ifs", _CYCLE_TIME, _WINDOW_END))


def test_national_identity_tile_matches_an_uppercase_source_id_from_a_lowercase_path(national_tile: Any) -> None:
    """Path segment `ifs` must find a run stored as `source_id = 'IFS'`.

    Production stores exactly `gfs` and `IFS`, so an equality match instead of
    `lower(h.source_id) = :source` would 424 every IFS tile in the fleet, and
    no other test in the repo would notice.
    """
    database_url, client = national_tile
    _seed_uppercase_ifs_run(database_url)
    _refresh_coverage(database_url, _IFS_RUN_ID)

    assert _query(
        database_url,
        "SELECT source_id FROM hydro.hydro_run WHERE run_id = %s",
        (_IFS_RUN_ID,),
    ) == [{"source_id": "IFS"}], "the stored spelling must be upper-case, or the case proves nothing"

    _assert_tile_carries_the_seeded_features(
        _request_identity_tile(client, "ifs", _IFS_CYCLE_TIME, _IFS_WINDOW_END)
    )
    # The gfs identity has no run at the IFS cycle, so it stays fail-closed.
    _assert_probe_said_no_data(_request_identity_tile(client, "gfs", _IFS_CYCLE_TIME, _IFS_WINDOW_END))
