"""QHH production bootstrap: scheduler-facing integration corpus (issue #1948 partition C).

This is the partition's only `@pytest.mark.integration` owner, and it imports the
helper-owned `qhh_scheduler_canonical_readiness` fixture at module scope.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
from psycopg2.extras import Json

from packages.common.model_registry import PsycopgModelRegistryStore
from services.orchestrator.scheduler import ProductionScheduler, ProductionSchedulerConfig
from tests.integration_helpers import apply_migrations_from_zero, psycopg_connection
from tests.qhh_production_bootstrap_helpers import (
    _QHH_SCHEDULER_FOUR_CYCLE_HOURS,
    _QHH_SCHEDULER_SOURCE_ID,
    _QHH_SCHEDULER_SOURCE_VERSION,
    _clear_qhh_scheduler_canonical_readiness,
    _qhh_registry_fixture,
    _qhh_scheduler_ready_gfs_adapter,
    qhh_scheduler_canonical_readiness,
)
from tests.test_production_scheduler import FakeAdapter, _dt
from workers.model_registry.qhh_production_bootstrap import (
    QhhProductionBootstrapError,
    bootstrap_qhh_production,
)

_QHH_SCHEDULER_REQUESTED_FIXTURES = (qhh_scheduler_canonical_readiness,)  # requested by argument name


@pytest.mark.integration
def test_bootstrap_qhh_production_success_idempotent_and_scheduler_ready(
    tmp_path: Path,
    integration_database_url: str,
    qhh_scheduler_canonical_readiness: Callable[[str], Any],
) -> None:
    apply_migrations_from_zero(integration_database_url)
    basin_slug = "qhh-success"
    root, _input_dir, inventory_path, manifest_path, model_id = _qhh_registry_fixture(tmp_path, basin_slug=basin_slug)
    evidence_dir = tmp_path / "evidence"
    evidence_dir.mkdir()

    first = bootstrap_qhh_production(
        database_url=integration_database_url,
        basins_root=root,
        qhh_basin_slug=basin_slug,
        model_id=model_id,
        inventory_path=inventory_path,
        package_manifest_path=manifest_path,
        evidence_dir=evidence_dir,
        evidence_path=evidence_dir / "first.json",
    )
    second = bootstrap_qhh_production(
        database_url=integration_database_url,
        basins_root=root,
        qhh_basin_slug=basin_slug,
        model_id=model_id,
        inventory_path=inventory_path,
        package_manifest_path=manifest_path,
    )

    assert first["active"] is True
    assert first["scheduler_readiness"]["ready"] is True
    assert first["station_row_counts"] == {"created": 2, "updated": 0, "unchanged": 0}
    assert first["output_segment_row_counts"] == {"created": 2, "updated": 0, "unchanged": 0}
    assert first["evidence_write_omitted"] is False
    assert first["output_segment_count"] == 2
    assert first["package_identity"]["manifest_uri"] == json.loads(manifest_path.read_text(encoding="utf-8"))[
        "manifest_uri"
    ]
    assert first["package_identity"]["model_package_uri"] == first["model_package_uri"]
    assert first["package_identity"]["package_checksum"]
    assert first["package_identity"]["manifest_sha256"]
    assert first["source_files"]["qhh_tsd_forc"]["station_count"] == 2
    assert first["source_files"]["qhh_sp_riv"]["output_segment_count"] == 2
    assert first["non_goal_proof"]["forcing_version_rows_created"] == 0
    assert first["non_goal_proof"]["forcing_station_timeseries_rows_created"] == 0
    assert first["non_goal_proof"] == {
        "forcing_version_rows_created": 0,
        "forcing_station_timeseries_rows_created": 0,
        "shud_runtime_executed": False,
        "slurm_submitted": False,
        "published_display_artifacts": False,
    }
    assert second["station_row_counts"] == {"created": 0, "updated": 0, "unchanged": 2}
    assert second["output_segment_row_counts"] == {"created": 0, "updated": 0, "unchanged": 2}
    assert second["package_identity"] == first["package_identity"]
    assert json.loads((evidence_dir / "first.json").read_text(encoding="utf-8"))["model_id"] == model_id

    with psycopg_connection(integration_database_url) as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT mi.active_flag,
                       mi.lifecycle_state,
                       mi.shud_code_version,
                       mi.model_package_uri,
                       mi.resource_profile,
                       bv.basin_id
                FROM core.model_instance mi
                JOIN core.basin_version bv
                  ON bv.basin_version_id = mi.basin_version_id
                WHERE mi.model_id = %s
                """,
                (model_id,),
            )
            model = cursor.fetchone()
            cursor.execute(
                """
                SELECT COUNT(*) AS count
                FROM met.met_station
                WHERE properties_json->>'model_id' = %s
                  AND station_role = 'forcing_grid'
                """,
                (model_id,),
            )
            station_count = int(cursor.fetchone()["count"])
            cursor.execute(
                """
                SELECT COUNT(*) AS count
                FROM core.river_segment
                WHERE river_network_version_id = %s
                  AND COALESCE(properties_json->>'shud_output_river', 'false') = 'true'
                """,
                (first["river_network_version_id"],),
            )
            output_count = int(cursor.fetchone()["count"])
            cursor.execute("SELECT COUNT(*) AS count FROM met.forcing_version WHERE model_id = %s", (model_id,))
            forcing_count = int(cursor.fetchone()["count"])

    assert model["active_flag"] is True
    assert model["lifecycle_state"] == "active"
    assert model["shud_code_version"] == "basins-shud"
    assert model["resource_profile"]["runnable"] is True
    assert model["resource_profile"]["project_name"] == "qhh"
    assert model["resource_profile"]["shud_input_name"] == "qhh"
    assert model["resource_profile"]["model_id"] == model_id
    assert model["resource_profile"]["model_package_uri"] == first["model_package_uri"]
    assert model["resource_profile"]["package_checksum"] == first["package_identity"]["package_checksum"]
    assert model["resource_profile"]["source_inventory_checksum"] == first["package_identity"][
        "source_inventory_checksum"
    ]
    assert model["resource_profile"]["station_count"] == 2
    assert model["resource_profile"]["output_segment_count"] == 2
    assert model["resource_profile"]["qhh_tsd_forc_sha256"] == first["source_files"]["qhh_tsd_forc"]["sha256"]
    assert model["resource_profile"]["qhh_sp_riv_sha256"] == first["source_files"]["qhh_sp_riv"]["sha256"]
    assert "forcing_uri" not in model["resource_profile"]
    assert "forecast_cycle" not in model["resource_profile"]
    assert "publish_uri" not in model["resource_profile"]
    assert station_count == 2
    assert output_count == 2
    assert forcing_count == 0

    readiness_provider = qhh_scheduler_canonical_readiness(model_id)
    scheduler = ProductionScheduler(
        ProductionSchedulerConfig(
            workspace_root=tmp_path / "workspace",
            model_ids=(model_id,),
            now=_dt("2026-05-21T12:00:00Z"),
            allowed_cycle_hours_utc=_QHH_SCHEDULER_FOUR_CYCLE_HOURS,
        ),
        registry=PsycopgModelRegistryStore(integration_database_url),
        adapters={"gfs": _qhh_scheduler_ready_gfs_adapter()},
        canonical_readiness_provider=readiness_provider,
    )
    result = scheduler.run_once()
    reasons = {item["reason"] for item in result.evidence["model_discovery"]["exclusions"]}
    scheduler_candidate = next(item for item in result.evidence["candidates"] if item["model_id"] == model_id)
    assert scheduler_candidate["output_segment_count"] == 2
    assert scheduler_candidate["resource_profile"]["output_segment_count"] == 2
    assert scheduler_candidate["resource_profile"]["project_name"] == "qhh"
    assert not {"not_shud_model", "not_runnable", "incomplete_model_metadata"} & reasons


