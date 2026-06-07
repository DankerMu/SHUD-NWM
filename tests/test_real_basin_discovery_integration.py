from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest

from packages.common.model_registry import PsycopgModelRegistryStore
from tests.integration_helpers import apply_migrations_from_zero, psycopg_connection

pytestmark = pytest.mark.integration

# Scenario 3 of the multibasin-product-discovery spec: a freshly produced basin
# with a published (display-ready) run must surface in has_display_product
# discovery, while a basin whose only run is not display-ready must not. This is
# the real-SQL oracle for the EXISTS / `status::text = ANY(%s)` / JOIN chain in
# PsycopgModelRegistryStore.list_basins; the unit suite only covers shallow mocks.
_PREFIX = "itbd"
_BASIN_READY = f"{_PREFIX}_basin_ready"
_BASIN_PENDING = f"{_PREFIX}_basin_pending"
_RUN_START = datetime(2026, 5, 14, 0, tzinfo=UTC)
_RUN_END = datetime(2026, 5, 14, 1, tzinfo=UTC)


def _seed_basin(connection: Any, *, basin_id: str, basin_name: str, run_status: str) -> None:
    suffix = basin_id
    basin_version_id = f"{suffix}_v1"
    river_network_version_id = f"{suffix}_rnv"
    mesh_version_id = f"{suffix}_mesh"
    model_id = f"{suffix}_model"
    run_id = f"{suffix}_run"
    with connection.cursor() as cursor:
        cursor.execute(
            "INSERT INTO core.basin (basin_id, basin_name, basin_group, description) VALUES (%s, %s, %s, %s)",
            (basin_id, basin_name, "discovery", "basin discovery integration"),
        )
        cursor.execute(
            """
            INSERT INTO core.basin_version (
                basin_version_id, basin_id, version_label, geom, active_flag, source_uri, checksum
            )
            VALUES (
                %s, %s, 'v1', ST_Multi(ST_MakeEnvelope(109.0, 29.0, 112.0, 32.0, 4490)),
                true, 'integration://basin', 'basin-sha'
            )
            """,
            (basin_version_id, basin_id),
        )
        cursor.execute(
            """
            INSERT INTO core.river_network_version (
                river_network_version_id, basin_version_id, version_label, segment_count, source_uri, checksum
            )
            VALUES (%s, %s, 'v1', 0, 'integration://rnv', 'rnv-sha')
            """,
            (river_network_version_id, basin_version_id),
        )
        cursor.execute(
            """
            INSERT INTO core.mesh_version (
                mesh_version_id, basin_version_id, version_label, mesh_uri, checksum
            )
            VALUES (%s, %s, 'v1', 'integration://mesh', 'mesh-sha')
            """,
            (mesh_version_id, basin_version_id),
        )
        cursor.execute(
            """
            INSERT INTO core.model_instance (
                model_id, basin_version_id, river_network_version_id, mesh_version_id,
                calibration_version_id, shud_code_version, model_package_uri, active_flag, lifecycle_state
            )
            VALUES (%s, %s, %s, %s, 'calib-v1', 'shud-v1', 'integration://package/', true, 'active')
            """,
            (model_id, basin_version_id, river_network_version_id, mesh_version_id),
        )
        cursor.execute(
            """
            INSERT INTO hydro.hydro_run (
                run_id, run_type, scenario_id, model_id, basin_version_id,
                start_time, end_time, status, run_manifest_uri
            )
            VALUES (%s, 'forecast', 'forecast_gfs_deterministic', %s, %s, %s, %s, %s, 'integration://manifest.json')
            """,
            (run_id, model_id, basin_version_id, _RUN_START, _RUN_END, run_status),
        )


def _clear(connection: Any) -> None:
    with connection.cursor() as cursor:
        cursor.execute("DELETE FROM hydro.hydro_run WHERE run_id LIKE %s", (f"{_PREFIX}%",))
        cursor.execute("DELETE FROM core.model_instance WHERE model_id LIKE %s", (f"{_PREFIX}%",))
        cursor.execute("DELETE FROM core.mesh_version WHERE mesh_version_id LIKE %s", (f"{_PREFIX}%",))
        cursor.execute(
            "DELETE FROM core.river_network_version WHERE river_network_version_id LIKE %s",
            (f"{_PREFIX}%",),
        )
        cursor.execute("DELETE FROM core.basin_version WHERE basin_version_id LIKE %s", (f"{_PREFIX}%",))
        cursor.execute("DELETE FROM core.basin WHERE basin_id LIKE %s", (f"{_PREFIX}%",))


def test_list_basins_has_display_product_reflects_real_run_status(integration_database_url: str) -> None:
    apply_migrations_from_zero(integration_database_url)
    with psycopg_connection(integration_database_url) as connection:
        _clear(connection)
        # frequency_done is in QHH_LATEST_READY_RUN_STATUSES => display-ready.
        _seed_basin(connection, basin_id=_BASIN_READY, basin_name="ZZ Discovery Ready", run_status="frequency_done")
        # running is a valid run_status but NOT display-ready.
        _seed_basin(connection, basin_id=_BASIN_PENDING, basin_name="ZZ Discovery Pending", run_status="running")

    store = PsycopgModelRegistryStore(integration_database_url)
    try:
        # Real SQL must not raise: EXISTS + `status::text = ANY(%s)` on the enum
        # column + JOIN basin_version->hydro_run column names all resolve.
        display_only = {row["basin_id"] for row in store.list_basins(limit=100, offset=0, has_display_product=True)}
        assert _BASIN_READY in display_only
        assert _BASIN_PENDING not in display_only

        # Backward-compatible default lists every registered basin.
        all_basins = {row["basin_id"] for row in store.list_basins(limit=100, offset=0)}
        assert {_BASIN_READY, _BASIN_PENDING} <= all_basins
    finally:
        with psycopg_connection(integration_database_url) as connection:
            _clear(connection)
