"""QHH production bootstrap support: builders, seeds and scheduler-readiness helpers.

Non-collectible (the filename is deliberately not `test_*`), so pytest never runs it
as a suite; its three QHH bootstrap consumers import it. Its two registry-fixture
imports come from the #1913 non-collectible registry helper
(`tests/basins_registry_import_helpers.py`), never from a collectible registry
suite, and no QHH bootstrap partition imports that helper directly.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Iterator
from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest
from psycopg2.extras import Json

from packages.common.met_store import PsycopgMetStore
from services.orchestrator.scheduler import _MetStoreCanonicalReadinessProvider
from tests.basins_registry_import_helpers import _write_registry_fixture
from tests.integration_helpers import psycopg_connection
from tests.test_production_scheduler import FakeAdapter, _dt
from workers.canonical_converter.converter import GFS_REQUIRED_STANDARD_VARIABLES

_QHH_SCHEDULER_SOURCE_ID = "gfs"
_QHH_SCHEDULER_SOURCE_VERSION = "qhh-scheduler-readiness-fixture"
_QHH_SCHEDULER_SOURCE_NAME = "GFS QHH scheduler readiness fixture"
_QHH_SCHEDULER_FOUR_CYCLE_HOURS = (0, 6, 12, 18)


def _qhh_registry_fixture(tmp_path: Path, *, basin_slug: str = "qhh") -> tuple[Path, Path, Path, Path, str]:
    root, input_dir, inventory_path, manifest_path, model_id = _write_registry_fixture(
        tmp_path,
        basin_slug=basin_slug,
        sp_segment_count=2,
    )
    input_dir = _rename_fixture_input_to_qhh(input_dir)
    _write_valid_qhh_tsd_forc(input_dir)
    _refresh_inventory_and_manifest(tmp_path, root, inventory_path, manifest_path)
    return root, input_dir, inventory_path, manifest_path, model_id


@pytest.fixture
def qhh_scheduler_canonical_readiness(integration_database_url: str) -> Iterator[Callable[[str], Any]]:
    seeded = False
    created_data_source = False

    def seed(model_id: str) -> Any:
        nonlocal created_data_source, seeded
        _clear_qhh_scheduler_canonical_readiness(integration_database_url)
        seeded = True
        readiness_provider, seed_created_data_source = _seed_qhh_scheduler_canonical_readiness(
            integration_database_url,
            model_id=model_id,
        )
        created_data_source = created_data_source or seed_created_data_source
        return readiness_provider

    try:
        yield seed
    finally:
        if seeded:
            _clear_qhh_scheduler_canonical_readiness(
                integration_database_url,
                delete_created_data_source=created_data_source,
            )


def _clear_qhh_scheduler_canonical_readiness(database_url: str, *, delete_created_data_source: bool = False) -> None:
    cycle_time = _dt("2026-05-21T06:00:00Z")
    with psycopg_connection(database_url) as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                DELETE FROM met.forcing_version_component
                WHERE canonical_product_id IN (
                    SELECT canonical_product_id
                    FROM met.canonical_met_product
                    WHERE source_id = %s
                      AND cycle_time = %s
                      AND source_version = %s
                      AND lineage_json->'policy_identity' = %s::jsonb
                      AND lineage_json->'source_object_identity' = %s::jsonb
                )
                """,
                (
                    _QHH_SCHEDULER_SOURCE_ID,
                    cycle_time,
                    _QHH_SCHEDULER_SOURCE_VERSION,
                    json.dumps(_qhh_scheduler_policy_identity(), sort_keys=True),
                    json.dumps(_qhh_scheduler_source_object_identity(), sort_keys=True),
                ),
            )
            cursor.execute(
                """
                DELETE FROM met.canonical_met_product
                WHERE source_id = %s
                  AND cycle_time = %s
                  AND source_version = %s
                  AND lineage_json->'policy_identity' = %s::jsonb
                  AND lineage_json->'source_object_identity' = %s::jsonb
                """,
                (
                    _QHH_SCHEDULER_SOURCE_ID,
                    cycle_time,
                    _QHH_SCHEDULER_SOURCE_VERSION,
                    json.dumps(_qhh_scheduler_policy_identity(), sort_keys=True),
                    json.dumps(_qhh_scheduler_source_object_identity(), sort_keys=True),
                ),
            )
            if delete_created_data_source:
                cursor.execute(
                    """
                    DELETE FROM met.data_source
                    WHERE source_id = %s
                      AND source_name = %s
                      AND config_json->>'qhh_scheduler_readiness_fixture' = 'true'
                    """,
                    (_QHH_SCHEDULER_SOURCE_ID, _QHH_SCHEDULER_SOURCE_NAME),
                )