@pytest.mark.integration
def test_qhh_scheduler_canonical_readiness_preserves_existing_gfs_data_source(
    integration_database_url: str,
    qhh_scheduler_canonical_readiness: Callable[[str], Any],
) -> None:
    apply_migrations_from_zero(integration_database_url)
    model_id = "basins_qhh_shud_existing_gfs"
    existing_config = {"owner": "shared-integration", "qhh_scheduler_readiness_fixture": False}
    with psycopg_connection(integration_database_url) as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                INSERT INTO met.data_source (
                    source_id, source_name, source_type, status, native_format, adapter_name, config_json
                )
                VALUES (%s, 'Shared GFS source', 'forecast', 'enabled', 'grib2', 'shared-gfs', %s)
                ON CONFLICT (source_id) DO UPDATE SET
                    source_name = EXCLUDED.source_name,
                    source_type = EXCLUDED.source_type,
                    status = EXCLUDED.status,
                    native_format = EXCLUDED.native_format,
                    adapter_name = EXCLUDED.adapter_name,
                    config_json = EXCLUDED.config_json
                """,
                (_QHH_SCHEDULER_SOURCE_ID, Json(existing_config)),
            )

    qhh_scheduler_canonical_readiness(model_id)

    with psycopg_connection(integration_database_url) as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT COUNT(*) AS count
                FROM met.canonical_met_product
                WHERE source_id = %s
                  AND source_version = %s
                """,
                (_QHH_SCHEDULER_SOURCE_ID, _QHH_SCHEDULER_SOURCE_VERSION),
            )
            seeded_canonical_count = int(cursor.fetchone()["count"])
            cursor.execute(
                """
                SELECT source_name, source_type, status, native_format, adapter_name, config_json
                FROM met.data_source
                WHERE source_id = %s
                """,
                (_QHH_SCHEDULER_SOURCE_ID,),
            )
            data_source = cursor.fetchone()

    assert seeded_canonical_count > 0
    assert data_source == {
        "source_name": "Shared GFS source",
        "source_type": "forecast",
        "status": "enabled",
        "native_format": "grib2",
        "adapter_name": "shared-gfs",
        "config_json": existing_config,
    }
    _clear_qhh_scheduler_canonical_readiness(integration_database_url)
    with psycopg_connection(integration_database_url) as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT COUNT(*) AS count
                FROM met.canonical_met_product
                WHERE source_id = %s
                  AND source_version = %s
                """,
                (_QHH_SCHEDULER_SOURCE_ID, _QHH_SCHEDULER_SOURCE_VERSION),
            )
            cleaned_canonical_count = int(cursor.fetchone()["count"])
            cursor.execute(
                """
                SELECT source_name, source_type, status, native_format, adapter_name, config_json
                FROM met.data_source
                WHERE source_id = %s
                """,
                (_QHH_SCHEDULER_SOURCE_ID,),
            )
            restored_data_source = cursor.fetchone()

    assert cleaned_canonical_count == 0
    assert restored_data_source == data_source


@pytest.mark.integration
def test_bootstrap_station_failure_rolls_back_model_readiness(
    tmp_path: Path,
    integration_database_url: str,
) -> None:
    apply_migrations_from_zero(integration_database_url)
    basin_slug = "qhh-rollback-model"
    root, _input_dir, inventory_path, manifest_path, model_id = _qhh_registry_fixture(tmp_path, basin_slug=basin_slug)

    with pytest.raises(QhhProductionBootstrapError) as exc_info:
        bootstrap_qhh_production(
            database_url=integration_database_url,
            basins_root=root,
            qhh_basin_slug=basin_slug,
            model_id=model_id,
            inventory_path=inventory_path,
            package_manifest_path=manifest_path,
            fail_after_model_metadata=True,
        )

    assert exc_info.value.error_code == "QHH_BOOTSTRAP_PARTIAL_BOOTSTRAP_ROLLBACK"
    with psycopg_connection(integration_database_url) as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT COUNT(*) AS count FROM core.model_instance WHERE model_id = %s AND active_flag = true",
                (model_id,),
            )
            active_count = int(cursor.fetchone()["count"])
            cursor.execute(
                "SELECT COUNT(*) AS count FROM met.met_station WHERE properties_json->>'model_id' = %s",
                (model_id,),
            )
            station_count = int(cursor.fetchone()["count"])
    assert active_count == 0
    assert station_count == 0


@pytest.mark.integration
@pytest.mark.parametrize(
    ("failure_flag", "failure_point"),
    [
        ("fail_during_station_seed", "station_seed"),
        ("fail_during_output_segment_seed", "output_segment_seed"),
    ],
)
def test_bootstrap_seed_failures_roll_back_rows_and_scheduler_visibility(
    tmp_path: Path,
    integration_database_url: str,
    failure_flag: str,
    failure_point: str,
) -> None:
    apply_migrations_from_zero(integration_database_url)
    basin_slug = f"qhh-rollback-{failure_point.replace('_', '-')}"
    root, _input_dir, inventory_path, manifest_path, model_id = _qhh_registry_fixture(tmp_path, basin_slug=basin_slug)

    with pytest.raises(QhhProductionBootstrapError) as exc_info:
        bootstrap_qhh_production(
            database_url=integration_database_url,
            basins_root=root,
            qhh_basin_slug=basin_slug,
            model_id=model_id,
            inventory_path=inventory_path,
            package_manifest_path=manifest_path,
            **{failure_flag: True},
        )

    assert exc_info.value.error_code == "QHH_BOOTSTRAP_PARTIAL_BOOTSTRAP_ROLLBACK"
    assert exc_info.value.details["failure_point"] == failure_point
    with psycopg_connection(integration_database_url) as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT COUNT(*) AS count
                FROM core.model_instance
                WHERE model_id = %s
                  AND active_flag = true
                  AND COALESCE(lifecycle_state, 'active') = 'active'
                """,
                (model_id,),
            )
            active_count = int(cursor.fetchone()["count"])
            cursor.execute(
                "SELECT COUNT(*) AS count FROM met.met_station WHERE properties_json->>'model_id' = %s",
                (model_id,),
            )
            station_count = int(cursor.fetchone()["count"])
            cursor.execute(
                """
                SELECT COUNT(*) AS count
                FROM core.river_segment
                WHERE COALESCE(properties_json->>'shud_output_river', 'false') = 'true'
                  AND properties_json->>'model_id' = %s
                """,
                (model_id,),
            )
            output_count = int(cursor.fetchone()["count"])

    scheduler = ProductionScheduler(
        ProductionSchedulerConfig(
            workspace_root=tmp_path / f"workspace-{failure_point}",
            model_ids=(model_id,),
            now=_dt("2026-05-21T12:00:00Z"),
            allowed_cycle_hours_utc=_QHH_SCHEDULER_FOUR_CYCLE_HOURS,
        ),
        registry=PsycopgModelRegistryStore(integration_database_url),
        adapters={"gfs": FakeAdapter("gfs", [("2026-05-21T06:00:00Z", True)])},
    )
    result = scheduler.run_once()

    assert active_count == 0
    assert station_count == 0
    assert output_count == 0
    assert model_id not in {item["model_id"] for item in result.evidence["candidates"]}


