from __future__ import annotations

import hashlib
import json
import os
import shutil
from pathlib import Path
from typing import Any

import pytest
from psycopg2.extras import Json, execute_values
from pyproj import Transformer

import workers.model_registry.basins_geometry as basins_geometry
from packages.common.auth_policy import cli_policy_decision_from_evidence
from packages.common.model_registry import PsycopgModelRegistryStore
from tests.integration_helpers import apply_migrations_from_zero, psycopg_connection
from workers.model_registry.basins_discovery import discover_basins_inventory, write_inventory
from workers.model_registry.basins_geometry import parse_basins_geometry
from workers.model_registry.basins_registry_import import (
    BasinsRegistryImportError,
    _backfill_output_segment_geometry,
    _ensure_output_river_segments,
    _output_river_segment_rows,
    _resource_profile,
    import_basins_registry,
    prepare_basins_import_sources,
)
from workers.model_registry.cli import _argparse_main, _click_main

_CLI_MODEL_ADMIN_AUTH_ARGS = ["--auth-actor-id", "cli-model-admin", "--auth-role", "model_admin"]
_PUBLIC_IMPORT_UNKNOWN_TARGET_ID = "unknown"


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
    assert parsed.evidence_counts == {
        "river_count": 2,
        "river_columns": 6,
        "rivseg_segment_count": 2,
        "rivseg_columns": 4,
    }
    assert [segment.segment_order for segment in parsed.river_segments] == [1, 2]
    assert parsed.river_segments[0].downstream_segment_id == f"{model_id}_seg_2"
    assert parsed.river_segments[1].downstream_segment_id is None
    assert parsed.river_segments[0].properties["source_downstream_segment_id"] == "2"
    # geom is gap-split MultiLineString now (a seamless reach is a one-part MLS).
    assert parsed.river_segments[0].geom_wkt.startswith("MULTILINESTRING")


def test_parser_splits_cross_gap_record_into_multilinestring_parts(tmp_path: Path) -> None:
    # One source record whose vertices contain a real >300m gap (78m mesh edges with
    # one ~1668m jump). The parser must emit a 2-part MultiLineString where the gap
    # endpoints land in different parts -- the cross-gap straight bridge is in neither.
    root, input_dir, inventory_path, manifest_path, model_id = _write_registry_fixture(
        tmp_path, sp_segment_count=1
    )
    del root, inventory_path, manifest_path
    _write_line_shapefile(
        input_dir / "gis" / "seg",
        points=[
            [
                (100.0, 30.0),
                (100.0, 30.0007),
                (100.0, 30.0014),
                (100.0, 30.0164),
                (100.0, 30.0171),
            ]
        ],
        records=[(1, 1, 0, 100.0)],
    )
    inventory = discover_basins_inventory(tmp_path / "basins")
    model = inventory["models"][0]

    parsed = parse_basins_geometry(
        model_id=model_id,
        input_dir=input_dir,
        shud_input_name="alias-a",
        required_files=model["required_files"],
    )

    assert parsed.segment_count == 1
    wkt = parsed.river_segments[0].geom_wkt
    assert wkt.startswith("MULTILINESTRING(")
    # exactly two parts (one "),(") separating the gap endpoints
    assert wkt.replace(" ", "").count("),(") == 1
    # the gap endpoints (30.0014 and 30.0164) are never adjacent inside one ring
    compact = wkt.replace(", ", ",")
    assert "30.0014,100 30.0164" not in compact


def test_parser_disambiguates_duplicate_raw_segment_ids(tmp_path: Path) -> None:
    root, input_dir, inventory_path, manifest_path, model_id = _write_registry_fixture(tmp_path, sp_segment_count=3)
    del root, inventory_path, manifest_path
    _write_line_shapefile(
        input_dir / "gis" / "seg",
        points=[
            [(100.1, 30.1), (100.3, 30.3)],
            [(100.3, 30.3), (100.5, 30.5)],
            [(100.5, 30.5), (100.7, 30.7)],
        ],
        records=[
            (1, 1, 2, 100.0),
            (1, 2, 2, 200.0),
            (2, 3, 1, 300.0),
        ],
    )
    inventory = discover_basins_inventory(tmp_path / "basins")
    model = inventory["models"][0]

    parsed = parse_basins_geometry(
        model_id=model_id,
        input_dir=input_dir,
        shud_input_name="alias-a",
        required_files=model["required_files"],
    )

    ids = [segment.river_segment_id for segment in parsed.river_segments]
    assert len(ids) == 3
    assert len(set(ids)) == 3
    assert ids[:2] == [
        f"{model_id}_seg_1_ord_000001_rec_000001",
        f"{model_id}_seg_1_ord_000002_rec_000002",
    ]
    assert ids[2] == f"{model_id}_seg_2"
    assert parsed.river_segments[0].downstream_segment_id == f"{model_id}_seg_2"
    assert parsed.river_segments[1].downstream_segment_id == f"{model_id}_seg_2"
    assert parsed.river_segments[2].downstream_segment_id is None
    assert parsed.river_segments[0].properties["source_raw_segment_id"] == 1
    assert parsed.river_segments[0].properties["source_stable_segment_id_base"] == f"{model_id}_seg_1"
    assert parsed.river_segments[0].properties["source_duplicate_segment_id_disambiguated"] is True
    assert "source_duplicate_segment_id_disambiguated" not in parsed.river_segments[2].properties


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
            *_CLI_MODEL_ADMIN_AUTH_ARGS,
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
            *_CLI_MODEL_ADMIN_AUTH_ARGS,
        ]
    )

    error = json.loads(capsys.readouterr().err)
    assert exit_code == 1
    assert error["error_code"] == "BASINS_REGISTRY_SOURCE_MISMATCH"
    assert error["model_id"] == model_id
    assert error["fields"] == ["source_inventory_checksum"]


def test_manifest_uri_is_required_before_database(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _, _, inventory_path, manifest_path, model_id = _write_registry_fixture(tmp_path)
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    del manifest["manifest_uri"]
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
            *_CLI_MODEL_ADMIN_AUTH_ARGS,
        ]
    )

    error = json.loads(capsys.readouterr().err)
    assert exit_code == 1
    assert error["error_code"] == "BASINS_REGISTRY_PACKAGE_MANIFEST_INVALID"
    assert error["model_id"] == model_id


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
            *_CLI_MODEL_ADMIN_AUTH_ARGS,
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
            *_CLI_MODEL_ADMIN_AUTH_ARGS,
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
            *_CLI_MODEL_ADMIN_AUTH_ARGS,
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
            *_CLI_MODEL_ADMIN_AUTH_ARGS,
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
            *_CLI_MODEL_ADMIN_AUTH_ARGS,
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


