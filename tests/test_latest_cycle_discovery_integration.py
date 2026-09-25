"""#2424 D1 on a real PostgreSQL + TimescaleDB: old and new latest-cycle discovery agree.

A text oracle cannot say which cycles a statement SELECTS, so this module seeds a
throwaway catalog (every migration applied) with the run shapes tasks.md 2.1
enumerates and runs, on ONE snapshot, the pre-#2424 statement (the frozen
test-only oracle in ``tests/latest_cycle_discovery_oracle.py``) and the shipping
``PsycopgForecastStore._per_source_latest_cycles``. Every pin must give EQUAL
per-scenario dicts, and the dicts must equal the cycles worked out by hand below
(an independent expectation, so two statements agreeing on a wrong answer is red
too).

Run on node-27's disposable database (never production):

    mkdir -p /home/nwm/tmp && export TMPDIR=/home/nwm/tmp
    NHMS_RUN_INTEGRATION=1 NHMS_INTEGRATION_DATABASE_URL=... \\
        uv run pytest -q tests/test_latest_cycle_discovery_integration.py

SILENT-SKIP TRAP: without both variables every case below skips. A green run
that skipped is not evidence; read the collected/passed count.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import psycopg2
import pytest
from psycopg2.extras import RealDictCursor, execute_values

from packages.common import forecast_store
from packages.common.forecast_store import PsycopgForecastStore
from tests.integration_helpers import (
    BASIN_ID,
    BASIN_VERSION_ID,
    CYCLE_TIME,
    FORECAST_RUN_ID,
    ISSUE_126_PREFIX,
    MESH_VERSION_ID,
    MODEL_ID,
    RIVER_NETWORK_VERSION_ID,
    apply_migrations_from_zero,
    insert_river_timeseries_dual_written,
    seed_issue_126_data,
)
from tests.latest_cycle_discovery_oracle import pre_2424_latest_cycles

pytestmark = pytest.mark.integration

SEG_INSIDE = f"{ISSUE_126_PREFIX}_seg_inside"
SEG_OUTSIDE = f"{ISSUE_126_PREFIX}_seg_outside"
MODEL_2 = f"{ISSUE_126_PREFIX}_model_2"
OTHER_BASIN_VERSION_ID = f"{ISSUE_126_PREFIX}_other_basin_v1"
OTHER_RIVER_NETWORK_VERSION_ID = f"{ISSUE_126_PREFIX}_other_rnv_v1"
OTHER_SEGMENT = f"{ISSUE_126_PREFIX}_other_seg"

GFS = "forecast_gfs_deterministic"
IFS = "forecast_ifs_deterministic"


def _cycle(hours: int) -> datetime:
    return CYCLE_TIME + timedelta(hours=hours)


#: (run_id, scenario, source, model, basin, cycle offset hours or None, status,
#:  segments that get q_down rows as (network, segment)). The seed's own
#: FORECAST_RUN_ID (GFS, cycle +0, parsed) carries rows on BOTH seeded segments.
RUNS: tuple[tuple[str, str, str, str, str, int | None, str, tuple[tuple[str, str], ...]], ...] = (
    # GFS, newer than the seed run and FAILED, with rows on the inside segment:
    # still eligible — no status predicate (user decision (a)).
    (
        f"{ISSUE_126_PREFIX}_gfs_failed",
        GFS,
        "gfs",
        MODEL_ID,
        BASIN_VERSION_ID,
        12,
        "failed",
        ((RIVER_NETWORK_VERSION_ID, SEG_INSIDE),),
    ),
    # GFS, the basin's NEWEST run, rows only on the OUTSIDE segment: for the
    # inside segment the older failed cycle must win; for the outside one, this.
    (
        f"{ISSUE_126_PREFIX}_gfs_newest_outside_only",
        GFS,
        "gfs",
        MODEL_ID,
        BASIN_VERSION_ID,
        18,
        "published",
        ((RIVER_NETWORK_VERSION_ID, SEG_OUTSIDE),),
    ),
    # GFS, cycle_time NULL, with rows: never a candidate in either shape.
    (
        f"{ISSUE_126_PREFIX}_gfs_null_cycle",
        GFS,
        "gfs",
        MODEL_ID,
        BASIN_VERSION_ID,
        None,
        "published",
        ((RIVER_NETWORK_VERSION_ID, SEG_INSIDE), (RIVER_NETWORK_VERSION_ID, SEG_OUTSIDE)),
    ),
    # GFS, a newer run of ANOTHER basin with rows only on that basin's segment.
    (
        f"{ISSUE_126_PREFIX}_gfs_other_basin",
        GFS,
        "gfs",
        MODEL_ID,
        OTHER_BASIN_VERSION_ID,
        30,
        "published",
        ((OTHER_RIVER_NETWORK_VERSION_ID, OTHER_SEGMENT),),
    ),
    # IFS, an older published run with rows.
    (
        f"{ISSUE_126_PREFIX}_ifs_old",
        IFS,
        "ifs",
        MODEL_ID,
        BASIN_VERSION_ID,
        -12,
        "published",
        ((RIVER_NETWORK_VERSION_ID, SEG_INSIDE),),
    ),
    # IFS, the latest cycle is SUPERSEDED and still has rows: still selected.
    (
        f"{ISSUE_126_PREFIX}_ifs_superseded",
        IFS,
        "ifs",
        MODEL_ID,
        BASIN_VERSION_ID,
        6,
        "superseded",
        ((RIVER_NETWORK_VERSION_ID, SEG_INSIDE),),
    ),
    # IFS, a SECOND run on that same max cycle (duplicate max cycle), on MODEL_2.
    (
        f"{ISSUE_126_PREFIX}_ifs_duplicate_max",
        IFS,
        "ifs",
        MODEL_2,
        BASIN_VERSION_ID,
        6,
        "published",
        ((RIVER_NETWORK_VERSION_ID, SEG_INSIDE), (RIVER_NETWORK_VERSION_ID, SEG_OUTSIDE)),
    ),
    # IFS on MODEL_2, newest overall but no rows at all (e.g. still parsing).
    (f"{ISSUE_126_PREFIX}_ifs_model2_no_rows", IFS, "ifs", MODEL_2, BASIN_VERSION_ID, 24, "parsed", ()),
)


def _connect(database_url: str) -> Any:
    connection = psycopg2.connect(database_url, cursor_factory=RealDictCursor)
    connection.autocommit = True
    return connection


def _seed(database_url: str) -> None:
    connection = _connect(database_url)
    try:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                INSERT INTO met.data_source (source_id, source_name, source_type, status, native_format, adapter_name)
                VALUES ('ifs', 'IFS Integration', 'forecast', 'mock', 'grib2', 'ifs')
                ON CONFLICT (source_id) DO NOTHING
                """
            )
            cursor.execute(
                """
                INSERT INTO core.model_instance (
                    model_id, basin_version_id, river_network_version_id, mesh_version_id,
                    calibration_version_id, shud_code_version, model_package_uri,
                    active_flag, lifecycle_state
                )
                VALUES (%s, %s, %s, %s, 'calib-v2', 'shud-v1', 's3://nhms/models/it126/package-2/',
                        false, 'superseded')
                """,
                (MODEL_2, BASIN_VERSION_ID, RIVER_NETWORK_VERSION_ID, MESH_VERSION_ID),
            )
            cursor.execute(
                """
                INSERT INTO core.basin_version (
                    basin_version_id, basin_id, version_label, geom, active_flag, source_uri, checksum
                )
                VALUES (%s, %s, 'v-other', ST_Multi(ST_MakeEnvelope(109.0, 29.0, 112.0, 32.0, 4490)),
                        false, 'integration://other-basin', 'other-basin-sha')
                """,
                (OTHER_BASIN_VERSION_ID, BASIN_ID),
            )
            cursor.execute(
                """
                INSERT INTO core.river_network_version (
                    river_network_version_id, basin_version_id, version_label, segment_count, source_uri, checksum
                )
                VALUES (%s, %s, 'v-other', 1, 'integration://other-network', 'other-rnv-sha')
                """,
                (OTHER_RIVER_NETWORK_VERSION_ID, OTHER_BASIN_VERSION_ID),
            )
            cursor.execute(
                """
                INSERT INTO core.river_segment (
                    river_segment_id, river_network_version_id, segment_order, length_m, geom, properties_json
                )
                VALUES (%s, %s, 1, 900.0, ST_Multi(ST_GeomFromText('LINESTRING(110.0 30.0, 110.2 30.2)', 4490)),
                        '{}'::jsonb)
                """,
                (OTHER_SEGMENT, OTHER_RIVER_NETWORK_VERSION_ID),
            )
            execute_values(
                cursor,
                """
                INSERT INTO hydro.hydro_run (
                    run_id, run_type, scenario_id, model_id, basin_version_id, source_id,
                    cycle_time, start_time, end_time, status, run_manifest_uri
                )
                VALUES %s
                """,
                [
                    (
                        run_id,
                        "forecast",
                        scenario,
                        model,
                        basin,
                        source,
                        None if offset is None else _cycle(offset),
                        _cycle(offset or 0),
                        _cycle((offset or 0) + 168),
                        status,
                        f"s3://nhms/runs/{run_id}/manifest.json",
                    )
                    for run_id, scenario, source, model, basin, offset, status, _rows in RUNS
                ],
            )
            fact_rows = []
            for run_id, _scenario, _source, _model, basin, offset, _status, targets in RUNS:
                for network, segment in targets:
                    for lead in (1, 2):
                        fact_rows.append(
                            (
                                run_id,
                                basin,
                                network,
                                segment,
                                _cycle((offset or 0) + lead),
                                lead,
                                "q_down",
                                float(lead),
                                "m3/s",
                                "ok",
                            )
                        )
            insert_river_timeseries_dual_written(cursor, fact_rows)
    finally:
        connection.close()


