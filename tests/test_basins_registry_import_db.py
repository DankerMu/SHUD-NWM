"""Basins registry import: real-database rows, conflicts, rollback and smoke gate.

Partition 6 of 7 of the former monolith ``tests/test_basins_registry_import.py``
(issue #1913): the 5 database contracts, every one marked ``integration`` and bound
to a ci.yml ``database:`` trigger path, including the opt-in real-Basins import smoke
this file owns (``docs/VALIDATION.md`` "Real registry import smoke").  Shared test
support lives in the non-collectible
``tests/basins_registry_import_helpers.py``.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from packages.common.model_registry import PsycopgModelRegistryStore
from tests.basins_registry_import_helpers import (
    _assert_registry_fixture_rows_absent,
    _package_manifest_for_model,
    _write_registry_fixture,
    _write_river_shapefile,
)
from tests.integration_helpers import apply_migrations_from_zero, psycopg_connection
from workers.model_registry.basins_discovery import (
    discover_basins_inventory,
    write_inventory,
)
from workers.model_registry.cli import _argparse_main


@pytest.mark.integration
def test_registry_import_creates_idempotent_inactive_rows(
    tmp_path: Path,
    integration_database_url: str,
    capsys: pytest.CaptureFixture[str],
) -> None:
    apply_migrations_from_zero(integration_database_url)
    basin_slug = "basin-a-idempotent"
    _, _, inventory_path, manifest_path, model_id = _write_registry_fixture(tmp_path, basin_slug=basin_slug)
    report_path = tmp_path / "import-report.json"
    args = [
        "import-basins-registry",
        "--inventory",
        str(inventory_path),
        "--package-manifest",
        str(manifest_path),
        "--database-url",
        integration_database_url,
        "--output",
        str(report_path),
        "--auth-actor-id",
        "cli-model-admin",
        "--auth-role",
        "model_admin",
    ]

    assert _argparse_main(args) == 0
    first = json.loads(capsys.readouterr().out)
    assert _argparse_main(args) == 0
    second = json.loads(capsys.readouterr().out)

    assert first["status"] == "imported"
    assert first["active"] is False
    assert first["row_counts"] == {
        "basin": 1,
        "basin_version": 1,
        "river_network_version": 1,
        "river_segment": 2,
        "output_river_segment": 2,
        "river_segment_crosswalk": 2,
        "mesh_version": 1,
        "model_instance": 1,
    }
    assert second["status"] == "already_imported"
    assert second["row_counts"] == {
        "basin": 0,
        "basin_version": 0,
        "river_network_version": 0,
        "river_segment": 0,
        "output_river_segment": 0,
        "river_segment_crosswalk": 0,
        "mesh_version": 0,
        "model_instance": 0,
    }
    assert json.loads(report_path.read_text(encoding="utf-8"))["model_id"] == model_id

    with psycopg_connection(integration_database_url) as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT mi.active_flag,
                       mi.resource_profile,
                       rnv.segment_count,
                       COUNT(rs.river_segment_id) AS segment_rows,
                       MAX(rs.downstream_segment_id) FILTER (
                         WHERE rs.river_segment_id = %s
                       ) AS first_downstream,
                       MAX(rs.downstream_segment_id) FILTER (
                         WHERE rs.river_segment_id = %s
                       ) AS second_downstream,
                       ST_AsText(bv.geom) AS basin_geom
                FROM core.model_instance mi
                JOIN core.river_network_version rnv
                  ON rnv.river_network_version_id = mi.river_network_version_id
                JOIN core.basin_version bv
                  ON bv.basin_version_id = mi.basin_version_id
                LEFT JOIN core.river_segment rs
                  ON rs.river_network_version_id = rnv.river_network_version_id
                WHERE mi.model_id = %s
                GROUP BY mi.active_flag, mi.resource_profile, rnv.segment_count, bv.geom
                """,
                (
                    f"{model_id}_reach_000001",
                    f"{model_id}_reach_000002",
                    model_id,
                ),
            )
            row = cursor.fetchone()
    assert row is not None
    assert row["active_flag"] is False
    assert row["resource_profile"]["package_checksum"] == "package-sha-1"
    assert row["resource_profile"]["basin_slug"] == basin_slug
    # PR 2: river_network.segment_count == reach count (1 row per reach).
    assert row["segment_count"] == 2
    # core.river_segment holds the 2 reach rows + 2 .sp.riv SHUD output rows.
    assert row["segment_rows"] == 4
    # First reach's Down=2 resolves to <model>_reach_000002.
    assert row["first_downstream"] == f"{model_id}_reach_000002"
    assert row["second_downstream"] is None
    assert row["basin_geom"].startswith("MULTIPOLYGON")

    store = PsycopgModelRegistryStore(integration_database_url)
    segments = store.list_river_segments(
        basin_version_id=first["basin_version_id"],
        river_network_version_id=first["river_network_version_id"],
        limit=10,
        offset=0,
    )
    assert segments["type"] == "FeatureCollection"
    # PR 2 Path C: API returns segment-level FeatureCollection sliced from
    # parent reach polylines via ST_LineSubstring. The crosswalk fixture
    # has 2 seg.shp records (matching ``_make_valid_model`` defaults), so
    # 2 segment-level features come back. The legacy 4-row "geometry +
    # .sp.riv output" total no longer applies because the segment-slice
    # path does its own grouping.
    assert segments["total"] == 2
    assert segments["feature_total"] == 2
    # Sliced geometry is a LineString (ST_LineSubstring against a
    # single-part MultiLineString returns LineString).
    assert segments["features"][0]["geometry"]["type"] in ("LineString", "MultiLineString")
    # Segment-level id preserves the frontend contract:
    # ``<model>_seg_<iRiv>_<iEle>`` (OQ2 in feat-reach-geom-oq-findings).
    assert segments["features"][0]["properties"]["river_segment_id"].startswith(
        f"{model_id}_seg_"
    )
    assert segments["features"][0]["properties"]["river_network_version_id"] == first["river_network_version_id"]
    assert segments["features"][0]["properties"]["basin_version_id"] == first["basin_version_id"]


