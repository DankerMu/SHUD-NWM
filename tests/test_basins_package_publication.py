from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path

import pytest

import workers.model_registry.basins_package as basins_package
from workers.model_registry.basins_discovery import discover_basins_inventory, write_inventory
from workers.model_registry.cli import DEFAULT_BASINS_MIGRATION_SOURCE_URI, _argparse_main, _click_main


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
    assert manifest["schema_version"] == "basins.package.v1"
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
    assert manifest["forcing"]["aggregate_checksum"]
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
    assert manifest["forcing"]["aggregate_checksum"]
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


def test_publish_basins_accepts_symlink_root_with_calib_files(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    real_root = tmp_path / "real-basins"
    _make_valid_model(real_root / "basin-a", "alias-a", calibration_count=2)
    linked_root = tmp_path / "linked-basins"
    linked_root.symlink_to(real_root, target_is_directory=True)
    inventory_path = tmp_path / "inventory.json"
    inventory = discover_basins_inventory(linked_root)
    write_inventory(inventory, inventory_path)
    model_id = inventory["models"][0]["model_id"]
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
                "vbasins-symlink",
                "--output",
                str(tmp_path / "manifest.json"),
            ]
        )
        == 0
    )
    manifest = json.loads((tmp_path / "manifest.json").read_text(encoding="utf-8"))

    calibration_paths = [
        entry["relative_path"] for entry in manifest["included_files"] if entry["role"] == "calibration"
    ]
    assert calibration_paths == ["CALIB/top01.calib", "CALIB/top02.calib"]
    assert inventory["source_is_symlink"] is True
    assert manifest["source_is_symlink"] is False
    assert manifest["source_path"] == str(linked_root / "basin-a")
    assert manifest["resolved_source_path"] == str((real_root / "basin-a").resolve())
    assert (object_root / "models" / model_id / "vbasins-symlink" / "package" / "CALIB" / "top01.calib").is_file()
    assert (object_root / "models" / model_id / "vbasins-symlink" / "package" / "CALIB" / "top02.calib").is_file()


def test_publish_basins_rejects_symlinked_required_runtime_file(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    inventory_path, model_id = _write_valid_inventory(tmp_path)
    runtime_file = tmp_path / "basins" / "basin-a" / "input" / "alias-a" / "alias-a.cfg.para"
    real_file = tmp_path / "basins" / "basin-a" / "input" / "alias-a" / "alias-a.cfg.para.real"
    runtime_file.rename(real_file)
    try:
        runtime_file.symlink_to(real_file)
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
            "vbasins-runtime-symlink",
            "--output",
            str(tmp_path / "manifest.json"),
        ]
    )

    captured = capsys.readouterr()
    error = json.loads(captured.err)
    assert exit_code == 1
    assert captured.out == ""
    assert error["error_code"] == "BASINS_PACKAGE_PATH_UNSAFE"
    assert error["path"] == str(runtime_file)


def test_publish_basins_rejects_symlinked_required_gis_sidecar(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    inventory_path, model_id = _write_valid_inventory(tmp_path)
    gis_file = tmp_path / "basins" / "basin-a" / "input" / "alias-a" / "gis" / "domain.shp"
    real_file = tmp_path / "basins" / "basin-a" / "input" / "alias-a" / "gis" / "domain.real.shp"
    gis_file.rename(real_file)
    try:
        gis_file.symlink_to(real_file)
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
            "vbasins-gis-symlink",
            "--output",
            str(tmp_path / "manifest.json"),
        ]
    )

    captured = capsys.readouterr()
    error = json.loads(captured.err)
    assert exit_code == 1
    assert captured.out == ""
    assert error["error_code"] == "BASINS_PACKAGE_PATH_UNSAFE"
    assert error["path"] == str(gis_file)


def test_publish_basins_rejects_symlinked_forcing_directory(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    inventory_path, model_id = _write_valid_inventory(tmp_path, forcing_count=1)
    forcing_dir = tmp_path / "basins" / "basin-a" / "forcing"
    real_forcing_dir = tmp_path / "basins" / "basin-a" / "forcing-real"
    forcing_dir.rename(real_forcing_dir)
    try:
        forcing_dir.symlink_to(real_forcing_dir, target_is_directory=True)
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
            "vbasins-forcing-dir-symlink",
            "--output",
            str(tmp_path / "manifest.json"),
        ]
    )

    captured = capsys.readouterr()
    error = json.loads(captured.err)
    assert exit_code == 1
    assert captured.out == ""
    assert error["error_code"] == "BASINS_PACKAGE_PATH_UNSAFE"
    assert error["path"] == str(forcing_dir)