def _seed_qhh_scheduler_canonical_readiness(database_url: str, *, model_id: str) -> tuple[Any, bool]:
    cycle_time = _dt("2026-05-21T06:00:00Z")
    forecast_hours = _qhh_scheduler_forecast_hours()
    policy_identity = _qhh_scheduler_policy_identity()
    source_object_identity = _qhh_scheduler_source_object_identity()
    created_data_source = False
    with psycopg_connection(database_url) as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                INSERT INTO met.data_source (
                    source_id, source_name, source_type, status, native_format, adapter_name, config_json
                )
                VALUES (%s, %s, 'forecast', 'mock', 'netcdf', 'gfs', %s)
                ON CONFLICT (source_id) DO NOTHING
                RETURNING source_id
                """,
                (
                    _QHH_SCHEDULER_SOURCE_ID,
                    _QHH_SCHEDULER_SOURCE_NAME,
                    Json(
                        {
                            "integration": True,
                            "model_id": model_id,
                            "qhh_scheduler_readiness_fixture": True,
                        }
                    ),
                ),
            )
            created_data_source = cursor.fetchone() is not None

    store = PsycopgMetStore(database_url)
    for row in _qhh_scheduler_canonical_rows(
        cycle_time=cycle_time,
        forecast_hours=forecast_hours,
        policy_identity=policy_identity,
        source_object_identity=source_object_identity,
    ):
        store.upsert_canonical_product(row)
    return _MetStoreCanonicalReadinessProvider(store), created_data_source


def _qhh_scheduler_ready_gfs_adapter() -> FakeAdapter:
    return FakeAdapter(
        "gfs",
        [("2026-05-21T06:00:00Z", True)],
        policy_identity=_qhh_scheduler_policy_identity(),
        source_object_identity=_qhh_scheduler_source_object_identity(),
    )


def _qhh_scheduler_forecast_hours() -> tuple[int, ...]:
    return tuple(range(0, 169, 3))


def _qhh_scheduler_policy_identity() -> dict[str, Any]:
    return {"source": _QHH_SCHEDULER_SOURCE_ID, "forecast_hours": list(_qhh_scheduler_forecast_hours())}


def _qhh_scheduler_source_object_identity() -> dict[str, Any]:
    return {"source": _QHH_SCHEDULER_SOURCE_ID, "object": "fake"}


def _qhh_scheduler_canonical_rows(
    *,
    cycle_time: Any,
    forecast_hours: tuple[int, ...],
    policy_identity: dict[str, Any],
    source_object_identity: dict[str, Any],
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for forecast_hour in forecast_hours:
        for variable in GFS_REQUIRED_STANDARD_VARIABLES:
            rows.append(
                {
                    "canonical_product_id": f"gfs_{cycle_time:%Y%m%d%H}_{variable}_f{forecast_hour:03d}",
                    "source_id": _QHH_SCHEDULER_SOURCE_ID,
                    "source_version": _QHH_SCHEDULER_SOURCE_VERSION,
                    "cycle_time": cycle_time,
                    "valid_time": cycle_time + timedelta(hours=forecast_hour),
                    "lead_time_hours": forecast_hour,
                    "variable": variable,
                    "unit": "1",
                    "grid_id": "gfs_0p25",
                    "grid_definition_uri": "canonical/gfs/grid/gfs_0p25/grid.json",
                    "native_time_resolution": "3h",
                    "native_spatial_resolution": "0.25deg",
                    "object_uri": f"s3://nhms/canonical/gfs/2026052106/{variable}/f{forecast_hour:03d}.nc",
                    "checksum": f"sha256:qhh:{variable}:{forecast_hour}",
                    "quality_flag": "ok",
                    "lineage_json": {
                        "policy_identity": dict(policy_identity),
                        "source_object_identity": dict(source_object_identity),
                    },
                }
            )
    return rows


def _rename_fixture_input_to_qhh(input_dir: Path) -> Path:
    for path in list(input_dir.glob("alias-a.*")):
        target = input_dir / path.name.replace("alias-a", "qhh", 1)
        path.rename(target)
    qhh_input_dir = input_dir.parent / "qhh"
    input_dir.rename(qhh_input_dir)
    return qhh_input_dir


def _write_valid_qhh_tsd_forc(input_dir: Path) -> None:
    input_dir.joinpath("qhh.tsd.forc").write_text(
        "2 6\n"
        "/forcing\n"
        "ID Lon Lat X Y Z Filename\n"
        "1 100.1 30.1 1 2 -9999 X000001.csv\n"
        "2 100.2 30.2 3 4 12.5 X000002.csv\n",
        encoding="utf-8",
    )


def _refresh_inventory_and_manifest(
    tmp_path: Path,
    root: Path,
    inventory_path: Path,
    manifest_path: Path,
) -> None:
    from tests.basins_registry_import_helpers import _package_manifest_for_model
    from workers.model_registry.basins_discovery import discover_basins_inventory, write_inventory

    inventory = discover_basins_inventory(root)
    write_inventory(inventory, inventory_path)
    model = inventory["models"][0]
    manifest = _package_manifest_for_model(model, model["model_id"], inventory=inventory)
    manifest["package_checksum"] = f"package-sha-{model['model_id']}"
    manifest_path.write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    del tmp_path
