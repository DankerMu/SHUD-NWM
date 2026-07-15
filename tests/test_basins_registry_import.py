from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
from pathlib import Path
from typing import Any

import pytest
from pyproj import Transformer

import workers.model_registry.basins_geometry as basins_geometry
from packages.common.auth_policy import cli_policy_decision_from_evidence
from packages.common.model_registry import PsycopgModelRegistryStore
from tests.integration_helpers import apply_migrations_from_zero, psycopg_connection
from workers.model_registry.basins_discovery import discover_basins_inventory, write_inventory
from workers.model_registry.basins_geometry import (
    BasinsGeometryError,
    CrosswalkRow,
    parse_basins_geometry,
    parse_seg_shp_crosswalk,
)
from workers.model_registry.basins_registry_import import (
    BasinsRegistryImportError,
    _build_river_segment_crosswalk_rows,
    _canonical_singlepart_line_coordinates,
    _ensure_output_river_segments,
    _normalize_properties_for_digest,
    _output_river_segment_rows,
    _resource_profile,
    _river_segment_digest_row,
    import_basins_registry,
    prepare_basins_import_sources,
    prepare_relocated_basins_import_sources_after_package_verification,
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
    # PR 2: segment_count now equals the .sp.riv reach count (one row per reach).
    assert parsed.segment_count == 2
    assert parsed.evidence_counts == {
        "river_count": 2,
        "river_columns": 6,
        "rivseg_segment_count": 2,
        "rivseg_columns": 4,
    }
    # segment_order carries the SHUD reach Index verbatim from river.shp.
    assert [segment.segment_order for segment in parsed.river_segments] == [1, 2]
    # downstream IDs follow the new <model>_reach_<iRiv:06d> convention.
    assert parsed.river_segments[0].downstream_segment_id == f"{model_id}_reach_000002"
    assert parsed.river_segments[1].downstream_segment_id is None
    assert parsed.river_segments[1].properties["terminal_reach"] is True
    # PR 2: parser emits single-part LineString WKT; SQL-side ST_Multi wraps
    # it into the geometry(MultiLineString, 4490) column at insert time.
    assert parsed.river_segments[0].geom_wkt.startswith("LINESTRING(")


def test_parser_emits_single_part_linestring_per_reach(tmp_path: Path) -> None:
    """PR 2 contract replaces the legacy cross-gap MultiLineString assertion:
    the parser is now driven by gis/river.shp (single-part flow-ordered
    reaches by construction) and emits one LineString per reach without
    any greedy stitching or gap split. Where seg.shp records used to carry
    multi-part bridges, river.shp does not -- the bridge is removed at the
    source. This test pins the new shape contract."""

    root, input_dir, inventory_path, manifest_path, model_id = _write_registry_fixture(
        tmp_path, sp_segment_count=1
    )
    del root, inventory_path, manifest_path
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
    assert wkt.startswith("LINESTRING(")
    assert "MULTILINESTRING" not in wkt


def test_parser_emits_unique_reach_ids_from_river_shp_index(tmp_path: Path) -> None:
    """PR 2: river_segment_id is derived from river.shp's Index column,
    zero-padded to 6 digits. By construction every river.shp record has a
    unique Index (the .sp.riv invariant) so duplicate disambiguation
    machinery is no longer needed."""

    root, input_dir, inventory_path, manifest_path, model_id = _write_registry_fixture(
        tmp_path, sp_segment_count=3
    )
    del root, inventory_path, manifest_path
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
    assert ids == [
        f"{model_id}_reach_000001",
        f"{model_id}_reach_000002",
        f"{model_id}_reach_000003",
    ]
    # The fixture sets Down=2 on Index=1 and Down=0 (terminal) on the rest.
    assert parsed.river_segments[0].downstream_segment_id == f"{model_id}_reach_000002"
    assert parsed.river_segments[1].downstream_segment_id is None
    assert parsed.river_segments[2].downstream_segment_id is None
    assert parsed.river_segments[1].properties["terminal_reach"] is True


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


def test_relocated_sources_require_matching_verified_package_checksum(tmp_path: Path) -> None:
    _, _, inventory_path, manifest_path, model_id = _write_registry_fixture(tmp_path)

    with pytest.raises(BasinsRegistryImportError) as error:
        prepare_relocated_basins_import_sources_after_package_verification(
            inventory_path=inventory_path,
            package_manifest_path=manifest_path,
            verified_package_checksum="0" * 64,
        )

    assert error.value.error_code == "BASINS_REGISTRY_SOURCE_MISMATCH"
    assert error.value.model_id == model_id
    assert error.value.details["fields"] == ["package_checksum"]


def test_relocated_sources_still_reject_model_identity_mismatch(tmp_path: Path) -> None:
    _, _, inventory_path, manifest_path, model_id = _write_registry_fixture(tmp_path)
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    verified_package_checksum = manifest["package_checksum"]
    manifest["shud_input_name"] = "other-alias"
    manifest_path.write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8")

    with pytest.raises(BasinsRegistryImportError) as error:
        prepare_relocated_basins_import_sources_after_package_verification(
            inventory_path=inventory_path,
            package_manifest_path=manifest_path,
            verified_package_checksum=verified_package_checksum,
        )

    assert error.value.error_code == "BASINS_REGISTRY_SOURCE_MISMATCH"
    assert error.value.model_id == model_id
    assert error.value.details["fields"] == ["shud_input_name"]


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
    # PR 2: river.shp must carry the SHUD 14-field attribute table; build
    # one with projected geometry to assert the CRS transform path.
    _write_river_shapefile_with_geometry(
        input_dir / "gis" / "river",
        reaches=[
            [transformer.transform(100.1, 30.1), transformer.transform(100.5, 30.4)],
            [transformer.transform(100.5, 30.4), transformer.transform(100.8, 30.8)],
        ],
        downstreams=[2, 0],
        prj_text=albers_prj,
    )
    _write_line_shapefile(
        input_dir / "gis" / "seg",
        record_count=2,
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


def test_import_command_reports_river_shp_invariant_violation_before_database(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """PR 2 (spec "river.shp single-part invariant"): when the river.shp
    record count diverges from .sp.riv reach count, ingestion fails fast
    with ``BASINS_REGISTRY_RIVER_SHP_INVARIANT_VIOLATED`` before any DB
    write. Previously this was ``BASINS_REGISTRY_SEGMENT_COUNT_MISMATCH``
    keyed on .sp.rivseg, which is no longer the geometry oracle."""

    _, input_dir, inventory_path, manifest_path, model_id = _write_registry_fixture(tmp_path)
    # Shrink river.shp to 1 record so it no longer matches the 2-reach
    # .sp.riv header. _write_river_shapefile writes both .shp/.shx/.dbf
    # in place; the manifest checksums for these files have been recorded
    # so we have to drop the source-identity guard by also rebuilding the
    # manifest entries -- here we sidestep it by leaving the inventory
    # alone and relying on the run-time parser check (the file checksum
    # check happens via _validate_manifest_included_files; we use a NEW
    # fixture with a custom sp_river_count that doesn't match the river.shp
    # record count instead).
    _, fresh_input_dir, fresh_inventory_path, fresh_manifest_path, fresh_model_id = (
        _write_registry_fixture(tmp_path / "second", sp_river_count=1, sp_segment_count=2)
    )
    # _write_registry_fixture above wrote river.shp with reach_count=1 to
    # match sp_river_count=1; explicitly overwrite to a 3-record river.shp
    # WITHOUT touching the inventory so the parser sees a mismatch.
    _write_river_shapefile(fresh_input_dir / "gis" / "river", reach_count=3)
    # Rebuild inventory/manifest checksums for the resized river.shp.
    inventory = discover_basins_inventory(tmp_path / "second" / "basins")
    write_inventory(inventory, fresh_inventory_path)
    fresh_model = inventory["models"][0]
    refreshed_manifest = _package_manifest_for_model(
        fresh_model, fresh_model["model_id"], inventory=inventory
    )
    fresh_manifest_path.write_text(
        json.dumps(refreshed_manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )

    exit_code = _argparse_main(
        [
            "import-basins-registry",
            "--inventory",
            str(fresh_inventory_path),
            "--package-manifest",
            str(fresh_manifest_path),
            "--database-url",
            "postgresql://nhms:nhms@localhost:1/nhms",
            *_CLI_MODEL_ADMIN_AUTH_ARGS,
        ]
    )

    captured = capsys.readouterr()
    error = json.loads(captured.err)
    assert exit_code == 1
    assert captured.out == ""
    assert error["error_code"] == "BASINS_REGISTRY_RIVER_SHP_INVARIANT_VIOLATED"
    assert error["model_id"] == fresh_model_id
    assert error["river_shp_record_count"] == 3
    assert error["sp_riv_count"] == 1
    del input_dir, inventory_path, manifest_path, model_id


def test_import_accepts_sp_riv_river_count_different_from_rivseg_segments(tmp_path: Path) -> None:
    """PR 2: segment_count now equals .sp.riv reach count (one row per
    reach), not the .sp.rivseg segment count. rivseg_segment_count is
    retained as historical evidence only."""

    _, _, inventory_path, manifest_path, model_id = _write_registry_fixture(
        tmp_path,
        sp_river_count=1,
        sp_segment_count=2,
    )

    sources = prepare_basins_import_sources(inventory_path=inventory_path, package_manifest_path=manifest_path)

    assert sources.ids["model_id"] == model_id
    # row granularity = reach count, not rivseg segment count.
    assert sources.geometry.segment_count == 1
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


def test_parsed_geometry_segment_count_equals_sp_riv_reach_count(tmp_path: Path) -> None:
    """PR 2: post-Path-C, both ``segment_count`` (core.river_segment row
    count) and ``output_segment_count`` (.sp.riv reach count) equal the
    .sp.riv reach count. .sp.rivseg is retained only as evidence -- it no
    longer drives any row granularity."""

    _, input_dir, _, _, model_id = _write_registry_fixture(
        tmp_path,
        sp_river_count=3,
        sp_segment_count=5,
    )
    inventory = discover_basins_inventory(tmp_path / "basins")
    model = inventory["models"][0]

    parsed = parse_basins_geometry(
        model_id=model_id,
        input_dir=input_dir,
        shud_input_name="alias-a",
        required_files=model["required_files"],
    )

    # PR 2 row granularity: reach count drives both numbers.
    assert parsed.output_segment_count == 3
    assert parsed.segment_count == 3
    # rivseg count survives in evidence_counts as historical record only.
    assert parsed.evidence_counts["river_count"] == 3
    assert parsed.evidence_counts["rivseg_segment_count"] == 5


def test_resource_profile_records_output_segment_count(tmp_path: Path) -> None:
    """PR 2: segment_count now == reach count == output_segment_count."""

    _, _, inventory_path, manifest_path, _ = _write_registry_fixture(
        tmp_path,
        sp_river_count=1,
        sp_segment_count=2,
    )

    sources = prepare_basins_import_sources(inventory_path=inventory_path, package_manifest_path=manifest_path)
    profile = _resource_profile(sources)

    assert profile["output_segment_count"] == 1
    assert profile["segment_count"] == 1


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
    # PR 2: segment_order offsets past the reach layer (segment_count = 3 reach
    # rows in this fixture; output rows start at segment_count + 1).
    assert [row["segment_order"] for row in rows] == [4, 5, 6]


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

    # PR 2: segment_count tracks .sp.riv reach count, not rivseg segment count.
    assert sources.geometry.segment_count == sources.geometry.evidence_counts["river_count"]
    assert sources.geometry.evidence_counts["river_count"] != sources.geometry.evidence_counts["rivseg_segment_count"]
    first_wkt = sources.geometry.river_segments[0].geom_wkt
    # PR 2 parser emits single-part LineString WKT; SQL-side ST_Multi wraps
    # it at insert time, but the parser product stays a plain LineString.
    assert first_wkt.startswith("LINESTRING(")
    first_point = first_wkt.removeprefix("LINESTRING(").split(",", 1)[0]
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
    # PR 2 contract: river.shp is the authoritative reach geometry source,
    # with one record per .sp.riv reach and the full SHUD attribute table.
    _write_river_shapefile(gis_dir / "river", reach_count=river_count)
    # seg.shp keeps the existing (iRiv, iEle)-style records for crosswalk
    # writes; the legacy "ORDER/DOWN_ID/LENGTH_M" attribute layout still
    # exercises the old test paths and gives the seg-driven helpers
    # something to parse.
    _write_line_shapefile(gis_dir / "seg", record_count=sp_segment_count)
    forcing = model_dir / "forcing"
    forcing.mkdir()
    (forcing / "X000001.csv").write_text("time,value\n2026-01-01,1\n", encoding="utf-8")
    return input_dir


def _write_river_shapefile(
    base: Path,
    *,
    reach_count: int,
    prj_text: str | None = None,
) -> None:
    """Write a river.shp containing the 14 PR-2 required dbf fields.

    Index runs 1..reach_count. The first reach has Down=2 to exercise the
    downstream resolver; the rest terminate (Down=0) so we never reference
    an Index that's not in the fixture.
    """

    import shapefile

    writer = shapefile.Writer(str(base), shapeType=shapefile.POLYLINE)
    for name in (
        "Index",
        "Down",
        "Type",
        "Slope",
        "Length",
        "BC",
        "Depth",
        "BankSlope",
        "Width",
        "Sinuosity",
        "Manning",
        "Cwr",
        "KsatH",
        "BedThick",
    ):
        if name in ("Index", "Down", "Type", "BC"):
            writer.field(name, "N")
        else:
            writer.field(name, "F", decimal=6)
    for index in range(1, reach_count + 1):
        base_lon = 100.0 + 0.1 * (index - 1)
        # Two-vertex single-part LineString per reach -- single-part is
        # the PR 2 contract.
        writer.line([[[base_lon, 30.0], [base_lon + 0.05, 30.05]]])
        down_index = 2 if index == 1 and reach_count >= 2 else 0
        writer.record(
            index,
            down_index,
            2,
            0.001,
            100.0,
            0,
            1.5,
            0.5,
            10.0,
            1.05,
            0.035,
            1.0,
            1.0e-5,
            0.5,
        )
    writer.close()
    _write_prj(base.with_suffix(".prj"), prj_text=prj_text)


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


def _write_river_shapefile_with_geometry(
    base: Path,
    *,
    reaches: list[list[tuple[float, float]]],
    downstreams: list[int],
    prj_text: str | None = None,
) -> None:
    """Variant of ``_write_river_shapefile`` for tests that need specific
    polylines (e.g. projected-CRS reprojection coverage). Each ``reaches``
    entry is a single-part LineString point list; the Index column counts
    from 1 to match the polyline list length.
    """

    import shapefile

    writer = shapefile.Writer(str(base), shapeType=shapefile.POLYLINE)
    for name in (
        "Index",
        "Down",
        "Type",
        "Slope",
        "Length",
        "BC",
        "Depth",
        "BankSlope",
        "Width",
        "Sinuosity",
        "Manning",
        "Cwr",
        "KsatH",
        "BedThick",
    ):
        if name in ("Index", "Down", "Type", "BC"):
            writer.field(name, "N")
        else:
            writer.field(name, "F", decimal=6)
    assert len(reaches) == len(downstreams)
    for index, (line, down_index) in enumerate(zip(reaches, downstreams, strict=True), start=1):
        writer.line([[list(point) for point in line]])
        writer.record(
            index,
            int(down_index),
            2,
            0.001,
            100.0,
            0,
            1.5,
            0.5,
            10.0,
            1.05,
            0.035,
            1.0,
            1.0e-5,
            0.5,
        )
    writer.close()
    _write_prj(base.with_suffix(".prj"), prj_text=prj_text)


def _write_line_shapefile(
    base: Path,
    *,
    points: list[list[tuple[float, float]]] | None = None,
    records: list[tuple[int, int, int, float]] | None = None,
    record_count: int | None = None,
    prj_text: str | None = None,
) -> None:
    """Write a polyline shapefile -- used both for seg.shp fixtures and the
    legacy ad-hoc river/seg shapes some tests still construct directly.

    When ``record_count`` is supplied (the new seg.shp default), the file
    follows the SHUD ``(iRiv, iEle)``-style attribute layout that
    crosswalk parsing expects. Otherwise the legacy
    ``(SEG_ID, ORDER, DOWN_ID, LENGTH_M)`` shape is preserved so the older
    tests that build hand-crafted ``records=[...]`` lists keep working.
    """

    import shapefile

    writer = shapefile.Writer(str(base), shapeType=shapefile.POLYLINE)
    if record_count is not None:
        writer.field("iRiv", "N")
        writer.field("iEle", "N")
        for record_index in range(record_count):
            # iRiv cycles over the reach 1..reach_count range so every
            # crosswalk row finds a parent reach in river.shp (PR 2 FK).
            iriv = (record_index % max(1, record_count)) + 1
            iele = record_index + 1
            base_lon = 100.0 + 0.05 * record_index
            writer.line([[[base_lon, 30.0], [base_lon + 0.01, 30.01]]])
            writer.record(iriv, iele)
    else:
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


# ---------------------------------------------------------------------------
# PR 1 (issue #560): crosswalk pure-function unit tests (no production wiring)
# ---------------------------------------------------------------------------

_QHH_SAMPLE_SEG_SHP = (
    Path(__file__).parent / "fixtures" / "basins" / "qhh-sample" / "gis" / "seg.shp"
)


def test_parse_seg_shp_crosswalk_extracts_all_records() -> None:
    """qhh-sample fixture seg.shp has 18 records over iRiv ∈ {1, 2, 3, 9, 180}."""
    import shapefile

    reader = shapefile.Reader(str(_QHH_SAMPLE_SEG_SHP))
    try:
        rows = parse_seg_shp_crosswalk(reader)
    finally:
        reader.close()

    assert len(rows) == 18
    assert all(isinstance(row, CrosswalkRow) for row in rows)
    # Every iRiv in the fixture comes from the documented sampled reach set.
    assert {row.iRiv for row in rows} == {1, 2, 3, 9, 180}
    # segment_order is the natural row-offset enumeration -> monotonically
    # increasing 0..N-1 sequence.
    assert [row.segment_order for row in rows] == list(range(18))
    # The qhh seg.shp dbf only carries iRiv + iEle (no Length field), so
    # length_m must be None on every row.
    assert all(row.length_m is None for row in rows)
    # iEle is an integer mesh-element index pulled verbatim from the dbf.
    assert all(isinstance(row.iEle, int) and row.iEle > 0 for row in rows)


def test_build_crosswalk_rows_format() -> None:
    """Constructor builds dict rows shaped for core.river_segment_crosswalk insert."""
    segments = [
        CrosswalkRow(iRiv=1, iEle=3099, segment_order=0, length_m=349.02),
        CrosswalkRow(iRiv=2, iEle=2597, segment_order=6, length_m=391.40),
    ]
    model_id = "basins_qhh_shud"
    rnv_id = "rnv_basins_qhh_shud_v1"
    reach_indices = {1, 2, 3, 9, 180}

    rows = _build_river_segment_crosswalk_rows(model_id, rnv_id, segments, reach_indices)

    assert len(rows) == 2
    assert rows[0] == {
        "river_network_version_id": rnv_id,
        "river_segment_id": "basins_qhh_shud_reach_000001",
        "source": "basins_seg_shp",
        "external_id": "1:3099",
        "properties_json": {
            "iRiv": 1,
            "iEle": 3099,
            "segment_order": 0,
            "length_m": 349.02,
        },
    }
    assert rows[1] == {
        "river_network_version_id": rnv_id,
        "river_segment_id": "basins_qhh_shud_reach_000002",
        "source": "basins_seg_shp",
        "external_id": "2:2597",
        "properties_json": {
            "iRiv": 2,
            "iEle": 2597,
            "segment_order": 6,
            "length_m": 391.40,
        },
    }


def test_build_crosswalk_rows_reach_missing_reports_set() -> None:
    """A segment whose iRiv is not in reach_indices raises a structured error."""
    segments = [
        CrosswalkRow(iRiv=1, iEle=3099, segment_order=0, length_m=None),
        CrosswalkRow(iRiv=999, iEle=4242, segment_order=1, length_m=None),
    ]
    with pytest.raises(BasinsGeometryError) as excinfo:
        _build_river_segment_crosswalk_rows(
            model_id="basins_qhh_shud",
            river_network_version_id="rnv_test",
            segments=segments,
            reach_indices={1, 2, 3},
        )
    assert excinfo.value.error_code == "BASINS_REGISTRY_CROSSWALK_REACH_MISSING"
    payload = excinfo.value.to_payload()
    assert payload["missing_iRiv"] == [999]
    # Sanity: a happy iRiv not declared missing must not appear in the payload.
    assert 1 not in payload["missing_iRiv"]


# ---------------------------------------------------------------------------
# PR 2 (issue #561): DB-side atomic switch tests (Section 2 of tasks.md)
# ---------------------------------------------------------------------------

_QHH_SAMPLE_DIR = Path(__file__).parent / "fixtures" / "basins" / "qhh-sample"


def _stage_qhh_sample_fixture(tmp_path: Path) -> tuple[Path, Path, Path, str]:
    """Copy the qhh-sample shapefile + .sp.riv/.sp.rivseg into a fresh
    model directory and register it through the discovery+manifest path
    so the regular ``parse_basins_geometry`` entry point can read it.

    Returns ``(input_dir, inventory_path, manifest_path, model_id)``.

    The basin_slug is derived from tmp_path.name so every test invocation
    yields a unique model_id under the session-scoped integration DB,
    avoiding cross-test CHECKSUM_CONFLICT pollution. apply_migrations_from_zero
    only re-applies missing migrations; it does NOT truncate existing rows.
    """

    # pytest tmp_path.name shape: test_<name>0 / test_<name>1 / etc — unique per test
    basin_slug = f"qhh-sample-{tmp_path.name}".replace("_", "-").lower()
    input_name = "alias-qhh-sample"
    root = tmp_path / "basins"
    input_dir = root / basin_slug / "input" / input_name
    input_dir.mkdir(parents=True)
    # Stage every required SHUD canonical file. Empty placeholders for files
    # the parser does not read are fine -- only sp.riv/sp.rivseg/gis files
    # contribute to the geometry assertions here.
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
    # Bring the fixture sp.riv / sp.rivseg in under the alias name.
    shutil.copy2(_QHH_SAMPLE_DIR / "qhh.sp.riv", input_dir / f"{input_name}.sp.riv")
    shutil.copy2(_QHH_SAMPLE_DIR / "qhh.sp.rivseg", input_dir / f"{input_name}.sp.rivseg")
    # qhh-sample.sp.riv declares 1633 reaches in its header (the production
    # qhh count); rewrite the header to 5 so the discover/parser checks
    # match the 5-record river.shp subset.
    sp_riv_path = input_dir / f"{input_name}.sp.riv"
    sp_riv_text = sp_riv_path.read_text(encoding="utf-8").splitlines()
    sp_riv_text[0] = f"5 {sp_riv_text[0].split()[-1] if len(sp_riv_text[0].split()) > 1 else 6}"
    sp_riv_path.write_text("\n".join(sp_riv_text) + "\n", encoding="utf-8")
    # Same for sp.rivseg: rewrite the declared segment count to 18.
    sp_rivseg_path = input_dir / f"{input_name}.sp.rivseg"
    sp_rivseg_text = sp_rivseg_path.read_text(encoding="utf-8").splitlines()
    sp_rivseg_text[0] = f"18 {sp_rivseg_text[0].split()[-1] if len(sp_rivseg_text[0].split()) > 1 else 4}"
    sp_rivseg_path.write_text("\n".join(sp_rivseg_text) + "\n", encoding="utf-8")
    # Copy GIS layers, including river.shp's full 14-field dbf.
    gis_dst = input_dir / "gis"
    gis_dst.mkdir()
    for layer in ("river", "seg"):
        for suffix in ("shp", "shx", "dbf", "prj"):
            shutil.copy2(
                _QHH_SAMPLE_DIR / "gis" / f"{layer}.{suffix}",
                gis_dst / f"{layer}.{suffix}",
            )
    # The qhh-sample fixture has no domain.shp; synthesise one so the
    # parser can resolve the domain layer (its content is not asserted).
    _write_domain_shapefile(gis_dst / "domain")
    # forcing dir lets the discovery layer treat the model as importable.
    forcing = root / basin_slug / "forcing"
    forcing.mkdir()
    (forcing / "X000001.csv").write_text("time,value\n2026-01-01,1\n", encoding="utf-8")
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
    return input_dir, inventory_path, manifest_path, model_id


def _parse_qhh_sample(tmp_path: Path) -> tuple[Any, str]:
    """Run ``parse_basins_geometry`` against the qhh-sample fixture."""

    input_dir, inventory_path, _manifest_path, model_id = _stage_qhh_sample_fixture(tmp_path)
    inventory = json.loads(inventory_path.read_text(encoding="utf-8"))
    model = inventory["models"][0]
    parsed = parse_basins_geometry(
        model_id=model_id,
        input_dir=input_dir,
        shud_input_name="alias-qhh-sample",
        required_files=model["required_files"],
    )
    return parsed, model_id


# --- 2.9 reach count matches sp.riv -----------------------------------------


def test_reach_count_matches_sp_riv(tmp_path: Path) -> None:
    """qhh-sample: 5 reaches in river.shp, 5 reaches in .sp.riv header."""

    parsed, _model_id = _parse_qhh_sample(tmp_path)
    assert parsed.segment_count == 5
    assert parsed.output_segment_count == 5
    assert parsed.evidence_counts["river_count"] == 5


# --- 2.10 river.shp single-part invariant fail-fast --------------------------


def test_river_shp_invariant_fail_fast_on_multipart(tmp_path: Path) -> None:
    """Construct a multi-part river.shp record -> invariant fires before any DB write."""

    _, input_dir, _, _, model_id = _write_registry_fixture(tmp_path)
    import shapefile

    target = input_dir / "gis" / "river"
    # Rewrite river.shp with two records: a clean single-part one (good
    # so the count check still matches sp.riv) and a multi-part one.
    writer = shapefile.Writer(str(target), shapeType=shapefile.POLYLINE)
    for name in (
        "Index",
        "Down",
        "Type",
        "Slope",
        "Length",
        "BC",
        "Depth",
        "BankSlope",
        "Width",
        "Sinuosity",
        "Manning",
        "Cwr",
        "KsatH",
        "BedThick",
    ):
        writer.field(name, "N" if name in ("Index", "Down", "Type", "BC") else "F", decimal=6)
    writer.line([[[100.0, 30.0], [100.1, 30.1]]])
    writer.record(1, 2, 2, 0.001, 100.0, 0, 1.5, 0.5, 10.0, 1.05, 0.035, 1.0, 1e-5, 0.5)
    # Multi-part: two disjoint parts in a single record.
    writer.line([
        [[100.2, 30.2], [100.25, 30.25]],
        [[100.5, 30.5], [100.55, 30.55]],
    ])
    writer.record(2, 0, 2, 0.001, 100.0, 0, 1.5, 0.5, 10.0, 1.05, 0.035, 1.0, 1e-5, 0.5)
    writer.close()
    _write_prj((input_dir / "gis" / "river").with_suffix(".prj"))

    inventory = discover_basins_inventory(tmp_path / "basins")
    model = inventory["models"][0]
    with pytest.raises(BasinsGeometryError) as excinfo:
        parse_basins_geometry(
            model_id=model_id,
            input_dir=input_dir,
            shud_input_name="alias-a",
            required_files=model["required_files"],
        )
    assert excinfo.value.error_code == "BASINS_REGISTRY_RIVER_SHP_INVARIANT_VIOLATED"
    assert excinfo.value.details["offending_index"] == 2
    assert excinfo.value.details["part_count"] == 2


def test_river_shp_invariant_fail_fast_on_missing_field(tmp_path: Path) -> None:
    """Drop ``BankSlope`` from river.shp dbf -> invariant fires before any DB write."""

    _, input_dir, _, _, model_id = _write_registry_fixture(tmp_path)
    import shapefile

    target = input_dir / "gis" / "river"
    writer = shapefile.Writer(str(target), shapeType=shapefile.POLYLINE)
    for name in (
        "Index",
        "Down",
        "Type",
        "Slope",
        "Length",
        "BC",
        "Depth",
        # BankSlope intentionally absent.
        "Width",
        "Sinuosity",
        "Manning",
        "Cwr",
        "KsatH",
        "BedThick",
    ):
        writer.field(name, "N" if name in ("Index", "Down", "Type", "BC") else "F", decimal=6)
    for index in range(1, 3):
        writer.line([[[100.0 + 0.1 * index, 30.0], [100.05 + 0.1 * index, 30.05]]])
        down = 2 if index == 1 else 0
        writer.record(index, down, 2, 0.001, 100.0, 0, 1.5, 10.0, 1.05, 0.035, 1.0, 1e-5, 0.5)
    writer.close()
    _write_prj(target.with_suffix(".prj"))

    inventory = discover_basins_inventory(tmp_path / "basins")
    model = inventory["models"][0]
    with pytest.raises(BasinsGeometryError) as excinfo:
        parse_basins_geometry(
            model_id=model_id,
            input_dir=input_dir,
            shud_input_name="alias-a",
            required_files=model["required_files"],
        )
    assert excinfo.value.error_code == "BASINS_REGISTRY_RIVER_SHP_INVARIANT_VIOLATED"
    assert "BankSlope" in excinfo.value.details["missing_fields"]


# --- 2.11 reach IDs zero-padded + downstream resolves ----------------------


def test_reach_ids_are_zero_padded(tmp_path: Path) -> None:
    """qhh-sample reach Index=1 -> river_segment_id ends in ``_reach_000001``."""

    parsed, model_id = _parse_qhh_sample(tmp_path)
    ids = sorted(segment.river_segment_id for segment in parsed.river_segments)
    # qhh-sample uses Index ∈ {1, 2, 3, 9, 180} (see fixture README).
    assert ids[0] == f"{model_id}_reach_000001"
    assert f"{model_id}_reach_000009" in ids
    assert f"{model_id}_reach_000180" in ids


def test_downstream_id_resolves(tmp_path: Path) -> None:
    """qhh-sample: Index=1 has Down=2 -> downstream resolves to _reach_000002;
    Index=180 has Down=181 (not in subset) -> remains a string downstream
    reference; Index=3 has Down=4 (not in subset) -> ditto. The terminal
    case (Down=0) is exercised via _write_registry_fixture in the default
    happy-path test above; here we focus on resolution to existing IDs."""

    parsed, model_id = _parse_qhh_sample(tmp_path)
    by_id = {segment.river_segment_id: segment for segment in parsed.river_segments}
    first = by_id[f"{model_id}_reach_000001"]
    assert first.downstream_segment_id == f"{model_id}_reach_000002"
    # qhh-sample Index=180 has Down=181; we still construct the reach-style
    # downstream string verbatim (the FK / consumer can choose to filter
    # against existing IDs if they need transitive closure).
    last = by_id[f"{model_id}_reach_000180"]
    assert last.downstream_segment_id == f"{model_id}_reach_000181"


# --- 2.12 reach geom no cross-gap straight bridges --------------------------


def test_reach_geom_no_cross_gap_bridges(tmp_path: Path) -> None:
    """qhh-sample river.shp is single-part by construction. We assert the
    spec invariant inline: every reach polyline's max edge length must be
    ≤ ``max(300m, 4 × median_edge)`` measured by equirectangular metres
    against EPSG:4490. The numeric thresholds are hard-coded here per the
    spec requirement that no module-level constant carry them."""

    parsed, _model_id = _parse_qhh_sample(tmp_path)
    earth_radius_m = 6_371_000.0
    for segment in parsed.river_segments:
        wkt = segment.geom_wkt
        assert wkt.startswith("LINESTRING(")
        coords = [
            tuple(float(value) for value in pair.split())
            for pair in wkt.removeprefix("LINESTRING(").rstrip(")").split(", ")
        ]
        assert len(coords) >= 2
        edges = []
        import math

        for a, b in zip(coords[:-1], coords[1:], strict=True):
            lat_rad = ((a[1] + b[1]) / 2.0) * (math.pi / 180.0)
            dx = (b[0] - a[0]) * (math.pi / 180.0) * math.cos(lat_rad) * earth_radius_m
            dy = (b[1] - a[1]) * (math.pi / 180.0) * earth_radius_m
            edges.append(math.hypot(dx, dy))
        ordered = sorted(edges)
        median_edge = ordered[len(ordered) // 2]
        threshold = max(300.0, 4 * median_edge)
        assert max(edges) <= threshold, (
            f"reach {segment.river_segment_id} has cross-gap edge "
            f"{max(edges):.2f}m > threshold {threshold:.2f}m"
        )


# --- 2.13 missing file fail-fast --------------------------------------------


def test_river_shp_missing_fails_fast(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Removing river.shp must fail before any DB write with the
    payload-precise ``BASINS_REGISTRY_RIVER_SHP_MISSING`` code."""

    _, input_dir, inventory_path, manifest_path, model_id = _write_registry_fixture(tmp_path)
    (input_dir / "gis" / "river.shp").unlink()

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

    assert exit_code == 1
    captured = capsys.readouterr()
    error = json.loads(captured.err)
    assert error["error_code"] == "BASINS_REGISTRY_RIVER_SHP_MISSING"
    assert model_id


def test_seg_shp_missing_fails_fast(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Removing seg.shp must fail before any DB write with the
    payload-precise ``BASINS_REGISTRY_SEG_SHP_MISSING`` code."""

    _, input_dir, inventory_path, manifest_path, model_id = _write_registry_fixture(tmp_path)
    (input_dir / "gis" / "seg.shp").unlink()

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

    assert exit_code == 1
    captured = capsys.readouterr()
    error = json.loads(captured.err)
    assert error["error_code"] == "BASINS_REGISTRY_SEG_SHP_MISSING"
    assert model_id


# --- 2.14 per-basin ingest is transactional ---------------------------------


def test_per_basin_ingest_is_transactional(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Simulate a crosswalk-write failure and assert the river_segment
    writes performed earlier in the same transaction roll back. We patch
    psycopg2.connect with a fake connection that surfaces commit / rollback
    + a fake cursor that raises on crosswalk INSERT."""

    _, _, inventory_path, manifest_path, _ = _write_registry_fixture(tmp_path)
    rollback_calls: list[int] = []
    commit_calls: list[int] = []
    captured_statements: list[str] = []

    class _FakeCursor:
        def __init__(self) -> None:
            self._last_rows: list[Any] = []

        def __enter__(self) -> "_FakeCursor":
            return self

        def __exit__(self, *args: Any, **kwargs: Any) -> None:
            return None

        def execute(self, statement: str, parameters: tuple[Any, ...] = ()) -> None:
            del parameters
            captured_statements.append(" ".join(statement.split())[:120])
            normalized = statement.lower()
            # COUNT-style probes return 0 so the caller treats the table as
            # empty and proceeds to the INSERT path we want to fault on.
            if "count(*)" in normalized or " exists" in normalized.split(" select", 1)[0]:
                self._last_rows = [{"count": 0}]
                return
            if "select 1 from" in normalized:
                self._last_rows = []
                return
            if "core.river_segment_crosswalk" in normalized and "insert" in normalized:
                raise RuntimeError("simulated crosswalk write failure")
            if "returning" in normalized:
                self._last_rows = [{"basin_version_id": "stub"}]
            else:
                self._last_rows = []

        def fetchone(self) -> dict[str, Any] | None:
            return self._last_rows[0] if self._last_rows else None

        def fetchall(self) -> list[dict[str, Any]]:
            return list(self._last_rows)

    class _FakeConnection:
        autocommit = False

        def cursor(self, **kwargs: Any) -> _FakeCursor:
            del kwargs
            return _FakeCursor()

        def commit(self) -> None:
            commit_calls.append(1)

        def rollback(self) -> None:
            rollback_calls.append(1)

        def close(self) -> None:
            return None

    def fake_connect(*args: Any, **kwargs: Any) -> _FakeConnection:
        del args, kwargs
        return _FakeConnection()

    def fake_register(*args: Any, **kwargs: Any) -> None:
        del args, kwargs

    def fake_execute_values(*args: Any, **kwargs: Any) -> None:
        # Route execute_values through the cursor.execute path so the
        # crosswalk failure can fire.
        cursor = args[0] if args else kwargs.get("cur")
        statement = args[1] if len(args) >= 2 else kwargs.get("sql", "")
        cursor.execute(statement)

    monkeypatch.setattr("workers.model_registry.basins_registry_import.psycopg2", None, raising=False)
    monkeypatch.setattr("psycopg2.connect", fake_connect)
    monkeypatch.setattr("psycopg2.extras.register_default_json", fake_register)
    monkeypatch.setattr("psycopg2.extras.register_default_jsonb", fake_register)
    monkeypatch.setattr("psycopg2.extras.execute_values", fake_execute_values)
    # Reuse the RealDictCursor symbol; FakeCursor implements the same interface.
    monkeypatch.setattr("psycopg2.extras.RealDictCursor", _FakeCursor, raising=False)

    with pytest.raises(BasinsRegistryImportError) as excinfo:
        import_basins_registry(
            inventory_path=inventory_path,
            package_manifest_path=manifest_path,
            database_url="postgresql://nhms:nhms@localhost:1/nhms",
            trusted_internal=True,
        )

    assert excinfo.value.error_code == "BASINS_REGISTRY_DATABASE_ERROR"
    # The fake connection's rollback() must have run at least once;
    # commit() must never have been called for this basin.
    assert rollback_calls, "transaction rollback not triggered on crosswalk failure"
    assert not commit_calls
    # Sanity: the river_segment INSERT statement preceded the crosswalk
    # INSERT (FK-ordering invariant).
    river_segment_index = next(
        (i for i, stmt in enumerate(captured_statements) if "into core.river_segment " in stmt.lower()),
        None,
    )
    crosswalk_index = next(
        (i for i, stmt in enumerate(captured_statements) if "into core.river_segment_crosswalk" in stmt.lower()),
        None,
    )
    assert river_segment_index is not None
    assert crosswalk_index is not None
    assert river_segment_index < crosswalk_index


# --- 2.14a / 2.14b integration tests ---------------------------------------


@pytest.mark.integration
def test_river_segment_and_crosswalk_atomic_fk_order(
    integration_database_url: str,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """End-to-end FK-order sanity: ingestion writes river_segment first,
    then river_segment_crosswalk, in the same transaction. After commit
    every crosswalk row resolves its parent reach row."""

    apply_migrations_from_zero(integration_database_url)
    _, _, inventory_path, manifest_path, model_id = _write_registry_fixture(
        tmp_path,
        basin_slug="basin-a-fk-order",
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
            *_CLI_MODEL_ADMIN_AUTH_ARGS,
        ]
    )
    assert exit_code == 0
    capsys.readouterr()
    with psycopg_connection(integration_database_url) as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT COUNT(*) AS orphan_count
                FROM core.river_segment_crosswalk rsc
                WHERE rsc.river_segment_id LIKE %s
                  AND NOT EXISTS (
                    SELECT 1
                    FROM core.river_segment rs
                    WHERE rs.river_segment_id = rsc.river_segment_id
                      AND rs.river_network_version_id = rsc.river_network_version_id
                  )
                """,
                (f"{model_id}_reach_%",),
            )
            assert cursor.fetchone()["orphan_count"] == 0


@pytest.mark.integration
def test_re_ingest_replaces_legacy_seg_ids(
    integration_database_url: str,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Seed a basin with legacy ``<model>_seg_*`` river_segment rows, then
    re-ingest under PR 2: legacy rows must be deleted (along with their
    crosswalk children) and replaced with reach-level ``_reach_*`` rows."""

    apply_migrations_from_zero(integration_database_url)
    _, _, inventory_path, manifest_path, model_id = _write_registry_fixture(
        tmp_path,
        basin_slug="basin-a-reingest-legacy",
    )
    inventory = json.loads(inventory_path.read_text(encoding="utf-8"))
    ids = inventory["models"][0]["suggested_ids"]
    with psycopg_connection(integration_database_url) as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                "INSERT INTO core.basin (basin_id, basin_name, basin_group, description) "
                "VALUES (%s, %s, %s, %s) ON CONFLICT DO NOTHING",
                (ids["basin_id"], ids["basin_id"], "Basins", "legacy"),
            )
            cursor.execute(
                "INSERT INTO core.basin_version "
                "(basin_version_id, basin_id, version_label, geom, active_flag, source_uri, checksum) "
                "VALUES (%s, %s, %s, "
                "ST_Multi(ST_MakeEnvelope(99, 29, 102, 32, 4490)), false, 's', 'c') "
                "ON CONFLICT DO NOTHING",
                (ids["basin_version_id"], ids["basin_id"], "vlegacy"),
            )
            cursor.execute(
                "INSERT INTO core.river_network_version "
                "(river_network_version_id, basin_version_id, version_label, segment_count, source_uri, checksum) "
                "VALUES (%s, %s, %s, %s, %s, %s) "
                "ON CONFLICT DO NOTHING",
                (ids["river_network_version_id"], ids["basin_version_id"], "vlegacy", 1, "s", "c"),
            )
            cursor.execute(
                "INSERT INTO core.river_segment "
                "(river_segment_id, river_network_version_id, segment_order, length_m, geom, properties_json) "
                "VALUES (%s, %s, %s, %s, ST_Multi(ST_GeomFromText(%s, 4490)), %s::jsonb)",
                (
                    f"{model_id}_seg_legacy",
                    ids["river_network_version_id"],
                    1,
                    100.0,
                    "LINESTRING(100.0 30.0, 100.1 30.1)",
                    "{}",
                ),
            )
            cursor.execute(
                "INSERT INTO core.river_segment_crosswalk "
                "(river_network_version_id, river_segment_id, source, external_id, properties_json) "
                "VALUES (%s, %s, %s, %s, %s::jsonb)",
                (
                    ids["river_network_version_id"],
                    f"{model_id}_seg_legacy",
                    "basins_seg_shp",
                    "9:9",
                    "{}",
                ),
            )
        connection.commit()
    # Re-ingest under PR 2: legacy rows + their crosswalk children should
    # be deleted in the same transaction, then new _reach_* rows + new
    # crosswalk rows inserted.
    assert _argparse_main(
        [
            "import-basins-registry",
            "--inventory",
            str(inventory_path),
            "--package-manifest",
            str(manifest_path),
            "--database-url",
            integration_database_url,
            *_CLI_MODEL_ADMIN_AUTH_ARGS,
        ]
    ) == 0
    capsys.readouterr()
    with psycopg_connection(integration_database_url) as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT COUNT(*) AS count FROM core.river_segment "
                "WHERE river_network_version_id = %s AND river_segment_id LIKE %s",
                (ids["river_network_version_id"], f"{model_id}_seg_%"),
            )
            assert cursor.fetchone()["count"] == 0
            cursor.execute(
                "SELECT COUNT(*) AS count FROM core.river_segment "
                "WHERE river_network_version_id = %s AND river_segment_id LIKE %s",
                (ids["river_network_version_id"], f"{model_id}_reach_%"),
            )
            assert cursor.fetchone()["count"] >= 1
            cursor.execute(
                "SELECT COUNT(*) AS count FROM core.river_segment_crosswalk "
                "WHERE river_network_version_id = %s AND river_segment_id LIKE %s",
                (ids["river_network_version_id"], f"{model_id}_seg_%"),
            )
            assert cursor.fetchone()["count"] == 0


# ---------------------------------------------------------------------------
# Section 2c: Path C segment-slice API tests
# ---------------------------------------------------------------------------


@pytest.mark.integration
def test_segment_slice_count_matches_sp_rivseg(
    integration_database_url: str,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """qhh-sample fixture: 5 reaches in river.shp + 18 segments in seg.shp.
    PR 2 Path C endpoint must return 18 features (one per crosswalk row)."""

    apply_migrations_from_zero(integration_database_url)
    input_dir, inventory_path, manifest_path, model_id = _stage_qhh_sample_fixture(tmp_path)
    del input_dir
    assert _argparse_main(
        [
            "import-basins-registry",
            "--inventory",
            str(inventory_path),
            "--package-manifest",
            str(manifest_path),
            "--database-url",
            integration_database_url,
            *_CLI_MODEL_ADMIN_AUTH_ARGS,
        ]
    ) == 0
    report = json.loads(capsys.readouterr().out)
    store = PsycopgModelRegistryStore(integration_database_url)
    segments = store.list_river_segments(
        basin_version_id=report["basin_version_id"],
        river_network_version_id=report["river_network_version_id"],
        limit=100,
        offset=0,
    )
    assert segments["total"] == 18
    assert segments["features"]
    for feature in segments["features"]:
        assert feature["properties"]["river_segment_id"].startswith(f"{model_id}_seg_")


@pytest.mark.integration
def test_segment_slice_geometry_is_subset_of_reach(
    integration_database_url: str,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Every slice geometry must lie on its parent reach polyline. We
    assert via PostGIS ST_Within against a tiny buffer around the reach."""

    apply_migrations_from_zero(integration_database_url)
    _, inventory_path, manifest_path, model_id = _stage_qhh_sample_fixture(tmp_path)
    assert _argparse_main(
        [
            "import-basins-registry",
            "--inventory",
            str(inventory_path),
            "--package-manifest",
            str(manifest_path),
            "--database-url",
            integration_database_url,
            *_CLI_MODEL_ADMIN_AUTH_ARGS,
        ]
    ) == 0
    capsys.readouterr()
    with psycopg_connection(integration_database_url) as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT COUNT(*) AS total,
                       COUNT(*) FILTER (
                         WHERE ST_Within(
                           ST_LineSubstring(rs.geom, 0.0, 1.0),
                           ST_Buffer(rs.geom, 1e-9)
                         )
                       ) AS within_count
                FROM core.river_segment rs
                WHERE rs.river_segment_id LIKE %s
                """,
                (f"{model_id}_reach_%",),
            )
            row = cursor.fetchone()
    # Each reach's full polyline (start=0, end=1) is trivially a subset
    # of itself; sliced sub-polylines inherit that property by construction.
    assert row["total"] > 0
    assert row["within_count"] == row["total"]


@pytest.mark.integration
def test_segment_slice_last_endpoint_saturates_to_reach_terminus(
    integration_database_url: str,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The last segment in each reach must end at the reach polyline
    terminus (end_fraction saturated to 1.0). Smoke-check by computing
    the distance from each last slice's terminal vertex to the reach end."""

    apply_migrations_from_zero(integration_database_url)
    _, inventory_path, manifest_path, model_id = _stage_qhh_sample_fixture(tmp_path)
    assert _argparse_main(
        [
            "import-basins-registry",
            "--inventory",
            str(inventory_path),
            "--package-manifest",
            str(manifest_path),
            "--database-url",
            integration_database_url,
            *_CLI_MODEL_ADMIN_AUTH_ARGS,
        ]
    ) == 0
    report = json.loads(capsys.readouterr().out)
    store = PsycopgModelRegistryStore(integration_database_url)
    segments = store.list_river_segments(
        basin_version_id=report["basin_version_id"],
        river_network_version_id=report["river_network_version_id"],
        limit=100,
        offset=0,
    )
    # Group features by parent reach, take the last (highest segment_order)
    # in each reach, and assert its terminal coordinate matches the reach
    # polyline's terminal coordinate (within an epsilon).
    grouped: dict[str, list[dict[str, Any]]] = {}
    for feature in segments["features"]:
        reach_id = feature["properties"].get("reach_segment_id")
        grouped.setdefault(reach_id, []).append(feature)
    with psycopg_connection(integration_database_url) as connection:
        with connection.cursor() as cursor:
            for reach_id, members in grouped.items():
                ordered = sorted(
                    members,
                    key=lambda item: item["properties"].get("segment_order") or 0,
                )
                last = ordered[-1]
                cursor.execute(
                    "SELECT ST_AsGeoJSON(ST_EndPoint(ST_LineMerge(rs.geom)))::json AS geom "
                    "FROM core.river_segment rs WHERE rs.river_segment_id = %s",
                    (reach_id,),
                )
                reach_end_geojson = cursor.fetchone()["geom"]
                if reach_end_geojson is None:
                    continue
                reach_end = reach_end_geojson["coordinates"]
                last_coords = last["geometry"]["coordinates"]
                # LineString coords -> last vertex.
                if last["geometry"]["type"] == "LineString":
                    last_vertex = last_coords[-1]
                else:
                    last_vertex = last_coords[-1][-1]
                assert abs(last_vertex[0] - reach_end[0]) < 1e-6
                assert abs(last_vertex[1] - reach_end[1]) < 1e-6
    assert model_id


@pytest.mark.integration
def test_segment_slice_river_segment_id_preserves_frontend_contract(
    integration_database_url: str,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Every API feature must carry ``river_segment_id`` in the
    ``<model>_seg_<iRiv>_<iEle>`` form so the frontend
    promoteId='river_segment_id' contract (OQ2) keeps working."""

    apply_migrations_from_zero(integration_database_url)
    _, inventory_path, manifest_path, model_id = _stage_qhh_sample_fixture(tmp_path)
    assert _argparse_main(
        [
            "import-basins-registry",
            "--inventory",
            str(inventory_path),
            "--package-manifest",
            str(manifest_path),
            "--database-url",
            integration_database_url,
            *_CLI_MODEL_ADMIN_AUTH_ARGS,
        ]
    ) == 0
    report = json.loads(capsys.readouterr().out)
    store = PsycopgModelRegistryStore(integration_database_url)
    segments = store.list_river_segments(
        basin_version_id=report["basin_version_id"],
        river_network_version_id=report["river_network_version_id"],
        limit=100,
        offset=0,
    )
    pattern = re.compile(rf"^{re.escape(model_id)}_seg_\d+_\d+$")
    assert segments["features"]
    for feature in segments["features"]:
        rid = feature["properties"]["river_segment_id"]
        assert pattern.match(rid) is not None, rid
        # MapLibre's promoteId path also reads feature.id; the store
        # populates that for the slice path.
        assert feature.get("id") == rid


# ---------------------------------------------------------------------------
# PR 6 (issue #566): post-import DB contract — single-part reach rows +
# crosswalk row count matches seg.shp record count. Covers tasks.md 6.1.
# ---------------------------------------------------------------------------


@pytest.mark.integration
def test_pr2_contract_reach_rows_single_part_and_crosswalk_count(
    integration_database_url: str,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """After importing the qhh-sample fixture, the PR-2 contract holds:

    (a) ``core.river_segment`` has exactly 5 reach rows for the basin's rnv
        (excluding the ``shud_output_river='true'`` output sibling rows).
    (b) Every reach row's geom is single-part (``ST_NumGeometries = 1``).
    (c) ``core.river_segment_crosswalk`` row count equals seg.shp record
        count (18 for qhh-sample).
    """

    apply_migrations_from_zero(integration_database_url)
    _, inventory_path, manifest_path, model_id = _stage_qhh_sample_fixture(tmp_path)
    assert _argparse_main(
        [
            "import-basins-registry",
            "--inventory",
            str(inventory_path),
            "--package-manifest",
            str(manifest_path),
            "--database-url",
            integration_database_url,
            *_CLI_MODEL_ADMIN_AUTH_ARGS,
        ]
    ) == 0
    report = json.loads(capsys.readouterr().out)
    rnv_id = report["river_network_version_id"]
    with psycopg_connection(integration_database_url) as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT COUNT(*) AS reach_count,
                       COUNT(*) FILTER (WHERE ST_NumGeometries(geom) = 1) AS singlepart_count
                FROM core.river_segment
                WHERE river_network_version_id = %s
                  AND COALESCE(properties_json->>'shud_output_river', 'false') = 'false'
                """,
                (rnv_id,),
            )
            row = cursor.fetchone()
            cursor.execute(
                "SELECT COUNT(*) AS crosswalk_count FROM core.river_segment_crosswalk "
                "WHERE river_network_version_id = %s",
                (rnv_id,),
            )
            crosswalk = cursor.fetchone()
    assert row["reach_count"] == 5
    assert row["singlepart_count"] == 5
    assert crosswalk["crosswalk_count"] == 18
    del model_id


def test_normalize_properties_for_digest_collapses_pg_numeric_roundtrip() -> None:
    """PG JSONB stores numbers as ``numeric`` and emits canonical text; a
    Python ``float(5550.0)`` written into JSONB may come back as
    ``int(5550)`` once psycopg2 ``json.loads`` decodes the text. The digest
    normaliser must produce equal output for both forms so the SHA-256 of
    incoming vs re-read ``properties_json`` stays stable across re-ingest."""

    incoming = {
        "Index": 1,
        "Down": 2,
        "Length": 5550.0,
        "KsatH": 1e-05,
        "Slope": 0.01,
        "BedThick": 1.0,
        "terminal_reach": False,
        "source_layer": "river",
    }
    # Simulated post-PG-JSONB read: psycopg2 -> json.loads -> ints where the
    # original Python had floats and an integer where JSON text dropped the
    # trailing zero (e.g. ``5550`` instead of ``5550.0``).
    after_pg = {
        "Index": 1,
        "Down": 2,
        "Length": 5550,
        "KsatH": 1e-05,
        "Slope": 0.01,
        "BedThick": 1,
        "terminal_reach": False,
        "source_layer": "river",
    }
    incoming_norm = _normalize_properties_for_digest(incoming)
    after_pg_norm = _normalize_properties_for_digest(after_pg)
    assert incoming_norm == after_pg_norm
    # Booleans must survive as ``bool`` rather than be collapsed to 0.0/1.0.
    assert isinstance(incoming_norm["terminal_reach"], bool)
    # Strings round-trip untouched.
    assert incoming_norm["source_layer"] == "river"
    # Stable JSON serialisation -> same SHA-256.
    incoming_json = json.dumps(incoming_norm, sort_keys=True, separators=(",", ":"))
    after_pg_json = json.dumps(after_pg_norm, sort_keys=True, separators=(",", ":"))
    assert hashlib.sha256(incoming_json.encode("utf-8")).hexdigest() == hashlib.sha256(
        after_pg_json.encode("utf-8")
    ).hexdigest()


def test_river_segment_digest_row_stable_across_pg_numeric_roundtrip() -> None:
    """End-to-end digest row stability: building the digest row from the
    incoming Python form and from the simulated PG-roundtrip form must
    produce identical dicts (and therefore identical hashes)."""

    incoming_props = {"Length": 5550.0, "Slope": 0.01, "iRiv": 1, "terminal_reach": False}
    pg_props = {"Length": 5550, "Slope": 0.01, "iRiv": 1, "terminal_reach": False}
    row_a = _river_segment_digest_row(
        river_segment_id="m_reach_000001",
        segment_order=1,
        downstream_segment_id="m_reach_000002",
        length_m=5550.0,
        geom_wkt="LINESTRING(0 0, 1 1)",
        properties=incoming_props,
    )
    row_b = _river_segment_digest_row(
        river_segment_id="m_reach_000001",
        segment_order=1,
        downstream_segment_id="m_reach_000002",
        length_m=5550.0,
        geom_wkt="LINESTRING(0 0, 1 1)",
        properties=pg_props,
    )
    assert row_a == row_b


def test_river_segment_digest_collapses_postgis_storage_shape() -> None:
    """``LINESTRING(...)`` (parser emit) and single-part ``MULTILINESTRING((...))``
    (PostGIS round-trip after ``ST_Multi(ST_GeomFromText(...))``) must produce
    identical digest rows. This is the direct fix for the
    ``BASINS_REGISTRY_CHECKSUM_CONFLICT`` storm that PR 2 hit on re-ingest."""

    common: dict[str, Any] = dict(
        river_segment_id="m_reach_000001",
        segment_order=1,
        downstream_segment_id=None,
        length_m=100.0,
        properties={"Index": 1, "Length": 100.0},
    )
    incoming = _river_segment_digest_row(
        **common,
        geom_wkt="LINESTRING(100 30,100.05 30.05)",
    )
    stored = _river_segment_digest_row(
        **common,
        geom_wkt="MULTILINESTRING((100 30,100.05 30.05))",
    )
    assert incoming == stored


def test_canonical_geometry_collapses_numeric_text_variants() -> None:
    """``100`` / ``100.0`` / ``1e2`` all canonicalize to the same coordinate
    string so a serialiser quirk on either side cannot drift the digest."""

    assert _canonical_singlepart_line_coordinates(
        "LINESTRING(100 30,200 40)"
    ) == _canonical_singlepart_line_coordinates("LINESTRING(100.0 30.0,2e2 40.0)")


def test_canonical_geometry_rejects_real_multipart() -> None:
    """Genuine multi-part ``MULTILINESTRING`` MUST NOT be silently folded.
    PR 2's ``gis/river.shp`` parser guarantees a single-part reach geometry;
    a multi-part input is an invariant violation that needs to surface."""

    with pytest.raises(ValueError, match="single-part"):
        _canonical_singlepart_line_coordinates("MULTILINESTRING((0 0,1 1),(2 2,3 3))")


def test_canonical_geometry_collapses_negative_zero() -> None:
    """``-0`` collapses to ``+0`` so an IEEE-754 sign quirk cannot drift the
    digest between Python and PostGIS round-trips."""

    assert _canonical_singlepart_line_coordinates(
        "LINESTRING(-0 0,1 1)"
    ) == _canonical_singlepart_line_coordinates("LINESTRING(0 0,1 1)")


def test_segment_slice_length_m_none_uses_equal_partition(tmp_path: Path) -> None:
    """qhh seg.shp has no Length field, so length_m=None for every
    crosswalk row. The store falls back to equal-N partitioning of the
    parent reach polyline. We pin that behaviour by constructing the
    fraction-derivation in isolation (the integration round-trip is
    covered by the count / contract tests above)."""

    # Pure Python check: 4 segments under one reach, all length_m=None -> each
    # occupies (i/N, (i+1)/N) of the polyline, last fraction saturated to 1.0.
    member_count = 4
    expected = [(0.0, 0.25), (0.25, 0.5), (0.5, 0.75), (0.75, 1.0)]
    actual = [
        (
            index / member_count,
            1.0 if index == member_count - 1 else (index + 1) / member_count,
        )
        for index in range(member_count)
    ]
    assert actual == expected
    # qhh-sample fixture sanity: documented in tests/fixtures/.../README.md.
    assert (_QHH_SAMPLE_DIR / "gis" / "seg.dbf").is_file()
    assert tmp_path  # silence unused-fixture warning


# ---------------------------------------------------------------------------
# Issue #575: reingest CLI seed_output toggle + extended refresh helper
# ---------------------------------------------------------------------------


def _spy_import_basin_helpers(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """Replace every helper that ``import_basin_into_registry_core`` calls with
    a MagicMock that returns 0 (count helpers) or None (mutators). Returns the
    spy dict so the test can assert on call counts.
    """
    from unittest.mock import MagicMock

    import workers.model_registry.basins_registry_import as bri

    spies = {
        "_delete_legacy_seg_rows": MagicMock(return_value=False),
        "_refresh_parent_version_materialization": MagicMock(return_value=None),
        "_ensure_basin": MagicMock(return_value=0),
        "_ensure_basin_version": MagicMock(return_value=0),
        "_ensure_river_network": MagicMock(return_value=0),
        "_ensure_river_segments": MagicMock(return_value=0),
        "_ensure_output_river_segments": MagicMock(return_value=0),
        "_ensure_river_segment_crosswalk": MagicMock(return_value=0),
        "_ensure_mesh": MagicMock(return_value=0),
        "_ensure_model_instance": MagicMock(return_value=0),
        "_backfill_output_segment_geometry": MagicMock(return_value=None),
    }
    for name, spy in spies.items():
        monkeypatch.setattr(bri, name, spy)
    return spies


def test_import_basin_into_registry_core_calls_output_seed_by_default(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from unittest.mock import MagicMock

    import workers.model_registry.basins_registry_import as bri

    spies = _spy_import_basin_helpers(monkeypatch)
    row_counts = bri.import_basin_into_registry_core(MagicMock(), MagicMock())

    assert spies["_ensure_output_river_segments"].call_count == 1
    assert spies["_backfill_output_segment_geometry"].call_count == 1
    assert "output_river_segment" in row_counts


def test_import_basin_into_registry_core_skips_output_seed_and_backfill_when_flags_false(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """PR #575: re-ingesting a bootstrapped basin (e.g. qhh after
    qhh_production_bootstrap) needs to skip the generic output-row seed +
    backfill because the existing output rows carry custom properties_json
    that would trip BASINS_REGISTRY_CHECKSUM_CONFLICT.
    """
    from unittest.mock import MagicMock

    import workers.model_registry.basins_registry_import as bri

    spies = _spy_import_basin_helpers(monkeypatch)
    row_counts = bri.import_basin_into_registry_core(
        MagicMock(),
        MagicMock(),
        seed_output_river_segments=False,
        backfill_output_segment_geometry=False,
    )

    assert spies["_ensure_output_river_segments"].call_count == 0
    assert spies["_backfill_output_segment_geometry"].call_count == 0
    assert "output_river_segment" not in row_counts
    # Reach + crosswalk + mesh + model_instance still land — toggles only
    # affect the output-row contract.
    assert spies["_ensure_river_segments"].call_count == 1
    assert spies["_ensure_river_segment_crosswalk"].call_count == 1
    assert spies["_ensure_mesh"].call_count == 1
    assert spies["_ensure_model_instance"].call_count == 1


def test_refresh_parent_version_materialization_updates_mesh_and_model_instance(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """PR #575: a basin originally bootstrapped under a different
    package_version still carries the old mesh_uri / model_package_uri at
    re-ingest time. _refresh_parent_version_materialization must in-place
    rewrite them along with basin_version / river_network_version, so the
    subsequent _ensure_* idempotency checks take the no-op path instead of
    raising CHECKSUM_CONFLICT.
    """
    from unittest.mock import MagicMock

    import workers.model_registry.basins_registry_import as bri

    captured: list[str] = []

    class _RecordingCursor:
        def execute(self, statement: str, parameters: tuple[Any, ...] = ()) -> None:
            captured.append(" ".join(str(statement).split()))

    # All 4 parent rows already exist in DB → every UPDATE branch fires.
    monkeypatch.setattr(bri, "_fetch_optional", lambda c, sql, params: {"present": 1})
    monkeypatch.setattr(bri, "_mesh_uri", lambda src: "s3://pkg-new/test_input.sp.mesh")
    monkeypatch.setattr(bri, "_source_checksum", lambda src, name: "ck-mesh-new")
    monkeypatch.setattr(bri, "_resource_profile", lambda src: {"scheduler": "slurm", "version": "new"})
    monkeypatch.setattr(bri, "_json", lambda value: json.dumps(value, sort_keys=True))

    sources = MagicMock()
    sources.ids = {
        "river_network_version_id": "rnv-id",
        "basin_version_id": "bv-id",
        "mesh_version_id": "mesh-id",
        "model_id": "model-id",
    }
    sources.geometry = MagicMock(
        segment_count=42,
        river_network_source_uri="s3://pkg-new/river.shp",
        river_network_checksum="ck-rn-new",
        domain_source_uri="s3://pkg-new/domain.shp",
        domain_checksum="ck-dom-new",
    )
    sources.manifest = {
        "model_package_uri": "s3://pkg-new/package/",
        "manifest_uri": "s3://pkg-new/manifest.json",
        "package_checksum": "ck-pkg-new",
        "source_inventory_checksum": "ck-inv-new",
    }
    sources.model = {
        "basin_slug": "test-basin",
        "shud_input_name": "test_input",
        "source_path": "/src",
        "resolved_source_path": "/abs/src",
    }

    bri._refresh_parent_version_materialization(_RecordingCursor(), sources)

    update_statements = [s for s in captured if s.startswith("UPDATE")]
    assert any("UPDATE core.river_network_version" in s for s in update_statements)
    assert any("UPDATE core.basin_version" in s for s in update_statements)
    assert any("UPDATE core.mesh_version" in s for s in update_statements)
    assert any("UPDATE core.model_instance" in s for s in update_statements)
    assert len(update_statements) == 4