def test_publish_basins_rejects_symlinked_calib_directory(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    inventory_path, model_id = _write_valid_inventory(tmp_path, calibration_count=1)
    calib_dir = tmp_path / "basins" / "basin-a" / "CALIB"
    real_calib_dir = tmp_path / "basins" / "basin-a" / "CALIB-real"
    calib_dir.rename(real_calib_dir)
    try:
        calib_dir.symlink_to(real_calib_dir, target_is_directory=True)
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
            "vbasins-calib-dir-symlink",
            "--output",
            str(tmp_path / "manifest.json"),
        ]
    )

    captured = capsys.readouterr()
    error = json.loads(captured.err)
    assert exit_code == 1
    assert captured.out == ""
    assert error["error_code"] == "BASINS_PACKAGE_PATH_UNSAFE"
    assert error["path"] == str(calib_dir)


def test_publish_basins_rejects_partial_model_with_structured_error(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    root = tmp_path / "basins"
    _make_valid_model(root / "tailanhe", "tlh", include_tsd_rl=False, forcing_count=1, forcing_dir_name="focing")
    inventory_path = tmp_path / "inventory.json"
    inventory = discover_basins_inventory(root)
    write_inventory(inventory, inventory_path)
    _object_store_env(tmp_path, monkeypatch)
    model_id = inventory["models"][0]["model_id"]

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
            str(tmp_path / "manifest.json"),
        ]
    )

    error = json.loads(capsys.readouterr().err)
    assert exit_code == 1
    assert error["error_code"] == "BASINS_MODEL_NOT_PUBLISHABLE"
    assert error["model_id"] == model_id
    assert error["version"] == "vbasins-test"
    assert "tailanhe" in error["path"]


def test_publish_basins_rejects_invalid_utf8_inventory_with_structured_error(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    inventory_path = tmp_path / "inventory.json"
    inventory_path.write_bytes(b'{"models": [\xff]}\n')
    output = tmp_path / "manifest.json"

    exit_code = _argparse_main(
        [
            "publish-basins",
            "--inventory",
            str(inventory_path),
            "--model-id",
            "basins_basin_a_shud",
            "--version",
            "vbasins-invalid-utf8",
            "--output",
            str(output),
        ]
    )

    captured = capsys.readouterr()
    error = json.loads(captured.err)
    assert exit_code == 1
    assert captured.out == ""
    assert error["error_code"] == "BASINS_INVENTORY_INVALID"
    assert error["path"] == str(inventory_path)
    assert "Traceback" not in captured.err
    assert not output.exists()


def test_click_publish_basins_rejects_invalid_utf8_inventory_with_structured_error(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    pytest.importorskip("click")
    inventory_path = tmp_path / "inventory.json"
    inventory_path.write_bytes(b'{"models": [\xff]}\n')
    output = tmp_path / "manifest.json"

    exit_code = _invoke_click(
        [
            "publish-basins",
            "--inventory",
            str(inventory_path),
            "--model-id",
            "basins_basin_a_shud",
            "--version",
            "vbasins-invalid-utf8",
            "--output",
            str(output),
        ]
    )

    captured = capsys.readouterr()
    error = json.loads(captured.err)
    assert exit_code == 1
    assert captured.out == ""
    assert error["error_code"] == "BASINS_INVENTORY_INVALID"
    assert error["path"] == str(inventory_path)
    assert "Traceback" not in captured.err
    assert not output.exists()


@pytest.mark.parametrize(
    ("field", "unsafe_value"),
    [
        ("model_id", "basins/basin-a/shud"),
        ("model_id", " basins_basin_a_shud"),
        ("version", "vbasins/test"),
        ("version", "vbasins\\test"),
        ("version", "."),
        ("version", "vbasins\x1ftest"),
    ],
)
def test_publish_basins_rejects_unsafe_model_id_and_version_segments(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    field: str,
    unsafe_value: str,
) -> None:
    inventory_path, model_id = _write_valid_inventory(tmp_path)
    object_root = _object_store_env(tmp_path, monkeypatch)
    version = "vbasins-test"
    if field == "model_id":
        model_id = unsafe_value
    else:
        version = unsafe_value

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
            str(tmp_path / "manifest.json"),
        ]
    )

    captured = capsys.readouterr()
    error = json.loads(captured.err)
    assert exit_code == 1
    assert captured.out == ""
    assert error["error_code"] == "BASINS_PACKAGE_IDENTIFIER_INVALID"
    assert error["model_id"] == model_id
    assert error["version"] == version
    assert not (tmp_path / "manifest.json").exists()
    assert not (object_root / "models").exists()