@pytest.mark.integration
@pytest.mark.parametrize(
    ("stale_kind", "error_code"),
    [
        ("station", "QHH_BOOTSTRAP_STALE_STATION_IDENTITY"),
        ("output", "QHH_BOOTSTRAP_STALE_OUTPUT_SEGMENT_IDENTITY"),
    ],
)
def test_bootstrap_blocks_stale_qhh_sibling_rows_before_scheduler_visibility(
    tmp_path: Path,
    integration_database_url: str,
    stale_kind: str,
    error_code: str,
) -> None:
    apply_migrations_from_zero(integration_database_url)
    basin_slug = f"qhh-stale-{stale_kind}"
    root, _input_dir, inventory_path, manifest_path, model_id = _qhh_registry_fixture(tmp_path, basin_slug=basin_slug)
    first = bootstrap_qhh_production(
        database_url=integration_database_url,
        basins_root=root,
        qhh_basin_slug=basin_slug,
        model_id=model_id,
        inventory_path=inventory_path,
        package_manifest_path=manifest_path,
    )
    with psycopg_connection(integration_database_url) as connection:
        with connection.cursor() as cursor:
            if stale_kind == "station":
                cursor.execute(
                    """
                    INSERT INTO met.met_station (
                        station_id,
                        basin_version_id,
                        station_name,
                        geom,
                        elevation_m,
                        station_role,
                        active_flag,
                        properties_json
                    )
                    VALUES (
                        'qhh_forc_999',
                        %s,
                        'Stale QHH forcing station',
                        ST_SetSRID(ST_MakePoint(100.9, 30.9), 4490),
                        0,
                        'forcing_grid',
                        true,
                        %s
                    )
                    """,
                    (
                        first["basin_version_id"],
                        Json(
                            {
                                "seed": "qhh_production_bootstrap",
                                "model_id": model_id,
                                "basin_version_id": first["basin_version_id"],
                                "source": "qhh.tsd.forc",
                            }
                        ),
                    ),
                )
            else:
                cursor.execute(
                    """
                    INSERT INTO core.river_segment (
                        river_segment_id,
                        river_network_version_id,
                        segment_order,
                        properties_json
                    )
                    VALUES (
                        %s,
                        %s,
                        999,
                        %s
                    )
                    """,
                    (
                        f"{model_id}_shud_riv_999999",
                        first["river_network_version_id"],
                        Json(
                            {
                                "seed": "qhh_production_bootstrap",
                                "model_id": model_id,
                                "basin_version_id": first["basin_version_id"],
                                "shud_output_river": True,
                            }
                        ),
                    ),
                )

    with pytest.raises(QhhProductionBootstrapError) as exc_info:
        bootstrap_qhh_production(
            database_url=integration_database_url,
            basins_root=root,
            qhh_basin_slug=basin_slug,
            model_id=model_id,
            inventory_path=inventory_path,
            package_manifest_path=manifest_path,
        )

    assert exc_info.value.error_code == error_code
    assert exc_info.value.details["persistent_scheduler_visibility_removed"] is True
    scheduler = ProductionScheduler(
        ProductionSchedulerConfig(
            workspace_root=tmp_path / f"workspace-{stale_kind}",
            model_ids=(model_id,),
            now=_dt("2026-05-21T12:00:00Z"),
            allowed_cycle_hours_utc=_QHH_SCHEDULER_FOUR_CYCLE_HOURS,
        ),
        registry=PsycopgModelRegistryStore(integration_database_url),
        adapters={"gfs": FakeAdapter("gfs", [("2026-05-21T06:00:00Z", True)])},
    )
    result = scheduler.run_once()
    with psycopg_connection(integration_database_url) as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT active_flag,
                       COALESCE(lifecycle_state, CASE WHEN active_flag THEN 'active' ELSE 'inactive' END)
                         AS lifecycle_state
                FROM core.model_instance
                WHERE model_id = %s
                """,
                (model_id,),
            )
            model = cursor.fetchone()
            cursor.execute("SELECT COUNT(*) AS count FROM met.forcing_version WHERE model_id = %s", (model_id,))
            forcing_count = int(cursor.fetchone()["count"])

    assert model["active_flag"] is False
    assert model["lifecycle_state"] == "inactive"
    assert forcing_count == 0
    assert model_id not in {item["model_id"] for item in result.evidence["candidates"]}


@pytest.mark.integration
def test_bootstrap_blocks_unmarked_same_basin_active_forcing_grid_and_removes_scheduler_visibility(
    tmp_path: Path,
    integration_database_url: str,
) -> None:
    apply_migrations_from_zero(integration_database_url)
    basin_slug = "qhh-unmarked-station"
    root, _input_dir, inventory_path, manifest_path, model_id = _qhh_registry_fixture(tmp_path, basin_slug=basin_slug)
    first = bootstrap_qhh_production(
        database_url=integration_database_url,
        basins_root=root,
        qhh_basin_slug=basin_slug,
        model_id=model_id,
        inventory_path=inventory_path,
        package_manifest_path=manifest_path,
    )
    with psycopg_connection(integration_database_url) as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                INSERT INTO met.met_station (
                    station_id,
                    basin_version_id,
                    station_name,
                    geom,
                    elevation_m,
                    station_role,
                    active_flag,
                    properties_json
                )
                VALUES (
                    'unmarked_same_basin_forcing',
                    %s,
                    'Unmarked same-basin forcing grid',
                    ST_SetSRID(ST_MakePoint(100.95, 30.95), 4490),
                    0,
                    'forcing_grid',
                    true,
                    %s
                )
                """,
                (first["basin_version_id"], Json({})),
            )

    with pytest.raises(QhhProductionBootstrapError) as exc_info:
        bootstrap_qhh_production(
            database_url=integration_database_url,
            basins_root=root,
            qhh_basin_slug=basin_slug,
            model_id=model_id,
            inventory_path=inventory_path,
            package_manifest_path=manifest_path,
        )

    assert exc_info.value.error_code == "QHH_BOOTSTRAP_STALE_STATION_IDENTITY"
    assert exc_info.value.details["extra_station_ids"] == ["unmarked_same_basin_forcing"]
    assert exc_info.value.details["persistent_scheduler_visibility_removed"] is True

    scheduler = ProductionScheduler(
        ProductionSchedulerConfig(
            workspace_root=tmp_path / "workspace-unmarked-station",
            model_ids=(model_id,),
            now=_dt("2026-05-21T12:00:00Z"),
            allowed_cycle_hours_utc=_QHH_SCHEDULER_FOUR_CYCLE_HOURS,
        ),
        registry=PsycopgModelRegistryStore(integration_database_url),
        adapters={"gfs": FakeAdapter("gfs", [("2026-05-21T06:00:00Z", True)])},
    )
    result = scheduler.run_once()
    with psycopg_connection(integration_database_url) as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT active_flag,
                       COALESCE(lifecycle_state, CASE WHEN active_flag THEN 'active' ELSE 'inactive' END)
                         AS lifecycle_state,
                       resource_profile
                FROM core.model_instance
                WHERE model_id = %s
                """,
                (model_id,),
            )
            model = cursor.fetchone()

    assert model["active_flag"] is False
    assert model["lifecycle_state"] == "inactive"
    assert model["resource_profile"]["runnable"] is False
    assert model["resource_profile"]["qhh_scheduler_blocker"]["code"] == "QHH_BOOTSTRAP_STALE_STATION_IDENTITY"
    assert model_id not in {item["model_id"] for item in result.evidence["candidates"]}


@pytest.mark.integration
def test_bootstrap_replaces_stale_run_scoped_resource_profile_and_scheduler_derives_current_identity(
    tmp_path: Path,
    integration_database_url: str,
    qhh_scheduler_canonical_readiness: Callable[[str], Any],
) -> None:
    apply_migrations_from_zero(integration_database_url)
    basin_slug = "qhh-stale-profile"
    root, _input_dir, inventory_path, manifest_path, model_id = _qhh_registry_fixture(tmp_path, basin_slug=basin_slug)
    first = bootstrap_qhh_production(
        database_url=integration_database_url,
        basins_root=root,
        qhh_basin_slug=basin_slug,
        model_id=model_id,
        inventory_path=inventory_path,
        package_manifest_path=manifest_path,
    )
    with psycopg_connection(integration_database_url) as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                UPDATE core.model_instance
                SET active_flag = false,
                    lifecycle_state = 'inactive',
                    resource_profile = resource_profile || %s
                WHERE model_id = %s
                """,
                (
                    Json(
                        {
                            "canonical_product_id": "stale-canon",
                            "published_manifest_id": "stale-manifest",
                            "pipeline_job_id": "stale-job",
                            "output_uri": "s3://nhms/runs/stale/output/",
                            "partition": "debug",
                        }
                    ),
                    model_id,
                ),
            )

    second = bootstrap_qhh_production(
        database_url=integration_database_url,
        basins_root=root,
        qhh_basin_slug=basin_slug,
        model_id=model_id,
        inventory_path=inventory_path,
        package_manifest_path=manifest_path,
    )

    readiness_provider = qhh_scheduler_canonical_readiness(model_id)
    scheduler = ProductionScheduler(
        ProductionSchedulerConfig(
            workspace_root=tmp_path / "workspace-stale-profile",
            model_ids=(model_id,),
            now=_dt("2026-05-21T12:00:00Z"),
            allowed_cycle_hours_utc=_QHH_SCHEDULER_FOUR_CYCLE_HOURS,
        ),
        registry=PsycopgModelRegistryStore(integration_database_url),
        adapters={"gfs": _qhh_scheduler_ready_gfs_adapter()},
        canonical_readiness_provider=readiness_provider,
    )
    result = scheduler.run_once()
    candidate = next(item for item in result.evidence["candidates"] if item["model_id"] == model_id)
    with psycopg_connection(integration_database_url) as connection:
        with connection.cursor() as cursor:
            cursor.execute("SELECT resource_profile FROM core.model_instance WHERE model_id = %s", (model_id,))
            profile = cursor.fetchone()["resource_profile"]

    assert second["package_identity"] == first["package_identity"]
    assert not {"canonical_product_id", "published_manifest_id", "pipeline_job_id", "output_uri"} & set(profile)
    assert profile["partition"] == "standard"
    assert candidate["canonical_product_id"] == "canon_gfs_2026052106"
    assert candidate["published_manifest_id"] == f"manifest_fcst_gfs_2026052106_{model_id}"
    assert "pipeline_job_id" not in candidate["production_identity_contract"]["identity"]


