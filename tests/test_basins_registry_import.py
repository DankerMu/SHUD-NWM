from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

import pytest

from tests.integration_helpers import apply_migrations_from_zero, psycopg_connection
from workers.model_registry.basins_discovery import discover_basins_inventory, write_inventory
from workers.model_registry.basins_geometry import parse_basins_geometry
from workers.model_registry.basins_registry_import import prepare_basins_import_sources
from workers.model_registry.cli import _argparse_main, _click_main


def test_parser_reads_real_shapefiles_and_shud_evidence(tmp_path: Path) -> None:
    root, input_dir, inventory_path, manifest_path, model_id = _write_registry_fixture(tmp_path)
    del root, inventory_path, manifest_path
    inventory = discover_basins_inventory(tmp_path / "basins")
    model = inventory["models"][0]

    parsed = parse_basins_geometry(
        model_id=model_id,
        input_dir=input_dir,
        shud_input_name="alias-a",
        required_files=model["required_files"],
    )

    assert parsed.domain_wkt.startswith("MULTIPOLYGON")
    assert parsed.segment_count == 2
    assert parsed.evidence_counts == {"sp_riv": 2, "sp_rivseg": 2}
    assert [segment.segment_order for segment in parsed.river_segments] == [1, 2]
    assert parsed.river_segments[0].geom_wkt.startswith("LINESTRING")