def _both(
    database_url: str,
    *,
    basin_version_id: str = BASIN_VERSION_ID,
    segment_id: str = SEG_INSIDE,
    river_network_version_id: str = RIVER_NETWORK_VERSION_ID,
    scenarios: list[str],
    run_id: str | None = None,
    model_id: str | None = None,
) -> tuple[dict[str, datetime], dict[str, datetime]]:
    """(old, new) on ONE readonly snapshot, so no concurrent write can split them."""
    scenario_filter = forecast_store._scenario_filter(scenarios)
    identity_filter = forecast_store._run_identity_filter(run_id=run_id, model_id=model_id)
    identity = {
        "basin_version_id": basin_version_id,
        "segment_id": segment_id,
        "river_network_version_id": river_network_version_id,
        "scenario_filter": scenario_filter,
        "identity_filter": identity_filter,
    }
    connection = psycopg2.connect(database_url, cursor_factory=RealDictCursor)
    try:
        connection.set_session(isolation_level="REPEATABLE READ", readonly=True, autocommit=False)
        with connection.cursor() as cursor:
            old = pre_2424_latest_cycles(cursor, **identity)
            new = PsycopgForecastStore(database_url)._per_source_latest_cycles(cursor, **identity)
        connection.rollback()
    finally:
        connection.close()
    return old, new


