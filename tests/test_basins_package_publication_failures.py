"""Basins publication failures: output, planning, lock and stale-state contracts.

Partition 3 of 6 of the former monolith ``tests/test_basins_package_publication.py``
(issue #1912).  Shared test support lives in the non-collectible
``tests/basins_package_helpers.py``.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

import workers.model_registry.basins_package as basins_package
from tests.basins_package_helpers import (
    _make_valid_model,
    _object_store_env,
    _required_files_for_input_name,
    _write_valid_inventory,
)
from workers.model_registry.basins_discovery import discover_basins_inventory, write_inventory
from workers.model_registry.cli import _argparse_main


def test_publish_basins_reports_output_write_failure_as_json(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    inventory_path, model_id = _write_valid_inventory(tmp_path)
    object_root = _object_store_env(tmp_path, monkeypatch)
    output_parent = tmp_path / "not-a-dir"
    output_parent.write_text("file blocks output parent\n", encoding="utf-8")

    exit_code = _argparse_main(
        [
            "publish-basins",
            "--inventory",
            str(inventory_path),
            "--model-id",
            model_id,
            "--version",
            "vbasins-output-fail",
            "--output",
            str(output_parent / "manifest.json"),
        ]
    )

    captured = capsys.readouterr()
    error = json.loads(captured.err)
    assert exit_code == 1
    assert captured.out == ""
    assert error["error_code"] == "BASINS_PACKAGE_OUTPUT_WRITE_FAILED"
    assert error["model_id"] == model_id
    assert error["version"] == "vbasins-output-fail"
    assert error["manifest_uri"] == f"s3://nhms/models/{model_id}/vbasins-output-fail/manifest.json"
    assert error["path"] == str(output_parent / "manifest.json")
    assert "Traceback" not in captured.err
    assert not (object_root / "models" / model_id / "vbasins-output-fail" / "manifest.json").exists()


def test_publish_basins_reports_stale_required_file_planning_failure_as_json(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    inventory_path, model_id = _write_valid_inventory(tmp_path)
    object_root = _object_store_env(tmp_path, monkeypatch)
    output = tmp_path / "manifest.json"
    original_package_source_files = basins_package._package_source_files
    deleted_paths: list[Path] = []

    def stale_package_source_files(*args: object, **kwargs: object) -> list[basins_package.SourceFile]:
        files = original_package_source_files(*args, **kwargs)
        required_file = next(
            source_file
            for source_file in files
            if source_file.role == "runtime_input" and source_file.relative_path.endswith(".cfg.para")
        )
        required_file.source_path.unlink()
        deleted_paths.append(required_file.source_path)
        return files

    monkeypatch.setattr(basins_package, "_package_source_files", stale_package_source_files)

    exit_code = _argparse_main(
        [
            "publish-basins",
            "--inventory",
            str(inventory_path),
            "--model-id",
            model_id,
            "--version",
            "vbasins-stale-source",
            "--output",
            str(output),
        ]
    )

    captured = capsys.readouterr()
    error = json.loads(captured.err)
    assert exit_code == 1
    assert captured.out == ""
    assert error["error_code"] == "BASINS_PACKAGE_WRITE_FAILED"
    assert error["model_id"] == model_id
    assert error["version"] == "vbasins-stale-source"
    assert error["path"] == str(deleted_paths[0])
    assert error["manifest_uri"] == f"s3://nhms/models/{model_id}/vbasins-stale-source/manifest.json"
    assert "Traceback" not in captured.err
    assert not output.exists()
    assert not (object_root / "models" / model_id / "vbasins-stale-source" / "manifest.json").exists()


def test_publish_basins_reports_deleted_required_file_before_planning_with_manifest_uri(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    inventory_path, model_id = _write_valid_inventory(tmp_path)
    object_root = _object_store_env(tmp_path, monkeypatch)
    output = tmp_path / "manifest.json"
    deleted_file = tmp_path / "basins" / "basin-a" / "input" / "alias-a" / "alias-a.cfg.para"
    deleted_file.unlink()

    exit_code = _argparse_main(
        [
            "publish-basins",
            "--inventory",
            str(inventory_path),
            "--model-id",
            model_id,
            "--version",
            "vbasins-deleted-before-planning",
            "--output",
            str(output),
        ]
    )

    captured = capsys.readouterr()
    error = json.loads(captured.err)
    assert exit_code == 1
    assert captured.out == ""
    assert error["error_code"] == "BASINS_SOURCE_NOT_FOUND"
    assert error["model_id"] == model_id
    assert error["version"] == "vbasins-deleted-before-planning"
    assert error["path"] == str(deleted_file)
    assert error["manifest_uri"] == f"s3://nhms/models/{model_id}/vbasins-deleted-before-planning/manifest.json"
    assert "Traceback" not in captured.err
    assert not output.exists()
    assert not (object_root / "models" / model_id / "vbasins-deleted-before-planning" / "manifest.json").exists()


def test_publish_basins_does_not_write_local_output_when_manifest_verify_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    inventory_path, model_id = _write_valid_inventory(tmp_path)
    object_root = _object_store_env(tmp_path, monkeypatch)
    output = tmp_path / "manifest.json"
    original_verify = basins_package._verify_object_bytes

    def failing_manifest_verify(
        store: object,
        key: str,
        *,
        expected_size: int,
        expected_sha256: str,
        model_id: str | None = None,
        version: str | None = None,
        manifest_uri: str | None = None,
    ) -> None:
        if key.endswith("/manifest.json"):
            raise basins_package.ObjectStoreError("synthetic manifest verification failure")
        original_verify(  # type: ignore[arg-type]
            store,
            key,
            expected_size=expected_size,
            expected_sha256=expected_sha256,
            model_id=model_id,
            version=version,
            manifest_uri=manifest_uri,
        )

    monkeypatch.setattr(basins_package, "_verify_object_bytes", failing_manifest_verify)

    exit_code = _argparse_main(
        [
            "publish-basins",
            "--inventory",
            str(inventory_path),
            "--model-id",
            model_id,
            "--version",
            "vbasins-manifest-verify-fail",
            "--output",
            str(output),
        ]
    )

    captured = capsys.readouterr()
    error = json.loads(captured.err)
    assert exit_code == 1
    assert captured.out == ""
    assert error["error_code"] == "BASINS_PACKAGE_WRITE_FAILED"
    assert error["model_id"] == model_id
    assert error["version"] == "vbasins-manifest-verify-fail"
    assert not output.exists()
    assert (object_root / "models" / model_id / "vbasins-manifest-verify-fail" / "manifest.json").is_file()


def test_publish_basins_rejects_tampered_inventory_paths(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    inventory_path, model_id = _write_valid_inventory(tmp_path)
    outside_root = tmp_path / "outside"
    _make_valid_model(outside_root / "basin-a", "alias-a")
    inventory = json.loads(inventory_path.read_text(encoding="utf-8"))
    model = inventory["models"][0]
    model["resolved_source_path"] = str((outside_root / "basin-a").resolve())
    model["source_path"] = str(outside_root / "basin-a")
    model["input_dir"] = str(outside_root / "basin-a" / "input" / "alias-a")
    write_inventory(inventory, inventory_path)
    _object_store_env(tmp_path, monkeypatch)

    exit_code = _argparse_main(
        [
            "publish-basins",
            "--inventory",
            str(inventory_path),
            "--model-id",
            model_id,
            "--version",
            "vbasins-tampered",
            "--output",
            str(tmp_path / "manifest.json"),
        ]
    )

    captured = capsys.readouterr()
    error = json.loads(captured.err)
    assert exit_code == 1
    assert captured.out == ""
    assert error["error_code"] == "BASINS_INVENTORY_PATH_MISMATCH"
    assert error["model_id"] == model_id
    assert error["version"] == "vbasins-tampered"
    assert error["path"] == str((outside_root / "basin-a").resolve())


def test_publish_basins_rejects_same_root_tampered_input_dir_without_outputs(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    inventory_path, model_id = _write_valid_inventory(tmp_path)
    root = tmp_path / "basins"
    alt_input_dir = _make_valid_model(root / "basin-a" / "alt", "alt-alias")
    inventory = json.loads(inventory_path.read_text(encoding="utf-8"))
    model = inventory["models"][0]
    model["status"] = "valid"
    model["default_publish_eligible"] = True
    model["missing_required_files"] = []
    model["input_dir"] = str(alt_input_dir)
    model["gis_dir"] = str(alt_input_dir / "gis")
    model["required_files"] = _required_files_for_input_name("alt-alias")
    write_inventory(inventory, inventory_path)
    object_root = _object_store_env(tmp_path, monkeypatch)
    output = tmp_path / "manifest.json"

    exit_code = _argparse_main(
        [
            "publish-basins",
            "--inventory",
            str(inventory_path),
            "--model-id",
            model_id,
            "--version",
            "vbasins-same-root-input-tampered",
            "--output",
            str(output),
        ]
    )

    captured = capsys.readouterr()
    error = json.loads(captured.err)
    assert exit_code == 1
    assert captured.out == ""
    assert error["error_code"] == "BASINS_INVENTORY_PATH_MISMATCH"
    assert error["model_id"] == model_id
    assert error["version"] == "vbasins-same-root-input-tampered"
    assert error["path"] == str(alt_input_dir.resolve())
    assert not output.exists()
    assert not (object_root / "models" / model_id / "vbasins-same-root-input-tampered" / "manifest.json").exists()
    assert not (object_root / "models" / model_id / "vbasins-same-root-input-tampered" / "package").exists()


def test_publish_basins_rejects_same_root_tampered_forcing_dir_without_outputs(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    inventory_path, model_id = _write_valid_inventory(tmp_path, forcing_count=1)
    root = tmp_path / "basins"
    alt_forcing_dir = root / "basin-a" / "alt-forcing"
    alt_forcing_dir.mkdir()
    (alt_forcing_dir / "X999999.csv").write_text("time,value\n2026-01-01,999\n", encoding="utf-8")
    inventory = json.loads(inventory_path.read_text(encoding="utf-8"))
    model = inventory["models"][0]
    model["forcing_dir"] = str(alt_forcing_dir)
    write_inventory(inventory, inventory_path)
    object_root = _object_store_env(tmp_path, monkeypatch)
    output = tmp_path / "manifest.json"

    exit_code = _argparse_main(
        [
            "publish-basins",
            "--inventory",
            str(inventory_path),
            "--model-id",
            model_id,
            "--version",
            "vbasins-same-root-forcing-tampered",
            "--output",
            str(output),
        ]
    )

    captured = capsys.readouterr()
    error = json.loads(captured.err)
    assert exit_code == 1
    assert captured.out == ""
    assert error["error_code"] == "BASINS_INVENTORY_PATH_MISMATCH"
    assert error["model_id"] == model_id
    assert error["version"] == "vbasins-same-root-forcing-tampered"
    assert error["path"] == str(alt_forcing_dir.resolve())
    assert not output.exists()
    assert not (object_root / "models" / model_id / "vbasins-same-root-forcing-tampered" / "manifest.json").exists()
    assert not (object_root / "models" / model_id / "vbasins-same-root-forcing-tampered" / "package").exists()
    assert not (object_root / "models" / model_id / "vbasins-same-root-forcing-tampered" / "forcing").exists()


@pytest.mark.parametrize(
    ("field_name", "tampered_value", "version"),
    [
        ("input_dir", "other/basin-a/input/alias-a", "vbasins-relative-input-prefix-tampered"),
        ("gis_dir", "other/basin-a/input/alias-a/gis", "vbasins-relative-gis-prefix-tampered"),
        ("forcing_dir", "other/basin-a/forcing", "vbasins-relative-forcing-prefix-tampered"),
    ],
)
def test_publish_basins_rejects_arbitrary_prefix_relative_inventory_path_without_outputs(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    field_name: str,
    tampered_value: str,
    version: str,
) -> None:
    inventory_path, model_id = _write_valid_inventory(tmp_path, forcing_count=1)
    inventory = json.loads(inventory_path.read_text(encoding="utf-8"))
    model = inventory["models"][0]
    model[field_name] = tampered_value
    write_inventory(inventory, inventory_path)
    object_root = _object_store_env(tmp_path, monkeypatch)
    output = tmp_path / "manifest.json"

    exit_code = _argparse_main(
        [
            "publish-basins",
            "--inventory",
            str(inventory_path),
            "--model-id",
            model_id,
            "--version",
            version,
            "--output",
            str(output),
        ]
    )

    captured = capsys.readouterr()
    error = json.loads(captured.err)
    assert exit_code == 1
    assert captured.out == ""
    assert error["error_code"] == "BASINS_INVENTORY_PATH_MISMATCH"
    assert error["model_id"] == model_id
    assert error["version"] == version
    assert error["path"] == str(tmp_path / "basins" / tampered_value)
    assert not output.exists()
    assert not (object_root / "models" / model_id / version / "manifest.json").exists()
    assert not (object_root / "models" / model_id / version / "package").exists()


def test_publish_basins_rejects_unresolvable_symlink_descendant_as_json(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    inventory_path, model_id = _write_valid_inventory(tmp_path)
    loop = tmp_path / "basins" / "basin-a" / "CALIB" / "loop"
    loop.parent.mkdir()
    try:
        loop.symlink_to(loop)
    except (NotImplementedError, OSError) as error:
        pytest.skip(f"symlink support unavailable: {error}")
    _object_store_env(tmp_path, monkeypatch)

    exit_code = _argparse_main(
        [
            "publish-basins",
            "--inventory",
            str(inventory_path),
            "--model-id",
            model_id,
            "--version",
            "vbasins-loop",
            "--output",
            str(tmp_path / "manifest.json"),
        ]
    )

    captured = capsys.readouterr()
    error = json.loads(captured.err)
    assert exit_code == 1
    assert captured.out == ""
    assert error["error_code"] == "BASINS_PACKAGE_PATH_UNSAFE"
    assert error["path"] == str(loop)
    assert "Traceback" not in captured.err


def test_publish_basins_rejects_symlink_descendant_with_structured_error(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    root = tmp_path / "basins"
    model_dir = root / "basin-a"
    _make_valid_model(model_dir, "alias-a", calibration_count=1)
    linked_file = model_dir / "CALIB" / "linked.calib"
    try:
        linked_file.symlink_to(model_dir / "CALIB" / "top01.calib")
    except (NotImplementedError, OSError) as error:
        pytest.skip(f"symlink support unavailable: {error}")
    inventory = discover_basins_inventory(root)
    inventory_path = tmp_path / "inventory.json"
    write_inventory(inventory, inventory_path)
    model_id = inventory["models"][0]["model_id"]
    _object_store_env(tmp_path, monkeypatch)

    exit_code = _argparse_main(
        [
            "publish-basins",
            "--inventory",
            str(inventory_path),
            "--model-id",
            model_id,
            "--version",
            "vbasins-cycle",
            "--output",
            str(tmp_path / "manifest.json"),
        ]
    )

    captured = capsys.readouterr()
    error = json.loads(captured.err)
    assert exit_code == 1
    assert captured.out == ""
    assert error["error_code"] == "BASINS_PACKAGE_PATH_UNSAFE"
    assert error["path"] == str(linked_file)
    assert "Traceback" not in captured.err


def test_publish_basins_existing_manifest_does_not_require_lock(
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
    (object_root / "models" / model_id / "vbasins-test" / ".publish.lock").write_text("stale\n", encoding="utf-8")

    assert _argparse_main(args) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["status"] == "already_done"


def test_publish_basins_rejects_in_progress_lock(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    inventory_path, model_id = _write_valid_inventory(tmp_path)
    object_root = _object_store_env(tmp_path, monkeypatch)
    lock_path = object_root / "models" / model_id / "vbasins-locked" / ".publish.lock"
    lock_path.parent.mkdir(parents=True)
    lock_path.write_text("busy\n", encoding="utf-8")

    exit_code = _argparse_main(
        [
            "publish-basins",
            "--inventory",
            str(inventory_path),
            "--model-id",
            model_id,
            "--version",
            "vbasins-locked",
            "--output",
            str(tmp_path / "manifest.json"),
        ]
    )

    captured = capsys.readouterr()
    error = json.loads(captured.err)
    assert exit_code == 1
    assert captured.out == ""
    assert error["error_code"] == "BASINS_PACKAGE_PUBLISH_IN_PROGRESS"
    assert error["model_id"] == model_id
    assert error["version"] == "vbasins-locked"
    assert error["path"] == str(lock_path)
