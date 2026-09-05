"""Basins registry import: manifests, checksums, TOCTOU, traversal and CWD contracts.

Partition 4 of 7 of the former monolith ``tests/test_basins_registry_import.py``
(issue #1913): the 20 pre-database refusal contracts that pin the importer against
mutated sources, symlink swaps, path traversal and relative-path reinterpretation.
Shared test support lives in the non-collectible
``tests/basins_registry_import_helpers.py``.
"""

from __future__ import annotations

import hashlib
import json
import shutil
from pathlib import Path

import pytest

import workers.model_registry.basins_geometry as basins_geometry
from tests.basins_registry_import_helpers import (
    _CLI_MODEL_ADMIN_AUTH_ARGS,
    _copy_matching_fixture_payload,
    _make_valid_model,
    _package_manifest_for_model,
    _replace_directory_with_symlink,
    _sha256_file,
    _write_registry_fixture,
)
from workers.model_registry.basins_discovery import (
    discover_basins_inventory,
    write_inventory,
)
from workers.model_registry.basins_registry_import import (
    BasinsRegistryImportError,
    prepare_basins_import_sources,
    prepare_relocated_basins_import_sources_after_package_verification,
)
from workers.model_registry.cli import _argparse_main


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


def test_import_refusal_payload_carries_model_causes_before_database(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A model the #1197 IC header gate marked invalid is refused for import; the
    refusal payload has to carry the model record's cause keys (#1432)."""

    root, input_dir, inventory_path, manifest_path, model_id = _write_registry_fixture(tmp_path)
    (input_dir / "alias-a.cfg.ic").write_text("23106\t6\n1\t0.1\n", encoding="utf-8")
    inventory = discover_basins_inventory(root)
    write_inventory(inventory, inventory_path)
    model = inventory["models"][0]
    manifest = _package_manifest_for_model(model, model_id, inventory=inventory)
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
    assert error["error_code"] == "BASINS_REGISTRY_MODEL_NOT_IMPORTABLE"
    assert error["message"] == "Basins model is not importable from this inventory."
    assert error["model_id"] == model_id
    assert error["path"] == model["source_path"]
    assert error["status"] == "partial"
    assert error["missing_required_files"] == []
    assert [reason.split(":")[0] for reason in error["invalid_required_files"]] == ["alias-a.cfg.ic"]
    assert error["unreadable_required_files"] == []


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