@pytest.mark.integration
def test_registry_import_checksum_conflict_rolls_back(
    tmp_path: Path,
    integration_database_url: str,
    capsys: pytest.CaptureFixture[str],
) -> None:
    apply_migrations_from_zero(integration_database_url)
    _, _, inventory_path, manifest_path, model_id = _write_registry_fixture(
        tmp_path,
        basin_slug="basin-a-checksum-conflict",
    )
    args = [
        "import-basins-registry",
        "--inventory",
        str(inventory_path),
        "--package-manifest",
        str(manifest_path),
        "--database-url",
        integration_database_url,
        "--auth-actor-id",
        "cli-model-admin",
        "--auth-role",
        "model_admin",
    ]
    assert _argparse_main(args) == 0
    capsys.readouterr()

    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["package_checksum"] = "package-sha-mutated"
    manifest_path.write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8")

    assert _argparse_main(args) == 1
    captured = capsys.readouterr()
    error = json.loads(captured.err)
    assert error["error_code"] == "BASINS_REGISTRY_CHECKSUM_CONFLICT"
    assert error["model_id"] == model_id

    with psycopg_connection(integration_database_url) as connection:
        with connection.cursor() as cursor:
            cursor.execute("SELECT resource_profile FROM core.model_instance WHERE model_id = %s", (model_id,))
            row = cursor.fetchone()
    assert row["resource_profile"]["package_checksum"] == "package-sha-1"