def test_publish_basins_rejects_tampered_required_files_despite_valid_status(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    inventory_path, model_id = _write_valid_inventory(tmp_path)
    inventory = json.loads(inventory_path.read_text(encoding="utf-8"))
    model = inventory["models"][0]
    model["status"] = "valid"
    model["default_publish_eligible"] = True
    model["missing_required_files"] = []
    model["required_files"].pop("tsd_rl")
    model["required_files"]["gis_domain_shp"] = []
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
            "vbasins-tampered-required",
            "--output",
            str(tmp_path / "manifest.json"),
        ]
    )

    captured = capsys.readouterr()
    error = json.loads(captured.err)
    assert exit_code == 1
    assert captured.out == ""
    assert error["error_code"] == "BASINS_REQUIRED_FILES_MISSING"
    assert error["model_id"] == model_id
    assert error["version"] == "vbasins-tampered-required"
    assert "tsd_rl" in error["message"]
    assert "gis_domain_shp" in error["message"]
    assert not (tmp_path / "manifest.json").exists()


def test_publish_basins_rejects_nested_runtime_required_file_despite_valid_status(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    inventory_path, model_id = _write_valid_inventory(tmp_path)
    fake_runtime = tmp_path / "basins" / "basin-a" / "input" / "alias-a" / "gis" / "fake.cfg.para"
    fake_runtime.write_text("nested runtime impostor\n", encoding="utf-8")
    inventory = json.loads(inventory_path.read_text(encoding="utf-8"))
    model = inventory["models"][0]
    model["status"] = "valid"
    model["default_publish_eligible"] = True
    model["missing_required_files"] = []
    model["required_files"]["cfg_para"] = ["gis/fake.cfg.para"]
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
            "vbasins-nested-runtime",
            "--output",
            str(tmp_path / "manifest.json"),
        ]
    )

    captured = capsys.readouterr()
    error = json.loads(captured.err)
    assert exit_code == 1
    assert captured.out == ""
    assert error["error_code"] == "BASINS_REQUIRED_FILES_MISSING"
    assert error["model_id"] == model_id
    assert error["version"] == "vbasins-nested-runtime"
    assert "cfg_para" in error["message"]
    assert not (tmp_path / "manifest.json").exists()