def test_import_command_requires_database_but_consumes_manifests_first(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _, _, inventory_path, manifest_path, model_id = _write_registry_fixture(tmp_path)
    monkeypatch.delenv("DATABASE_URL", raising=False)

    exit_code = _argparse_main(
        [
            "import-basins-registry",
            "--inventory",
            str(inventory_path),
            "--package-manifest",
            str(manifest_path),
        ]
    )

    captured = capsys.readouterr()
    error = json.loads(captured.err)
    assert exit_code == 1
    assert captured.out == ""
    assert error["error_code"] == "BASINS_REGISTRY_DATABASE_URL_MISSING"
    assert error["model_id"] == model_id


def test_import_command_reports_missing_sidecar_as_json(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _, input_dir, inventory_path, manifest_path, model_id = _write_registry_fixture(tmp_path)
    (input_dir / "gis" / "domain.shx").unlink()

    exit_code = _argparse_main(
        [
            "import-basins-registry",
            "--inventory",
            str(inventory_path),
            "--package-manifest",
            str(manifest_path),
            "--database-url",
            "postgresql://nhms:nhms@localhost:1/nhms",
        ]
    )

    captured = capsys.readouterr()
    error = json.loads(captured.err)
    assert exit_code == 1
    assert captured.out == ""
    assert error["error_code"] == "BASINS_REGISTRY_GIS_SIDECAR_MISSING"
    assert error["model_id"] == model_id
    assert error["missing_sidecar"] == "gis/domain.shx"


def test_import_command_reports_segment_count_mismatch_before_database(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _, input_dir, inventory_path, manifest_path, model_id = _write_registry_fixture(tmp_path, sp_segment_count=3)

    exit_code = _argparse_main(
        [
            "import-basins-registry",
            "--inventory",
            str(inventory_path),
            "--package-manifest",
            str(manifest_path),
            "--database-url",
            "postgresql://nhms:nhms@localhost:1/nhms",
        ]
    )

    captured = capsys.readouterr()
    error = json.loads(captured.err)
    assert exit_code == 1
    assert captured.out == ""
    assert error["error_code"] == "BASINS_REGISTRY_SEGMENT_COUNT_MISMATCH"
    assert error["model_id"] == model_id
    assert error["gis_segment_count"] == 2
    assert error["evidence_count"] == 3
    assert error["path"] == str(input_dir / "alias-a.sp.riv")


def test_click_path_exposes_import_basins_registry(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _, _, inventory_path, manifest_path, _ = _write_registry_fixture(tmp_path)
    monkeypatch.delenv("DATABASE_URL", raising=False)

    exit_code = _invoke_click(
        [
            "import-basins-registry",
            "--inventory",
            str(inventory_path),
            "--package-manifest",
            str(manifest_path),
        ]
    )

    assert exit_code == 1


def test_prepare_import_sources_does_not_need_data_basins_default(tmp_path: Path) -> None:
    _, _, inventory_path, manifest_path, model_id = _write_registry_fixture(tmp_path)

    sources = prepare_basins_import_sources(inventory_path=inventory_path, package_manifest_path=manifest_path)

    assert sources.ids["model_id"] == model_id
    assert sources.source_root == (tmp_path / "basins" / "basin-a").resolve()


@pytest.mark.integration
def test_registry_import_creates_idempotent_inactive_rows(
    tmp_path: Path,
    integration_database_url: str,
    capsys: pytest.CaptureFixture[str],
) -> None:
    apply_migrations_from_zero(integration_database_url)
    _, _, inventory_path, manifest_path, model_id = _write_registry_fixture(tmp_path)
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
        "mesh_version": 1,
        "model_instance": 1,
    }
    assert second["status"] == "already_imported"
    assert second["row_counts"] == {
        "basin": 0,
        "basin_version": 0,
        "river_network_version": 0,
        "river_segment": 0,
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
                (model_id,),
            )
            row = cursor.fetchone()
    assert row is not None
    assert row["active_flag"] is False
    assert row["resource_profile"]["package_checksum"] == "package-sha-1"
    assert row["resource_profile"]["basin_slug"] == "basin-a"
    assert row["segment_count"] == 2
    assert row["segment_rows"] == 2
    assert row["basin_geom"].startswith("MULTIPOLYGON")


@pytest.mark.integration
def test_registry_import_checksum_conflict_rolls_back(
    tmp_path: Path,
    integration_database_url: str,
    capsys: pytest.CaptureFixture[str],
) -> None:
    apply_migrations_from_zero(integration_database_url)
    _, _, inventory_path, manifest_path, model_id = _write_registry_fixture(tmp_path)
    args = [
        "import-basins-registry",
        "--inventory",
        str(inventory_path),
        "--package-manifest",
        str(manifest_path),
        "--database-url",
        integration_database_url,
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
def test_registry_import_mismatch_rolls_back_all_rows(
    tmp_path: Path,
    integration_database_url: str,
    capsys: pytest.CaptureFixture[str],
) -> None:
    apply_migrations_from_zero(integration_database_url)
    _, _, inventory_path, manifest_path, model_id = _write_registry_fixture(tmp_path, sp_segment_count=3)
    exit_code = _argparse_main(
        [
            "import-basins-registry",
            "--inventory",
            str(inventory_path),
            "--package-manifest",
            str(manifest_path),
            "--database-url",
            integration_database_url,
        ]
    )

    error = json.loads(capsys.readouterr().err)
    assert exit_code == 1
    assert error["error_code"] == "BASINS_REGISTRY_SEGMENT_COUNT_MISMATCH"
    with psycopg_connection(integration_database_url) as connection:
        with connection.cursor() as cursor:
            cursor.execute("SELECT COUNT(*) AS count FROM core.model_instance WHERE model_id = %s", (model_id,))
            assert cursor.fetchone()["count"] == 0
            cursor.execute("SELECT COUNT(*) AS count FROM core.basin WHERE basin_id = 'basins_basin_a'")
            assert cursor.fetchone()["count"] == 0


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
        ]
    ) == 0
    report = json.loads(capsys.readouterr().out)
    assert report["segment_count"] > 0
    assert report["active"] is False


def _write_registry_fixture(
    tmp_path: Path,
    *,
    sp_segment_count: int = 2,
) -> tuple[Path, Path, Path, Path, str]:
    root = tmp_path / "basins"
    input_dir = _make_valid_model(root / "basin-a", "alias-a", sp_segment_count=sp_segment_count)
    inventory = discover_basins_inventory(root)
    inventory_path = tmp_path / "inventory.json"
    write_inventory(inventory, inventory_path)
    model = inventory["models"][0]
    model_id = model["model_id"]
    manifest = _package_manifest_for_model(model, model_id)
    manifest_path = tmp_path / "manifest.json"
    manifest_path.write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return root, input_dir, inventory_path, manifest_path, model_id


def _make_valid_model(model_dir: Path, input_name: str, *, sp_segment_count: int) -> Path:
    input_dir = model_dir / "input" / input_name
    input_dir.mkdir(parents=True)
    for suffix in (
        "cfg.para",
        "cfg.ic",
        "cfg.calib",
        "sp.mesh",
        "sp.att",
        "para.soil",
        "para.geol",
        "para.lc",
        "tsd.forc",
        "tsd.lai",
        "tsd.mf",
        "tsd.rl",
    ):
        (input_dir / f"{input_name}.{suffix}").write_text(f"{suffix}\n", encoding="utf-8")
    (input_dir / f"{input_name}.sp.riv").write_text(f"{sp_segment_count}\n", encoding="utf-8")
    (input_dir / f"{input_name}.sp.rivseg").write_text(f"{sp_segment_count}\n", encoding="utf-8")
    gis_dir = input_dir / "gis"
    gis_dir.mkdir()
    _write_domain_shapefile(gis_dir / "domain")
    _write_line_shapefile(gis_dir / "river")
    _write_line_shapefile(gis_dir / "seg")
    forcing = model_dir / "forcing"
    forcing.mkdir()
    (forcing / "X000001.csv").write_text("time,value\n2026-01-01,1\n", encoding="utf-8")
    return input_dir


def _write_domain_shapefile(base: Path) -> None:
    import shapefile

    writer = shapefile.Writer(str(base), shapeType=shapefile.POLYGON)
    writer.field("ID", "N")
    writer.poly([[[100.0, 30.0], [101.0, 30.0], [101.0, 31.0], [100.0, 31.0], [100.0, 30.0]]])
    writer.record(1)
    writer.close()
    _write_prj(base.with_suffix(".prj"))


def _write_line_shapefile(base: Path) -> None:
    import shapefile

    writer = shapefile.Writer(str(base), shapeType=shapefile.POLYLINE)
    writer.field("SEG_ID", "N")
    writer.field("ORDER", "N")
    writer.field("DOWN_ID", "N")
    writer.field("LENGTH_M", "F", decimal=3)
    writer.line([[[100.1, 30.1], [100.5, 30.4]]])
    writer.record(1, 1, 2, 50000.0)
    writer.line([[[100.5, 30.4], [100.8, 30.8]]])
    writer.record(2, 2, 0, 60000.0)
    writer.close()
    _write_prj(base.with_suffix(".prj"))


def _write_prj(path: Path) -> None:
    path.write_text(
        'GEOGCS["GCS_WGS_1984",DATUM["D_WGS_1984",'
        'SPHEROID["WGS_1984",6378137,298.257223563]],'
        'PRIMEM["Greenwich",0],UNIT["Degree",0.0174532925199433]]\n',
        encoding="utf-8",
    )


def _package_manifest_for_model(model: dict[str, Any], model_id: str) -> dict[str, Any]:
    version = "vbasins-test"
    package_uri = f"s3://nhms/models/{model_id}/{version}/package/"
    included_files = [
        {
            "relative_path": f"{model['shud_input_name']}.sp.mesh",
            "object_uri": package_uri + f"{model['shud_input_name']}.sp.mesh",
            "size_bytes": 8,
            "sha256": "mesh-sha",
            "role": "runtime_input",
        }
    ]
    return {
        "schema_version": "basins.package.v1",
        "model_id": model_id,
        "version": version,
        "basin_slug": model["basin_slug"],
        "shud_input_name": model["shud_input_name"],
        "model_package_uri": package_uri,
        "manifest_uri": f"s3://nhms/models/{model_id}/{version}/manifest.json",
        "package_checksum": "package-sha-1",
        "source_inventory_checksum": "inventory-sha-1",
        "source_inventory_schema_version": "basins.discovery.v1",
        "source_path": model["source_path"],
        "resolved_source_path": model["resolved_source_path"],
        "source_is_symlink": False,
        "included_files": included_files,
        "forcing": {"policy": "excluded_by_default", "csv_count": 1},
        "calibration": {"source_count": 0, "included_count": 0},
        "created_at": "2026-05-16T00:00:00Z",
    }


def _invoke_click(argv: list[str]) -> int:
    try:
        return _click_main(argv)
    except SystemExit as error:
        if isinstance(error.code, int):
            return error.code
        return 1