#: (case, call, expected). Expected cycles are worked out from RUNS by hand.
CASES: tuple[tuple[str, dict[str, Any], dict[str, datetime]], ...] = (
    (
        "both_scenarios_inside_segment",
        {"scenarios": ["GFS", "IFS"]},
        {GFS: _cycle(12), IFS: _cycle(6)},
    ),
    ("no_scenario_filter_inside_segment", {"scenarios": []}, {GFS: _cycle(12), IFS: _cycle(6)}),
    ("gfs_failed_run_with_rows_wins", {"scenarios": ["GFS"]}, {GFS: _cycle(12)}),
    ("ifs_superseded_and_duplicate_max_cycle", {"scenarios": ["IFS"]}, {IFS: _cycle(6)}),
    (
        "outside_segment_newest_run_is_selected",
        {"scenarios": ["GFS", "IFS"], "segment_id": SEG_OUTSIDE},
        {GFS: _cycle(18), IFS: _cycle(6)},
    ),
    ("model_id_filter", {"scenarios": ["GFS", "IFS"], "model_id": MODEL_2}, {IFS: _cycle(6)}),
    (
        "model_id_filter_seed_model",
        {"scenarios": ["GFS", "IFS"], "model_id": MODEL_ID},
        {GFS: _cycle(12), IFS: _cycle(6)},
    ),
    ("run_id_filter", {"scenarios": ["IFS"], "run_id": f"{ISSUE_126_PREFIX}_ifs_old"}, {IFS: _cycle(-12)}),
    ("run_id_filter_seed_run", {"scenarios": ["GFS"], "run_id": FORECAST_RUN_ID}, {GFS: _cycle(0)}),
    (
        "run_id_filter_run_without_rows_on_segment",
        {"scenarios": ["GFS"], "run_id": f"{ISSUE_126_PREFIX}_gfs_newest_outside_only"},
        {},
    ),
    ("unknown_basin", {"scenarios": ["GFS", "IFS"], "basin_version_id": "it126_no_such_basin"}, {}),
    ("unknown_network", {"scenarios": ["GFS", "IFS"], "river_network_version_id": "it126_no_such_rnv"}, {}),
    ("unknown_segment", {"scenarios": ["GFS", "IFS"], "segment_id": "it126_no_such_segment"}, {}),
    (
        "other_basin_segment_under_its_own_basin",
        {
            "scenarios": ["GFS"],
            "basin_version_id": OTHER_BASIN_VERSION_ID,
            "river_network_version_id": OTHER_RIVER_NETWORK_VERSION_ID,
            "segment_id": OTHER_SEGMENT,
        },
        {GFS: _cycle(30)},
    ),
    (
        "other_basin_segment_under_the_wrong_basin",
        {
            "scenarios": ["GFS"],
            "river_network_version_id": OTHER_RIVER_NETWORK_VERSION_ID,
            "segment_id": OTHER_SEGMENT,
        },
        {},
    ),
)


