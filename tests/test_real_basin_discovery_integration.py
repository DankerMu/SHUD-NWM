from __future__ import annotations

import inspect
from datetime import UTC, datetime
from typing import Any

import pytest

import packages.common.model_registry as model_registry_module
from packages.common.model_registry import PsycopgModelRegistryStore
from tests.integration_helpers import apply_migrations_from_zero, psycopg_connection

pytestmark = pytest.mark.integration

# Scenario 3 of the multibasin-product-discovery spec: a freshly produced basin
# with a published (display-ready) run must surface in has_display_product
# discovery, while a basin whose only run is not display-ready must not. This is
# the real-SQL oracle for the EXISTS / `status = ANY(%s::hydro.run_status[])` / JOIN chain in
# PsycopgModelRegistryStore.list_basins; the unit suite only covers shallow mocks.
_PREFIX = "itbd"
_BASIN_READY = f"{_PREFIX}_basin_ready"
_BASIN_PENDING = f"{_PREFIX}_basin_pending"
# A brand-new basin id that appears NOWHERE in the discovery source code: it must
# still surface purely from data (registration + a display-ready run). This is the
# "zero-code-change extensibility" id used by the dedicated test below.
_BASIN_BRANDNEW = f"{_PREFIX}_basin_brandnew_2099"
# A basin whose only ready run is run_type='analysis' (not forecast): it must NOT
# surface, because discovery is aligned with the latest-product candidate query
# (forecast-only). Same for a forecast run with a NULL cycle_time.
_BASIN_ANALYSIS = f"{_PREFIX}_basin_analysis"
_BASIN_NO_CYCLE = f"{_PREFIX}_basin_no_cycle"
_RUN_START = datetime(2026, 5, 14, 0, tzinfo=UTC)
_RUN_END = datetime(2026, 5, 14, 1, tzinfo=UTC)
_CYCLE_TIME = datetime(2026, 5, 14, 0, tzinfo=UTC)


def _seed_basin(
    connection: Any,
    *,
    basin_id: str,
    basin_name: str,
    run_status: str,
    run_type: str = "forecast",
    cycle_time: datetime | None = _CYCLE_TIME,
) -> None:
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
                cycle_time, start_time, end_time, status, run_manifest_uri
            )
            VALUES (%s, %s, 'forecast_gfs_deterministic', %s, %s, %s, %s, %s, %s, 'integration://manifest.json')
            """,
            (run_id, run_type, model_id, basin_version_id, cycle_time, _RUN_START, _RUN_END, run_status),
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
        # parsed is in QHH_LATEST_READY_RUN_STATUSES => display-ready.
        _seed_basin(connection, basin_id=_BASIN_READY, basin_name="ZZ Discovery Ready", run_status="parsed")
        # running is a valid run_status but NOT display-ready.
        _seed_basin(connection, basin_id=_BASIN_PENDING, basin_name="ZZ Discovery Pending", run_status="running")

    store = PsycopgModelRegistryStore(integration_database_url)
    try:
        # Real SQL must not raise: EXISTS + `status = ANY(%s::hydro.run_status[])` on the enum
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


def test_list_basins_has_display_product_excludes_non_forecast_and_null_cycle(
    integration_database_url: str,
) -> None:
    """M-1 alignment oracle: discovery matches the latest-product candidate query
    on run_type and cycle_time.

    A basin whose only display-ready run is run_type='analysis', and a basin whose
    only display-ready run is a forecast with NULL cycle_time, must both be absent
    from has_display_product discovery — even though their run_status is otherwise
    display-ready. This is the real-SQL oracle for the EXISTS clause's
    `run_type = 'forecast' AND cycle_time IS NOT NULL` predicates.
    """
    apply_migrations_from_zero(integration_database_url)
    with psycopg_connection(integration_database_url) as connection:
        _clear(connection)
        # Ready status but run_type='analysis' => not a latest-product candidate.
        _seed_basin(
            connection,
            basin_id=_BASIN_ANALYSIS,
            basin_name="ZZ Discovery Analysis",
            run_status="parsed",
            run_type="analysis",
        )
        # Ready forecast but cycle_time IS NULL => not a latest-product candidate.
        _seed_basin(
            connection,
            basin_id=_BASIN_NO_CYCLE,
            basin_name="ZZ Discovery No Cycle",
            run_status="parsed",
            run_type="forecast",
            cycle_time=None,
        )

    store = PsycopgModelRegistryStore(integration_database_url)
    try:
        display_only = {row["basin_id"] for row in store.list_basins(limit=100, offset=0, has_display_product=True)}
        assert _BASIN_ANALYSIS not in display_only
        assert _BASIN_NO_CYCLE not in display_only

        # Both are still registered basins, so the default listing includes them.
        all_basins = {row["basin_id"] for row in store.list_basins(limit=100, offset=0)}
        assert {_BASIN_ANALYSIS, _BASIN_NO_CYCLE} <= all_basins
    finally:
        with psycopg_connection(integration_database_url) as connection:
            _clear(connection)


def test_brandnew_basin_surfaces_without_any_code_change(integration_database_url: str) -> None:
    """§8.1 zero-code-change extensibility (multibasin-product-discovery Scenario 3).

    A basin id that is not referenced in the discovery module source still surfaces
    in has_display_product discovery purely from data (registration + one
    display-ready forecast run). The source-text check below is only weak evidence
    (absence of a literal); the live-DB assertion at the end of this test is the
    real oracle that discovery is data-driven, not whitelist-driven.
    """
    # Weak evidence: this brand-new id is not a literal in the discovery module.
    discovery_source = inspect.getsource(model_registry_module)
    assert _BASIN_BRANDNEW not in discovery_source
    # Discovery filters on a run-status set, not a basin enum.
    assert _BASIN_BRANDNEW not in model_registry_module.QHH_LATEST_READY_RUN_STATUSES
    # A known production basin id is likewise NOT a literal in the discovery module:
    # discovery cannot be relying on a hardcoded basin whitelist for it either.
    assert "basins_qhh" not in discovery_source

    apply_migrations_from_zero(integration_database_url)
    with psycopg_connection(integration_database_url) as connection:
        _clear(connection)
        _seed_basin(
            connection,
            basin_id=_BASIN_BRANDNEW,
            basin_name="ZZ Brand New Basin 2099",
            run_status="published",
        )

    store = PsycopgModelRegistryStore(integration_database_url)
    try:
        display_only = {
            row["basin_id"] for row in store.list_basins(limit=100, offset=0, has_display_product=True)
        }
        # Surfaced by data alone — no frontend/backend code change required.
        assert _BASIN_BRANDNEW in display_only
    finally:
        with psycopg_connection(integration_database_url) as connection:
            _clear(connection)
