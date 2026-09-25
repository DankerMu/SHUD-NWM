"""#2484 D4 on a real PostgreSQL: readonly DB discovery returns one self-consistent identity.

A throwaway catalog (every migration applied) is seeded with two basins, mixed-case
``source_id`` spellings (``gfs`` / ``IFS``), a newer ``running`` run next to older
display-ready runs, and pipeline jobs whose newest logged row overall belongs to a
DIFFERENT run than the one discovery must pick. The expected identities below are
worked out by hand from that seed.

Run on node-27's disposable database (never production):

    mkdir -p /home/nwm/tmp && export TMPDIR=/home/nwm/tmp
    NHMS_RUN_INTEGRATION=1 NHMS_INTEGRATION_DATABASE_URL=... \\
        uv run pytest -q tests/test_readonly_db_discovery_integration.py

SILENT-SKIP TRAP: without both variables every case below skips. A green run
that skipped is not evidence; read the collected/passed count.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import psycopg2
import pytest
from psycopg2.extras import Json

from services.production_closure.identity_matching import normalized_cycle_time_identity
from services.production_closure.readonly_db_probe_adapter import PsycopgReadonlyDbProbeAdapter
from tests.integration_helpers import apply_migrations_from_zero

pytestmark = pytest.mark.integration

PREFIX = "it2484"
BASIN_A = f"{PREFIX}_basin_a"
BASIN_B = f"{PREFIX}_basin_b"
T0 = datetime(2026, 9, 24, 0, tzinfo=UTC)

#: (run_id, source_id as stored, basin, model, cycle offset hours, status, updated_at offset hours)
RUNS = (
    # The newest-updated row overall is still running: never the unattended pick.
    (f"{PREFIX}_gfs_running", "gfs", BASIN_A, f"{PREFIX}_model_a", 24, "running", 9),
    # The newest display-ready row overall (IFS, stored upper-case).
    (f"{PREFIX}_ifs_published", "IFS", BASIN_A, f"{PREFIX}_model_a", 12, "published", 4),
    # The newest display-ready GFS row, on the second basin, stored lower-case.
    (f"{PREFIX}_gfs_parsed", "gfs", BASIN_B, f"{PREFIX}_model_b", 12, "parsed", 3),
    # An older ready GFS row with no logged job at all.
    (f"{PREFIX}_gfs_succeeded_no_job", "gfs", BASIN_B, f"{PREFIX}_model_b", 0, "succeeded", 2),
)

#: (job_id, run_id, log_uri, updated_at offset hours)
JOBS = (
    # The newest logged job overall belongs to the running run.
    (f"{PREFIX}_job_running", f"{PREFIX}_gfs_running", "s3://nhms/logs/running.log", 10),
    (f"{PREFIX}_job_ifs", f"{PREFIX}_ifs_published", "s3://nhms/logs/ifs.log", 4),
    (f"{PREFIX}_job_gfs_old", f"{PREFIX}_gfs_parsed", "s3://nhms/logs/gfs-old.log", 1),
    (f"{PREFIX}_job_gfs_new", f"{PREFIX}_gfs_parsed", "s3://nhms/logs/gfs-new.log", 2),
    # Newer than both logged GFS jobs but has no log: never the pick.
    (f"{PREFIX}_job_gfs_unlogged", f"{PREFIX}_gfs_parsed", None, 5),
)


def _hours(offset: int) -> datetime:
    return T0 + timedelta(hours=offset)


def _seed(database_url: str) -> None:
    connection = psycopg2.connect(database_url)
    connection.autocommit = True
    try:
        with connection.cursor() as cursor:
            for source_id, adapter in (("gfs", "gfs"), ("IFS", "ifs")):
                cursor.execute(
                    """
                    INSERT INTO met.data_source (
                        source_id, source_name, source_type, status, native_format, adapter_name
                    )
                    VALUES (%s, %s, 'forecast', 'mock', 'grib2', %s)
                    ON CONFLICT (source_id) DO NOTHING
                    """,
                    (source_id, f"{source_id} integration", adapter),
                )
            for basin, suffix in ((BASIN_A, "a"), (BASIN_B, "b")):
                basin_version = f"{basin}_v1"
                network = f"{PREFIX}_rnv_{suffix}"
                mesh = f"{PREFIX}_mesh_{suffix}"
                cursor.execute(
                    "INSERT INTO core.basin (basin_id, basin_name, basin_group, description) VALUES (%s, %s, %s, %s)",
                    (basin, f"Issue 2484 basin {suffix}", "integration", "Readonly discovery integration basin."),
                )
                cursor.execute(
                    """
                    INSERT INTO core.basin_version (
                        basin_version_id, basin_id, version_label, geom, active_flag, source_uri, checksum
                    )
                    VALUES (%s, %s, 'v1', ST_Multi(ST_MakeEnvelope(109.0, 29.0, 112.0, 32.0, 4490)),
                            true, 'integration://basin', 'basin-sha')
                    """,
                    (basin_version, basin),
                )
                cursor.execute(
                    """
                    INSERT INTO core.river_network_version (
                        river_network_version_id, basin_version_id, version_label, segment_count, source_uri, checksum
                    )
                    VALUES (%s, %s, 'v1', 0, 'integration://river-network', 'rnv-sha')
                    """,
                    (network, basin_version),
                )
                cursor.execute(
                    """
                    INSERT INTO core.mesh_version (
                        mesh_version_id, basin_version_id, version_label, mesh_uri, checksum, properties_json
                    )
                    VALUES (%s, %s, 'v1', 's3://nhms/models/it2484/mesh', 'mesh-sha', %s)
                    """,
                    (mesh, basin_version, Json({"cell_count": 1})),
                )
                cursor.execute(
                    """
                    INSERT INTO core.model_instance (
                        model_id, basin_version_id, river_network_version_id, mesh_version_id,
                        calibration_version_id, shud_code_version, model_package_uri,
                        active_flag, lifecycle_state, resource_profile
                    )
                    VALUES (%s, %s, %s, %s, 'calib-v1', 'shud-v1', 's3://nhms/models/it2484/package/',
                            true, 'active', %s)
                    """,
                    (f"{PREFIX}_model_{suffix}", basin_version, network, mesh, Json({"partition": "test"})),
                )
            for run_id, source_id, basin, model, cycle_offset, status, updated_offset in RUNS:
                cursor.execute(
                    """
                    INSERT INTO hydro.hydro_run (
                        run_id, run_type, scenario_id, model_id, basin_version_id, source_id,
                        cycle_time, start_time, end_time, status, run_manifest_uri, updated_at
                    )
                    VALUES (%s, 'forecast', 'forecast_deterministic', %s, %s, %s, %s, %s, %s, %s, %s, %s)
                    """,
                    (
                        run_id,
                        model,
                        f"{basin}_v1",
                        source_id,
                        _hours(cycle_offset),
                        _hours(cycle_offset),
                        _hours(cycle_offset + 168),
                        status,
                        f"s3://nhms/runs/{run_id}/manifest.json",
                        _hours(updated_offset),
                    ),
                )
            for job_id, run_id, log_uri, updated_offset in JOBS:
                cursor.execute(
                    """
                    INSERT INTO ops.pipeline_job (job_id, run_id, job_type, status, stage, log_uri, updated_at)
                    VALUES (%s, %s, 'shud_forecast', 'succeeded', 'forecast', %s, %s)
                    """,
                    (job_id, run_id, log_uri, _hours(updated_offset)),
                )
    finally:
        connection.close()


def _discover(database_url: str, **kwargs: Any) -> dict[str, Any]:
    adapter = PsycopgReadonlyDbProbeAdapter(database_url, ddl_suffix="it2484", connect_fn=psycopg2.connect)
    return adapter.discover_display_identity(**kwargs)


def _without_cycle(identity: dict[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in identity.items() if key != "cycle_time"}


def test_discovery_returns_one_self_consistent_identity_on_a_real_catalog(throwaway_database_url: str) -> None:
    apply_migrations_from_zero(throwaway_database_url)
    _seed(throwaway_database_url)

    unattended = _discover(throwaway_database_url)
    assert _without_cycle(unattended) == {
        "run_id": f"{PREFIX}_ifs_published",
        "source": "IFS",
        "model_id": f"{PREFIX}_model_a",
        "basin_id": BASIN_A,
        "job_id": f"{PREFIX}_job_ifs",
    }
    assert normalized_cycle_time_identity(unattended["cycle_time"]) == "2026092412"

    for configured_source in ("GFS", "gfs"):
        gfs = _discover(throwaway_database_url, source=configured_source)
        assert _without_cycle(gfs) == {
            "run_id": f"{PREFIX}_gfs_parsed",
            "source": "GFS",
            "model_id": f"{PREFIX}_model_b",
            "basin_id": BASIN_B,
            "job_id": f"{PREFIX}_job_gfs_new",
        }, configured_source

    running = _discover(throwaway_database_url, run_id=f"{PREFIX}_gfs_running")
    assert _without_cycle(running) == {
        "run_id": f"{PREFIX}_gfs_running",
        "source": "GFS",
        "model_id": f"{PREFIX}_model_a",
        "basin_id": BASIN_A,
        "job_id": f"{PREFIX}_job_running",
    }
    assert normalized_cycle_time_identity(running["cycle_time"]) == "2026092500"

    no_job = _discover(throwaway_database_url, run_id=f"{PREFIX}_gfs_succeeded_no_job")
    assert _without_cycle(no_job) == {
        "run_id": f"{PREFIX}_gfs_succeeded_no_job",
        "source": "GFS",
        "model_id": f"{PREFIX}_model_b",
        "basin_id": BASIN_B,
    }
