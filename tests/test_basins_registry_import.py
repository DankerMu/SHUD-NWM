from __future__ import annotations

import hashlib
import json
import os
import shutil
from pathlib import Path
from typing import Any

import pytest

import workers.model_registry.basins_geometry as basins_geometry
from packages.common.model_registry import PsycopgModelRegistryStore
from tests.integration_helpers import apply_migrations_from_zero, psycopg_connection
from workers.model_registry.basins_discovery import discover_basins_inventory, write_inventory
from workers.model_registry.basins_geometry import parse_basins_geometry
from workers.model_registry.basins_registry_import import BasinsRegistryImportError, prepare_basins_import_sources
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
    assert parsed.river_segments[0].downstream_segment_id == f"{model_id}_seg_2"
    assert parsed.river_segments[1].downstream_segment_id is None
    assert parsed.river_segments[0].properties["source_downstream_segment_id"] == "2"
    assert parsed.river_segments[0].geom_wkt.startswith("LINESTRING")


def test_manifest_must_match_selected_inventory_source_before_database(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _, _, inventory_path, manifest_path, model_id = _write_registry_fixture(tmp_path)
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["shud_input_name"] = "other-alias"
    manifest_path.write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8")

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

    error = json.loads(capsys.readouterr().err)
    assert exit_code == 1
    assert error["error_code"] == "BASINS_REGISTRY_SOURCE_MISMATCH"
    assert error["model_id"] == model_id
    assert "shud_input_name" in error["fields"]


def test_manifest_source_identity_fields_are_required_before_database(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _, _, inventory_path, manifest_path, model_id = _write_registry_fixture(tmp_path)
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    del manifest["source_inventory_checksum"]
    manifest_path.write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8")

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

    error = json.loads(capsys.readouterr().err)
    assert exit_code == 1
    assert error["error_code"] == "BASINS_REGISTRY_SOURCE_MISMATCH"
    assert error["model_id"] == model_id
    assert error["fields"] == ["source_inventory_checksum"]


def test_import_accepts_raw_inventory_byte_checksum_for_noncanonical_json(tmp_path: Path) -> None:
    _, _, inventory_path, manifest_path, model_id = _write_registry_fixture(tmp_path)
    inventory = json.loads(inventory_path.read_text(encoding="utf-8"))
    raw_inventory = json.dumps(inventory, ensure_ascii=False, indent=4, sort_keys=False).encode("utf-8")
    raw_inventory = raw_inventory.replace(b'\n    "schema_version"', b'\n\n    "schema_version"')
    inventory_path.write_bytes(raw_inventory)
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["source_inventory_checksum"] = hashlib.sha256(raw_inventory).hexdigest()
    manifest_path.write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8")

    sources = prepare_basins_import_sources(inventory_path=inventory_path, package_manifest_path=manifest_path)

    assert sources.ids["model_id"] == model_id


def test_import_rejects_wrong_raw_inventory_byte_checksum(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _, _, inventory_path, manifest_path, model_id = _write_registry_fixture(tmp_path)
    inventory = json.loads(inventory_path.read_text(encoding="utf-8"))
    raw_inventory = json.dumps(inventory, ensure_ascii=False, indent=4, sort_keys=False).encode("utf-8")
    inventory_path.write_bytes(raw_inventory)
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["source_inventory_checksum"] = "0" * 64
    manifest_path.write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8")

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

    error = json.loads(capsys.readouterr().err)
    assert exit_code == 1
    assert error["error_code"] == "BASINS_REGISTRY_SOURCE_MISMATCH"
    assert error["model_id"] == model_id
    assert error["fields"] == ["source_inventory_checksum"]
    assert error["expected"] == hashlib.sha256(raw_inventory).hexdigest()


def test_manifest_checksum_conflict_fails_before_database(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _, _, inventory_path, manifest_path, model_id = _write_registry_fixture(tmp_path)
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    mesh_entry = next(entry for entry in manifest["included_files"] if entry["relative_path"] == "alias-a.sp.mesh")
    mesh_entry["sha256"] = "0" * 64
    manifest_path.write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8")

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

    error = json.loads(capsys.readouterr().err)
    assert exit_code == 1
    assert error["error_code"] == "BASINS_REGISTRY_CHECKSUM_CONFLICT"
    assert error["model_id"] == model_id
    assert "alias-a.sp.mesh" in error["relative_paths"]


def test_import_rejects_required_file_traversal_before_database(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _, _, inventory_path, manifest_path, model_id = _write_registry_fixture(tmp_path)
    inventory = json.loads(inventory_path.read_text(encoding="utf-8"))
    inventory["models"][0]["required_files"]["sp_riv"] = ["../secret.sp.riv"]
    inventory_path.write_text(json.dumps(inventory, indent=2, sort_keys=True) + "\n", encoding="utf-8")

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

    error = json.loads(capsys.readouterr().err)
    assert exit_code == 1
    assert error["error_code"] in {"BASINS_REQUIRED_FILES_NON_CANONICAL", "BASINS_REGISTRY_SOURCE_MISMATCH"}
    assert error["model_id"] == model_id


def test_import_rejects_mutated_source_symlink_before_database(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _, input_dir, inventory_path, manifest_path, model_id = _write_registry_fixture(tmp_path)
    target = tmp_path / "external.mesh"
    target.write_text("external\n", encoding="utf-8")
    (input_dir / "alias-a.sp.mesh").unlink()
    (input_dir / "alias-a.sp.mesh").symlink_to(target)

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

    error = json.loads(capsys.readouterr().err)
    assert exit_code == 1
    assert error["error_code"] in {"BASINS_REGISTRY_PATH_UNSAFE", "BASINS_REGISTRY_CHECKSUM_CONFLICT"}
    assert error["model_id"] == model_id


def test_import_rejects_input_alias_directory_symlink_before_database(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    root, input_dir, inventory_path, manifest_path, model_id = _write_registry_fixture(tmp_path)
    external_model_dir = tmp_path / "external" / "basin-a"
    external_input_dir = _make_valid_model(external_model_dir, "alias-a", sp_segment_count=2)
    _copy_matching_fixture_payload(input_dir, external_input_dir)
    _replace_directory_with_symlink(input_dir, external_input_dir)

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

    error = json.loads(capsys.readouterr().err)
    assert exit_code == 1
    assert error["error_code"] == "BASINS_REGISTRY_PATH_UNSAFE"
    assert error["model_id"] == model_id
    assert error["path"] == str(root / "basin-a" / "input" / "alias-a")
    assert error["role"] == "shud_input_name"


def test_import_rejects_shud_evidence_replaced_between_validation_and_open(tmp_path: Path) -> None:
    _, input_dir, inventory_path, manifest_path, model_id = _write_registry_fixture(tmp_path)
    target = input_dir / "alias-a.sp.riv"
    replacement = tmp_path / "replacement.sp.riv"
    replacement.write_text("2\n", encoding="utf-8")
    mutated = False

    def hook(path: Path, role: str, phase: str) -> None:
        nonlocal mutated
        if path == target and role == "shud_evidence" and phase == "before_open" and not mutated:
            target.unlink()
            target.symlink_to(replacement)
            mutated = True

    basins_geometry._SAFE_OPEN_TEST_HOOK = hook
    try:
        with pytest.raises(BasinsRegistryImportError) as error:
            prepare_basins_import_sources(inventory_path=inventory_path, package_manifest_path=manifest_path)
    finally:
        basins_geometry._SAFE_OPEN_TEST_HOOK = None

    assert error.value.error_code == "BASINS_REGISTRY_PATH_UNSAFE"
    assert error.value.path == str(target)
    assert error.value.details["role"] == "shud_evidence"
    assert model_id


def test_import_rejects_checksum_file_replaced_between_validation_and_open(tmp_path: Path) -> None:
    _, input_dir, inventory_path, manifest_path, model_id = _write_registry_fixture(tmp_path)
    target = input_dir / "alias-a.sp.mesh"
    replacement = tmp_path / "replacement.sp.mesh"
    replacement.write_text("sp.mesh\n", encoding="utf-8")
    mutated = False

    def hook(path: Path, role: str, phase: str) -> None:
        nonlocal mutated
        if path == target and role == "checksum" and phase == "before_open" and not mutated:
            target.unlink()
            target.symlink_to(replacement)
            mutated = True

    basins_geometry._SAFE_OPEN_TEST_HOOK = hook
    try:
        with pytest.raises(BasinsRegistryImportError) as error:
            prepare_basins_import_sources(inventory_path=inventory_path, package_manifest_path=manifest_path)
    finally:
        basins_geometry._SAFE_OPEN_TEST_HOOK = None

    assert error.value.error_code == "BASINS_REGISTRY_PATH_UNSAFE"
    assert error.value.path == str(target)
    assert error.value.details["role"] == "checksum"
    assert model_id


def test_import_rejects_gis_sidecar_replaced_between_validation_and_reader(tmp_path: Path) -> None:
    _, input_dir, inventory_path, manifest_path, model_id = _write_registry_fixture(tmp_path)
    target = input_dir / "gis" / "domain.shp"
    replacement = tmp_path / "domain-replacement.shp"
    shutil.copy2(target, replacement)
    mutated = False

    def hook(path: Path, role: str, phase: str) -> None:
        nonlocal mutated
        if path == target and role == "gis_domain_shp" and phase == "before_open" and not mutated:
            target.unlink()
            target.symlink_to(replacement)
            mutated = True

    basins_geometry._SAFE_OPEN_TEST_HOOK = hook
    try:
        with pytest.raises(BasinsRegistryImportError) as error:
            prepare_basins_import_sources(inventory_path=inventory_path, package_manifest_path=manifest_path)
    finally:
        basins_geometry._SAFE_OPEN_TEST_HOOK = None

    assert error.value.error_code == "BASINS_REGISTRY_PATH_UNSAFE"
    assert error.value.path == str(target)
    assert error.value.details["role"] == "gis_domain_shp"
    assert model_id


def test_import_rejects_source_bytes_mutated_between_parse_and_validation(tmp_path: Path) -> None:
    _, input_dir, inventory_path, manifest_path, model_id = _write_registry_fixture(tmp_path)
    target = input_dir / "alias-a.sp.riv"
    original = target.read_text(encoding="utf-8")
    mutated = False

    def hook(path: Path, role: str, phase: str) -> None:
        nonlocal mutated
        if path == target and role == "shud_evidence" and phase == "after_read" and not mutated:
            target.write_text("999\n", encoding="utf-8")
            mutated = True

    basins_geometry._SAFE_OPEN_TEST_HOOK = hook
    try:
        with pytest.raises(BasinsRegistryImportError) as error:
            prepare_basins_import_sources(inventory_path=inventory_path, package_manifest_path=manifest_path)
    finally:
        basins_geometry._SAFE_OPEN_TEST_HOOK = None
        target.write_text(original, encoding="utf-8")

    assert error.value.error_code == "BASINS_REGISTRY_CHECKSUM_CONFLICT"
    assert error.value.details["relative_paths"] == ["alias-a.sp.riv"]
    assert model_id


def test_parser_rejects_projected_prj_with_epsg(tmp_path: Path) -> None:
    _, input_dir, _, _, model_id = _write_registry_fixture(tmp_path)
    projected = (
        'PROJCS["WGS_1984_Web_Mercator_Auxiliary_Sphere",'
        'GEOGCS["GCS_WGS_1984",DATUM["D_WGS_1984",'
        'SPHEROID["WGS_1984",6378137,298.257223563]],'
        'PRIMEM["Greenwich",0],UNIT["Degree",0.0174532925199433]],'
        'PROJECTION["Mercator_Auxiliary_Sphere"],AUTHORITY["EPSG","3857"]]\n'
    )
    (input_dir / "gis" / "domain.prj").write_text(projected, encoding="utf-8")
    inventory = discover_basins_inventory(tmp_path / "basins")
    model = inventory["models"][0]

    with pytest.raises(basins_geometry.BasinsGeometryError) as error:
        parse_basins_geometry(
            model_id=model_id,
            input_dir=input_dir,
            shud_input_name="alias-a",
            required_files=model["required_files"],
        )

    assert error.value.error_code == "BASINS_REGISTRY_GIS_CRS_UNSUPPORTED"


def test_parser_preserves_domain_polygon_holes(tmp_path: Path) -> None:
    _, input_dir, _, _, model_id = _write_registry_fixture(tmp_path, domain_with_hole=True)
    inventory = discover_basins_inventory(tmp_path / "basins")
    model = inventory["models"][0]

    parsed = parse_basins_geometry(
        model_id=model_id,
        input_dir=input_dir,
        shud_input_name="alias-a",
        required_files=model["required_files"],
    )

    assert parsed.domain_wkt.startswith("MULTIPOLYGON(((")
    assert ")), ((" not in parsed.domain_wkt
    assert "), (" in parsed.domain_wkt


def test_parser_enforces_resource_limits(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _, input_dir, _, _, model_id = _write_registry_fixture(tmp_path)
    inventory = discover_basins_inventory(tmp_path / "basins")
    model = inventory["models"][0]
    monkeypatch.setattr(basins_geometry, "MAX_BASINS_GIS_FEATURES", 1)

    with pytest.raises(basins_geometry.BasinsGeometryError) as error:
        parse_basins_geometry(
            model_id=model_id,
            input_dir=input_dir,
            shud_input_name="alias-a",
            required_files=model["required_files"],
        )

    assert error.value.error_code == "BASINS_REGISTRY_RESOURCE_LIMIT_EXCEEDED"
    assert error.value.details["resource"] == "features"


def test_parser_enforces_shud_evidence_resource_limits(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _, input_dir, _, _, model_id = _write_registry_fixture(tmp_path)
    inventory = discover_basins_inventory(tmp_path / "basins")
    model = inventory["models"][0]
    (input_dir / "alias-a.sp.riv").write_text("1 2\n3 4\n", encoding="utf-8")
    monkeypatch.setattr(basins_geometry, "MAX_BASINS_SHUD_EVIDENCE_LINES", 1)

    with pytest.raises(basins_geometry.BasinsGeometryError) as error:
        parse_basins_geometry(
            model_id=model_id,
            input_dir=input_dir,
            shud_input_name="alias-a",
            required_files=model["required_files"],
        )

    assert error.value.error_code == "BASINS_REGISTRY_RESOURCE_LIMIT_EXCEEDED"
    assert error.value.details == {"resource": "shud_evidence_lines", "count": 2, "limit": 1}


def test_parser_stops_at_declared_shud_count_header(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _, input_dir, _, _, model_id = _write_registry_fixture(tmp_path)
    inventory = discover_basins_inventory(tmp_path / "basins")
    model = inventory["models"][0]
    trailing_payload = "\n".join("1 2 3" for _ in range(20))
    (input_dir / "alias-a.sp.riv").write_text(f"2\n{trailing_payload}\n", encoding="utf-8")
    (input_dir / "alias-a.sp.rivseg").write_text(f"2\n{trailing_payload}\n", encoding="utf-8")
    monkeypatch.setattr(basins_geometry, "MAX_BASINS_SHUD_EVIDENCE_LINES", 1)

    parsed = parse_basins_geometry(
        model_id=model_id,
        input_dir=input_dir,
        shud_input_name="alias-a",
        required_files=model["required_files"],
    )

    assert parsed.segment_count == 2
    assert parsed.evidence_counts == {"sp_riv": 2, "sp_rivseg": 2}


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


def test_import_command_reports_missing_gis_directory_as_json(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _, input_dir, inventory_path, manifest_path, model_id = _write_registry_fixture(tmp_path)
    shutil.rmtree(input_dir / "gis")

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
    assert "Traceback" not in captured.err
    assert error["error_code"] == "BASINS_REGISTRY_GIS_SIDECAR_MISSING"
    assert error["model_id"] == model_id
    assert error["missing_sidecar"] == "gis/domain.shp"


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
                (f"{model_id}_seg_1", f"{model_id}_seg_2", model_id),
            )
            row = cursor.fetchone()
    assert row is not None
    assert row["active_flag"] is False
    assert row["resource_profile"]["package_checksum"] == "package-sha-1"
    assert row["resource_profile"]["basin_slug"] == "basin-a"
    assert row["segment_count"] == 2
    assert row["segment_rows"] == 2
    assert row["first_downstream"] == f"{model_id}_seg_2"
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
    assert segments["total"] == 2
    assert segments["feature_total"] == 2
    assert segments["features"][0]["geometry"]["type"] == "LineString"
    assert segments["features"][0]["properties"]["river_segment_id"] == f"{model_id}_seg_1"
    assert segments["features"][0]["properties"]["river_network_version_id"] == first["river_network_version_id"]
    assert segments["features"][0]["properties"]["basin_version_id"] == first["basin_version_id"]


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
def test_registry_import_conflicts_on_existing_river_segment_drift(
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

    with psycopg_connection(integration_database_url) as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                UPDATE core.river_segment
                SET length_m = length_m + 1
                WHERE river_network_version_id = %s
                  AND river_segment_id = %s
                """,
                ("basins_basin_a_rivnet_vbasins", f"{model_id}_seg_1"),
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

    store = PsycopgModelRegistryStore(integration_database_url)
    segments = store.list_river_segments(
        basin_version_id=report["basin_version_id"],
        river_network_version_id=report["river_network_version_id"],
        limit=5,
        offset=0,
    )
    assert segments["type"] == "FeatureCollection"
    assert segments["feature_total"] > 0
    assert segments["features"][0]["geometry"]["type"] == "LineString"
    assert segments["features"][0]["properties"]["river_segment_id"]
    assert segments["features"][0]["properties"]["river_network_version_id"] == report["river_network_version_id"]
    assert segments["features"][0]["properties"]["basin_version_id"] == report["basin_version_id"]


def _write_registry_fixture(
    tmp_path: Path,
    *,
    sp_segment_count: int = 2,
    domain_with_hole: bool = False,
) -> tuple[Path, Path, Path, Path, str]:
    root = tmp_path / "basins"
    input_dir = _make_valid_model(
        root / "basin-a",
        "alias-a",
        sp_segment_count=sp_segment_count,
        domain_with_hole=domain_with_hole,
    )
    inventory = discover_basins_inventory(root)
    inventory_path = tmp_path / "inventory.json"
    write_inventory(inventory, inventory_path)
    model = inventory["models"][0]
    model_id = model["model_id"]
    manifest = _package_manifest_for_model(model, model_id, inventory=inventory)
    manifest_path = tmp_path / "manifest.json"
    manifest_path.write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return root, input_dir, inventory_path, manifest_path, model_id


def _make_valid_model(
    model_dir: Path,
    input_name: str,
    *,
    sp_segment_count: int,
    domain_with_hole: bool = False,
) -> Path:
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
    _write_domain_shapefile(gis_dir / "domain", with_hole=domain_with_hole)
    _write_line_shapefile(gis_dir / "river")
    _write_line_shapefile(gis_dir / "seg")
    forcing = model_dir / "forcing"
    forcing.mkdir()
    (forcing / "X000001.csv").write_text("time,value\n2026-01-01,1\n", encoding="utf-8")
    return input_dir


def _copy_matching_fixture_payload(source: Path, destination: Path) -> None:
    if destination.exists():
        shutil.rmtree(destination)
    shutil.copytree(source, destination)


def _replace_directory_with_symlink(path: Path, target: Path) -> None:
    shutil.rmtree(path)
    path.symlink_to(target, target_is_directory=True)


def _write_domain_shapefile(base: Path, *, with_hole: bool = False) -> None:
    import shapefile

    writer = shapefile.Writer(str(base), shapeType=shapefile.POLYGON)
    writer.field("ID", "N")
    rings = [[[100.0, 30.0], [101.0, 30.0], [101.0, 31.0], [100.0, 31.0], [100.0, 30.0]]]
    if with_hole:
        rings.append([[100.2, 30.2], [100.2, 30.8], [100.8, 30.8], [100.8, 30.2], [100.2, 30.2]])
    writer.poly(rings)
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


def _package_manifest_for_model(
    model: dict[str, Any],
    model_id: str,
    *,
    inventory: dict[str, Any],
) -> dict[str, Any]:
    version = "vbasins-test"
    package_uri = f"s3://nhms/models/{model_id}/{version}/package/"
    included_files = [
        {
            "relative_path": relative_path,
            "object_uri": package_uri + relative_path,
            "size_bytes": (Path(model["input_dir"]) / relative_path).stat().st_size,
            "sha256": checksum,
            "role": "gis" if relative_path.startswith("gis/") else "runtime_input",
        }
        for relative_path, checksum in sorted(model["checksums"].items())
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
        "source_inventory_checksum": _sha256_inventory_document(inventory),
        "source_inventory_schema_version": "basins.discovery.v1",
        "source_path": model["source_path"],
        "resolved_source_path": model["resolved_source_path"],
        "source_is_symlink": False,
        "included_files": included_files,
        "forcing": {"policy": "excluded_by_default", "csv_count": 1},
        "calibration": {"source_count": 0, "included_count": 0},
        "created_at": "2026-05-16T00:00:00Z",
    }


def _sha256_inventory_document(inventory: dict[str, Any]) -> str:
    content = (json.dumps(inventory, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode("utf-8")
    return hashlib.sha256(content).hexdigest()


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _invoke_click(argv: list[str]) -> int:
    try:
        return _click_main(argv)
    except SystemExit as error:
        if isinstance(error.code, int):
            return error.code
        return 1