@pytest.mark.integration
def test_registry_import_conflicts_on_existing_river_segment_drift(
    tmp_path: Path,
    integration_database_url: str,
    capsys: pytest.CaptureFixture[str],
) -> None:
    apply_migrations_from_zero(integration_database_url)
    _, _, inventory_path, manifest_path, model_id = _write_registry_fixture(
        tmp_path,
        basin_slug="basin-a-segment-drift",
    )
    args = [
        "import-basins-registry",
        "--inventory",
        str(inventory_path),
        "--package-manifest",
        str(manifest_path),
        "--database-url",
        integration_database_url,
        "--auth-actor-id",
        "cli-model-admin",
        "--auth-role",
        "model_admin",
    ]
    assert _argparse_main(args) == 0
    report = json.loads(capsys.readouterr().out)

    with psycopg_connection(integration_database_url) as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                UPDATE core.river_segment
                SET length_m = length_m + 1
                WHERE river_network_version_id = %s
                  AND river_segment_id = %s
                """,
                (report["river_network_version_id"], f"{model_id}_reach_000001"),
            )

    assert _argparse_main(args) == 1
    error = json.loads(capsys.readouterr().err)
    assert error["error_code"] == "BASINS_REGISTRY_CHECKSUM_CONFLICT"
    assert error["model_id"] == model_id
    assert error["resource"] == "river_segment"


@pytest.mark.integration
def test_registry_import_mismatch_rolls_back_all_rows(
    tmp_path: Path,
    integration_database_url: str,
    capsys: pytest.CaptureFixture[str],
) -> None:
    apply_migrations_from_zero(integration_database_url)
    _, input_dir, inventory_path, manifest_path, model_id = _write_registry_fixture(
        tmp_path,
        basin_slug="basin-a-mismatch",
        sp_river_count=2,
        sp_segment_count=2,
    )
    # Force river.shp to declare more records than .sp.riv: PR 2 then
    # surfaces the new BASINS_REGISTRY_RIVER_SHP_INVARIANT_VIOLATED.
    _write_river_shapefile(input_dir / "gis" / "river", reach_count=3)
    inventory = discover_basins_inventory(tmp_path / "basins")
    write_inventory(inventory, inventory_path)
    fresh_model = inventory["models"][0]
    refreshed_manifest = _package_manifest_for_model(
        fresh_model, fresh_model["model_id"], inventory=inventory
    )
    manifest_path.write_text(
        json.dumps(refreshed_manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )

    exit_code = _argparse_main(
        [
            "import-basins-registry",
            "--inventory",
            str(inventory_path),
            "--package-manifest",
            str(manifest_path),
            "--database-url",
            integration_database_url,
            "--auth-actor-id",
            "cli-model-admin",
            "--auth-role",
            "model_admin",
        ]
    )

    error = json.loads(capsys.readouterr().err)
    assert exit_code == 1
    assert error["error_code"] == "BASINS_REGISTRY_RIVER_SHP_INVARIANT_VIOLATED"
    _assert_registry_fixture_rows_absent(integration_database_url, inventory_path, model_id)


@pytest.mark.skipif(
    os.getenv("NHMS_RUN_REAL_BASINS_IMPORT") != "1" or not Path("data/Basins").exists(),
    reason="real Basins import smoke is opt-in and requires data/Basins",
)
@pytest.mark.integration
def test_real_basins_import_smoke_is_gated(
    tmp_path: Path,
    integration_database_url: str,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    apply_migrations_from_zero(integration_database_url)
    inventory = discover_basins_inventory(Path("data/Basins"))
    valid = next(model for model in inventory["models"] if model["status"] == "valid")
    inventory["models"] = [valid]
    inventory["model_count"] = 1
    inventory_path = tmp_path / "real-inventory.json"
    write_inventory(inventory, inventory_path)
    object_root = tmp_path / "object-store"
    monkeypatch.setenv("OBJECT_STORE_ROOT", str(object_root))
    monkeypatch.setenv("OBJECT_STORE_PREFIX", "s3://nhms")
    manifest_path = tmp_path / "real-manifest.json"
    assert _argparse_main(
        [
            "publish-basins",
            "--inventory",
            str(inventory_path),
            "--model-id",
            valid["model_id"],
            "--version",
            "vbasins-real-smoke",
            "--output",
            str(manifest_path),
        ]
    ) == 0
    capsys.readouterr()

    assert _argparse_main(
        [
            "import-basins-registry",
            "--inventory",
            str(inventory_path),
            "--package-manifest",
            str(manifest_path),
            "--database-url",
            integration_database_url,
            "--auth-actor-id",
            "cli-model-admin",
            "--auth-role",
            "model_admin",
        ]
    ) == 0
    report = json.loads(capsys.readouterr().out)
    assert report["segment_count"] > 0
    assert report["active"] is False

    store = PsycopgModelRegistryStore(integration_database_url)
    segments = store.list_river_segments(
        basin_version_id=report["basin_version_id"],
        river_network_version_id=report["river_network_version_id"],
        limit=5,
        offset=0,
    )
    assert segments["type"] == "FeatureCollection"
    assert segments["feature_total"] > 0
    # PR 2 Path C: API segment-slice path can return LineString
    # (ST_LineSubstring against a single-part MultiLineString returns a
    # LineString). Keep both shapes as acceptable.
    assert segments["features"][0]["geometry"]["type"] in (
        "LineString",
        "MultiLineString",
    )
    assert segments["features"][0]["properties"]["river_segment_id"]
    assert segments["features"][0]["properties"]["river_network_version_id"] == report["river_network_version_id"]
    assert segments["features"][0]["properties"]["basin_version_id"] == report["basin_version_id"]
