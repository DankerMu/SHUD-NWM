"""Shared seeds, fixture and assertions of the ``hydro-national`` identity-probe oracle.

Non-collectible support module for the three partitions of the #1596 oracle
(#2490 split, pure move): ``tests/test_mvt_national_identity_probe_integration.py``
(the tile/424 cases and the module docstring that explains the traps),
``tests/test_mvt_national_identity_probe_cycles_integration.py`` (#2009 cycles
catalog) and ``tests/test_mvt_national_identity_probe_digest_integration.py``
(digest binding, geometry backfill, full-coverage identity). The ``integration``
in this file name keeps it inside the ci.yml ``database`` filter glob
``tests/*integration*.py``.
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
from packages.common import display_coverage
from packages.common.forecast_store import MVP_STATION_VARIABLES, QHH_LATEST_EXPECTED_HORIZON_HOURS
from services.tiles.mvt import (
    MVT_MEDIA_TYPE,
)
from tests.integration_helpers import (
    FORCING_PLANE_MIGRATIONS,
    apply_migrations_from_zero,
    insert_river_timeseries_dual_written,
    set_integration_env,
)

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
    """Fixture preparation: seed a coverage row with the PRODUCTION refresh DML.

    It ran the frozen pre-store snapshot until #1342's contract (task 6.3)
    deleted it together with the store mapping each test used to select. Running
    the live statement is strictly better for a fixture: this file is not the
    refresh DML's oracle (``tests/test_display_coverage_refresh.py`` is), and a
    seed built from a frozen copy would drift silently away from the rows the
    probe under test actually reads.
    """
    connection = psycopg2.connect(database_url, cursor_factory=RealDictCursor)
    try:
        with connection.cursor() as cursor:
            cursor.execute(
                display_coverage._REFRESH_SQL,
                {
                    "horizon": QHH_LATEST_EXPECTED_HORIZON_HOURS,
                    "basin_id": None,
                    "run_id": run_id,
                    "variables": list(MVP_STATION_VARIABLES),
                    "variable_count": len(MVP_STATION_VARIABLES),
                    "force": False,
                    # #2504: unbound cutoff = the pre-#2504 guard.
                    "expired_cutoff": None,
                    "scan_run_id": None,
                    "scan_forcing_version_id": None,
                    "scan_basin_version_id": None,
                    "scan_river_network_version_id": None,
                    "scan_source_id_lower": None,
                    "scan_display_start": None,
                    "scan_display_end": None,
                },
            )
            assert [dict(row) for row in cursor.fetchall()] == [{"run_id": run_id}]
        connection.commit()
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
    # 000058 pins RIVER's pre-identity catalog. The display coverage refresh
    # this fixture's tests drive also reads the FORCING plane, whose readers
    # name `met.forcing_station_timeseries_legacy` unconditionally after
    # #1991, so the forcing expand applies on top of the river pin.
    apply_migrations_from_zero(throwaway_database_url, through="000058", also=FORCING_PLANE_MIGRATIONS)
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
#   `tests/test_hydro_display_mvt_scaling_catalog.py::test_national_coverage_statements_pin_their_shape`
#   (#2074 partitioned the former single suite; the case itself is unchanged).
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


def _assert_coverage_segment_counts(database_url: str, expected: dict[str, int]) -> None:
    """Non-vacuity: the runs a case argues about really are (or are not) candidates."""
    observed = _query(
        database_url,
        "SELECT run_id, segment_count FROM hydro.run_display_coverage WHERE run_id = ANY(%s) ORDER BY run_id",
        (sorted(expected),),
    )
    assert observed == [{"run_id": run_id, "segment_count": expected[run_id]} for run_id in sorted(expected)]