@pytest.mark.integration
def test_bootstrap_duplicate_active_model_blocks_before_station_writes(
    tmp_path: Path,
    integration_database_url: str,
) -> None:
    apply_migrations_from_zero(integration_database_url)
    basin_slug = "qhh-duplicate-active"
    root, _input_dir, inventory_path, manifest_path, model_id = _qhh_registry_fixture(tmp_path, basin_slug=basin_slug)
    bootstrap_qhh_production(
        database_url=integration_database_url,
        basins_root=root,
        qhh_basin_slug=basin_slug,
        model_id=model_id,
        inventory_path=inventory_path,
        package_manifest_path=manifest_path,
    )
    with psycopg_connection(integration_database_url) as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                INSERT INTO core.basin (basin_id, basin_name, basin_group)
                VALUES ('basins_qhh_duplicate', 'Duplicate QHH', 'integration')
                """
            )
            cursor.execute(
                """
                INSERT INTO core.basin_version (
                    basin_version_id, basin_id, version_label, geom, active_flag, source_uri, checksum
                )
                VALUES (
                    'basins_qhh_duplicate_vbasins',
                    'basins_qhh_duplicate',
                    'vbasins',
                    ST_Multi(ST_MakeEnvelope(100, 30, 101, 31, 4490)),
                    true,
                    'integration://qhh-duplicate',
                    'dup-basin-sha'
                )
                """
            )
            cursor.execute(
                """
                INSERT INTO core.river_network_version (
                    river_network_version_id, basin_version_id, version_label, segment_count, source_uri, checksum
                )
                VALUES (
                    'basins_qhh_duplicate_rivnet_vbasins',
                    'basins_qhh_duplicate_vbasins',
                    'vbasins',
                    1,
                    'integration://qhh-duplicate-rivnet',
                    'dup-rivnet-sha'
                )
                """
            )
            cursor.execute(
                """
                INSERT INTO core.mesh_version (
                    mesh_version_id, basin_version_id, version_label, mesh_uri, checksum, properties_json
                )
                VALUES (
                    'basins_qhh_duplicate_mesh_vbasins',
                    'basins_qhh_duplicate_vbasins',
                    'vbasins',
                    'integration://qhh-duplicate-mesh',
                    'dup-mesh-sha',
                    %s
                )
                """,
                (Json({}),),
            )
            cursor.execute(
                """
                INSERT INTO core.model_instance (
                    model_id,
                    basin_version_id,
                    river_network_version_id,
                    mesh_version_id,
                    calibration_version_id,
                    shud_code_version,
                    model_package_uri,
                    active_flag,
                    lifecycle_state,
                    resource_profile
                )
                SELECT
                    'basins_qhh_shud_duplicate',
                    'basins_qhh_duplicate_vbasins',
                    'basins_qhh_duplicate_rivnet_vbasins',
                    'basins_qhh_duplicate_mesh_vbasins',
                    'duplicate-calib',
                    shud_code_version,
                    model_package_uri,
                    true,
                    'active',
                    %s
                FROM core.model_instance
                WHERE model_id = %s
                """,
                (Json({}), model_id),
            )

    with pytest.raises(QhhProductionBootstrapError) as exc_info:
        bootstrap_qhh_production(
            database_url=integration_database_url,
            basins_root=root,
            qhh_basin_slug=basin_slug,
            model_id=model_id,
            inventory_path=inventory_path,
            package_manifest_path=manifest_path,
        )

    assert exc_info.value.error_code == "QHH_BOOTSTRAP_DUPLICATE_ACTIVE_MODEL"
    assert any(item["duplicate_reason"] == "model_package_uri" for item in exc_info.value.details["active_models"])
    with psycopg_connection(integration_database_url) as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT COUNT(*) AS count FROM met.met_station WHERE properties_json->>'model_id' = %s",
                ("basins_qhh_shud_duplicate",),
            )
            duplicate_station_count = int(cursor.fetchone()["count"])
            cursor.execute(
                """
                SELECT COUNT(*) AS count
                FROM core.river_segment
                WHERE COALESCE(properties_json->>'shud_output_river', 'false') = 'true'
                  AND properties_json->>'model_id' = %s
                """,
                ("basins_qhh_shud_duplicate",),
            )
            duplicate_output_count = int(cursor.fetchone()["count"])
    assert duplicate_station_count == 0
    assert duplicate_output_count == 0


@pytest.mark.integration
def test_registry_import_ignores_bootstrap_output_identity_rows_on_rerun(
    tmp_path: Path,
    integration_database_url: str,
) -> None:
    apply_migrations_from_zero(integration_database_url)
    basin_slug = "qhh-registry-rerun"
    root, _input_dir, inventory_path, manifest_path, model_id = _qhh_registry_fixture(tmp_path, basin_slug=basin_slug)

    first = bootstrap_qhh_production(
        database_url=integration_database_url,
        basins_root=root,
        qhh_basin_slug=basin_slug,
        model_id=model_id,
        inventory_path=inventory_path,
        package_manifest_path=manifest_path,
    )
    second = bootstrap_qhh_production(
        database_url=integration_database_url,
        basins_root=root,
        qhh_basin_slug=basin_slug,
        model_id=model_id,
        inventory_path=inventory_path,
        package_manifest_path=manifest_path,
    )

    assert first["registry_import"]["row_counts"]["river_segment"] == 2
    assert second["registry_import"]["row_counts"]["river_segment"] == 0
    assert second["status"] == "bootstrapped"
