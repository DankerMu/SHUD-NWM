"""Basins package publication core: source identity, valid publication, forcing policy.

Partition 1 of 6 of the former monolith ``tests/test_basins_package_publication.py``
(issue #1912).  Shared test support lives in the non-collectible
``tests/basins_package_helpers.py``.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

import scripts.publish_scheduler_file_registry as scheduler_registry
import workers.model_registry.basins_package as basins_package
from tests.basins_package_helpers import (
    _make_valid_model,
    _manifest_payload_checksum,
    _object_store_env,
    _one_entry,
    _write_valid_inventory,
)
from workers.model_registry.basins_discovery import discover_basins_inventory, write_inventory
from workers.model_registry.cli import _argparse_main


def test_package_source_identity_is_stable_across_repair_workspaces(tmp_path: Path) -> None:
    first_inventory, first_model_id = _write_valid_inventory(
        tmp_path / "run-a" / "repaired-basins",
        forcing_count=1,
        calibration_count=1,
        basin_slug="kashigeer",
        input_name="kashigeer",
    )
    second_inventory, second_model_id = _write_valid_inventory(
        tmp_path / "run-b" / "repaired-basins",
        forcing_count=1,
        calibration_count=1,
        basin_slug="kashigeer",
        input_name="kashigeer",
    )

    first_identity = basins_package.basins_package_source_identity(
        inventory_path=first_inventory,
        model_id=first_model_id,
    )
    second_identity = basins_package.basins_package_source_identity(
        inventory_path=second_inventory,
        model_id=second_model_id,
    )
    first_model = json.loads(first_inventory.read_text(encoding="utf-8"))["models"][0]
    second_model = json.loads(second_inventory.read_text(encoding="utf-8"))["models"][0]

    assert first_identity == second_identity
    assert scheduler_registry.package_version_for_model(
        first_model,
        source_identity=first_identity,
    ) == scheduler_registry.package_version_for_model(second_model, source_identity=second_identity)


# #1813: `forcing/X000001.csv` used to be a member of this parametrization.  It
# is no longer a published source class -- a package that declares forcing
# excluded does not carry its payloads, so their bytes cannot move its identity.
# The replacement coverage is
# test_excluded_forcing_payload_changes_do_not_move_package_identity.
@pytest.mark.parametrize(
    "relative_path",
    (
        "CALIB/top01.calib",
        "input/kashigeer/kashigeer.lake.sp",
    ),
    ids=("calibration", "optional-runtime"),
)
def test_package_source_identity_changes_for_every_published_source_class(
    tmp_path: Path,
    relative_path: str,
) -> None:
    inventory_path, model_id = _write_valid_inventory(
        tmp_path,
        forcing_count=1,
        calibration_count=1,
        basin_slug="kashigeer",
        input_name="kashigeer",
    )
    before = basins_package.basins_package_source_identity(
        inventory_path=inventory_path,
        model_id=model_id,
    )
    model = json.loads(inventory_path.read_text(encoding="utf-8"))["models"][0]
    before_version = scheduler_registry.package_version_for_model(model, source_identity=before)

    source = tmp_path / "basins" / "kashigeer" / relative_path
    source.write_bytes(source.read_bytes() + b"changed\n")
    after = basins_package.basins_package_source_identity(
        inventory_path=inventory_path,
        model_id=model_id,
    )
    after_version = scheduler_registry.package_version_for_model(model, source_identity=after)

    assert before["source_sha256"] == after["source_sha256"]
    assert before["content_sha256"] != after["content_sha256"]
    assert before_version != after_version


def test_kash_style_existing_base_version_does_not_conflict_after_calibration_change(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    inventory_path, model_id = _write_valid_inventory(
        tmp_path,
        forcing_count=1,
        calibration_count=1,
        basin_slug="kashigeer",
        input_name="kashigeer",
    )
    object_root = _object_store_env(tmp_path, monkeypatch)
    model = json.loads(inventory_path.read_text(encoding="utf-8"))["models"][0]
    base_identity = basins_package.basins_package_source_identity(
        inventory_path=inventory_path,
        model_id=model_id,
    )
    legacy_base_version = "vbasins-kashigeer-c6459e91cc0a-7cbb06e9"
    basins_package.publish_basins_package(
        inventory_path=inventory_path,
        model_id=model_id,
        version=legacy_base_version,
        output_path=tmp_path / "base-manifest.json",
        expected_source_identity=base_identity,
    )

    calib = tmp_path / "basins" / "kashigeer" / "CALIB" / "top01.calib"
    calib.write_text("calib-fixed\n", encoding="utf-8")
    repaired_identity = basins_package.basins_package_source_identity(
        inventory_path=inventory_path,
        model_id=model_id,
    )
    repaired_version = scheduler_registry.package_version_for_model(model, source_identity=repaired_identity)
    result = basins_package.publish_basins_package(
        inventory_path=inventory_path,
        model_id=model_id,
        version=repaired_version,
        output_path=tmp_path / "repaired-manifest.json",
        expected_source_identity=repaired_identity,
    )

    assert repaired_version != legacy_base_version
    assert result["status"] == "published"
    assert (object_root / "models" / model_id / legacy_base_version / "manifest.json").is_file()
    assert (object_root / "models" / model_id / repaired_version / "manifest.json").is_file()


def test_publish_rejects_source_change_after_version_planning_without_outputs(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    inventory_path, model_id = _write_valid_inventory(tmp_path, forcing_count=1)
    object_root = _object_store_env(tmp_path, monkeypatch)
    expected_identity = basins_package.basins_package_source_identity(
        inventory_path=inventory_path,
        model_id=model_id,
    )
    # #1813: an excluded forcing CSV is deliberately no longer a source change,
    # so this guard is exercised with a real package file.
    mesh = tmp_path / "basins" / "basin-a" / "input" / "alias-a" / "alias-a.sp.mesh"
    mesh.write_text("484\t8\nID\tNode1\tchanged\n", encoding="utf-8")

    with pytest.raises(basins_package.BasinsPackageError) as exc_info:
        basins_package.publish_basins_package(
            inventory_path=inventory_path,
            model_id=model_id,
            version="vbasins-planned-before-change",
            output_path=tmp_path / "manifest.json",
            expected_source_identity=expected_identity,
        )

    assert exc_info.value.error_code == "BASINS_PACKAGE_SOURCE_IDENTITY_CHANGED"
    assert not (object_root / "models" / model_id / "vbasins-planned-before-change" / "manifest.json").exists()


def test_publish_basins_reserves_local_manifest_before_file_creation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    inventory_path, model_id = _write_valid_inventory(tmp_path)
    _object_store_env(tmp_path, monkeypatch)
    manifest_path = tmp_path / "workspace" / "manifest.json"
    reservations: list[tuple[Path, int]] = []

    def reject_workspace_write(path: Path, size: int) -> None:
        assert not path.exists()
        reservations.append((path, size))
        raise RuntimeError("workspace_limit_exceeded")

    with pytest.raises(RuntimeError, match="workspace_limit_exceeded"):
        basins_package.publish_basins_package(
            inventory_path=inventory_path,
            model_id=model_id,
            version="vbasins-test",
            output_path=manifest_path,
            output_capacity_guard=reject_workspace_write,
        )

    assert len(reservations) == 1
    assert reservations[0][0] == manifest_path
    assert reservations[0][1] > 0
    assert not manifest_path.exists()
    assert not manifest_path.parent.exists()
    object_root = tmp_path / "object-store" / "models" / model_id / "vbasins-test"
    assert not any(path.is_file() for path in object_root.rglob("*"))


def test_publish_basins_writes_manifest_package_and_success_payload(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    inventory_path, model_id = _write_valid_inventory(tmp_path, forcing_count=1, calibration_count=1)
    object_root = _object_store_env(tmp_path, monkeypatch)
    manifest_path = tmp_path / "manifest.json"

    exit_code = _argparse_main(
        [
            "publish-basins",
            "--inventory",
            str(inventory_path),
            "--model-id",
            model_id,
            "--version",
            "vbasins-test",
            "--output",
            str(manifest_path),
        ]
    )

    payload = json.loads(capsys.readouterr().out)
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert exit_code == 0
    assert payload == {
        "status": "published",
        "model_id": model_id,
        "version": "vbasins-test",
        "model_package_uri": f"s3://nhms/models/{model_id}/vbasins-test/package/",
        "manifest_uri": f"s3://nhms/models/{model_id}/vbasins-test/manifest.json",
        "package_checksum": manifest["package_checksum"],
    }
    assert manifest["schema_version"] == basins_package.BASINS_PACKAGE_SCHEMA_VERSION
    assert manifest["model_id"] == model_id
    assert manifest["source_inventory_checksum"]
    assert manifest["source_path"]
    assert manifest["resolved_source_path"]
    assert manifest["source_is_symlink"] is False
    assert manifest["created_at"].endswith("Z")
    assert manifest["model_package_uri"] == f"s3://nhms/models/{model_id}/vbasins-test/package/"
    assert manifest["manifest_uri"] == f"s3://nhms/models/{model_id}/vbasins-test/manifest.json"
    assert (object_root / "models" / model_id / "vbasins-test" / "manifest.json").is_file()
    assert (object_root / "models" / model_id / "vbasins-test" / "package" / "alias-a.cfg.para").is_file()
    assert (object_root / "models" / model_id / "vbasins-test" / "package" / "alias-a.lake.sp").is_file()
    assert (object_root / "models" / model_id / "vbasins-test" / "package" / "alias-a.lake.bathy").is_file()
    assert (object_root / "models" / model_id / "vbasins-test" / "package" / "alias-a.lake.ic").is_file()
    assert (object_root / "models" / model_id / "vbasins-test" / "package" / "gis" / "domain.shp").is_file()
    assert (object_root / "models" / model_id / "vbasins-test" / "package" / "CALIB" / "top01.calib").is_file()
    assert {
        "relative_path",
        "object_uri",
        "size_bytes",
        "sha256",
        "role",
    } <= manifest["included_files"][0].keys()
    assert {entry["role"] for entry in manifest["included_files"]} == {
        "runtime_input",
        "gis",
        "calibration",
        "manifest",
    }
    manifest_entry = _one_entry(manifest, "manifest")
    object_manifest = object_root / "models" / model_id / "vbasins-test" / "manifest.json"
    object_manifest_bytes = object_manifest.read_bytes()
    assert manifest_entry["relative_path"] == "manifest.json"
    assert manifest_entry["object_uri"] == manifest["manifest_uri"]
    assert manifest_entry["size_bytes"] == len(object_manifest_bytes)
    assert manifest_entry["sha256"] == _manifest_payload_checksum(manifest)
    assert manifest["calibration"]["source_count"] == 1
    assert manifest["calibration"]["included_count"] == 1
    assert manifest["forcing"]["policy"] == "excluded_by_default"
    assert manifest["forcing"]["csv_count"] == 1
    # #1813: no aggregate payload digest is produced when payloads are excluded.
    assert manifest["forcing"]["aggregate_checksum"] is None
    assert manifest["forcing"]["sample_headers"] == ["time,value"]
    assert manifest["forcing"]["time_coverage"] == {"start": "2026-01-01", "end": "2026-01-01"}


def test_publish_basins_is_idempotent_for_unchanged_source(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    inventory_path, model_id = _write_valid_inventory(tmp_path)
    _object_store_env(tmp_path, monkeypatch)
    manifest_path = tmp_path / "manifest.json"
    args = [
        "publish-basins",
        "--inventory",
        str(inventory_path),
        "--model-id",
        model_id,
        "--version",
        "vbasins-test",
        "--output",
        str(manifest_path),
    ]

    assert _argparse_main(args) == 0
    first_payload = json.loads(capsys.readouterr().out)
    first_manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert _argparse_main(args) == 0
    second_payload = json.loads(capsys.readouterr().out)
    second_manifest = json.loads(manifest_path.read_text(encoding="utf-8"))

    assert first_payload["status"] == "published"
    assert second_payload["status"] == "already_done"
    assert second_payload["package_checksum"] == first_payload["package_checksum"]
    assert second_manifest == first_manifest


def test_publish_basins_rejects_tampered_existing_manifest_before_local_output(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    inventory_path, model_id = _write_valid_inventory(tmp_path)
    object_root = _object_store_env(tmp_path, monkeypatch)
    first_output = tmp_path / "first_manifest.json"
    second_output = tmp_path / "second_manifest.json"
    args = [
        "publish-basins",
        "--inventory",
        str(inventory_path),
        "--model-id",
        model_id,
        "--version",
        "vbasins-test",
        "--output",
    ]
    assert _argparse_main([*args, str(first_output)]) == 0
    capsys.readouterr()

    object_manifest = object_root / "models" / model_id / "vbasins-test" / "manifest.json"
    manifest = json.loads(object_manifest.read_text(encoding="utf-8"))
    manifest_entry = _one_entry(manifest, "manifest")
    manifest_entry["sha256"] = "0" * 64
    object_manifest.write_bytes(basins_package._json_bytes(manifest))

    exit_code = _argparse_main([*args, str(second_output)])

    captured = capsys.readouterr()
    error = json.loads(captured.err)
    assert exit_code == 1
    assert captured.out == ""
    assert error["error_code"] == "BASINS_PACKAGE_MANIFEST_INVALID"
    assert error["model_id"] == model_id
    assert error["version"] == "vbasins-test"
    assert error["manifest_uri"] == f"s3://nhms/models/{model_id}/vbasins-test/manifest.json"
    assert not second_output.exists()


def test_publish_basins_rejects_tampered_existing_package_object_before_local_output(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    inventory_path, model_id = _write_valid_inventory(tmp_path)
    object_root = _object_store_env(tmp_path, monkeypatch)
    first_output = tmp_path / "first_manifest.json"
    second_output = tmp_path / "second_manifest.json"
    args = [
        "publish-basins",
        "--inventory",
        str(inventory_path),
        "--model-id",
        model_id,
        "--version",
        "vbasins-test",
        "--output",
    ]
    assert _argparse_main([*args, str(first_output)]) == 0
    capsys.readouterr()

    package_object = object_root / "models" / model_id / "vbasins-test" / "package" / "alias-a.cfg.para"
    package_object.write_text("tampered\n", encoding="utf-8")

    exit_code = _argparse_main([*args, str(second_output)])

    captured = capsys.readouterr()
    error = json.loads(captured.err)
    assert exit_code == 1
    assert captured.out == ""
    assert error["error_code"] == "BASINS_PACKAGE_MANIFEST_INVALID"
    assert error["model_id"] == model_id
    assert error["version"] == "vbasins-test"
    assert error["manifest_uri"] == f"s3://nhms/models/{model_id}/vbasins-test/manifest.json"
    assert not second_output.exists()


def test_publish_basins_rejects_checksum_conflict_for_same_version(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    inventory_path, model_id = _write_valid_inventory(tmp_path)
    object_root = _object_store_env(tmp_path, monkeypatch)
    manifest_path = tmp_path / "manifest.json"
    args = [
        "publish-basins",
        "--inventory",
        str(inventory_path),
        "--model-id",
        model_id,
        "--version",
        "vbasins-test",
        "--output",
        str(manifest_path),
    ]
    assert _argparse_main(args) == 0
    capsys.readouterr()
    previous_manifest = json.loads(manifest_path.read_text(encoding="utf-8"))

    source_file = tmp_path / "basins" / "basin-a" / "input" / "alias-a" / "alias-a.cfg.para"
    source_file.write_text("mutated\n", encoding="utf-8")
    write_inventory(discover_basins_inventory(tmp_path / "basins"), inventory_path)
    exit_code = _argparse_main(args)

    captured = capsys.readouterr()
    error = json.loads(captured.err)
    assert exit_code == 1
    assert captured.out == ""
    assert error["error_code"] == "BASINS_PACKAGE_CHECKSUM_CONFLICT"
    assert error["model_id"] == model_id
    assert error["version"] == "vbasins-test"
    assert error["manifest_uri"] == f"s3://nhms/models/{model_id}/vbasins-test/manifest.json"
    object_manifest = object_root / "models" / model_id / "vbasins-test" / "manifest.json"
    assert json.loads(object_manifest.read_text()) == previous_manifest


def test_publish_basins_rejects_oversized_existing_manifest_before_conflict_check(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    inventory_path, model_id = _write_valid_inventory(tmp_path)
    object_root = _object_store_env(tmp_path, monkeypatch)
    manifest_path = tmp_path / "manifest.json"
    args = [
        "publish-basins",
        "--inventory",
        str(inventory_path),
        "--model-id",
        model_id,
        "--version",
        "vbasins-test",
        "--output",
        str(manifest_path),
    ]
    assert _argparse_main(args) == 0
    capsys.readouterr()

    object_manifest = object_root / "models" / model_id / "vbasins-test" / "manifest.json"
    object_manifest.write_bytes(b"{" + (b'"padding":' + b'"x"' * basins_package.MAX_EXISTING_MANIFEST_BYTES))
    source_file = tmp_path / "basins" / "basin-a" / "input" / "alias-a" / "alias-a.cfg.para"
    source_file.write_text("mutated\n", encoding="utf-8")
    write_inventory(discover_basins_inventory(tmp_path / "basins"), inventory_path)

    exit_code = _argparse_main(args)

    captured = capsys.readouterr()
    error = json.loads(captured.err)
    assert exit_code == 1
    assert captured.out == ""
    assert error["error_code"] == "BASINS_PACKAGE_MANIFEST_INVALID"
    assert error["model_id"] == model_id
    assert error["version"] == "vbasins-test"
    assert error["manifest_uri"] == f"s3://nhms/models/{model_id}/vbasins-test/manifest.json"
    assert manifest_path.exists()


def test_publish_basins_checksum_ignores_benign_inventory_churn(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    inventory_path, model_id = _write_valid_inventory(tmp_path)
    _object_store_env(tmp_path, monkeypatch)
    manifest_path = tmp_path / "manifest.json"
    args = [
        "publish-basins",
        "--inventory",
        str(inventory_path),
        "--model-id",
        model_id,
        "--version",
        "vbasins-test",
        "--output",
        str(manifest_path),
    ]
    assert _argparse_main(args) == 0
    first_payload = json.loads(capsys.readouterr().out)

    inventory = json.loads(inventory_path.read_text(encoding="utf-8"))
    inventory["benign_unrelated_field"] = {"note": "inventory-only churn"}
    inventory["models"].append(
        {
            "model_id": "basins_unrelated_shud",
            "status": "partial",
            "default_publish_eligible": False,
        }
    )
    inventory_path.write_text(json.dumps(inventory, indent=4, sort_keys=False) + "\n", encoding="utf-8")
    assert _argparse_main(args) == 0
    second_payload = json.loads(capsys.readouterr().out)
    second_manifest = json.loads(manifest_path.read_text(encoding="utf-8"))

    assert second_payload["status"] == "already_done"
    assert second_payload["package_checksum"] == first_payload["package_checksum"]
    assert second_manifest["package_checksum"] == first_payload["package_checksum"]


def test_publish_basins_rejects_relabelled_inventory_model_id_before_writes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    inventory_path, model_id = _write_valid_inventory(tmp_path)
    object_root = _object_store_env(tmp_path, monkeypatch)
    requested_model_id = "basins_relabelled_shud"
    inventory = json.loads(inventory_path.read_text(encoding="utf-8"))
    model = inventory["models"][0]
    model["model_id"] = requested_model_id
    model["suggested_ids"]["model_id"] = requested_model_id
    inventory_path.write_text(json.dumps(inventory, indent=2, sort_keys=True) + "\n", encoding="utf-8")

    exit_code = _argparse_main(
        [
            "publish-basins",
            "--inventory",
            str(inventory_path),
            "--model-id",
            requested_model_id,
            "--version",
            "vbasins-relabelled",
            "--output",
            str(tmp_path / "manifest.json"),
        ]
    )

    captured = capsys.readouterr()
    error = json.loads(captured.err)
    assert exit_code == 1
    assert captured.out == ""
    assert error["error_code"] == "BASINS_MODEL_ID_MISMATCH"
    assert error["model_id"] == requested_model_id
    assert error["version"] == "vbasins-relabelled"
    assert "manifest_uri" not in error
    assert not (object_root / "models" / requested_model_id / "vbasins-relabelled").exists()


def test_publish_basins_rejects_relabelled_identity_when_source_paths_unchanged_before_writes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    inventory_path, _model_id = _write_valid_inventory(tmp_path)
    object_root = _object_store_env(tmp_path, monkeypatch)
    requested_model_id = "basins_other_basin_shud"
    output = tmp_path / "manifest.json"
    inventory = json.loads(inventory_path.read_text(encoding="utf-8"))
    model = inventory["models"][0]
    model["basin_slug"] = "other-basin"
    model["model_id"] = requested_model_id
    model["suggested_ids"]["model_id"] = requested_model_id
    inventory_path.write_text(json.dumps(inventory, indent=2, sort_keys=True) + "\n", encoding="utf-8")

    exit_code = _argparse_main(
        [
            "publish-basins",
            "--inventory",
            str(inventory_path),
            "--model-id",
            requested_model_id,
            "--version",
            "vbasins-relabelled-source-path",
            "--output",
            str(output),
        ]
    )

    captured = capsys.readouterr()
    error = json.loads(captured.err)
    assert exit_code == 1
    assert captured.out == ""
    assert error["error_code"] == "BASINS_MODEL_ID_MISMATCH"
    assert error["model_id"] == requested_model_id
    assert error["version"] == "vbasins-relabelled-source-path"
    assert "manifest_uri" not in error
    assert not output.exists()
    assert not (object_root / "models" / requested_model_id / "vbasins-relabelled-source-path").exists()


def test_publish_basins_rejects_duplicate_inventory_model_id_before_writes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    inventory_path, model_id = _write_valid_inventory(tmp_path)
    object_root = _object_store_env(tmp_path, monkeypatch)
    inventory = json.loads(inventory_path.read_text(encoding="utf-8"))
    duplicate = dict(inventory["models"][0])
    duplicate["basin_slug"] = "other-basin"
    duplicate["source_path"] = str(tmp_path / "basins" / "other-basin")
    inventory["models"].append(duplicate)
    inventory_path.write_text(json.dumps(inventory, indent=2, sort_keys=True) + "\n", encoding="utf-8")

    exit_code = _argparse_main(
        [
            "publish-basins",
            "--inventory",
            str(inventory_path),
            "--model-id",
            model_id,
            "--version",
            "vbasins-duplicate-id",
            "--output",
            str(tmp_path / "manifest.json"),
        ]
    )

    captured = capsys.readouterr()
    error = json.loads(captured.err)
    assert exit_code == 1
    assert captured.out == ""
    assert error["error_code"] == "BASINS_MODEL_ID_DUPLICATE"
    assert error["model_id"] == model_id
    assert error["version"] == "vbasins-duplicate-id"
    assert "manifest_uri" not in error
    assert not (object_root / "models" / model_id / "vbasins-duplicate-id").exists()


def test_publish_basins_excludes_forcing_payloads_by_default(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    inventory_path, model_id = _write_valid_inventory(tmp_path, forcing_count=10)
    object_root = _object_store_env(tmp_path, monkeypatch)

    assert (
        _argparse_main(
            [
                "publish-basins",
                "--inventory",
                str(inventory_path),
                "--model-id",
                model_id,
                "--version",
                "vbasins-test",
                "--output",
                str(tmp_path / "manifest.json"),
            ]
        )
        == 0
    )
    manifest = json.loads((tmp_path / "manifest.json").read_text(encoding="utf-8"))

    assert manifest["forcing"]["csv_count"] == 10
    assert manifest["forcing"]["byte_count"] > 0
    # Count and bytes survive on stat alone; no payload is read end-to-end.
    assert manifest["forcing"]["aggregate_checksum"] is None
    assert manifest["forcing"]["payload_copied"] is False
    assert manifest["forcing"]["forcing_payload_uri"] is None
    assert all(entry["role"] != "forcing" for entry in manifest["included_files"])
    assert not (object_root / "models" / model_id / "vbasins-test" / "forcing").exists()


def test_publish_basins_copy_forcing_writes_explicit_payload_prefix(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    inventory_path, model_id = _write_valid_inventory(tmp_path, forcing_count=2)
    object_root = _object_store_env(tmp_path, monkeypatch)

    assert (
        _argparse_main(
            [
                "publish-basins",
                "--inventory",
                str(inventory_path),
                "--model-id",
                model_id,
                "--version",
                "vbasins-test",
                "--output",
                str(tmp_path / "manifest.json"),
                "--copy-forcing",
            ]
        )
        == 0
    )
    manifest = json.loads((tmp_path / "manifest.json").read_text(encoding="utf-8"))

    assert manifest["forcing"]["policy"] == "copied_explicitly"
    assert manifest["forcing"]["payload_copied"] is True
    assert manifest["forcing"]["forcing_payload_uri"] == f"s3://nhms/models/{model_id}/vbasins-test/forcing/"
    assert manifest["forcing"]["copied_file_count"] == 2
    assert manifest["forcing"]["copied_byte_count"] == manifest["forcing"]["byte_count"]
    assert len([entry for entry in manifest["included_files"] if entry["role"] == "forcing"]) == 2
    assert (object_root / "models" / model_id / "vbasins-test" / "forcing" / "X000001.csv").is_file()
    assert (object_root / "models" / model_id / "vbasins-test" / "forcing" / "X000002.csv").is_file()


def test_publish_basins_accepts_relative_discovery_inventory_after_cwd_change(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    workspace = tmp_path / "workspace"
    root = workspace / "basins"
    _make_valid_model(root / "basin-a", "alias-a", forcing_count=1, calibration_count=1)
    workspace.mkdir(exist_ok=True)
    monkeypatch.chdir(workspace)
    inventory = discover_basins_inventory(Path("basins"))
    inventory_path = tmp_path / "inventory.json"
    write_inventory(inventory, inventory_path)
    model_id = inventory["models"][0]["model_id"]
    assert inventory["models"][0]["input_dir"] == "basins/basin-a/input/alias-a"
    _object_store_env(tmp_path, monkeypatch)

    other_cwd = tmp_path / "other-cwd"
    other_cwd.mkdir()
    monkeypatch.chdir(other_cwd)
    output = tmp_path / "manifest.json"
    exit_code = _argparse_main(
        [
            "publish-basins",
            "--inventory",
            str(inventory_path),
            "--model-id",
            model_id,
            "--version",
            "vbasins-relative-cwd",
            "--output",
            str(output),
        ]
    )

    payload = json.loads(capsys.readouterr().out)
    manifest = json.loads(output.read_text(encoding="utf-8"))
    assert exit_code == 0
    assert payload["status"] == "published"
    assert manifest["model_id"] == model_id
    assert manifest["resolved_source_path"] == str((root / "basin-a").resolve())
    assert manifest["forcing"]["csv_count"] == 1


def test_publish_basins_forcing_metadata_is_bounded_and_copy_uses_iterator(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    inventory_path, model_id = _write_valid_inventory(tmp_path, forcing_count=7)
    _object_store_env(tmp_path, monkeypatch)
    original_walk = basins_package._walk_source_files
    yielded = 0

    def counting_walk(root: Path, source_root: Path) -> object:
        nonlocal yielded
        for path in original_walk(root, source_root):
            yielded += 1
            yield path

    monkeypatch.setattr(basins_package, "_walk_source_files", counting_walk)

    assert (
        _argparse_main(
            [
                "publish-basins",
                "--inventory",
                str(inventory_path),
                "--model-id",
                model_id,
                "--version",
                "vbasins-forcing-iter",
                "--output",
                str(tmp_path / "manifest.json"),
                "--copy-forcing",
            ]
        )
        == 0
    )
    manifest = json.loads((tmp_path / "manifest.json").read_text(encoding="utf-8"))

    assert yielded >= 7
    assert manifest["forcing"]["csv_count"] == 7
    assert manifest["forcing"]["copied_file_count"] == 7
    assert manifest["forcing"]["sample_file_limit"] == 5
    assert manifest["forcing"]["sampled_file_count"] == 5
    assert len(manifest["forcing"]["sample_headers"]) == 1


def test_publish_basins_forcing_time_evidence_samples_file_limit_not_unique_headers(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    inventory_path, model_id = _write_valid_inventory(tmp_path, forcing_count=8)
    _object_store_env(tmp_path, monkeypatch)
    sampled_paths: list[Path] = []
    original_csv_time_evidence = basins_package._csv_time_evidence

    def counting_csv_time_evidence(
        path: Path,
        source_root: Path,
        *,
        model_id: str | None = None,
        version: str | None = None,
        manifest_uri: str | None = None,
    ) -> tuple[str | None, str | None, str | None, int]:
        sampled_paths.append(path)
        return original_csv_time_evidence(
            path,
            source_root,
            model_id=model_id,
            version=version,
            manifest_uri=manifest_uri,
        )

    monkeypatch.setattr(basins_package, "_csv_time_evidence", counting_csv_time_evidence)

    assert (
        _argparse_main(
            [
                "publish-basins",
                "--inventory",
                str(inventory_path),
                "--model-id",
                model_id,
                "--version",
                "vbasins-forcing-sample-limit",
                "--output",
                str(tmp_path / "manifest.json"),
            ]
        )
        == 0
    )
    manifest = json.loads((tmp_path / "manifest.json").read_text(encoding="utf-8"))

    assert len(sampled_paths) == basins_package.FORCING_SAMPLE_FILE_LIMIT
    assert manifest["forcing"]["csv_count"] == 8
    assert manifest["forcing"]["sampled_file_count"] == basins_package.FORCING_SAMPLE_FILE_LIMIT
    assert manifest["forcing"]["sample_headers"] == ["time,value"]