def test_old_and_new_latest_cycle_discovery_select_the_same_cycles_on_every_seeded_case(
    throwaway_database_url: str,
) -> None:
    """tasks.md 2.1: every seeded case, old == new == the hand-worked cycles.

    One throwaway catalog for all cases (every case only READS the seed), and
    every failing case is listed in one message so one red case cannot hide
    another.
    """
    apply_migrations_from_zero(throwaway_database_url)
    seed_issue_126_data(throwaway_database_url)
    _seed(throwaway_database_url)

    failures = []
    for case, call, expected in CASES:
        old, new = _both(throwaway_database_url, **call)
        want = {scenario: cycle.astimezone(UTC) for scenario, cycle in expected.items()}
        if not (new == old == want):
            failures.append(f"{case}: old={old} new={new} expected={want}")
    assert not failures, "\n".join(failures)
    # Non-vacuity: most cases select something, so equality is not {} == {}.
    assert sum(1 for _case, _call, expected in CASES if expected) >= 9

    # End to end on the public method: `issue_time=latest` picks the D1 cycle.
    response = PsycopgForecastStore(throwaway_database_url).forecast_series(
        basin_version_id=BASIN_VERSION_ID,
        segment_id=SEG_INSIDE,
        river_network_version_id=RIVER_NETWORK_VERSION_ID,
        issue_time="latest",
        variables=["q_down"],
        scenarios=["GFS", "IFS"],
    )
    assert response["issue_time"] == "2026-05-03T12:00:00Z"
    assert {series["scenario_id"]: series["cycle_time"] for series in response["series"]} == {
        GFS: "2026-05-03T12:00:00Z",
        IFS: "2026-05-03T06:00:00Z",
    }