def test_publish_basins_rejects_extra_required_file_without_writing_outputs(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    inventory_path, model_id = _write_valid_inventory(tmp_path)
    extra_file = tmp_path / "basins" / "basin-a" / "input" / "alias-a" / "secret.txt"
    extra_file.write_text("do not publish\n", encoding="utf-8")
    inventory = json.loads(inventory_path.read_text(encoding="utf-8"))
    model = inventory["models"][0]
    model["status"] = "valid"
    model["default_publish_eligible"] = True
    model["missing_required_files"] = []
    model["required_files"]["cfg_para"].append("secret.txt")
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
            "vbasins-extra-required",
            "--output",
            str(output),
        ]
    )

    captured = capsys.readouterr()
    error = json.loads(captured.err)
    assert exit_code == 1
    assert captured.out == ""
    assert error["error_code"] == "BASINS_REQUIRED_FILES_NON_CANONICAL"
    assert error["model_id"] == model_id
    assert error["version"] == "vbasins-extra-required"
    assert "cfg_para:secret.txt" in error["message"]
    assert not output.exists()
    assert not (object_root / "models" / model_id / "vbasins-extra-required" / "manifest.json").exists()
    assert not (object_root / "models" / model_id / "vbasins-extra-required" / "package" / "secret.txt").exists()


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
    ) -> None:
        if key.endswith("/manifest.json"):
            raise basins_package.ObjectStoreError("synthetic manifest verification failure")
        original_verify(  # type: ignore[arg-type]
            store,
            key,
            expected_size=expected_size,
            expected_sha256=expected_sha256,
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
    model["forcing_csv_count"] = 1
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


def test_publish_basins_manifest_checksums_match_mutated_bytes_written(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    inventory_path, model_id = _write_valid_inventory(tmp_path)
    object_root = _object_store_env(tmp_path, monkeypatch)
    source_file = tmp_path / "basins" / "basin-a" / "input" / "alias-a" / "alias-a.cfg.para"
    original_writer = basins_package._write_file_to_store_streaming

    def mutating_writer(
        store: object,
        key: str,
        path: Path,
        source_root: Path,
        *,
        model_id: str | None = None,
        version: str | None = None,
        manifest_uri: str | None = None,
    ) -> tuple[int, str]:
        if path == source_file:
            path.write_text("mutated-before-write\n", encoding="utf-8")
        return original_writer(
            store,  # type: ignore[arg-type]
            key,
            path,
            source_root,
            model_id=model_id,
            version=version,
            manifest_uri=manifest_uri,
        )

    monkeypatch.setattr(basins_package, "_write_file_to_store_streaming", mutating_writer)

    exit_code = _argparse_main(
        [
            "publish-basins",
            "--inventory",
            str(inventory_path),
            "--model-id",
            model_id,
            "--version",
            "vbasins-mutated-write",
            "--output",
            str(tmp_path / "manifest.json"),
        ]
    )

    assert exit_code == 0
    capsys.readouterr()
    manifest = json.loads((tmp_path / "manifest.json").read_text(encoding="utf-8"))
    entry = next(item for item in manifest["included_files"] if item["relative_path"] == "alias-a.cfg.para")
    object_bytes = (
        object_root / "models" / model_id / "vbasins-mutated-write" / "package" / "alias-a.cfg.para"
    ).read_bytes()
    assert object_bytes == b"mutated-before-write\n"
    assert entry["size_bytes"] == len(object_bytes)
    assert entry["sha256"] == hashlib.sha256(object_bytes).hexdigest()


def test_publish_basins_rejects_source_file_replaced_with_symlink_before_final_copy(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    inventory_path, model_id = _write_valid_inventory(tmp_path)
    object_root = _object_store_env(tmp_path, monkeypatch)
    output = tmp_path / "manifest.json"
    source_file = tmp_path / "basins" / "basin-a" / "input" / "alias-a" / "alias-a.cfg.para"
    outside_file = tmp_path / "outside-secret.txt"
    outside_file.write_text("outside\n", encoding="utf-8")
    original_writer = basins_package._write_file_to_store_streaming
    mutated = False

    def symlink_swapping_writer(
        store: object,
        key: str,
        path: Path,
        source_root: Path,
        *,
        model_id: str | None = None,
        version: str | None = None,
        manifest_uri: str | None = None,
    ) -> tuple[int, str]:
        nonlocal mutated
        if path == source_file and not mutated:
            mutated = True
            path.unlink()
            try:
                path.symlink_to(outside_file)
            except (NotImplementedError, OSError) as error:
                pytest.skip(f"symlink support unavailable: {error}")
        return original_writer(
            store,  # type: ignore[arg-type]
            key,
            path,
            source_root,
            model_id=model_id,
            version=version,
            manifest_uri=manifest_uri,
        )

    monkeypatch.setattr(basins_package, "_write_file_to_store_streaming", symlink_swapping_writer)

    exit_code = _argparse_main(
        [
            "publish-basins",
            "--inventory",
            str(inventory_path),
            "--model-id",
            model_id,
            "--version",
            "vbasins-symlink-final-copy",
            "--output",
            str(output),
        ]
    )

    captured = capsys.readouterr()
    error = json.loads(captured.err)
    assert exit_code == 1
    assert captured.out == ""
    assert error["error_code"] == "BASINS_PACKAGE_PATH_UNSAFE"
    assert error["model_id"] == model_id
    assert error["version"] == "vbasins-symlink-final-copy"
    assert error["path"] == str(source_file)
    assert error["manifest_uri"] == f"s3://nhms/models/{model_id}/vbasins-symlink-final-copy/manifest.json"
    assert "Traceback" not in captured.err
    assert not output.exists()
    assert not (object_root / "models" / model_id / "vbasins-symlink-final-copy" / "manifest.json").exists()


def test_publish_basins_rejects_forcing_csv_replaced_with_symlink_before_sampling(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    inventory_path, model_id = _write_valid_inventory(tmp_path, forcing_count=1)
    object_root = _object_store_env(tmp_path, monkeypatch)
    output = tmp_path / "manifest.json"
    forcing_file = tmp_path / "basins" / "basin-a" / "forcing" / "X000001.csv"
    outside_file = tmp_path / "outside-forcing.csv"
    outside_file.write_text("time,value\n2026-01-01,999\n", encoding="utf-8")
    original_source_file_evidence = basins_package._source_file_evidence
    mutated = False

    def symlink_swapping_source_file_evidence(
        path: Path,
        source_root: Path,
        *,
        model_id: str | None = None,
        version: str | None = None,
        manifest_uri: str | None = None,
    ) -> tuple[int, str]:
        nonlocal mutated
        evidence = original_source_file_evidence(
            path,
            source_root,
            model_id=model_id,
            version=version,
            manifest_uri=manifest_uri,
        )
        if path == forcing_file and not mutated:
            mutated = True
            path.unlink()
            try:
                path.symlink_to(outside_file)
            except (NotImplementedError, OSError) as error:
                pytest.skip(f"symlink support unavailable: {error}")
        return evidence

    monkeypatch.setattr(basins_package, "_source_file_evidence", symlink_swapping_source_file_evidence)

    exit_code = _argparse_main(
        [
            "publish-basins",
            "--inventory",
            str(inventory_path),
            "--model-id",
            model_id,
            "--version",
            "vbasins-forcing-symlink-sample",
            "--output",
            str(output),
        ]
    )

    captured = capsys.readouterr()
    error = json.loads(captured.err)
    assert exit_code == 1
    assert captured.out == ""
    assert error["error_code"] == "BASINS_PACKAGE_PATH_UNSAFE"
    assert error["model_id"] == model_id
    assert error["version"] == "vbasins-forcing-symlink-sample"
    assert error["path"] == str(forcing_file)
    assert "Traceback" not in captured.err
    assert not output.exists()
    assert not (object_root / "models" / model_id / "vbasins-forcing-symlink-sample" / "manifest.json").exists()
    assert not (object_root / "models" / model_id / "vbasins-forcing-symlink-sample" / "package").exists()


def test_publish_basins_object_verification_streams_without_store_checksum(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    inventory_path, model_id = _write_valid_inventory(tmp_path)
    _object_store_env(tmp_path, monkeypatch)

    def forbidden_checksum(self: object, key_or_uri: str) -> str:
        raise AssertionError(f"LocalObjectStore.checksum must not be used for package verification: {key_or_uri}")

    def forbidden_read_bytes(self: object, key_or_uri: str) -> bytes:
        raise AssertionError(f"LocalObjectStore.read_bytes must not be used for package verification: {key_or_uri}")

    monkeypatch.setattr(basins_package.LocalObjectStore, "checksum", forbidden_checksum)
    monkeypatch.setattr(basins_package.LocalObjectStore, "read_bytes", forbidden_read_bytes)

    assert (
        _argparse_main(
            [
                "publish-basins",
                "--inventory",
                str(inventory_path),
                "--model-id",
                model_id,
                "--version",
                "vbasins-streaming-verify",
                "--output",
                str(tmp_path / "manifest.json"),
            ]
        )
        == 0
    )


def test_basins_migration_report_rejects_symlink_target(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    real_root = tmp_path / "real-basins"
    _make_valid_model(real_root / "basin-a", "alias-a")
    linked_root = tmp_path / "linked-basins"
    linked_root.symlink_to(real_root, target_is_directory=True)
    output = tmp_path / "report.json"

    exit_code = _argparse_main(
        [
            "basins-migration-report",
            "--basins-root",
            str(linked_root),
            "--output",
            str(output),
        ]
    )

    error = json.loads(capsys.readouterr().err)
    assert exit_code == 1
    assert error["error_code"] == "BASINS_MIGRATION_SYMLINK_TARGET"
    assert error["path"] == str(linked_root)
    assert not output.exists()


def test_click_basins_migration_report_rejects_symlink_target_without_source_uri(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    pytest.importorskip("click")
    real_root = tmp_path / "real-basins"
    _make_valid_model(real_root / "basin-a", "alias-a")
    linked_root = tmp_path / "linked-basins"
    linked_root.symlink_to(real_root, target_is_directory=True)
    output = tmp_path / "report.json"

    exit_code = _invoke_click(
        [
            "basins-migration-report",
            "--basins-root",
            str(linked_root),
            "--output",
            str(output),
        ]
    )

    captured = capsys.readouterr()
    error = json.loads(captured.err)
    assert exit_code == 1
    assert captured.out == ""
    assert error["error_code"] == "BASINS_MIGRATION_SYMLINK_TARGET"
    assert error["path"] == str(linked_root)
    assert "Missing option" not in captured.err
    assert not output.exists()


def test_basins_migration_report_rejects_unresolvable_symlink_descendant_as_json(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    real_root = tmp_path / "real-basins"
    _make_valid_model(real_root / "basin-a", "alias-a")
    loop = real_root / "loop"
    try:
        loop.symlink_to(loop)
    except (NotImplementedError, OSError) as error:
        pytest.skip(f"symlink support unavailable: {error}")
    output = tmp_path / "report.json"

    exit_code = _argparse_main(
        [
            "basins-migration-report",
            "--basins-root",
            str(real_root),
            "--output",
            str(output),
        ]
    )

    captured = capsys.readouterr()
    error = json.loads(captured.err)
    assert exit_code == 1
    assert captured.out == ""
    assert error["error_code"] == "BASINS_PACKAGE_PATH_UNSAFE"
    assert error["path"] == str(loop)
    assert "Traceback" not in captured.err
    assert not output.exists()


def test_basins_migration_report_rejects_symlink_descendant_as_json(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    real_root = tmp_path / "real-basins"
    model_dir = real_root / "basin-a"
    _make_valid_model(model_dir, "alias-a", calibration_count=1, forcing_count=1)
    linked_file = model_dir / "CALIB" / "linked.calib"
    try:
        linked_file.symlink_to(model_dir / "CALIB" / "top01.calib")
    except (NotImplementedError, OSError) as error:
        pytest.skip(f"symlink support unavailable: {error}")
    output = tmp_path / "report.json"

    exit_code = _argparse_main(
        [
            "basins-migration-report",
            "--basins-root",
            str(real_root),
            "--source-uri",
            "/volume/data/nwm/Basins",
            "--output",
            str(output),
        ]
    )

    captured = capsys.readouterr()
    error = json.loads(captured.err)
    assert exit_code == 1
    assert captured.out == ""
    assert error["error_code"] == "BASINS_PACKAGE_PATH_UNSAFE"
    assert error["path"] == str(linked_file)
    assert "Traceback" not in captured.err
    assert not output.exists()


def test_basins_migration_report_reports_output_write_failure_as_json(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    real_root = tmp_path / "real-basins"
    _make_valid_model(real_root / "basin-a", "alias-a")
    output_parent = tmp_path / "not-a-dir"
    output_parent.write_text("file blocks output parent\n", encoding="utf-8")

    exit_code = _argparse_main(
        [
            "basins-migration-report",
            "--basins-root",
            str(real_root),
            "--source-uri",
            "/volume/data/nwm/Basins",
            "--output",
            str(output_parent / "report.json"),
        ]
    )

    captured = capsys.readouterr()
    error = json.loads(captured.err)
    assert exit_code == 1
    assert captured.out == ""
    assert error["error_code"] == "BASINS_MIGRATION_REPORT_WRITE_FAILED"
    assert error["path"] == str(output_parent / "report.json")
    assert "Traceback" not in captured.err


def test_basins_migration_report_reports_deleted_evidence_file_as_json(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    real_root = tmp_path / "real-basins"
    _make_valid_model(real_root / "basin-a", "alias-a")
    output = tmp_path / "report.json"
    stale_file = real_root / "basin-a" / "input" / "alias-a" / "alias-a.cfg.para"
    original_migration_source_file_evidence = basins_package._migration_source_file_evidence
    deleted = False

    def deleting_migration_source_file_evidence(path: Path, source_root: Path) -> tuple[int, str]:
        nonlocal deleted
        if path == stale_file and not deleted:
            deleted = True
            path.unlink()
        return original_migration_source_file_evidence(path, source_root)

    monkeypatch.setattr(basins_package, "_migration_source_file_evidence", deleting_migration_source_file_evidence)

    exit_code = _argparse_main(
        [
            "basins-migration-report",
            "--basins-root",
            str(real_root),
            "--source-uri",
            "/volume/data/nwm/Basins",
            "--output",
            str(output),
        ]
    )

    captured = capsys.readouterr()
    error = json.loads(captured.err)
    assert exit_code == 1
    assert captured.out == ""
    assert error["error_code"] == "BASINS_MIGRATION_EVIDENCE_READ_FAILED"
    assert error["path"] == str(stale_file)
    assert "Traceback" not in captured.err
    assert not output.exists()


def test_basins_migration_report_rejects_file_replaced_with_symlink_before_evidence_hash(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    real_root = tmp_path / "real-basins"
    _make_valid_model(real_root / "basin-a", "alias-a")
    output = tmp_path / "report.json"
    source_file = real_root / "basin-a" / "input" / "alias-a" / "alias-a.cfg.para"
    outside_file = tmp_path / "outside-secret.txt"
    outside_file.write_text("outside\n", encoding="utf-8")
    original_walk_source_files = basins_package._walk_source_files
    mutated = False

    def symlink_swapping_walk_source_files(root: Path, source_root: Path):
        nonlocal mutated
        for path in original_walk_source_files(root, source_root):
            if path == source_file and not mutated:
                mutated = True
                path.unlink()
                try:
                    path.symlink_to(outside_file)
                except (NotImplementedError, OSError) as error:
                    pytest.skip(f"symlink support unavailable: {error}")
            yield path

    monkeypatch.setattr(basins_package, "_walk_source_files", symlink_swapping_walk_source_files)

    exit_code = _argparse_main(
        [
            "basins-migration-report",
            "--basins-root",
            str(real_root),
            "--source-uri",
            "/volume/data/nwm/Basins",
            "--output",
            str(output),
        ]
    )

    captured = capsys.readouterr()
    error = json.loads(captured.err)
    assert exit_code == 1
    assert captured.out == ""
    assert error["error_code"] == "BASINS_PACKAGE_PATH_UNSAFE"
    assert error["path"] == str(source_file)
    assert "Traceback" not in captured.err
    assert not output.exists()


def test_basins_migration_report_accepts_real_copied_root(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    real_root = tmp_path / "real-basins"
    _make_valid_model(real_root / "basin-a", "alias-a", forcing_count=1)
    output = tmp_path / "report.json"

    exit_code = _argparse_main(
        [
            "basins-migration-report",
            "--basins-root",
            str(real_root),
            "--source-uri",
            "/volume/data/nwm/Basins",
            "--output",
            str(output),
        ]
    )

    payload = json.loads(capsys.readouterr().out)
    report = json.loads(output.read_text(encoding="utf-8"))
    assert exit_code == 0
    assert payload["status"] == "ok"
    assert payload["production_ready"] is True
    assert report["schema_version"] == "basins.migration.v1"
    assert report["source_uri"] == DEFAULT_BASINS_MIGRATION_SOURCE_URI
    assert report["target_path"] == str(real_root)
    assert report["source_is_symlink"] is False
    assert report["file_count"] > 0
    assert report["byte_count"] > 0
    assert report["inventory_checksum"]
    assert report["content_checksum"]
    assert report["source_to_target"]["symlink_allowed"] is False
    assert report["production_ready"] is True


@pytest.mark.skipif(
    not Path("data/Basins").exists(),
    reason="real Basins package smoke requires data/Basins",
)
@pytest.mark.skipif(
    os.getenv("NHMS_RUN_BASINS_SMOKE") != "1",
    reason="real Basins package smoke is opt-in with NHMS_RUN_BASINS_SMOKE=1",
)
def test_real_basins_package_smoke_opt_in(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    inventory = discover_basins_inventory(Path("data/Basins"))
    publishable_model = next(model for model in inventory["models"] if model["status"] == "valid")
    inventory_path = tmp_path / "real-inventory.json"
    write_inventory(inventory, inventory_path)
    _object_store_env(tmp_path, monkeypatch)
    manifest_path = tmp_path / "real-manifest.json"

    exit_code = _argparse_main(
        [
            "publish-basins",
            "--inventory",
            str(inventory_path),
            "--model-id",
            publishable_model["model_id"],
            "--version",
            "vbasins-real-smoke",
            "--output",
            str(manifest_path),
        ]
    )

    payload = json.loads(capsys.readouterr().out)
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert exit_code == 0
    assert payload["status"] == "published"
    assert payload["model_id"] == publishable_model["model_id"]
    assert manifest["forcing"]["payload_copied"] is False
    assert all(entry["role"] != "forcing" for entry in manifest["included_files"])


def _write_valid_inventory(
    tmp_path: Path,
    *,
    forcing_count: int = 0,
    calibration_count: int = 0,
) -> tuple[Path, str]:
    root = tmp_path / "basins"
    _make_valid_model(root / "basin-a", "alias-a", forcing_count=forcing_count, calibration_count=calibration_count)
    inventory = discover_basins_inventory(root)
    inventory_path = tmp_path / "inventory.json"
    write_inventory(inventory, inventory_path)
    return inventory_path, inventory["models"][0]["model_id"]


def _object_store_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    object_root = tmp_path / "object-store"
    monkeypatch.setenv("OBJECT_STORE_ROOT", str(object_root))
    monkeypatch.setenv("OBJECT_STORE_PREFIX", "s3://nhms")
    return object_root


def _required_files_for_input_name(input_name: str) -> dict[str, list[str]]:
    required = {
        "cfg_para": [f"{input_name}.cfg.para"],
        "cfg_ic": [f"{input_name}.cfg.ic"],
        "cfg_calib": [f"{input_name}.cfg.calib"],
        "sp_mesh": [f"{input_name}.sp.mesh"],
        "sp_riv": [f"{input_name}.sp.riv"],
        "sp_rivseg": [f"{input_name}.sp.rivseg"],
        "sp_att": [f"{input_name}.sp.att"],
        "para_soil": [f"{input_name}.para.soil"],
        "para_geol": [f"{input_name}.para.geol"],
        "para_lc": [f"{input_name}.para.lc"],
        "tsd_forc": [f"{input_name}.tsd.forc"],
        "tsd_lai": [f"{input_name}.tsd.lai"],
        "tsd_mf": [f"{input_name}.tsd.mf"],
        "tsd_rl": [f"{input_name}.tsd.rl"],
    }
    for layer in ("domain", "river", "seg"):
        for suffix in ("shp", "shx", "dbf", "prj"):
            required[f"gis_{layer}_{suffix}"] = [f"gis/{layer}.{suffix}"]
    return required


def _one_entry(manifest: dict[str, object], role: str) -> dict[str, object]:
    entries = [
        entry
        for entry in manifest["included_files"]  # type: ignore[index]
        if isinstance(entry, dict) and entry.get("role") == role
    ]
    assert len(entries) == 1
    return entries[0]


def _manifest_payload_checksum(manifest: dict[str, object]) -> str:
    payload = dict(manifest)
    payload["included_files"] = [
        entry
        for entry in manifest["included_files"]  # type: ignore[index]
        if isinstance(entry, dict) and entry.get("role") != "manifest"
    ]
    content = json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True).encode("utf-8") + b"\n"
    return hashlib.sha256(content).hexdigest()


def _invoke_click(argv: list[str]) -> int:
    try:
        return _click_main(argv)
    except SystemExit as error:
        if isinstance(error.code, int):
            return error.code
        return 1


def _make_valid_model(
    model_dir: Path,
    input_name: str,
    *,
    include_tsd_rl: bool = True,
    calibration_count: int = 0,
    forcing_count: int = 0,
    forcing_dir_name: str = "forcing",
) -> Path:
    input_dir = model_dir / "input" / input_name
    input_dir.mkdir(parents=True)
    for suffix in (
        "cfg.para",
        "cfg.ic",
        "cfg.calib",
        "sp.mesh",
        "sp.riv",
        "sp.rivseg",
        "sp.att",
        "para.soil",
        "para.geol",
        "para.lc",
        "tsd.forc",
        "tsd.lai",
        "tsd.mf",
    ):
        (input_dir / f"{input_name}.{suffix}").write_text(f"{suffix}\n", encoding="utf-8")
    if include_tsd_rl:
        (input_dir / f"{input_name}.tsd.rl").write_text("radiation\n", encoding="utf-8")

    gis_dir = input_dir / "gis"
    gis_dir.mkdir()
    for layer in ("domain", "river", "seg"):
        for suffix in ("shp", "shx", "dbf", "prj"):
            (gis_dir / f"{layer}.{suffix}").write_text(f"{layer}.{suffix}\n", encoding="utf-8")

    if calibration_count:
        calib_dir = model_dir / "CALIB"
        calib_dir.mkdir()
        for index in range(calibration_count):
            (calib_dir / f"top{index + 1:02d}.calib").write_text("calib\n", encoding="utf-8")

    if forcing_count:
        forcing_dir = model_dir / forcing_dir_name
        forcing_dir.mkdir()
        for index in range(forcing_count):
            (forcing_dir / f"X{index + 1:06d}.csv").write_text("time,value\n2026-01-01,1\n", encoding="utf-8")

    return input_dir