def test_import_rejects_gis_sidecar_growing_after_precheck_before_buffering(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _, input_dir, inventory_path, manifest_path, model_id = _write_registry_fixture(tmp_path)
    target = input_dir / "gis" / "domain.shp"
    original_limit = max(path.stat().st_size for path in (input_dir / "gis").iterdir() if path.is_file()) + 1
    replacement = tmp_path / "oversized-domain.shp"
    replacement.write_bytes(b"0" * (original_limit + 1))
    mutated = False
    monkeypatch.setattr(basins_geometry, "MAX_BASINS_GIS_SIDECAR_BYTES", original_limit)

    def hook(path: Path, role: str, phase: str) -> None:
        nonlocal mutated
        if path == input_dir and role == "gis_sidecar_limits" and phase == "after_precheck" and not mutated:
            target.unlink()
            shutil.copy2(replacement, target)
            mutated = True

    basins_geometry._SAFE_OPEN_TEST_HOOK = hook
    try:
        with pytest.raises(BasinsRegistryImportError) as error:
            prepare_basins_import_sources(inventory_path=inventory_path, package_manifest_path=manifest_path)
    finally:
        basins_geometry._SAFE_OPEN_TEST_HOOK = None

    assert error.value.error_code == "BASINS_REGISTRY_RESOURCE_LIMIT_EXCEEDED"
    assert error.value.path == str(target)
    assert error.value.details["resource"] == "gis_sidecar_bytes"
    assert error.value.details["count"] > error.value.details["limit"]
    assert model_id


def test_import_rejects_input_directory_swap_after_input_dir_resolution(tmp_path: Path) -> None:
    _, input_dir, inventory_path, manifest_path, model_id = _write_registry_fixture(tmp_path)
    external_model_dir = tmp_path / "external-after-prepare" / "basin-a"
    external_input_dir = _make_valid_model(external_model_dir, "alias-a", sp_segment_count=2)
    _copy_matching_fixture_payload(input_dir, external_input_dir)
    mutated = False

    def hook(path: Path, role: str, phase: str) -> None:
        nonlocal mutated
        if path == input_dir and role == "shud_input_name" and phase == "before_parse" and not mutated:
            _replace_directory_with_symlink(input_dir, external_input_dir)
            mutated = True

    basins_geometry._SAFE_OPEN_TEST_HOOK = hook
    try:
        with pytest.raises(BasinsRegistryImportError) as error:
            prepare_basins_import_sources(inventory_path=inventory_path, package_manifest_path=manifest_path)
    finally:
        basins_geometry._SAFE_OPEN_TEST_HOOK = None

    assert error.value.error_code == "BASINS_REGISTRY_PATH_UNSAFE"
    assert error.value.path == str(input_dir)
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


def test_import_accepts_relative_inventory_paths_across_cwd(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    root = workspace / "basins"
    _make_valid_model(root / "basin-a", "alias-a", sp_segment_count=2)
    monkeypatch.chdir(workspace)
    inventory = discover_basins_inventory(Path("basins"))
    inventory_path = workspace / "inventory.json"
    write_inventory(inventory, inventory_path)
    model = inventory["models"][0]
    model_id = model["model_id"]
    manifest = _package_manifest_for_model(model, model_id, inventory=inventory)
    manifest_path = workspace / "manifest.json"
    manifest_path.write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    other_cwd = tmp_path / "other-cwd"
    other_cwd.mkdir()
    monkeypatch.chdir(other_cwd)

    sources = prepare_basins_import_sources(inventory_path=inventory_path, package_manifest_path=manifest_path)

    assert sources.ids["model_id"] == model_id
    assert sources.input_dir.path == (root / "basin-a" / "input" / "alias-a").resolve()


def test_mesh_checksum_uses_manifest_when_inventory_checksum_is_absent(tmp_path: Path) -> None:
    _, input_dir, inventory_path, manifest_path, model_id = _write_registry_fixture(tmp_path)
    inventory = json.loads(inventory_path.read_text(encoding="utf-8"))
    del inventory["models"][0]["checksums"]["alias-a.sp.mesh"]
    raw_inventory = (json.dumps(inventory, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode("utf-8")
    inventory_path.write_bytes(raw_inventory)
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["source_inventory_checksum"] = hashlib.sha256(raw_inventory).hexdigest()
    manifest_path.write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8")

    sources = prepare_basins_import_sources(inventory_path=inventory_path, package_manifest_path=manifest_path)

    assert sources.ids["model_id"] == model_id
    assert sources.manifest_checksums["alias-a.sp.mesh"] == _sha256_file(input_dir / "alias-a.sp.mesh")
    assert (
        basins_geometry.safe_basins_file_sha256(input_dir / "alias-a.sp.mesh", input_dir)
        == sources.manifest_checksums["alias-a.sp.mesh"]
    )


def test_parser_enforces_gis_sidecar_byte_limit_before_buffering(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _, input_dir, _, _, model_id = _write_registry_fixture(tmp_path)
    inventory = discover_basins_inventory(tmp_path / "basins")
    model = inventory["models"][0]
    monkeypatch.setattr(basins_geometry, "MAX_BASINS_GIS_SIDECAR_BYTES", 1)

    with pytest.raises(basins_geometry.BasinsGeometryError) as error:
        parse_basins_geometry(
            model_id=model_id,
            input_dir=input_dir,
            shud_input_name="alias-a",
            required_files=model["required_files"],
        )

    assert error.value.error_code == "BASINS_REGISTRY_RESOURCE_LIMIT_EXCEEDED"
    assert error.value.details["resource"] == "gis_sidecar_bytes"
    assert error.value.details["count"] > error.value.details["limit"]


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


def test_parser_reprojects_basins_albers_to_lon_lat(tmp_path: Path) -> None:
    _, input_dir, _, _, model_id = _write_registry_fixture(tmp_path)
    albers_prj = (
        'PROJCS["unknown",GEOGCS["unknown",DATUM["WGS_1984",'
        'SPHEROID["WGS 84",6378137,298.257223563,AUTHORITY["EPSG","7030"]],'
        'AUTHORITY["EPSG","6326"]],PRIMEM["Greenwich",0,AUTHORITY["EPSG","8901"]],'
        'UNIT["degree",0.0174532925199433,AUTHORITY["EPSG","9122"]]],'
        'PROJECTION["Albers_Conic_Equal_Area"],PARAMETER["latitude_of_center",0],'
        'PARAMETER["longitude_of_center",102],PARAMETER["standard_parallel_1",34.35],'
        'PARAMETER["standard_parallel_2",33.85],PARAMETER["false_easting",0],'
        'PARAMETER["false_northing",0],UNIT["metre",1,AUTHORITY["EPSG","9001"]],'
        'AXIS["Easting",EAST],AXIS["Northing",NORTH]]\n'
    )
    transformer = Transformer.from_crs("EPSG:4490", albers_prj, always_xy=True)
    _write_domain_shapefile(
        input_dir / "gis" / "domain",
        points=[transformer.transform(x, y) for x, y in [(100.0, 30.0), (101.0, 30.0), (101.0, 31.0), (100.0, 31.0)]],
        prj_text=albers_prj,
    )
    _write_line_shapefile(
        input_dir / "gis" / "river",
        points=[
            [transformer.transform(100.1, 30.1), transformer.transform(100.5, 30.4)],
            [transformer.transform(100.5, 30.4), transformer.transform(100.8, 30.8)],
        ],
        prj_text=albers_prj,
    )
    _write_line_shapefile(
        input_dir / "gis" / "seg",
        points=[
            [transformer.transform(100.1, 30.1), transformer.transform(100.5, 30.4)],
            [transformer.transform(100.5, 30.4), transformer.transform(100.8, 30.8)],
        ],
        prj_text=albers_prj,
    )
    inventory = discover_basins_inventory(tmp_path / "basins")
    model = inventory["models"][0]

    parsed = parse_basins_geometry(
        model_id=model_id,
        input_dir=input_dir,
        shud_input_name="alias-a",
        required_files=model["required_files"],
    )

    assert "100.1" in parsed.river_segments[0].geom_wkt
    assert "30.1" in parsed.river_segments[0].geom_wkt
    assert "3600000" not in parsed.domain_wkt
    assert parsed.river_segments[0].properties["source_crs_projected"] is True
    assert parsed.river_segments[0].properties["source_projection_method"] == "albers equal area"


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
    (input_dir / "alias-a.sp.riv").write_text("# header\n1 2\n", encoding="utf-8")
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
    assert parsed.evidence_counts["river_count"] == 2
    assert parsed.evidence_counts["rivseg_segment_count"] == 2


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
            *_CLI_MODEL_ADMIN_AUTH_ARGS,
        ]
    )

    captured = capsys.readouterr()
    error = json.loads(captured.err)
    assert exit_code == 1
    assert captured.out == ""
    assert error["error_code"] == "BASINS_REGISTRY_DATABASE_URL_MISSING"
    assert error["model_id"] == model_id


def test_argparse_import_basins_registry_without_cli_auth_rejects_before_preparation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _, _, inventory_path, manifest_path, _ = _write_registry_fixture(tmp_path)
    report_path = tmp_path / "import-report.json"

    def fail_model_id_from_manifest(*_args: Any, **_kwargs: Any) -> str:
        raise AssertionError("_model_id_from_manifest must not run before missing auth is denied")

    def fail_read_manifest(*_args: Any, **_kwargs: Any) -> dict[str, Any]:
        raise AssertionError("service manifest reader must not run before missing auth is denied")

    def fail_prepare(*_args: Any, **_kwargs: Any) -> None:
        raise AssertionError("_prepare_sources must not run before denied auth policy")

    monkeypatch.setattr("workers.model_registry.cli._model_id_from_manifest", fail_model_id_from_manifest)
    monkeypatch.setattr("workers.model_registry.basins_registry_import._read_json_object", fail_read_manifest)
    monkeypatch.setattr("workers.model_registry.basins_registry_import._prepare_sources", fail_prepare)

    exit_code = _argparse_main(
        [
            "import-basins-registry",
            "--inventory",
            str(inventory_path),
            "--package-manifest",
            str(manifest_path),
            "--database-url",
            "postgresql://nhms:nhms@localhost:1/nhms",
            "--output",
            str(report_path),
        ]
    )

    captured = capsys.readouterr()
    error = json.loads(captured.err)
    assert exit_code == 1
    assert captured.out == ""
    assert error["error_code"] == "AUTH_REQUIRED"
    assert error["model_id"] == _PUBLIC_IMPORT_UNKNOWN_TARGET_ID
    assert error["policy_decision"]["target_id"] == _PUBLIC_IMPORT_UNKNOWN_TARGET_ID
    assert not report_path.exists()


def test_argparse_import_basins_registry_saml_blocks_cli_auth_before_preparation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setenv("AUTH_BACKEND", "saml")
    _, _, inventory_path, manifest_path, _ = _write_registry_fixture(tmp_path)
    report_path = tmp_path / "import-report.json"

    def fail_model_id_from_manifest(*_args: Any, **_kwargs: Any) -> str:
        raise AssertionError("_model_id_from_manifest must not run before release-blocked auth is denied")

    def fail_read_manifest(*_args: Any, **_kwargs: Any) -> dict[str, Any]:
        raise AssertionError("service manifest reader must not run before release-blocked auth is denied")

    def fail_prepare(*_args: Any, **_kwargs: Any) -> None:
        raise AssertionError("_prepare_sources must not run before release-blocked auth policy")

    monkeypatch.setattr("workers.model_registry.cli._model_id_from_manifest", fail_model_id_from_manifest)
    monkeypatch.setattr("workers.model_registry.basins_registry_import._read_json_object", fail_read_manifest)
    monkeypatch.setattr("workers.model_registry.basins_registry_import._prepare_sources", fail_prepare)

    exit_code = _argparse_main(
        [
            "import-basins-registry",
            "--inventory",
            str(inventory_path),
            "--package-manifest",
            str(manifest_path),
            "--database-url",
            "postgresql://nhms:nhms@localhost:1/nhms",
            "--output",
            str(report_path),
            "--auth-actor-id",
            "cli-model-admin",
            "--auth-role",
            "model_admin",
        ]
    )

    captured = capsys.readouterr()
    error = json.loads(captured.err)
    assert exit_code == 1
    assert captured.out == ""
    assert error["error_code"] == "RELEASE_BLOCKED"
    assert error["model_id"] == _PUBLIC_IMPORT_UNKNOWN_TARGET_ID
    assert error["policy_decision"]["target_id"] == _PUBLIC_IMPORT_UNKNOWN_TARGET_ID
    assert error["policy_decision"]["auth_mode"] == "cli_dev_test_blocked_by_auth_backend_saml"
    assert error["policy_decision"]["no_mutation_expected"] is True
    assert not report_path.exists()


def test_click_import_basins_registry_without_cli_auth_rejects_before_manifest_read(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _, _, inventory_path, manifest_path, _ = _write_registry_fixture(tmp_path)
    report_path = tmp_path / "import-report.json"

    def fail_model_id_from_manifest(*_args: Any, **_kwargs: Any) -> str:
        raise AssertionError("_model_id_from_manifest must not run before missing auth is denied")

    def fail_read_manifest(*_args: Any, **_kwargs: Any) -> dict[str, Any]:
        raise AssertionError("service manifest reader must not run before missing auth is denied")

    def fail_prepare(*_args: Any, **_kwargs: Any) -> None:
        raise AssertionError("_prepare_sources must not run before denied auth policy")

    monkeypatch.setattr("workers.model_registry.cli._model_id_from_manifest", fail_model_id_from_manifest)
    monkeypatch.setattr("workers.model_registry.basins_registry_import._read_json_object", fail_read_manifest)
    monkeypatch.setattr("workers.model_registry.basins_registry_import._prepare_sources", fail_prepare)

    exit_code = _invoke_click(
        [
            "import-basins-registry",
            "--inventory",
            str(inventory_path),
            "--package-manifest",
            str(manifest_path),
            "--database-url",
            "postgresql://nhms:nhms@localhost:1/nhms",
            "--output",
            str(report_path),
        ]
    )

    captured = capsys.readouterr()
    error = json.loads(captured.err)
    assert exit_code == 1
    assert captured.out == ""
    assert error["error_code"] == "AUTH_REQUIRED"
    assert error["model_id"] == _PUBLIC_IMPORT_UNKNOWN_TARGET_ID
    assert error["policy_decision"]["target_id"] == _PUBLIC_IMPORT_UNKNOWN_TARGET_ID
    assert not report_path.exists()


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
            *_CLI_MODEL_ADMIN_AUTH_ARGS,
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
            *_CLI_MODEL_ADMIN_AUTH_ARGS,
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
            *_CLI_MODEL_ADMIN_AUTH_ARGS,
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
    assert error["evidence"] == "rivseg_segment_count"
    assert error["path"] == str(input_dir / "alias-a.sp.rivseg")


def test_import_accepts_sp_riv_river_count_different_from_rivseg_segments(tmp_path: Path) -> None:
    _, _, inventory_path, manifest_path, model_id = _write_registry_fixture(
        tmp_path,
        sp_river_count=1,
        sp_segment_count=2,
    )

    sources = prepare_basins_import_sources(inventory_path=inventory_path, package_manifest_path=manifest_path)

    assert sources.ids["model_id"] == model_id
    assert sources.geometry.segment_count == 2
    assert sources.geometry.evidence_counts["river_count"] == 1
    assert sources.geometry.evidence_counts["rivseg_segment_count"] == 2


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


@pytest.mark.integration
def test_import_basins_registry_without_policy_rejects_before_writes(
    tmp_path: Path,
    integration_database_url: str,
) -> None:
    apply_migrations_from_zero(integration_database_url)
    _, _, inventory_path, manifest_path, model_id = _write_registry_fixture(
        tmp_path,
        basin_slug="basin-a-no-direct-auth",
    )

    with pytest.raises(BasinsRegistryImportError) as exc_info:
        import_basins_registry(
            inventory_path=inventory_path,
            package_manifest_path=manifest_path,
            database_url=integration_database_url,
        )

    assert exc_info.value.error_code == "AUTH_REQUIRED"
    assert exc_info.value.details["no_mutation_expected"] is True
    assert exc_info.value.details["policy_decision"]["action_id"] == "models.switch_version"
    assert exc_info.value.details["policy_decision"]["target_type"] == "model_registry"
    assert exc_info.value.model_id == _PUBLIC_IMPORT_UNKNOWN_TARGET_ID
    assert exc_info.value.details["policy_decision"]["target_id"] == _PUBLIC_IMPORT_UNKNOWN_TARGET_ID
    _assert_registry_fixture_rows_absent(integration_database_url, inventory_path, model_id)


def test_import_basins_registry_without_policy_rejects_before_manifest_read(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    inventory_path = tmp_path / "inventory.json"
    manifest_path = tmp_path / "attacker-manifest.json"

    def fail_read_manifest(*_args: Any, **_kwargs: Any) -> dict[str, Any]:
        raise AssertionError("service manifest reader must not run before missing auth is denied")

    def fail_prepare(*_args: Any, **_kwargs: Any) -> None:
        raise AssertionError("_prepare_sources must not run before missing auth is denied")

    monkeypatch.setattr("workers.model_registry.basins_registry_import._read_json_object", fail_read_manifest)
    monkeypatch.setattr("workers.model_registry.basins_registry_import._prepare_sources", fail_prepare)

    with pytest.raises(BasinsRegistryImportError) as exc_info:
        import_basins_registry(
            inventory_path=inventory_path,
            package_manifest_path=manifest_path,
            database_url="postgresql://nhms:nhms@localhost:1/nhms",
        )

    assert exc_info.value.error_code == "AUTH_REQUIRED"
    assert exc_info.value.model_id == _PUBLIC_IMPORT_UNKNOWN_TARGET_ID
    assert exc_info.value.details["policy_decision"]["target_id"] == _PUBLIC_IMPORT_UNKNOWN_TARGET_ID


def test_import_basins_registry_release_blocked_rejects_before_manifest_read(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    inventory_path = tmp_path / "inventory.json"
    manifest_path = tmp_path / "attacker-manifest.json"
    policy_decision = cli_policy_decision_from_evidence(
        "models.switch_version",
        target_type="model_registry",
        target_id=_PUBLIC_IMPORT_UNKNOWN_TARGET_ID,
        actor_id="cli-model-admin",
        roles=("model_admin",),
        env={"AUTH_BACKEND": "saml"},
    )

    def fail_read_manifest(*_args: Any, **_kwargs: Any) -> dict[str, Any]:
        raise AssertionError("service manifest reader must not run before release-blocked auth is denied")

    def fail_prepare(*_args: Any, **_kwargs: Any) -> None:
        raise AssertionError("_prepare_sources must not run before release-blocked auth is denied")

    monkeypatch.setattr("workers.model_registry.basins_registry_import._read_json_object", fail_read_manifest)
    monkeypatch.setattr("workers.model_registry.basins_registry_import._prepare_sources", fail_prepare)

    with pytest.raises(BasinsRegistryImportError) as exc_info:
        import_basins_registry(
            inventory_path=inventory_path,
            package_manifest_path=manifest_path,
            database_url="postgresql://nhms:nhms@localhost:1/nhms",
            policy_decision=policy_decision,
        )

    assert exc_info.value.error_code == "RELEASE_BLOCKED"
    assert exc_info.value.model_id == _PUBLIC_IMPORT_UNKNOWN_TARGET_ID
    assert exc_info.value.details["policy_decision"]["target_id"] == _PUBLIC_IMPORT_UNKNOWN_TARGET_ID
    assert exc_info.value.details["policy_decision"]["decision"] == "release_blocked"


@pytest.mark.parametrize(
    ("action_id", "target_type", "target_id"),
    [
        ("models.activate", "model_registry", _PUBLIC_IMPORT_UNKNOWN_TARGET_ID),
        ("models.switch_version", "model_instance", _PUBLIC_IMPORT_UNKNOWN_TARGET_ID),
        ("models.switch_version", "model_registry", "basins-other-model"),
    ],
)
def test_import_basins_registry_misbound_allow_rejects_before_manifest_read(
    action_id: str,
    target_type: str,
    target_id: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    inventory_path = tmp_path / "inventory.json"
    manifest_path = tmp_path / "attacker-manifest.json"
    policy_decision = cli_policy_decision_from_evidence(
        action_id,
        target_type=target_type,
        target_id=target_id,
        actor_id="cli-model-admin",
        roles=("model_admin",),
    )

    def fail_read_manifest(*_args: Any, **_kwargs: Any) -> dict[str, Any]:
        raise AssertionError("service manifest reader must not run before misbound auth is denied")

    def fail_prepare(*_args: Any, **_kwargs: Any) -> None:
        raise AssertionError("_prepare_sources must not run before misbound auth is denied")

    monkeypatch.setattr("workers.model_registry.basins_registry_import._read_json_object", fail_read_manifest)
    monkeypatch.setattr("workers.model_registry.basins_registry_import._prepare_sources", fail_prepare)

    with pytest.raises(BasinsRegistryImportError) as exc_info:
        import_basins_registry(
            inventory_path=inventory_path,
            package_manifest_path=manifest_path,
            database_url="postgresql://nhms:nhms@localhost:1/nhms",
            policy_decision=policy_decision,
        )

    assert policy_decision is not None
    assert policy_decision.decision == "allow"
    assert exc_info.value.error_code == "RBAC_FORBIDDEN"
    assert exc_info.value.model_id == _PUBLIC_IMPORT_UNKNOWN_TARGET_ID
    assert exc_info.value.details["no_mutation_expected"] is True
    assert exc_info.value.details["policy_decision"]["action_id"] == "models.switch_version"
    assert exc_info.value.details["policy_decision"]["target_type"] == "model_registry"
    assert exc_info.value.details["policy_decision"]["target_id"] == _PUBLIC_IMPORT_UNKNOWN_TARGET_ID
    assert exc_info.value.details["policy_decision"]["decision"] == "deny"


@pytest.mark.integration
def test_argparse_import_basins_registry_without_cli_auth_rejects_without_report_or_rows(
    tmp_path: Path,
    integration_database_url: str,
    capsys: pytest.CaptureFixture[str],
) -> None:
    apply_migrations_from_zero(integration_database_url)
    _, _, inventory_path, manifest_path, model_id = _write_registry_fixture(
        tmp_path,
        basin_slug="basin-a-argparse-no-auth",
    )
    report_path = tmp_path / "import-report.json"

    exit_code = _argparse_main(
        [
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
    )

    captured = capsys.readouterr()
    error = json.loads(captured.err)
    assert exit_code == 1
    assert captured.out == ""
    assert error["error_code"] == "AUTH_REQUIRED"
    assert error["policy_decision"]["target_id"] == _PUBLIC_IMPORT_UNKNOWN_TARGET_ID
    assert not report_path.exists()
    _assert_registry_fixture_rows_absent(integration_database_url, inventory_path, model_id)


@pytest.mark.integration
def test_click_import_basins_registry_without_cli_auth_rejects_without_report_or_rows(
    tmp_path: Path,
    integration_database_url: str,
    capsys: pytest.CaptureFixture[str],
) -> None:
    apply_migrations_from_zero(integration_database_url)
    _, _, inventory_path, manifest_path, model_id = _write_registry_fixture(
        tmp_path,
        basin_slug="basin-a-click-no-auth",
    )
    report_path = tmp_path / "import-report.json"

    exit_code = _invoke_click(
        [
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
    )

    captured = capsys.readouterr()
    error = json.loads(captured.err)
    assert exit_code == 1
    assert captured.out == ""
    assert error["error_code"] == "AUTH_REQUIRED"
    assert error["policy_decision"]["target_id"] == _PUBLIC_IMPORT_UNKNOWN_TARGET_ID
    assert not report_path.exists()
    _assert_registry_fixture_rows_absent(integration_database_url, inventory_path, model_id)


@pytest.mark.integration
def test_argparse_import_basins_registry_with_cli_model_admin_policy_imports_and_reports_auth(
    tmp_path: Path,
    integration_database_url: str,
    capsys: pytest.CaptureFixture[str],
) -> None:
    apply_migrations_from_zero(integration_database_url)
    _, _, inventory_path, manifest_path, model_id = _write_registry_fixture(
        tmp_path,
        basin_slug="basin-a-cli-auth",
    )
    report_path = tmp_path / "import-report.json"

    exit_code = _argparse_main(
        [
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
    )

    report = json.loads(capsys.readouterr().out)
    written = json.loads(report_path.read_text(encoding="utf-8"))
    decision = report["auth_policy_decision"]
    assert exit_code == 0
    assert report["status"] == "imported"
    assert written["auth_policy_decision"] == decision
    assert decision["actor_id"] == "cli-model-admin"
    assert decision["roles"] == ["model_admin"]
    assert decision["action_id"] == "models.switch_version"
    assert decision["target_type"] == "model_registry"
    assert decision["target_id"] == model_id
    assert decision["decision"] == "allow"
    assert decision["execution_mode"] == "backend_route_executed"


@pytest.mark.integration
def test_argparse_import_basins_registry_production_mode_blocks_cli_auth_without_report_or_rows(
    tmp_path: Path,
    integration_database_url: str,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("NHMS_AUTH_MODE", "production")
    apply_migrations_from_zero(integration_database_url)
    _, _, inventory_path, manifest_path, model_id = _write_registry_fixture(
        tmp_path,
        basin_slug="basin-a-prod-cli-blocked",
    )
    report_path = tmp_path / "import-report.json"

    exit_code = _argparse_main(
        [
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
    )

    captured = capsys.readouterr()
    error = json.loads(captured.err)
    assert exit_code == 1
    assert captured.out == ""
    assert error["error_code"] == "RELEASE_BLOCKED"
    assert error["policy_decision"]["target_id"] == _PUBLIC_IMPORT_UNKNOWN_TARGET_ID
    assert error["policy_decision"]["decision"] == "release_blocked"
    assert error["policy_decision"]["no_mutation_expected"] is True
    assert not report_path.exists()
    _assert_registry_fixture_rows_absent(integration_database_url, inventory_path, model_id)


def test_prepare_import_sources_does_not_need_data_basins_default(tmp_path: Path) -> None:
    _, _, inventory_path, manifest_path, model_id = _write_registry_fixture(tmp_path)

    sources = prepare_basins_import_sources(inventory_path=inventory_path, package_manifest_path=manifest_path)

    assert sources.ids["model_id"] == model_id
    assert sources.source_root == (tmp_path / "basins" / "basin-a").resolve()


def test_parsed_geometry_exposes_sp_riv_output_segment_count_distinct_from_geometry(tmp_path: Path) -> None:
    _, input_dir, _, _, model_id = _write_registry_fixture(
        tmp_path,
        sp_river_count=3,
        sp_segment_count=5,
    )
    _write_line_shapefile(
        input_dir / "gis" / "seg",
        points=[
            [(100.1, 30.1), (100.2, 30.2)],
            [(100.2, 30.2), (100.3, 30.3)],
            [(100.3, 30.3), (100.4, 30.4)],
            [(100.4, 30.4), (100.5, 30.5)],
            [(100.5, 30.5), (100.6, 30.6)],
        ],
        records=[
            (1, 1, 2, 100.0),
            (2, 2, 3, 100.0),
            (3, 3, 4, 100.0),
            (4, 4, 5, 100.0),
            (5, 5, 0, 100.0),
        ],
    )
    inventory = discover_basins_inventory(tmp_path / "basins")
    model = inventory["models"][0]

    parsed = parse_basins_geometry(
        model_id=model_id,
        input_dir=input_dir,
        shud_input_name="alias-a",
        required_files=model["required_files"],
    )

    # .sp.riv reach count (output/product) differs from seg.shp geometry count.
    assert parsed.output_segment_count == 3
    assert parsed.segment_count == 5
    assert parsed.output_segment_count != parsed.segment_count
    assert parsed.evidence_counts["river_count"] == 3
    assert parsed.evidence_counts["rivseg_segment_count"] == 5


def test_resource_profile_records_output_segment_count(tmp_path: Path) -> None:
    _, _, inventory_path, manifest_path, _ = _write_registry_fixture(
        tmp_path,
        sp_river_count=1,
        sp_segment_count=2,
    )

    sources = prepare_basins_import_sources(inventory_path=inventory_path, package_manifest_path=manifest_path)
    profile = _resource_profile(sources)

    assert profile["output_segment_count"] == 1
    assert profile["segment_count"] == 2


def test_output_river_segment_rows_use_canonical_ids_and_output_flag(tmp_path: Path) -> None:
    _, _, inventory_path, manifest_path, model_id = _write_registry_fixture(
        tmp_path,
        sp_river_count=3,
        sp_segment_count=2,
    )
    sources = prepare_basins_import_sources(inventory_path=inventory_path, package_manifest_path=manifest_path)

    rows = _output_river_segment_rows(sources)

    assert [row["river_segment_id"] for row in rows] == [
        f"{model_id}_shud_riv_000001",
        f"{model_id}_shud_riv_000002",
        f"{model_id}_shud_riv_000003",
    ]
    assert all(row["properties"]["shud_output_river"] is True for row in rows)
    assert [row["properties"]["shud_riv_index"] for row in rows] == [1, 2, 3]
    # segment_order is offset past the geometry layer to avoid collisions.
    assert [row["segment_order"] for row in rows] == [3, 4, 5]


def test_ensure_output_river_segments_seeds_exactly_output_segment_count(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _, _, inventory_path, manifest_path, model_id = _write_registry_fixture(
        tmp_path,
        sp_river_count=3,
        sp_segment_count=2,
    )
    sources = prepare_basins_import_sources(inventory_path=inventory_path, package_manifest_path=manifest_path)
    cursor = _FakeRiverSegmentCursor(sources.ids["river_network_version_id"])
    _patch_execute_values(monkeypatch)

    inserted = _ensure_output_river_segments(cursor, sources)

    output_rows = cursor.output_river_segments()
    assert inserted == 3
    assert [row["river_segment_id"] for row in output_rows] == [
        f"{model_id}_shud_riv_000001",
        f"{model_id}_shud_riv_000002",
        f"{model_id}_shud_riv_000003",
    ]
    assert all(row["properties_json"]["shud_output_river"] is True for row in output_rows)
    # Geometry-layer rows are untouched by output seeding.
    assert cursor.geometry_segment_count() == 0


def test_ensure_output_river_segments_is_idempotent(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _, _, inventory_path, manifest_path, _ = _write_registry_fixture(
        tmp_path,
        sp_river_count=3,
        sp_segment_count=2,
    )
    sources = prepare_basins_import_sources(inventory_path=inventory_path, package_manifest_path=manifest_path)
    cursor = _FakeRiverSegmentCursor(sources.ids["river_network_version_id"])
    _patch_execute_values(monkeypatch)

    first = _ensure_output_river_segments(cursor, sources)
    second = _ensure_output_river_segments(cursor, sources)

    assert first == 3
    assert second == 0
    assert len(cursor.output_river_segments()) == 3


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
    assert row["resource_profile"]["basin_slug"] == basin_slug
    assert row["segment_count"] == 2
    # river_network.segment_count stays geometry (2); core.river_segment now holds both layers:
    # 2 geometry (seg.shp) + 2 .sp.riv SHUD output rows seeded by the registration-fidelity fix.
    assert row["segment_rows"] == 4
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
    # total counts both layers (geometry + .sp.riv output); only the 2 geometry rows render
    # (output rows are NULL-geom until display backfill), so feature_total / features stay geometry-only.
    assert segments["total"] == 4
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
                (report["river_network_version_id"], f"{model_id}_seg_1"),
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
    _, _, inventory_path, manifest_path, model_id = _write_registry_fixture(
        tmp_path,
        basin_slug="basin-a-mismatch",
        sp_segment_count=3,
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
    assert error["error_code"] == "BASINS_REGISTRY_SEGMENT_COUNT_MISMATCH"
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
    assert segments["features"][0]["geometry"]["type"] == "LineString"
    assert segments["features"][0]["properties"]["river_segment_id"]
    assert segments["features"][0]["properties"]["river_network_version_id"] == report["river_network_version_id"]
    assert segments["features"][0]["properties"]["basin_version_id"] == report["basin_version_id"]


@pytest.mark.skipif(
    os.getenv("NHMS_RUN_REAL_BASINS_IMPORT") != "1" or not Path("data/Basins").exists(),
    reason="real Basins parser smoke is opt-in and requires data/Basins",
)
def test_real_basins_parser_smoke_reprojects_and_uses_rivseg_count(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
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
            "vbasins-real-parser-smoke",
            "--output",
            str(manifest_path),
        ]
    ) == 0

    sources = prepare_basins_import_sources(inventory_path=inventory_path, package_manifest_path=manifest_path)

    assert sources.geometry.segment_count == sources.geometry.evidence_counts["rivseg_segment_count"]
    assert sources.geometry.evidence_counts["river_count"] != sources.geometry.evidence_counts["rivseg_segment_count"]
    first_wkt = sources.geometry.river_segments[0].geom_wkt
    assert first_wkt.startswith("MULTILINESTRING((")
    first_point = first_wkt.removeprefix("MULTILINESTRING((").split(",", 1)[0]
    first_numbers = [float(value) for value in first_point.split()]
    assert -180 <= first_numbers[0] <= 180
    assert -90 <= first_numbers[1] <= 90


def _write_registry_fixture(
    tmp_path: Path,
    *,
    basin_slug: str = "basin-a",
    sp_river_count: int | None = None,
    sp_segment_count: int = 2,
    domain_with_hole: bool = False,
) -> tuple[Path, Path, Path, Path, str]:
    root = tmp_path / "basins"
    input_dir = _make_valid_model(
        root / basin_slug,
        "alias-a",
        sp_river_count=sp_river_count,
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
    sp_river_count: int | None = None,
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
    river_count = sp_segment_count if sp_river_count is None else sp_river_count
    sp_riv_rows = "".join(f"{index} 0 0 0.01 100 0\n" for index in range(1, river_count + 1))
    (input_dir / f"{input_name}.sp.riv").write_text(f"{river_count} 6\n{sp_riv_rows}", encoding="utf-8")
    (input_dir / f"{input_name}.sp.rivseg").write_text(
        f"{sp_segment_count} 4\n1 1 1 100\n",
        encoding="utf-8",
    )
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


def _write_domain_shapefile(
    base: Path,
    *,
    with_hole: bool = False,
    points: list[tuple[float, float]] | None = None,
    prj_text: str | None = None,
) -> None:
    import shapefile

    writer = shapefile.Writer(str(base), shapeType=shapefile.POLYGON)
    writer.field("ID", "N")
    outer = points or [(100.0, 30.0), (101.0, 30.0), (101.0, 31.0), (100.0, 31.0)]
    closed_outer = [list(point) for point in [*outer, outer[0]]]
    rings = [closed_outer]
    if with_hole:
        rings.append([[100.2, 30.2], [100.2, 30.8], [100.8, 30.8], [100.8, 30.2], [100.2, 30.2]])
    writer.poly(rings)
    writer.record(1)
    writer.close()
    _write_prj(base.with_suffix(".prj"), prj_text=prj_text)


def _write_line_shapefile(
    base: Path,
    *,
    points: list[list[tuple[float, float]]] | None = None,
    records: list[tuple[int, int, int, float]] | None = None,
    prj_text: str | None = None,
) -> None:
    import shapefile

    writer = shapefile.Writer(str(base), shapeType=shapefile.POLYLINE)
    writer.field("SEG_ID", "N")
    writer.field("ORDER", "N")
    writer.field("DOWN_ID", "N")
    writer.field("LENGTH_M", "F", decimal=3)
    lines = points or [[(100.1, 30.1), (100.5, 30.4)], [(100.5, 30.4), (100.8, 30.8)]]
    line_records = records or [(1, 1, 2, 50000.0), (2, 2, 0, 60000.0)]
    for line, record in zip(lines, line_records, strict=True):
        writer.line([[list(point) for point in line]])
        writer.record(*record)
    writer.close()
    _write_prj(base.with_suffix(".prj"), prj_text=prj_text)


def _write_prj(path: Path, *, prj_text: str | None = None) -> None:
    path.write_text(
        prj_text
        or (
            'GEOGCS["GCS_WGS_1984",DATUM["D_WGS_1984",'
            'SPHEROID["WGS_1984",6378137,298.257223563]],'
            'PRIMEM["Greenwich",0],UNIT["Degree",0.0174532925199433]]\n'
        ),
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


def _assert_registry_fixture_rows_absent(database_url: str, inventory_path: Path, model_id: str) -> None:
    inventory = json.loads(inventory_path.read_text(encoding="utf-8"))
    model = next(model for model in inventory["models"] if model["model_id"] == model_id)
    ids = model["suggested_ids"]
    with psycopg_connection(database_url) as connection:
        with connection.cursor() as cursor:
            cursor.execute("SELECT COUNT(*) AS count FROM core.model_instance WHERE model_id = %s", (model_id,))
            assert cursor.fetchone()["count"] == 0
            cursor.execute(
                "SELECT COUNT(*) AS count FROM core.mesh_version WHERE mesh_version_id = %s",
                (ids["mesh_version_id"],),
            )
            assert cursor.fetchone()["count"] == 0
            cursor.execute(
                "SELECT COUNT(*) AS count FROM core.river_segment WHERE river_network_version_id = %s",
                (ids["river_network_version_id"],),
            )
            assert cursor.fetchone()["count"] == 0
            cursor.execute(
                "SELECT COUNT(*) AS count FROM core.river_network_version WHERE river_network_version_id = %s",
                (ids["river_network_version_id"],),
            )
            assert cursor.fetchone()["count"] == 0
            cursor.execute(
                "SELECT COUNT(*) AS count FROM core.basin_version WHERE basin_version_id = %s",
                (ids["basin_version_id"],),
            )
            assert cursor.fetchone()["count"] == 0
            cursor.execute("SELECT COUNT(*) AS count FROM core.basin WHERE basin_id = %s", (ids["basin_id"],))
            assert cursor.fetchone()["count"] == 0


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


class _FakeRiverSegmentCursor:
    """Minimal in-memory stand-in for core.river_segment writes.

    Routes the two read queries used by ``_ensure_output_river_segments``
    (output-row COUNT and the ordered digest SELECT) and records rows that the
    patched ``execute_values`` shim inserts. Avoids any live-DB dependency for
    local runs; real-DB coverage lives in the @integration tests.
    """

    def __init__(self, river_network_version_id: str) -> None:
        self._rnv_id = river_network_version_id
        self._rows: list[dict[str, Any]] = []
        self._last: list[dict[str, Any]] = []

    def insert_rows(self, rows: list[tuple[Any, ...]]) -> None:
        for river_segment_id, rnv_id, segment_order, properties in rows:
            self._rows.append(
                {
                    "river_segment_id": river_segment_id,
                    "river_network_version_id": rnv_id,
                    "segment_order": segment_order,
                    # store the plain dict (mirrors what RealDictCursor yields on read)
                    "properties_json": _adapted(properties),
                }
            )

    def _output_rows(self) -> list[dict[str, Any]]:
        return [
            row
            for row in self._rows
            if row["river_network_version_id"] == self._rnv_id
            and bool(_adapted(row["properties_json"]).get("shud_output_river"))
        ]

    def output_river_segments(self) -> list[dict[str, Any]]:
        return sorted(self._output_rows(), key=lambda row: row["river_segment_id"])

    def geometry_segment_count(self) -> int:
        return sum(
            1
            for row in self._rows
            if row["river_network_version_id"] == self._rnv_id
            and not bool(_adapted(row["properties_json"]).get("shud_output_river"))
        )

    def execute(self, statement: str, parameters: tuple[Any, ...] = ()) -> None:
        normalized = " ".join(str(statement).split())
        if "COUNT(*)" in normalized:
            self._last = [{"count": len(self._output_rows())}]
        else:
            self._last = [
                {
                    "river_segment_id": row["river_segment_id"],
                    "segment_order": row["segment_order"],
                    "properties_json": row["properties_json"],
                }
                for row in self.output_river_segments()
            ]

    def fetchone(self) -> dict[str, Any] | None:
        return self._last[0] if self._last else None

    def fetchall(self) -> list[dict[str, Any]]:
        return list(self._last)


def _adapted(value: Any) -> dict[str, Any]:
    adapted = getattr(value, "adapted", value)
    return adapted if isinstance(adapted, dict) else {}


def _patch_execute_values(monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_execute_values(cursor: Any, _sql: str, rows: list[tuple[Any, ...]], **_kwargs: Any) -> None:
        cursor.insert_rows(list(rows))

    monkeypatch.setattr("psycopg2.extras.execute_values", fake_execute_values)


def _invoke_click(argv: list[str]) -> int:
    try:
        return _click_main(argv)
    except SystemExit as error:
        if isinstance(error.code, int):
            return error.code
        return 1


@pytest.mark.integration
def test_backfill_output_segment_geometry_stitches_gap_and_keeps_honest_length(
    integration_database_url: str,
) -> None:
    """Real-DB coverage for the output-reach geometry backfill -- the path that
    actually produced the heihe cross-ridge lines AND the later channel breakage
    (the Python greedy stitch + gap-split is unit-tested in test_basins_geometry_merge
    and test_river_segment_gap_split).
    Asserts: every reach is now emitted as a MultiLineString; in-order + reversed
    fine segments stitch into ONE continuous part (no fabricated jump); a reach whose
    parts sit far apart but UNIFORMLY (here ~58km edges throughout) is NOT split -- the
    relative 4x-median guard correctly treats no edge as an anomalous gap, so it stays
    a one-part line with nothing dropped and the honest summed length; a lone
    single-segment reach is a one-part MultiLineString; and an output reach carrying a
    non-numeric shud_riv_index is skipped, not crashed, by the text-based target match.
    (gap_split's ABSOLUTE-floor cut on a small-median reach is exercised in
    test_river_segment_gap_split.)
    """
    apply_migrations_from_zero(integration_database_url)
    rnv = "geomfix_rnv_v1"
    with (
        psycopg_connection(integration_database_url) as connection,
        connection.cursor() as cursor,
    ):
        cursor.execute(
            "INSERT INTO core.basin (basin_id, basin_name, basin_group, description) "
            "VALUES ('geomfix_basin', 'Geom Fix', 'integration', '') ON CONFLICT DO NOTHING"
        )
        cursor.execute(
            "INSERT INTO core.basin_version "
            "(basin_version_id, basin_id, version_label, geom, active_flag, source_uri, checksum) "
            "VALUES ('geomfix_bv', 'geomfix_basin', 'v1', "
            "ST_Multi(ST_MakeEnvelope(109.0, 29.0, 113.0, 33.0, 4490)), true, 'i://b', 'b') "
            "ON CONFLICT DO NOTHING"
        )
        cursor.execute(
            "INSERT INTO core.river_network_version "
            "(river_network_version_id, basin_version_id, version_label, segment_count, source_uri, checksum) "
            "VALUES (%s, 'geomfix_bv', 'v1', 3, 'i://r', 'r') ON CONFLICT DO NOTHING",
            (rnv,),
        )
        # Fine GIS segments grouped by source_raw_segment_id (= SHUD reach index):
        #   reach 1: two parts sharing the (110.1 30.1) joint, 2nd stored REVERSED
        #            -> greedy stitch yields ONE continuous part (no back-and-forth
        #               jump), emitted as a one-part MultiLineString.
        #   reach 2: two parts with no shared endpoint, but all edges ~uniformly ~58km
        #            -> stitched into one continuous line; gap_split's relative
        #               4x-median guard sees no anomalous edge, so it stays ONE part
        #               (nothing dropped, length stays the SUM). Anomalous-gap splitting
        #               is covered by test_river_segment_gap_split.
        #   reach 3: a lone single segment -> one-part MultiLineString unchanged.
        execute_values(
            cursor,
            "INSERT INTO core.river_segment "
            "(river_segment_id, river_network_version_id, segment_order, length_m, geom, properties_json) "
            "VALUES %s",
            [
                ("gf_1a", rnv, 1, 100.0, "LINESTRING(110.0 30.0, 110.1 30.1)", Json({"source_raw_segment_id": "1"})),
                ("gf_1b", rnv, 2, 100.0, "LINESTRING(110.2 30.2, 110.1 30.1)", Json({"source_raw_segment_id": "1"})),
                ("gf_2a", rnv, 3, 100.0, "LINESTRING(111.0 31.0, 111.4 31.4)", Json({"source_raw_segment_id": "2"})),
                ("gf_2b", rnv, 4, 50.0, "LINESTRING(111.8 31.8, 111.9 31.9)", Json({"source_raw_segment_id": "2"})),
                ("gf_3a", rnv, 5, 300.0, "LINESTRING(112.0 32.0, 112.3 32.3)", Json({"source_raw_segment_id": "3"})),
            ],
            # geom is geometry(MultiLineString, 4490) (000036); ST_Multi wraps the
            # LineString fixtures so the insert satisfies the column type.
            template="(%s, %s, %s, %s, ST_Multi(ST_GeomFromText(%s, 4490)), %s)",
        )
        # Output reach rows: NULL geom, shud_output_river=true, indices 1/2/3 plus
        # one row with a NON-NUMERIC shud_riv_index. The target match compares the
        # index as text, so this malformed sibling is simply skipped (matches no gis
        # index) instead of aborting the whole UPDATE on a ::int cast.
        execute_values(
            cursor,
            "INSERT INTO core.river_segment "
            "(river_segment_id, river_network_version_id, segment_order, properties_json) VALUES %s",
            [
                ("gf_out_1", rnv, 101, Json({"shud_output_river": True, "shud_riv_index": 1})),
                ("gf_out_2", rnv, 102, Json({"shud_output_river": True, "shud_riv_index": 2})),
                ("gf_out_3", rnv, 103, Json({"shud_output_river": True, "shud_riv_index": 3})),
                ("gf_out_bad", rnv, 104, Json({"shud_output_river": True, "shud_riv_index": "not-an-int"})),
            ],
            template="(%s, %s, %s, %s)",
        )

        updated = _backfill_output_segment_geometry(cursor, rnv, record_geometry_source=True)
        assert updated == 3  # reaches 1/2/3 only; the malformed-index row matches nothing

        cursor.execute(
            "SELECT (properties_json->>'shud_riv_index')::int AS idx, "
            "GeometryType(geom) AS gtype, ST_NPoints(geom) AS npts, length_m, "
            "(properties_json->>'geometry_source_length_m')::float AS prov_len, "
            "(properties_json->>'geometry_source_segment_count')::int AS prov_cnt "
            "FROM core.river_segment WHERE river_network_version_id = %s "
            "AND COALESCE(properties_json->>'shud_output_river', 'false') = 'true' "
            "AND properties_json->>'shud_riv_index' ~ '^[0-9]+$' ORDER BY idx",
            (rnv,),
        )
        rows = {row["idx"]: row for row in cursor.fetchall()}

        # target-side text match: the non-numeric reach is skipped, geom stays NULL
        # (an unguarded ::int cast on the target would instead abort the whole UPDATE).
        cursor.execute(
            "SELECT geom FROM core.river_segment WHERE river_segment_id = 'gf_out_bad'"
        )
        assert cursor.fetchone()["geom"] is None

    # reach 1: reversed part stitched into one continuous part, emitted as a one-part
    # MultiLineString (3 deduped points).
    assert rows[1]["gtype"] == "MULTILINESTRING"
    assert rows[1]["npts"] == 3
    assert rows[1]["length_m"] == 200.0

    # reach 2: uniformly-spaced parts stitched into one continuous line (all 4 points,
    # nothing dropped); the relative gap guard keeps it ONE part. length stays the SUM.
    assert rows[2]["gtype"] == "MULTILINESTRING"
    assert rows[2]["npts"] == 4
    assert rows[2]["length_m"] == 150.0
    assert rows[2]["prov_len"] == 150.0
    assert rows[2]["prov_cnt"] == 2

    # reach 3: a lone single segment is a valid one-part MultiLineString.
    assert rows[3]["gtype"] == "MULTILINESTRING"
    assert rows[3]["length_m"] == 300.0
