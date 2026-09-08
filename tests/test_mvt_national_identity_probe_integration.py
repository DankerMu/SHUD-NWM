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
from sqlalchemy.orm import Session

from apps.api.main import app
from packages.common.display_coverage import refresh_run_display_coverage
from services.tiles.mvt import (
    MVT_MEDIA_TYPE,
    national_discharge_source_version,
    national_river_network_source_version,
)
from tests.integration_helpers import (
    apply_migrations_from_zero,
    insert_river_timeseries_dual_written,
    set_integration_env,
    sqlalchemy_engine,
)
from workers.model_registry.basins_registry_import import _backfill_output_segment_geometry

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

# Three instants, one per PRE-#2007 case, so the tile cache
# (``_cached_or_generated_mvt_response``) can never carry one of those answers
# into another. (The #2007 identity cases below deliberately share
# `_WINDOW_END` instead and rely on `(source, cycle)` being part of the cache
# key.) The base `_seed` writes `_WINDOW_START` and `_WINDOW_END` complete and
# nothing at the hour between them, which is the interior gap.
#
# `_GAP_TIME` is empty in the BASE SEED ONLY. `_LATE_CYCLE_TIME` below is the
# same instant, and the rival run seeded there by the cycle case writes a full
# segment set at it. Nothing breaks today — `throwaway_database_url` gives every
# test its own database and only that one case seeds the rival — but a new case
# that seeds the rival and then expects the interior gap to be empty is wrong.
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

# #2007's two competing-run cases. Both rival runs are display-ready and cover
# `_WINDOW_END`, so the ONLY thing that can keep them out of the answer is the
# bound identity.
#
# `run_id` is one of the layer's public tile columns
# (`_mvt_public_tile_columns("hydro-national")`), and MVT string values are
# plain UTF-8 in the protobuf, so it is the discriminator these cases assert on:
# segment ids and the network id are IDENTICAL across runs, so the shared
# `_assert_tile_carries_the_seeded_features` cannot tell two runs apart.
#
# The ids below deliberately neither contain nor are contained in `_RUN_ID`
# (`it1596_forecast_run`, which IS a prefix of `_IFS_RUN_ID`), because a
# substring would make `not in response.content` silently unfalsifiable.
_LATE_CYCLE_TIME = _CYCLE_TIME + timedelta(hours=1)
_LATE_GFS_RUN_ID = "it2007_gfs_late_cycle_run"
_LATE_GFS_FORCING_VERSION_ID = "it2007_forcing_gfs_late_v1"
_SAME_CYCLE_IFS_RUN_ID = "it2007_ifs_same_cycle_run"
_SAME_CYCLE_IFS_FORCING_VERSION_ID = "it2007_forcing_ifs_same_cycle_v1"

# A cycle NO run is ever seeded at, in either helper. It is the production shape
# the `:cycle` half of the identity PROBE (`source_identity_stats_sql`, the
# sub-select that decides 424-vs-200) has to fail closed on: an older cycle
# whose runs were pruned or failed while a newer cycle still covers the same
# valid_time. Older rather than newer so the request is a plausible hindcast
# rather than a cycle issued after the instant it forecasts.
_PRUNED_CYCLE_TIME = _CYCLE_TIME - timedelta(hours=6)

# A cycle NEWER than every seeded run, also never seeded. `_PRUNED_CYCLE_TIME`
# alone cannot tell `h.cycle_time = :cycle` from `h.cycle_time <= :cycle`: it is
# older than every run, so both spellings select nothing and both answer 424.
# This one is the other direction — the production shape is a client asking for
# a cycle that has not landed yet — and there `<=` silently paints the newest
# run as if it were the requested cycle, at HTTP 200. Kept off `_WINDOW_END`'s
# value so a reader cannot confuse a cycle with a valid_time.
_UNLANDED_CYCLE_TIME = _CYCLE_TIME + timedelta(hours=3)

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


def _seed_rival_display_ready_run(
    database_url: str,
    *,
    run_id: str,
    source_id: str,
    forcing_version_id: str,
    cycle_time: datetime,
    window_start: datetime,
    window_end: datetime,
    value_base: float,
    new_data_source_name: str | None = None,
) -> None:
    """A second display-ready run on the SAME model/network, seeded per case (#2007).

    Deliberately a separate helper from ``_seed_uppercase_ifs_run`` rather than
    a generalization of it: that one backs a case the suite already runs green,
    and these cases have to be able to fail on their own predicate rather than
    on a shared-fixture change.

    Like every display-ready run here it needs its own ``met.forcing_version``
    row (the coverage window is a GREATEST/LEAST against the forcing window, so
    without one the window is NULL and the case collapses onto the no-coverage
    branch) and complete rows at BOTH window endpoints (the window's endpoints
    are a MIN/MAX over instants whose ``segment_count`` equals the network's).
    ``new_data_source_name`` inserts the ``met.data_source`` parent when the
    source is not the one ``_seed`` already registered.
    """
    connection = psycopg2.connect(database_url, cursor_factory=RealDictCursor)
    connection.autocommit = True
    try:
        with connection.cursor() as cursor:
            if new_data_source_name is not None:
                cursor.execute(
                    """
                    INSERT INTO met.data_source
                        (source_id, source_name, source_type, status, native_format, adapter_name)
                    VALUES (%s, %s, 'forecast', 'mock', 'netcdf', %s)
                    """,
                    (source_id, new_data_source_name, source_id.lower()),
                )
            cursor.execute(
                """
                INSERT INTO met.forcing_version
                    (forcing_version_id, model_id, source_id, cycle_time, start_time, end_time,
                     station_count, forcing_package_uri, checksum)
                VALUES (%s, %s, %s, %s, %s, %s, 1, %s, %s)
                """,
                (
                    forcing_version_id,
                    _MODEL_ID,
                    source_id,
                    cycle_time,
                    window_start,
                    window_end,
                    f"s3://nhms/forcing/{forcing_version_id}/",
                    f"forcing-sha-{forcing_version_id}",
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
                    run_id,
                    _MODEL_ID,
                    _BASIN_VERSION_ID,
                    forcing_version_id,
                    source_id,
                    cycle_time,
                    window_start,
                    window_end,
                ),
            )
            insert_river_timeseries_dual_written(
                cursor,
                [
                    (
                        run_id,
                        _BASIN_VERSION_ID,
                        _NETWORK_ID,
                        segment_id,
                        valid_time,
                        lead,
                        _VARIABLE,
                        value_base + index,
                        "m3/s",
                        "ok",
                    )
                    for lead, valid_time in enumerate((window_start, window_end))
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

    # The legacy source-less alias, at the SAME instant, is the only place in
    # the repo where its `source=None` bind is observable: this test refreshes
    # coverage for the IFS run ONLY, so `_IFS_WINDOW_END` is an instant no gfs
    # run can serve. Everywhere else — every other case here and every unit
    # case — the seed is `_SOURCE_ID = "gfs"`, so a legacy route that started
    # binding `source="gfs"` instead of NULL would answer identically and stay
    # green. Here it 424s.
    assert _query(
        database_url,
        "SELECT run_id FROM hydro.run_display_coverage ORDER BY run_id",
        (),
    ) == [{"run_id": _IFS_RUN_ID}], "only the IFS run may be display-ready here, or the case proves nothing"
    legacy = _request_tile(client, _IFS_WINDOW_END)
    _assert_tile_carries_the_seeded_features(legacy)
    # `_assert_tile_was_painted_by` cannot be used for this pair: `_RUN_ID`
    # (`it1596_forecast_run`) is a PREFIX of `_IFS_RUN_ID`
    # (`it1596_forecast_run_ifs`), and that helper rejects such a pair outright
    # because its negative half would be unfalsifiable. The positive assertion
    # on the longer id is strictly stronger than the pair would have been.
    assert _IFS_RUN_ID.encode() in legacy.content, _IFS_RUN_ID


# --- #2007: two competing runs, one bound identity -------------------------
#
# Both cases below exist because the whole suite stayed GREEN when the
# `:source` / `:cycle` predicates were deleted from the tile SQL on node-27.
# The three pre-#2007 cases never have two rival runs covering one instant, and
# the two #2007 cases that do assert only 200-vs-424 -- so nothing anywhere
# proved that a bound identity actually SELECTS its own run rather than the
# newest one. That is the "同一张图 gfs/IFS 混源" failure this issue exists to
# fix: a 200 whose bytes come from the wrong run.


def _assert_both_runs_are_candidates_at(database_url: str, run_ids: tuple[str, str], valid_time: datetime) -> None:
    """Non-vacuity: neither rival run is excluded by anything except the identity.

    Without this, a case that seeds a rival with (say) a NULL coverage window
    would still "pass" -- the rival was never in the running, so the assertion
    that its `run_id` is absent proves nothing about the bound predicate.
    """
    for run_id in run_ids:
        coverage = _query(
            database_url,
            """
            SELECT segment_count, river_valid_time_start, river_valid_time_end
            FROM hydro.run_display_coverage WHERE run_id = %s
            """,
            (run_id,),
        )
        assert len(coverage) == 1, f"{run_id} has no coverage row, so it is not a candidate at all"
        assert coverage[0]["segment_count"] > 0, run_id
        assert coverage[0]["river_valid_time_start"] <= valid_time <= coverage[0]["river_valid_time_end"], run_id
        rows = _query(
            database_url,
            "SELECT COUNT(*) AS n FROM hydro.river_timeseries WHERE run_id = %s AND valid_time = %s",
            (run_id, valid_time),
        )
        assert rows[0]["n"] == len(_SEGMENT_IDS), f"{run_id} has no fact rows at {valid_time}"


def _assert_tile_was_painted_by(response: Any, expected_run_id: str, rejected_run_id: str) -> None:
    """The run identity in the tile bytes, which is the only thing that differs.

    `_assert_tile_carries_the_seeded_features` checks segment ids and the
    network id; both rival runs share all of those, so it passes no matter which
    run painted the tile. `run_id` is a public tile column for this layer and
    MVT string values are plain UTF-8 in the protobuf, so both directions are
    readable straight off the bytes.
    """
    _assert_tile_carries_the_seeded_features(response)
    assert expected_run_id not in rejected_run_id and rejected_run_id not in expected_run_id, (
        "one run id must not be a substring of the other, or the negative assertion is unfalsifiable"
    )
    assert expected_run_id.encode() in response.content, expected_run_id
    assert rejected_run_id.encode() not in response.content, rejected_run_id


def test_national_identity_tile_serves_the_requested_cycle_not_the_newest_one(national_tile: Any) -> None:
    """Same source, two seeded cycles plus a third with no run: each answer is its own.

    Run selection is `DISTINCT ON (river_network_version_id) ... ORDER BY
    h.cycle_time DESC`, so with the `:cycle` predicate deleted BOTH requests
    below would be painted by the late run -- a request for an old cycle served
    with the newest cycle's discharge, silently, at HTTP 200. Making an old
    identity addressable is the entire point of the issue, so this is its
    behavioral oracle.

    The legacy source-less route is asserted alongside precisely to show that
    newest-wins IS the unbound default: it still picks the late run, and only
    the bound cycle overrides it.

    The third request — `_PRUNED_CYCLE_TIME`, which has no run at all — is the
    only behavioral oracle on the `:cycle` half of the identity PROBE. The two
    painted-by cases above run entirely inside the 200 branch, so they cannot
    tell a probe that filters on `:cycle` from one where the predicate is
    present but ineffective; every other identity case in this file either
    leaves `:source` bound to a source with no run, or asks for a cycle that
    does have one. Delete or neuter `:cycle` in
    `source_identity_stats_sql` only and the probe answers "present" from the
    late gfs run, which turns this contract's 424 into an empty 200.
    """
    database_url, client = national_tile
    _seed_rival_display_ready_run(
        database_url,
        run_id=_LATE_GFS_RUN_ID,
        source_id=_SOURCE_ID,
        forcing_version_id=_LATE_GFS_FORCING_VERSION_ID,
        cycle_time=_LATE_CYCLE_TIME,
        window_start=_LATE_CYCLE_TIME,
        window_end=_WINDOW_END,
        value_base=300.0,
    )
    _refresh_coverage(database_url)
    _refresh_coverage(database_url, _LATE_GFS_RUN_ID)
    _assert_both_runs_are_candidates_at(database_url, (_RUN_ID, _LATE_GFS_RUN_ID), _WINDOW_END)
    same_source_runs = _query(
        database_url,
        "SELECT run_id FROM hydro.hydro_run WHERE source_id = %s ORDER BY cycle_time",
        (_SOURCE_ID,),
    )
    assert same_source_runs == [{"run_id": _RUN_ID}, {"run_id": _LATE_GFS_RUN_ID}], (
        "both rivals must be the SAME source, or :cycle is not what is under test"
    )

    _assert_tile_was_painted_by(
        _request_identity_tile(client, "gfs", _CYCLE_TIME, _WINDOW_END), _RUN_ID, _LATE_GFS_RUN_ID
    )
    _assert_tile_was_painted_by(
        _request_identity_tile(client, "gfs", _LATE_CYCLE_TIME, _WINDOW_END), _LATE_GFS_RUN_ID, _RUN_ID
    )
    # A gfs cycle that was never seeded, asked for at an instant BOTH seeded
    # runs cover (`_assert_both_runs_are_candidates_at` above established that
    # half; the mutation only bites because it holds). Fail-closed 424, not the
    # newest run's tile and not an empty 200.
    assert _query(
        database_url,
        "SELECT COUNT(*) AS n FROM hydro.hydro_run WHERE cycle_time = %s",
        (_PRUNED_CYCLE_TIME,),
    )[0]["n"] == 0, "the pruned cycle must have no run at all, or the case proves nothing"
    _assert_probe_said_no_data(_request_identity_tile(client, "gfs", _PRUNED_CYCLE_TIME, _WINDOW_END))
    # The same fail-closed demand from the OTHER side of the seeded cycles. The
    # pruned case above is older than every run, so `h.cycle_time <= :cycle`
    # selects nothing there either and answers 424 exactly like `=` does; only a
    # cycle NEWER than the newest run separates the two. Under `<=` this request
    # is painted by the late run, i.e. a not-yet-issued cycle silently served
    # with an older cycle's discharge at HTTP 200.
    assert _query(
        database_url,
        "SELECT COUNT(*) AS n FROM hydro.hydro_run WHERE cycle_time >= %s",
        (_UNLANDED_CYCLE_TIME,),
    )[0]["n"] == 0, "the unlanded cycle must be newer than every seeded run, or the case proves nothing"
    _assert_probe_said_no_data(_request_identity_tile(client, "gfs", _UNLANDED_CYCLE_TIME, _WINDOW_END))
    # Unbound default, unchanged: newest cycle wins.
    _assert_tile_was_painted_by(_request_tile(client, _WINDOW_END), _LATE_GFS_RUN_ID, _RUN_ID)


def test_national_digest_narrows_the_ranked_runs_to_the_bound_identity(national_tile: Any) -> None:
    """`national_discharge_source_version`'s narrowing, EXECUTED, which nothing ever did.

    The third `(source, cycle)` site lives in this helper's ranked sub-query and
    it was the only one with no behavioral oracle anywhere: `_CapturingSession`
    in `tests/test_hydro_display_mvt_scaling.py` records binds and returns
    canned rows without running SQL, so a predicate that is present but
    ineffective — the `AND (` -> `OR  (` flip, which SQL precedence turns into
    "every unbound row, OR the matching ones" — kept the whole suite green.
    `grep -rn "AND (CAST(:source" tests/` matched nothing before this case.

    Two seeded gfs cycles on one network, so the ranking has something to
    choose between:

    * bound to the EARLY cycle -> ranks run_a,
    * bound to the LATE cycle -> ranks the late run,
    * bound to nothing -> ranks each network's overall latest, i.e. the late run
      again, which is the legacy/catalog question and must not move,
    * bound to a cycle with no run at all -> ranks nothing.

    Under the flip all four collapse onto the unbound value. Freshness is what
    that costs: the digest reaches `source_version` and therefore `cache_key`,
    so a re-run of a non-latest identity would stop rotating its cache entry
    while the tile it names went stale, and the tile file cache has no TTL.
    """
    database_url, _client = national_tile
    _seed_rival_display_ready_run(
        database_url,
        run_id=_LATE_GFS_RUN_ID,
        source_id=_SOURCE_ID,
        forcing_version_id=_LATE_GFS_FORCING_VERSION_ID,
        cycle_time=_LATE_CYCLE_TIME,
        window_start=_LATE_CYCLE_TIME,
        window_end=_WINDOW_END,
        value_base=300.0,
    )
    # The digest's ranked sub-query INNER JOINs `hydro.run_display_coverage`, so
    # a run without a refreshed coverage row is not a candidate at all and the
    # comparisons below would be vacuous.
    _refresh_coverage(database_url)
    _refresh_coverage(database_url, _LATE_GFS_RUN_ID)
    _assert_both_runs_are_candidates_at(database_url, (_RUN_ID, _LATE_GFS_RUN_ID), _WINDOW_END)

    engine = sqlalchemy_engine(database_url)
    try:
        with Session(engine) as session:
            unbound = national_discharge_source_version(session)
            early = national_discharge_source_version(session, source="gfs", cycle=_CYCLE_TIME)
            late = national_discharge_source_version(session, source="gfs", cycle=_LATE_CYCLE_TIME)
            pruned = national_discharge_source_version(session, source="gfs", cycle=_PRUNED_CYCLE_TIME)
    finally:
        # The throwaway database is DROPped on teardown; a live pooled
        # connection would make the DROP block and take the file down with it.
        engine.dispose()

    assert early != late, "the digest does not narrow: both cycles rank the same run"
    assert unbound == late, "the unbound digest must stay the overall-latest question"
    assert pruned not in (unbound, early, late), "a cycle with no run must digest an empty ranking"
    # Non-vacuity for `unbound == late`: it is an equality, so it would also
    # hold if the helper returned a constant. `early` differing from it is what
    # rules that out, and `pruned` differing from all three rules out a digest
    # that only ever sees two shapes.
    assert len({unbound, early, pruned}) == 3


def test_national_identity_tile_serves_the_requested_source_not_the_other_one_at_that_cycle(
    national_tile: Any,
) -> None:
    """Same cycle, two sources: neither request may be painted by the other source's run.

    This is the mutation that survives everything else in the repo -- deleting
    the identity pair from the `latest_runs` DATA CTE alone, leaving the probe
    and the digest bound. The probe still answers 1, so the route still returns
    200, and the CTE paints the other source's discharge.

    Both runs share `cycle_time`, so the unbound tie-break is `ORDER BY h.run_id
    DESC`: `it2007_ifs_same_cycle_run` sorts above `it1596_forecast_run`, which
    makes the `gfs` request the direction the mutation actually bites. The `ifs`
    direction is asserted for symmetry -- it is the one that would break if the
    predicate were inverted or the case-folding dropped.
    """
    database_url, client = national_tile
    _seed_rival_display_ready_run(
        database_url,
        run_id=_SAME_CYCLE_IFS_RUN_ID,
        source_id=_IFS_SOURCE_ID,
        forcing_version_id=_SAME_CYCLE_IFS_FORCING_VERSION_ID,
        cycle_time=_CYCLE_TIME,
        window_start=_WINDOW_START,
        window_end=_WINDOW_END,
        value_base=400.0,
        new_data_source_name="IFS 2007 same cycle",
    )
    _refresh_coverage(database_url)
    _refresh_coverage(database_url, _SAME_CYCLE_IFS_RUN_ID)
    _assert_both_runs_are_candidates_at(database_url, (_RUN_ID, _SAME_CYCLE_IFS_RUN_ID), _WINDOW_END)
    assert _query(
        database_url,
        "SELECT run_id, source_id FROM hydro.hydro_run WHERE cycle_time = %s ORDER BY run_id",
        (_CYCLE_TIME,),
    ) == [
        {"run_id": _RUN_ID, "source_id": _SOURCE_ID},
        {"run_id": _SAME_CYCLE_IFS_RUN_ID, "source_id": _IFS_SOURCE_ID},
    ], "both rivals must share the cycle, or :source is not what is under test"

    _assert_tile_was_painted_by(
        _request_identity_tile(client, "gfs", _CYCLE_TIME, _WINDOW_END), _RUN_ID, _SAME_CYCLE_IFS_RUN_ID
    )
    _assert_tile_was_painted_by(
        _request_identity_tile(client, "ifs", _CYCLE_TIME, _WINDOW_END), _SAME_CYCLE_IFS_RUN_ID, _RUN_ID
    )


# ---------------------------------------------------------------------------
# #2009 (I5): the national discharge CYCLES catalog and its per-cycle valid
# times. Appended at the end of the file so every line citation above keeps its
# number.
#
# Why these cases cannot reuse the seeds above (verified against the code):
#
# * The three existing seeds write only the TWO window endpoints (`_seed` at
#   `:279-296` writes lead 0 at `_WINDOW_START` and lead 1 at `_WINDOW_END`,
#   deliberately nothing at `_GAP_TIME`). The coverage row that materialises from
#   that is `segment_count=2, river_sample_count=4, min_lead=0, max_lead=1`,
#   window `C … C+2h` -- and `_national_coverage_window`
#   (`services/tiles/mvt.py:2084-2091`) REJECTS it, because `7200 !=
#   (lead_count - 1) * 3600` with `lead_count = 2`. The eight tile cases above
#   never reach that function, so their green says nothing here. Everything
#   below therefore seeds through `_seed_hourly_display_ready_run`, which writes
#   EVERY segment at EVERY hour with `lead_time_hours` = the hour offset.
# * The ranked national query joins `core.model_instance mi ON mi.basin_version_id
#   = h.basin_version_id` (`mvt.py:2038`), and
#   `model_instance_active_basin_version_uidx`
#   (`db/migrations/000022_model_asset_lifecycle.sql:61-63`) allows only ONE
#   active `model_instance` per `basin_version_id`. A second ACTIVE network
#   therefore needs its own `core.basin_version` as well as its own
#   `core.river_network_version`; sharing basin version 1 is both rejected by the
#   index and would make network 2 see all of network 1's runs.
# * `hydro.hydro_run.run_id` is TEXT (`db/migrations/000006_hydro.sql:2`), so
#   `ORDER BY h.run_id DESC` is a lexical order. Run ids below carry an explicit
#   `_run_<n>_` rank segment, and every case whose outcome depends on that order
#   asserts it against the database's own collation before asserting behaviour.
# * Matrix row 51 (`AND mi.river_network_version_id IS NOT NULL`) gets NO case
#   here: `core.model_instance.river_network_version_id` is `TEXT NOT NULL`
#   (`db/migrations/000004_core.sql:74`) and no later migration drops that, so
#   the row the case would need is unsatisfiable by schema. Tripwire-only, in
#   `tests/test_hydro_display_mvt_scaling.py::test_national_coverage_statements_pin_their_shape`.
#
# The seed cycle is months older than the real 12-day cycle lookback, so every
# case except the lookback one widens
# `services.tiles.mvt.NATIONAL_DISCHARGE_CYCLE_LOOKBACK_DAYS` (read at call time,
# `mvt.py:1914`).
# ---------------------------------------------------------------------------

_I5_PREFIX = f"{_PREFIX}_i5"
_SECOND_BASIN_VERSION_ID = f"{_I5_PREFIX}_basin_v2"
_SECOND_NETWORK_ID = f"{_I5_PREFIX}_rnv_v2"
_SECOND_MODEL_ID = f"{_I5_PREFIX}_model_2"
_SECOND_SEGMENT_IDS = (f"{_I5_PREFIX}_seg_c", f"{_I5_PREFIX}_seg_d")

# Cycle A is the base seed's cycle; B is newer, P is older. A 2-hour window holds
# exactly ONE 3-hour-stride instant (the cycle itself), which is what makes the
# per-cycle list assertions below single-valued and easy to read.
_I5_CYCLE_A = _CYCLE_TIME
_I5_CYCLE_B = _CYCLE_TIME + timedelta(hours=12)
_I5_CYCLE_P = _CYCLE_TIME - timedelta(hours=6)
_I5_IFS_CYCLE = _CYCLE_TIME + timedelta(hours=6)
_I5_WINDOW = timedelta(hours=2)

_WIDE_CYCLE_LOOKBACK = "services.tiles.mvt.NATIONAL_DISCHARGE_CYCLE_LOOKBACK_DAYS"


def _seed_hourly_display_ready_run(
    database_url: str,
    *,
    run_id: str,
    cycle_time: datetime,
    window_start: datetime,
    window_end: datetime,
    source_id: str = _SOURCE_ID,
    forcing_version_id: str | None = None,
    value_base: float = 500.0,
    model_id: str = _MODEL_ID,
    basin_version_id: str = _BASIN_VERSION_ID,
    network_id: str = _NETWORK_ID,
    segment_ids: tuple[str, ...] = _SEGMENT_IDS,
    new_data_source_name: str | None = None,
    seed_river_rows: bool = True,
) -> None:
    """A display-ready run whose river timeseries fill the window HOURLY.

    The difference from ``_seed_rival_display_ready_run`` is the only thing that
    matters to the cycles catalog: that helper writes the two window endpoints,
    which materialises ``max_lead - min_lead + 1 == 2`` over a 2-hour window and
    is rejected by ``_national_coverage_window``'s rectangle check. This one
    writes every segment at every hour with ``lead_time_hours`` equal to the hour
    offset, so ``river_sample_count == segment_count * lead_count`` and
    ``end - start == (lead_count - 1) * 3600`` both hold.

    ``seed_river_rows=False`` writes the run and its forcing version but no river
    rows at all: ``refresh_run_display_coverage`` still materialises a row for it
    (``packages/common/display_coverage.py:756-773``), with ``segment_count = 0``.
    That is the zero-segment rival the ``segment_count > 0`` JOIN predicate has to
    drop.
    """
    assert int((window_end - window_start).total_seconds()) % 3600 == 0, "hourly grid only"
    forcing_version_id = forcing_version_id or f"{run_id}_fv"
    connection = psycopg2.connect(database_url, cursor_factory=RealDictCursor)
    connection.autocommit = True
    try:
        with connection.cursor() as cursor:
            if new_data_source_name is not None:
                cursor.execute(
                    """
                    INSERT INTO met.data_source
                        (source_id, source_name, source_type, status, native_format, adapter_name)
                    VALUES (%s, %s, 'forecast', 'mock', 'netcdf', %s)
                    """,
                    (source_id, new_data_source_name, source_id.lower()),
                )
            cursor.execute(
                """
                INSERT INTO met.forcing_version
                    (forcing_version_id, model_id, source_id, cycle_time, start_time, end_time,
                     station_count, forcing_package_uri, checksum)
                VALUES (%s, %s, %s, %s, %s, %s, 1, %s, %s)
                """,
                (
                    forcing_version_id,
                    model_id,
                    source_id,
                    cycle_time,
                    window_start,
                    window_end,
                    f"s3://nhms/forcing/{forcing_version_id}/",
                    f"forcing-sha-{forcing_version_id}",
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
                    run_id,
                    model_id,
                    basin_version_id,
                    forcing_version_id,
                    source_id,
                    cycle_time,
                    window_start,
                    window_end,
                ),
            )
            if not seed_river_rows:
                return
            lead_hours = int((window_end - window_start).total_seconds()) // 3600
            insert_river_timeseries_dual_written(
                cursor,
                [
                    (
                        run_id,
                        basin_version_id,
                        network_id,
                        segment_id,
                        window_start + timedelta(hours=lead),
                        lead,
                        _VARIABLE,
                        value_base + index,
                        "m3/s",
                        "ok",
                    )
                    for lead in range(lead_hours + 1)
                    for index, segment_id in enumerate(segment_ids)
                ],
            )
    finally:
        connection.close()


def _seed_second_network(database_url: str, *, active: bool = True) -> None:
    """A second river network with its own basin version and model instance.

    Its own ``core.basin_version`` is mandatory, not stylistic: the partial unique
    index ``model_instance_active_basin_version_uidx``
    (``db/migrations/000022_model_asset_lifecycle.sql:61-63``) permits exactly one
    ACTIVE model instance per basin version, and the national ranked query joins
    model instances to runs on ``basin_version_id``, so a second instance on
    basin version 1 would also inherit every one of network 1's runs.

    ``active=False`` sets ``lifecycle_state='inactive'`` alongside
    ``active_flag=false``: the CHECK at ``000022_model_asset_lifecycle.sql:41-46``
    ties the two together, and the intersection denominator is a DISTINCT over
    ``river_network_version_id`` restricted to ``active_flag``, so the inactive
    network must carry its OWN network id to be a real exclusion case.
    """
    connection = psycopg2.connect(database_url, cursor_factory=RealDictCursor)
    connection.autocommit = True
    try:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                INSERT INTO core.basin_version
                    (basin_version_id, basin_id, version_label, geom, active_flag)
                VALUES (%s, %s, 'v2',
                        ST_SetSRID(ST_GeomFromText(
                            'MULTIPOLYGON(((99 37, 99 39, 101 39, 101 37, 99 37)))'), 4490),
                        true)
                """,
                (_SECOND_BASIN_VERSION_ID, _BASIN_ID),
            )
            cursor.execute(
                """
                INSERT INTO core.river_network_version
                    (river_network_version_id, basin_version_id, version_label, segment_count)
                VALUES (%s, %s, 'v2', %s)
                """,
                (_SECOND_NETWORK_ID, _SECOND_BASIN_VERSION_ID, len(_SECOND_SEGMENT_IDS)),
            )
            for index, segment_id in enumerate(_SECOND_SEGMENT_IDS):
                cursor.execute(
                    f"""
                    INSERT INTO core.river_segment
                        (river_segment_id, river_network_version_id, segment_order,
                         geom, properties_json)
                    VALUES (%s, %s, %s, {_segment_geom_sql(index + 10)}, '{{"Type": 5}}'::jsonb)
                    """,
                    (segment_id, _SECOND_NETWORK_ID, index),
                )
            cursor.execute(
                """
                INSERT INTO core.model_instance
                    (model_id, basin_version_id, river_network_version_id, mesh_version_id,
                     calibration_version_id, shud_code_version, model_package_uri,
                     active_flag, lifecycle_state)
                VALUES (%s, %s, %s, 'mesh-2009', 'cal-2009', '1.0', 's3://nhms/model',
                        %s, %s)
                """,
                (
                    _SECOND_MODEL_ID,
                    _SECOND_BASIN_VERSION_ID,
                    _SECOND_NETWORK_ID,
                    active,
                    "active" if active else "inactive",
                ),
            )
    finally:
        connection.close()


def _seed_second_network_run(
    database_url: str,
    *,
    run_id: str,
    cycle_time: datetime,
    window_start: datetime,
    window_end: datetime,
    forcing_version_id: str | None = None,
) -> None:
    """An hourly display-ready run on the SECOND network's model and basin version.

    A named helper rather than a bare call with six overrides, for the same
    reason ``_seed_rival_display_ready_run`` is separate from
    ``_seed_uppercase_ifs_run``: the cases that need a second network must be able
    to fail on their own predicate, not on someone else's default drifting.
    """
    _seed_hourly_display_ready_run(
        database_url,
        run_id=run_id,
        cycle_time=cycle_time,
        window_start=window_start,
        window_end=window_end,
        forcing_version_id=forcing_version_id,
        value_base=600.0,
        model_id=_SECOND_MODEL_ID,
        basin_version_id=_SECOND_BASIN_VERSION_ID,
        network_id=_SECOND_NETWORK_ID,
        segment_ids=_SECOND_SEGMENT_IDS,
    )


def _cycles(client: TestClient, source: str = "gfs") -> dict[str, Any]:
    response = client.get("/api/v1/layers/discharge/cycles", params={"source": source})
    assert response.status_code == 200, response.text
    return dict(response.json()["data"])


def _valid_times(client: TestClient, *, cycle: datetime, source: str = "gfs") -> list[str]:
    response = client.get(
        "/api/v1/layers/discharge/valid-times",
        params={"source": source, "cycle": _stamp(cycle)},
    )
    assert response.status_code == 200, response.text
    return list(response.json()["data"]["valid_times"])


def _assert_coverage_segment_counts(database_url: str, expected: dict[str, int]) -> None:
    """Non-vacuity: the runs a case argues about really are (or are not) candidates."""
    observed = _query(
        database_url,
        "SELECT run_id, segment_count FROM hydro.run_display_coverage WHERE run_id = ANY(%s) ORDER BY run_id",
        (sorted(expected),),
    )
    assert observed == [{"run_id": run_id, "segment_count": expected[run_id]} for run_id in sorted(expected)]


def _assert_run_id_order(database_url: str, run_ids: tuple[str, ...]) -> None:
    """The DB's own collation puts ``run_ids`` in this order under ``ORDER BY run_id DESC``.

    ``ORDER BY h.run_id DESC`` is a TEXT sort, and which run wins ``rn = 1`` is
    the whole point of the cases that call this. Asserting the order in the
    database rather than in Python keeps the case honest under any collation.
    """
    observed = _query(
        database_url,
        "SELECT run_id FROM hydro.hydro_run WHERE run_id = ANY(%s) ORDER BY run_id DESC",
        (sorted(run_ids),),
    )
    assert [row["run_id"] for row in observed] == list(run_ids)


def test_national_cycles_list_only_cycles_covered_by_every_network(
    national_tile: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Matrix row 52: one ranked row per (network, cycle), not one per network.

    Network 1 holds cycles A and B (B newer AND its run id sorts higher);
    network 2 holds A only. Partitioning by network alone lets B win ``rn = 1``
    for network 1, after which NO cycle is covered by both networks and the
    catalog empties -- the fail-closed direction, but on a cycle that really is
    nationally covered.
    """
    database_url, client = national_tile
    monkeypatch.setattr(_WIDE_CYCLE_LOOKBACK, 100_000)
    n1_a = f"{_I5_PREFIX}_n1_run_1_at_a"
    n1_b = f"{_I5_PREFIX}_n1_run_2_at_b"
    n2_a = f"{_I5_PREFIX}_n2_run_1_at_a"
    _seed_second_network(database_url)
    _seed_hourly_display_ready_run(
        database_url,
        run_id=n1_a,
        cycle_time=_I5_CYCLE_A,
        window_start=_I5_CYCLE_A,
        window_end=_I5_CYCLE_A + _I5_WINDOW,
    )
    _seed_hourly_display_ready_run(
        database_url,
        run_id=n1_b,
        cycle_time=_I5_CYCLE_B,
        window_start=_I5_CYCLE_B,
        window_end=_I5_CYCLE_B + _I5_WINDOW,
    )
    _seed_second_network_run(
        database_url,
        run_id=n2_a,
        cycle_time=_I5_CYCLE_A,
        window_start=_I5_CYCLE_A,
        window_end=_I5_CYCLE_A + _I5_WINDOW,
    )
    for run_id in (n1_a, n1_b, n2_a):
        _refresh_coverage(database_url, run_id)
    # Without this the mutated partition would pick A for network 1 by accident
    # and the case would stay green for the wrong reason.
    _assert_run_id_order(database_url, (n1_b, n1_a))
    _assert_coverage_segment_counts(database_url, {n1_a: 2, n1_b: 2, n2_a: 2})
    assert _query(
        database_url,
        "SELECT count(DISTINCT river_network_version_id) AS n FROM core.model_instance WHERE active_flag",
        (),
    ) == [{"n": 2}]

    data = _cycles(client)

    assert data["cycles"] == [
        {
            "cycle_time": _stamp(_I5_CYCLE_A),
            "valid_time_start": _stamp(_I5_CYCLE_A),
            "valid_time_end": _stamp(_I5_CYCLE_A),
        }
    ]
    assert data["default_cycle"] == _stamp(_I5_CYCLE_A)


def test_national_valid_times_are_empty_for_a_cycle_outside_the_intersection(
    national_tile: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Matrix row 55: ``:cycle`` narrows the per-cycle query in SQL, not only in the fake.

    Both networks hold cycle A. P (= A - 6h) is a cycle NO run is ever seeded at
    -- the same production shape ``_PRUNED_CYCLE_TIME`` above stands for -- so it
    is outside the intersection and its timeline must be empty.

    P must be EARLIER than A, and P must hold no rows of its own. Dropping the
    ``:cycle`` conjunct does NOT collapse the result to one row per network (the
    window still partitions by ``(network, cycle)``, matrix row 52); it returns
    every ranked row, so the mutated call for P sees exactly the two A-rows,
    ``covered == active``, and the clamp from P lands on ``A`` -- non-empty, which
    is the red. Seeding P a run of its own would instead put ``[P, P+2h]`` into the
    intersection, ``window_end < window_start``, and ``mvt.py:2129`` would empty the
    mutated result too; a LATER cycle fails the same way. Either variant is green
    under the mutation and is NOT an oracle.
    """
    database_url, client = national_tile
    monkeypatch.setattr(_WIDE_CYCLE_LOOKBACK, 100_000)
    n1_a = f"{_I5_PREFIX}_n1_run_1_at_a"
    n2_a = f"{_I5_PREFIX}_n2_run_1_at_a"
    _seed_second_network(database_url)
    _seed_hourly_display_ready_run(
        database_url,
        run_id=n1_a,
        cycle_time=_I5_CYCLE_A,
        window_start=_I5_CYCLE_A,
        window_end=_I5_CYCLE_A + _I5_WINDOW,
    )
    _seed_second_network_run(
        database_url,
        run_id=n2_a,
        cycle_time=_I5_CYCLE_A,
        window_start=_I5_CYCLE_A,
        window_end=_I5_CYCLE_A + _I5_WINDOW,
    )
    for run_id in (n1_a, n2_a):
        _refresh_coverage(database_url, run_id)
    _assert_coverage_segment_counts(database_url, {n1_a: 2, n2_a: 2})
    # Non-vacuity: P really is unseeded, so the empty answer below is about the
    # intersection and not about a run that happens to be missing coverage.
    assert _query(
        database_url,
        "SELECT count(*) AS n FROM hydro.hydro_run WHERE cycle_time = %s",
        (_I5_CYCLE_P,),
    ) == [{"n": 0}]

    assert _valid_times(client, cycle=_I5_CYCLE_P) == []
    assert _valid_times(client, cycle=_I5_CYCLE_A) == [_stamp(_I5_CYCLE_A)]


def test_national_cycles_ignore_an_inactive_network(
    national_tile: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Matrix row 50: only ACTIVE instances form the intersection denominator.

    The second network is inactive and has no runs at all. With
    ``WHERE mi.active_flag`` widened to ``WHERE TRUE`` it joins the denominator,
    nothing covers it, and a nationally covered cycle disappears.
    """
    database_url, client = national_tile
    monkeypatch.setattr(_WIDE_CYCLE_LOOKBACK, 100_000)
    n1_a = f"{_I5_PREFIX}_n1_run_1_at_a"
    _seed_second_network(database_url, active=False)
    _seed_hourly_display_ready_run(
        database_url,
        run_id=n1_a,
        cycle_time=_I5_CYCLE_A,
        window_start=_I5_CYCLE_A,
        window_end=_I5_CYCLE_A + _I5_WINDOW,
    )
    _refresh_coverage(database_url, n1_a)
    # Two networks exist; exactly one of them is active.
    assert _query(
        database_url,
        "SELECT count(*) AS n FROM core.river_network_version",
        (),
    ) == [{"n": 2}]
    assert _query(
        database_url,
        "SELECT count(DISTINCT river_network_version_id) AS n FROM core.model_instance WHERE active_flag",
        (),
    ) == [{"n": 1}]

    data = _cycles(client)

    assert [entry["cycle_time"] for entry in data["cycles"]] == [_stamp(_I5_CYCLE_A)]
    assert data["default_cycle"] == _stamp(_I5_CYCLE_A)


def test_national_cycles_keep_a_cycle_whose_zero_segment_rival_run_sorts_first(
    national_tile: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Matrix row 3: ``AND rdc.segment_count > 0`` sits upstream of ``ROW_NUMBER()``.

    One network, two runs at cycle A: a complete hourly one and a rival with no
    river timeseries at all, whose run id sorts ABOVE it. Unmutated the JOIN
    predicate drops the zero-segment run before the ranking, so the complete run
    wins ``rn = 1`` and A is listed. Delete the predicate and the zero-segment run
    wins instead, ``_national_coverage_window`` rejects its NULL window, and a
    covered cycle vanishes.

    A SINGLE zero-segment run is not an oracle here: the cycle is unlisted either
    way. The rival pair is what makes the predicate observable.
    """
    database_url, client = national_tile
    monkeypatch.setattr(_WIDE_CYCLE_LOOKBACK, 100_000)
    complete_run = f"{_I5_PREFIX}_n1_run_1_complete"
    zero_segment_run = f"{_I5_PREFIX}_n1_run_2_zero_segment"
    _seed_hourly_display_ready_run(
        database_url,
        run_id=complete_run,
        cycle_time=_I5_CYCLE_A,
        window_start=_I5_CYCLE_A,
        window_end=_I5_CYCLE_A + _I5_WINDOW,
    )
    _seed_hourly_display_ready_run(
        database_url,
        run_id=zero_segment_run,
        cycle_time=_I5_CYCLE_A,
        window_start=_I5_CYCLE_A,
        window_end=_I5_CYCLE_A + _I5_WINDOW,
        seed_river_rows=False,
    )
    _refresh_coverage(database_url, complete_run)
    _refresh_coverage(database_url, zero_segment_run)
    _assert_run_id_order(database_url, (zero_segment_run, complete_run))
    _assert_coverage_segment_counts(database_url, {complete_run: 2, zero_segment_run: 0})

    data = _cycles(client)

    assert [entry["cycle_time"] for entry in data["cycles"]] == [_stamp(_I5_CYCLE_A)]
    assert data["default_cycle"] == _stamp(_I5_CYCLE_A)


def test_national_cycles_skip_a_cycle_older_than_the_lookback(national_tile: Any) -> None:
    """Matrix row 36: the lookback predicate really bounds the DB scan.

    The ONE case here that must run against the REAL
    ``NATIONAL_DISCHARGE_CYCLE_LOOKBACK_DAYS`` -- no widening. Both networks cover
    the seed cycle A (months old) and a recent cycle R, so the intersection alone
    would list both; only the ``h.cycle_time >= :since`` conjunct drops A.
    """
    database_url, client = national_tile
    recent = datetime.now(UTC).replace(minute=0, second=0, microsecond=0) - timedelta(days=1)
    n1_a = f"{_I5_PREFIX}_n1_run_1_at_a"
    n1_r = f"{_I5_PREFIX}_n1_run_2_at_r"
    n2_a = f"{_I5_PREFIX}_n2_run_1_at_a"
    n2_r = f"{_I5_PREFIX}_n2_run_2_at_r"
    _seed_second_network(database_url)
    for run_id, cycle_time in ((n1_a, _I5_CYCLE_A), (n1_r, recent)):
        _seed_hourly_display_ready_run(
            database_url,
            run_id=run_id,
            cycle_time=cycle_time,
            window_start=cycle_time,
            window_end=cycle_time + _I5_WINDOW,
        )
    for run_id, cycle_time in ((n2_a, _I5_CYCLE_A), (n2_r, recent)):
        _seed_second_network_run(
            database_url,
            run_id=run_id,
            cycle_time=cycle_time,
            window_start=cycle_time,
            window_end=cycle_time + _I5_WINDOW,
        )
    for run_id in (n1_a, n1_r, n2_a, n2_r):
        _refresh_coverage(database_url, run_id)
    _assert_coverage_segment_counts(database_url, {n1_a: 2, n1_r: 2, n2_a: 2, n2_r: 2})

    data = _cycles(client)

    # A is fully covered by both networks and still absent: age is the only
    # difference between it and R.
    assert [entry["cycle_time"] for entry in data["cycles"]] == [_stamp(recent)]
    assert data["default_cycle"] == _stamp(recent)


def test_national_cycles_match_an_uppercase_source_id_from_a_lowercase_query(
    national_tile: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Matrix row 54: ``lower(h.source_id) = :source`` narrows the query in SQL.

    Production stores ``gfs`` lower-case and ``IFS`` UPPER-case. The two sources
    sit at different cycles, so dropping the conjunct (keeping the bind) makes
    each request list both cycles instead of its own.
    """
    database_url, client = national_tile
    monkeypatch.setattr(_WIDE_CYCLE_LOOKBACK, 100_000)
    gfs_run = f"{_I5_PREFIX}_n1_run_1_gfs"
    ifs_run = f"{_I5_PREFIX}_n1_run_2_ifs"
    _seed_hourly_display_ready_run(
        database_url,
        run_id=gfs_run,
        cycle_time=_I5_CYCLE_A,
        window_start=_I5_CYCLE_A,
        window_end=_I5_CYCLE_A + _I5_WINDOW,
    )
    _seed_hourly_display_ready_run(
        database_url,
        run_id=ifs_run,
        source_id=_IFS_SOURCE_ID,
        cycle_time=_I5_IFS_CYCLE,
        window_start=_I5_IFS_CYCLE,
        window_end=_I5_IFS_CYCLE + _I5_WINDOW,
        value_base=700.0,
        new_data_source_name="IFS 2009",
    )
    _refresh_coverage(database_url, gfs_run)
    _refresh_coverage(database_url, ifs_run)
    _assert_coverage_segment_counts(database_url, {gfs_run: 2, ifs_run: 2})
    assert _query(
        database_url,
        "SELECT source_id FROM hydro.hydro_run WHERE run_id = %s",
        (ifs_run,),
    ) == [{"source_id": "IFS"}], "the case is about case folding; the seed must be upper-case"

    assert [entry["cycle_time"] for entry in _cycles(client, "ifs")["cycles"]] == [_stamp(_I5_IFS_CYCLE)]
    assert [entry["cycle_time"] for entry in _cycles(client, "gfs")["cycles"]] == [_stamp(_I5_CYCLE_A)]


def test_national_cycles_take_the_newest_run_at_a_cycle(
    national_tile: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Matrix rows 53 and 56: ``ORDER BY h.run_id DESC`` picks the winner, ``rn = 1`` keeps it.

    Two runs at cycle A on one network: the older covers ``A … A+2h``, the newer
    ``A … A+5h``. The listed window's END is the discriminator -- ``A+3h`` from the
    newer run, ``A`` from the older one. ``ORDER BY ... ASC`` picks the older run;
    ``WHERE rn >= 1`` keeps BOTH rows and the intersection clamp then takes the
    shorter window. Both mutations land on ``A``.
    """
    database_url, client = national_tile
    monkeypatch.setattr(_WIDE_CYCLE_LOOKBACK, 100_000)
    older_run = f"{_I5_PREFIX}_n1_run_1_short_window"
    newer_run = f"{_I5_PREFIX}_n1_run_2_long_window"
    _seed_hourly_display_ready_run(
        database_url,
        run_id=older_run,
        cycle_time=_I5_CYCLE_A,
        window_start=_I5_CYCLE_A,
        window_end=_I5_CYCLE_A + timedelta(hours=2),
    )
    _seed_hourly_display_ready_run(
        database_url,
        run_id=newer_run,
        cycle_time=_I5_CYCLE_A,
        window_start=_I5_CYCLE_A,
        window_end=_I5_CYCLE_A + timedelta(hours=5),
        value_base=800.0,
    )
    _refresh_coverage(database_url, older_run)
    _refresh_coverage(database_url, newer_run)
    _assert_run_id_order(database_url, (newer_run, older_run))
    _assert_coverage_segment_counts(database_url, {older_run: 2, newer_run: 2})

    data = _cycles(client)

    assert data["cycles"] == [
        {
            "cycle_time": _stamp(_I5_CYCLE_A),
            "valid_time_start": _stamp(_I5_CYCLE_A),
            "valid_time_end": _stamp(_I5_CYCLE_A + timedelta(hours=3)),
        }
    ]
    assert data["default_cycle"] == _stamp(_I5_CYCLE_A)


# ---------------------------------------------------------------------------
# #2031: the national cache identity must describe the data the tile reads.
# Appended at the end of the file so every line citation above keeps its number.
#
# Two failures, one invariant:
#
# * A. The digest ranked each network's latest run for the bound `(source,
#   cycle)` WITHOUT the coverage-window clamp `latest_runs` applies, so two
#   instants served by two different runs shared one cache key. Measured on
#   node-27 (`docs/runbooks/receipts/2026-09-08-issue-2031-digest-precondition.md`):
#   reachable on 38/38 active networks for the legacy route, and 20 same-cycle
#   double-run groups on the new one.
# * B. `_backfill_output_segment_geometry` rewrites `core.river_segment.geom`
#   and the STORED `stream_type` UNDER an unchanged network version. No run row
#   moves and `rnv.segment_count`/`checksum` describe the imported package, so
#   both national digests were blind to it. `geometry_generation` (000057) is
#   the missing signal.
#
# Neither is visible to a fake session: A is a SQL predicate that only a real
# planner applies, and B needs a real PostGIS UPDATE plus the generated
# `stream_type` column. Hence both live here.
# ---------------------------------------------------------------------------

# Lexically GREATER than `_RUN_ID` (`it1596_forecast_run`), and neither a
# substring of it nor containing it, so `_assert_tile_was_painted_by` stays
# falsifiable. Greater is the direction that matters: the ranking's tie-break at
# an equal `cycle_time` is `ORDER BY h.run_id DESC`, so this run wins rank 1
# wherever it is a candidate at all -- which is exactly what the window clamp
# has to take away at an instant it does not cover.
_RIVAL_RUN_ID = "it2031_gfs_same_cycle_rival_run"
_RIVAL_FORCING_VERSION_ID = "it2031_forcing_gfs_same_cycle_v1"
# The rival's window ENDS an hour before the base run's does. Same cycle, same
# source, same network: the window is the ONLY thing separating them.
_RIVAL_WINDOW_END = _LATE_CYCLE_TIME

_OUTPUT_SEGMENT_ID = f"{_PREFIX}_shud_riv_000001"
_REACH_SEGMENT_ID = f"{_PREFIX}_reach_000001"
_REACH_INDEX = 1


def _national_digests(database_url: str) -> dict[str, str]:
    """Both national digests, read through a connection of their own.

    A fresh engine per call rather than one long-lived session: these values are
    compared ACROSS a commit made by a different connection, and a session left
    holding an open transaction would be comparing snapshots instead of states.
    The engine is disposed because the throwaway database is DROPped on
    teardown and a live pooled connection makes that DROP block.
    """
    engine = sqlalchemy_engine(database_url)
    try:
        with Session(engine) as session:
            return {
                "discharge": national_discharge_source_version(session),
                "river_network": national_river_network_source_version(session),
            }
    finally:
        engine.dispose()


def _bound_digest(database_url: str, *, valid_time: datetime | None) -> str:
    engine = sqlalchemy_engine(database_url)
    try:
        with Session(engine) as session:
            return national_discharge_source_version(
                session, source="gfs", cycle=_CYCLE_TIME, valid_time=valid_time
            )
    finally:
        engine.dispose()


def _coverage_window(database_url: str, run_id: str) -> tuple[datetime, datetime]:
    rows = _query(
        database_url,
        """
        SELECT river_valid_time_start, river_valid_time_end
        FROM hydro.run_display_coverage WHERE run_id = %s
        """,
        (run_id,),
    )
    assert len(rows) == 1, f"{run_id} has no coverage row, so it is not a digest candidate at all"
    return rows[0]["river_valid_time_start"], rows[0]["river_valid_time_end"]


def _geometry_generation(database_url: str, river_network_version_id: str) -> int:
    rows = _query(
        database_url,
        "SELECT geometry_generation FROM core.river_network_version WHERE river_network_version_id = %s",
        (river_network_version_id,),
    )
    assert len(rows) == 1, river_network_version_id
    return int(rows[0]["geometry_generation"])


def _clear_tile_cache(database_url: str) -> int:
    """Drop the DB tile tier so the next request REGENERATES instead of replaying.

    Deliberately not routed through `_query`: that helper opens a connection
    without autocommit and never commits, so a DELETE through it is rolled back
    on close and silently does nothing -- the exact failure mode that would turn
    the tile-side oracle below back into a cache replay.

    Only the DB tier needs clearing: the fixture never sets
    `NHMS_MVT_FILE_CACHE_DIR`, so the file tier is off.
    """
    connection = psycopg2.connect(database_url, cursor_factory=RealDictCursor)
    connection.autocommit = True
    try:
        with connection.cursor() as cursor:
            cursor.execute("DELETE FROM map.tile_cache")
            return int(cursor.rowcount)
    finally:
        connection.close()


def test_national_digest_binds_the_instant_so_a_rival_outside_the_window_moves_nothing(
    national_tile: Any,
) -> None:
    """#2031 A: the digest ranks the run `latest_runs` paints, per instant.

    Two display-ready gfs runs at the SAME cycle on one network. The rival has
    the greater `run_id`, so the unbound tie-break gives it rank 1 everywhere --
    but its coverage window stops an hour short of `_WINDOW_END`, so the tile at
    `_WINDOW_END` is still painted by the base run. Before the clamp the digest
    said otherwise: the cache key for `_WINDOW_END` rotated onto a run that
    instant is never served from, and — the failure that actually bites — the
    key for `_WINDOW_END` and the key for an instant the rival DOES serve moved
    in lockstep, so one of the two was always describing the wrong run.

    Four assertions, four different questions:

    * bound to `_WINDOW_END` -> UNCHANGED by the rival's arrival (the clamp
      excludes it). This is the one that fails without the fix.
    * bound to `_RIVAL_WINDOW_END`, which BOTH runs cover -> MOVED (the rival is
      genuinely the rank-1 run there). Non-vacuity for the first: a digest that
      simply ignored the rival everywhere would satisfy it.
    * unbound -> MOVED, because the instant-less question is still "each
      network's overall latest run" and that is now the rival.
    * the TILE at `_WINDOW_END` is still painted by the base run -- the tile
      half of the same clamp, read out of `latest_runs` rather than out of the
      digest.

    That fourth question only exists because `_clear_tile_cache` runs first.
    With the digest unchanged the cache key is unchanged too, so without the
    clear the second response is a cache hit by construction and replays the
    baseline bytes: `_assert_tile_was_painted_by` would then re-assert the
    BASELINE tile and say nothing at all about how `latest_runs` ranks the two
    runs now that the rival exists. Clearing the DB tile tier forces a
    regeneration -- proved, not assumed, by `X-Tile-Cache: miss` -- so the run
    identity in those bytes is the one the tile SQL just selected.

    The byte equality is the weaker half and now asserts something different
    from before: that regenerating the tile reproduces the baseline bytes.
    `_assert_tile_was_painted_by` remains the load-bearing tile-side assertion.
    """
    database_url, client = national_tile
    _refresh_coverage(database_url)

    baseline_at_window_end = _bound_digest(database_url, valid_time=_WINDOW_END)
    baseline_at_rival_window_end = _bound_digest(database_url, valid_time=_RIVAL_WINDOW_END)
    baseline_unbound = _national_digests(database_url)["discharge"]
    # Non-vacuity: the baselines really do observe one ranked run. An empty
    # basis (no coverage refresh, say) would make every comparison below a
    # comparison of two empty digests.
    assert baseline_at_window_end.endswith(":1"), baseline_at_window_end
    assert baseline_unbound.endswith(":1"), baseline_unbound
    baseline_tile = _request_identity_tile(client, "gfs", _CYCLE_TIME, _WINDOW_END)
    _assert_tile_was_painted_by(baseline_tile, _RUN_ID, _RIVAL_RUN_ID)

    _seed_rival_display_ready_run(
        database_url,
        run_id=_RIVAL_RUN_ID,
        source_id=_SOURCE_ID,
        forcing_version_id=_RIVAL_FORCING_VERSION_ID,
        cycle_time=_CYCLE_TIME,
        window_start=_WINDOW_START,
        window_end=_RIVAL_WINDOW_END,
        value_base=500.0,
    )
    _refresh_coverage(database_url, _RIVAL_RUN_ID)

    # Non-vacuity for the whole case, in three parts.
    #
    # (1) Both runs are real display-ready candidates. Asserted at
    # `_WINDOW_START`, which both windows contain and where both runs hold a
    # full segment set -- `_assert_both_runs_are_candidates_at` also demands
    # fact rows at the instant, and neither `_WINDOW_END` (the rival has none)
    # nor `_RIVAL_WINDOW_END` (the base seed leaves that hour empty on purpose,
    # it is `_GAP_TIME`) satisfies that for both runs.
    _assert_both_runs_are_candidates_at(database_url, (_RUN_ID, _RIVAL_RUN_ID), _WINDOW_START)
    # (2) The windows really are what the case claims: only the base run covers
    # `_WINDOW_END`, and BOTH cover `_RIVAL_WINDOW_END`. This is the entire
    # mechanism, materialized by the production coverage refresh rather than
    # asserted from the seed arguments.
    base_start, base_end = _coverage_window(database_url, _RUN_ID)
    rival_start, rival_end = _coverage_window(database_url, _RIVAL_RUN_ID)
    assert base_start <= _WINDOW_END <= base_end
    assert rival_end < _WINDOW_END, "the rival must NOT cover _WINDOW_END, or the case proves nothing"
    assert base_start <= _RIVAL_WINDOW_END <= base_end
    assert rival_start <= _RIVAL_WINDOW_END <= rival_end
    # (3) The rival really would win the ranking wherever it is a candidate:
    # same cycle, greater run_id under the database's own collation.
    assert _query(
        database_url,
        "SELECT run_id FROM hydro.hydro_run WHERE source_id = %s AND cycle_time = %s ORDER BY run_id DESC",
        (_SOURCE_ID, _CYCLE_TIME),
    ) == [{"run_id": _RIVAL_RUN_ID}, {"run_id": _RUN_ID}], (
        "the rival must sort above the base run at the same cycle, or the clamp has nothing to undo"
    )

    assert _bound_digest(database_url, valid_time=_WINDOW_END) == baseline_at_window_end, (
        "a run whose coverage window excludes the requested instant must not enter the digest: "
        "the tile at that instant is still painted by the base run"
    )
    assert _bound_digest(database_url, valid_time=_RIVAL_WINDOW_END) != baseline_at_rival_window_end, (
        "at an instant BOTH runs cover, the rival IS the run the tile paints and the key must rotate"
    )
    assert _national_digests(database_url)["discharge"] != baseline_unbound, (
        "the instant-less question is unchanged: each network's overall latest run, now the rival"
    )

    # The tile half of the same clamp. Non-vacuity: the baseline request must
    # really have populated the DB tier, or there is nothing to clear and the
    # "miss" below would be describing a cache that was never warm.
    assert _clear_tile_cache(database_url) >= 1, (
        "the baseline tile request must have written the DB tile tier, "
        "or clearing it proves nothing about the request that follows"
    )
    after_tile = _request_identity_tile(client, "gfs", _CYCLE_TIME, _WINDOW_END)
    assert after_tile.headers["X-Tile-Cache"] == "miss", (
        "this tile must be REGENERATED, not replayed: on a cache hit the run identity "
        "below is the baseline's and asserts nothing about how latest_runs ranks the rival"
    )
    _assert_tile_was_painted_by(after_tile, _RUN_ID, _RIVAL_RUN_ID)
    assert after_tile.content == baseline_tile.content


def _seed_output_and_reach_rows(database_url: str) -> None:
    """The minimal pair `_backfill_output_segment_geometry` needs, and nothing else.

    It reads two sets out of ``core.river_segment`` for one network:

    * output rows -- ``shud_output_river='true'`` with a numeric
      ``shud_riv_index`` -- seeded here with NULL geom and no ``Type``, which is
      the state ``_ensure_output_river_segments`` leaves them in;
    * reach rows -- geom NOT NULL, NOT ``shud_output_river``, numeric ``iRiv`` --
      the gis/river.shp source it copies geom/``length_m``/``Type`` from.

    The base seed's `_SEGMENT_IDS` rows are neither (no ``iRiv``, no
    ``shud_output_river``), so they cannot accidentally act as a source or a
    target, and the network's `segment_count` is deliberately left at 2 so the
    coverage windows already materialized do not move.
    """
    connection = psycopg2.connect(database_url, cursor_factory=RealDictCursor)
    connection.autocommit = True
    try:
        with connection.cursor() as cursor:
            cursor.execute(
                f"""
                INSERT INTO core.river_segment
                    (river_segment_id, river_network_version_id, segment_order,
                     length_m, geom, properties_json)
                VALUES (%s, %s, %s, %s, {_segment_geom_sql(5)}, %s::jsonb)
                """,
                (
                    _REACH_SEGMENT_ID,
                    _NETWORK_ID,
                    10,
                    1234.5,
                    f'{{"iRiv": {_REACH_INDEX}, "Type": 4}}',
                ),
            )
            cursor.execute(
                """
                INSERT INTO core.river_segment
                    (river_segment_id, river_network_version_id, segment_order,
                     geom, properties_json)
                VALUES (%s, %s, %s, NULL, %s::jsonb)
                """,
                (
                    _OUTPUT_SEGMENT_ID,
                    _NETWORK_ID,
                    11,
                    f'{{"shud_output_river": "true", "shud_riv_index": "{_REACH_INDEX}"}}',
                ),
            )
    finally:
        connection.close()


def test_geometry_backfill_rotates_both_national_digests_exactly_once(national_tile: Any) -> None:
    """#2031 B: an in-place geometry rewrite must move both national cache keys.

    `_backfill_output_segment_geometry` is the only `UPDATE core.river_segment`
    in production code (pinned by
    `tests/test_river_segment_write_surface_scan.py`). It moves `geom`, `length_m`
    and the source `Type` -- and therefore the STORED `stream_type` both national
    tile queries filter on -- onto rows that already exist, under a network
    version whose `segment_count` and `checksum` do not move because they
    describe the imported package. Before `geometry_generation` both digests were
    byte-identical across that rewrite, so every cached national tile kept
    serving pre-backfill geometry with no TTL to save it.

    The second half is the guard that makes the first affordable: every
    bootstrap tick runs the same backfill with `only_missing=True` over
    already-complete networks. That pass must update nothing, bump nothing, and
    leave both digests byte-identical -- otherwise the fix would rotate all 38
    networks' keys on every tick.

    Ordering note: coverage is refreshed BEFORE the baselines are taken. The
    discharge digest INNER JOINs `hydro.run_display_coverage`, so without it the
    basis is empty and pre/post would match for the wrong reason -- hence the
    `:1` suffix assertion (the digest's trailing field is the basis row count).
    """
    database_url, _client = national_tile
    _refresh_coverage(database_url)
    _seed_output_and_reach_rows(database_url)

    assert _geometry_generation(database_url, _NETWORK_ID) == 0, (
        "migration 000057 must default every pre-existing network to 0"
    )
    before = _national_digests(database_url)
    assert before["discharge"].endswith(":1"), before["discharge"]
    assert before["river_network"].endswith(":1"), before["river_network"]

    # A real cursor, and NOT autocommit: the bump has to land in the same
    # transaction as the geometry it describes, and a rollback must drop both.
    connection = psycopg2.connect(database_url, cursor_factory=RealDictCursor)
    connection.autocommit = False
    try:
        with connection.cursor() as cursor:
            updated = _backfill_output_segment_geometry(cursor, _NETWORK_ID)
            # Visible inside the transaction, before anyone else can see it.
            cursor.execute(
                "SELECT geometry_generation FROM core.river_network_version "
                "WHERE river_network_version_id = %s",
                (_NETWORK_ID,),
            )
            in_transaction_generation = int(cursor.fetchone()["geometry_generation"])
        connection.commit()
    finally:
        connection.close()

    assert updated == 1, "the backfill must have rewritten the seeded output row"
    assert in_transaction_generation == 1
    # The rewrite really happened, and it carried the source `Type` through to
    # the generated column the national queries filter on.
    rewritten = _query(
        database_url,
        """
        SELECT geom IS NOT NULL AS has_geom, stream_type, length_m
        FROM core.river_segment
        WHERE river_segment_id = %s AND river_network_version_id = %s
        """,
        (_OUTPUT_SEGMENT_ID, _NETWORK_ID),
    )
    assert rewritten[0]["has_geom"] is True
    assert rewritten[0]["stream_type"] == 4.0
    assert rewritten[0]["length_m"] == 1234.5

    assert _geometry_generation(database_url, _NETWORK_ID) == 1
    after = _national_digests(database_url)
    assert after["discharge"] != before["discharge"], (
        "the discharge digest must rotate when the geometry its tile paints is rewritten"
    )
    assert after["river_network"] != before["river_network"], (
        "the river-network digest paints the same geometry and must rotate with it"
    )

    # Second pass on the now-complete network: the shape every bootstrap tick
    # runs. Nothing to update, so nothing may rotate.
    connection = psycopg2.connect(database_url, cursor_factory=RealDictCursor)
    connection.autocommit = False
    try:
        with connection.cursor() as cursor:
            second_pass = _backfill_output_segment_geometry(cursor, _NETWORK_ID, only_missing=True)
        connection.commit()
    finally:
        connection.close()

    assert second_pass == 0
    assert _geometry_generation(database_url, _NETWORK_ID) == 1, (
        "a zero-row backfill must not bump: every tick would otherwise rotate every national key"
    )
    unchanged = _national_digests(database_url)
    assert unchanged == after
