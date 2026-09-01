"""Basins publication refusals: source, path, model and refusal-payload contracts.

Partition 2 of 6 of the former monolith ``tests/test_basins_package_publication.py``
(issue #1912).  Shared test support lives in the non-collectible
``tests/basins_package_helpers.py``.
"""

from __future__ import annotations

import errno
import json
from pathlib import Path

import pytest

import workers.model_registry.basins_discovery as basins_discovery
import workers.model_registry.basins_package as basins_package
from tests.basins_package_helpers import _invoke_click, _make_valid_model, _object_store_env, _write_valid_inventory
from workers.model_registry.basins_discovery import discover_basins_inventory, write_inventory
from workers.model_registry.cli import _argparse_main


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


def test_publish_basins_refusal_payload_carries_partial_model_causes(tmp_path: Path) -> None:
    """Same missing-file geometry as the structured-error test above, asserted on
    the refusal payload's cause keys (#1432) instead of the CLI JSON envelope."""

    root = tmp_path / "basins"
    _make_valid_model(root / "tailanhe", "tlh", include_tsd_rl=False, forcing_count=1, forcing_dir_name="focing")
    inventory_path = tmp_path / "inventory.json"
    inventory = discover_basins_inventory(root)
    write_inventory(inventory, inventory_path)
    model = inventory["models"][0]

    with pytest.raises(basins_package.BasinsPackageError) as excinfo:
        basins_package.publish_basins_package(
            inventory_path=inventory_path,
            model_id=model["model_id"],
            version="vbasins-test",
            output_path=tmp_path / "manifest.json",
        )

    payload = excinfo.value.to_payload()
    assert payload["error_code"] == "BASINS_MODEL_NOT_PUBLISHABLE"
    assert payload["message"] == "Basins model is not publishable from this inventory."
    assert payload["model_id"] == model["model_id"]
    assert payload["version"] == "vbasins-test"
    assert payload["path"] == model["source_path"]
    assert payload["status"] == "partial"
    assert payload["missing_required_files"] == ["*.tsd.rl"]
    assert payload["invalid_required_files"] == []
    assert payload["unreadable_required_files"] == []


def test_publish_basins_refusal_payload_names_malformed_ic_file(tmp_path: Path) -> None:
    """The #1197 header shape gate marks the IC invalid at discovery; the refusal
    payload has to name that ``*.cfg.ic`` instead of a generic message (#1432)."""

    root = tmp_path / "basins"
    input_dir = _make_valid_model(root / "tailanhe", "tlh", forcing_count=1)
    (input_dir / "tlh.cfg.ic").write_text("23106\t6\n1\t0.1\n", encoding="utf-8")
    inventory_path = tmp_path / "inventory.json"
    inventory = discover_basins_inventory(root)
    write_inventory(inventory, inventory_path)
    model = inventory["models"][0]

    with pytest.raises(basins_package.BasinsPackageError) as excinfo:
        basins_package.publish_basins_package(
            inventory_path=inventory_path,
            model_id=model["model_id"],
            version="vbasins-test",
            output_path=tmp_path / "manifest.json",
        )

    payload = excinfo.value.to_payload()
    assert payload["error_code"] == "BASINS_MODEL_NOT_PUBLISHABLE"
    assert payload["status"] == "partial"
    assert payload["missing_required_files"] == []
    assert [reason.split(":")[0] for reason in payload["invalid_required_files"]] == ["tlh.cfg.ic"]
    assert payload["unreadable_required_files"] == []


def test_publish_basins_refusal_payload_names_unreadable_required_file(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Unreadable-required-file is discovery's third partial-status channel
    (#1552); the refusal payload carries it as its own key (#1432)."""

    root = tmp_path / "basins"
    _make_valid_model(root / "tailanhe", "tlh", forcing_count=1)
    unreadable_name = "tlh.tsd.lai"
    real_sha256 = basins_discovery._sha256

    def fake_sha256(path: Path) -> str:
        if path.name == unreadable_name:
            raise OSError(errno.EIO, "simulated hash failure")
        return real_sha256(path)

    monkeypatch.setattr(basins_discovery, "_sha256", fake_sha256)
    inventory_path = tmp_path / "inventory.json"
    inventory = discover_basins_inventory(root)
    write_inventory(inventory, inventory_path)
    model = inventory["models"][0]

    with pytest.raises(basins_package.BasinsPackageError) as excinfo:
        basins_package.publish_basins_package(
            inventory_path=inventory_path,
            model_id=model["model_id"],
            version="vbasins-test",
            output_path=tmp_path / "manifest.json",
        )

    payload = excinfo.value.to_payload()
    assert payload["error_code"] == "BASINS_MODEL_NOT_PUBLISHABLE"
    assert payload["status"] == "partial"
    assert payload["missing_required_files"] == []
    assert payload["invalid_required_files"] == []
    assert [reason.split(":")[0] for reason in payload["unreadable_required_files"]] == [unreadable_name]


def test_publish_basins_refusal_without_causes_keeps_payload_byte_identical(tmp_path: Path) -> None:
    """Backward-compat lock: the raise sites that pass no details keep exactly the
    pre-#1432 payload — no cause keys, no empty-list placeholders."""

    root = tmp_path / "basins"
    _make_valid_model(root / "tailanhe", "tlh", forcing_count=1)
    inventory_path = tmp_path / "inventory.json"
    write_inventory(discover_basins_inventory(root), inventory_path)

    with pytest.raises(basins_package.BasinsPackageError) as excinfo:
        basins_package.publish_basins_package(
            inventory_path=inventory_path,
            model_id="basins_absent_shud",
            version="vbasins-test",
            output_path=tmp_path / "manifest.json",
        )

    payload = excinfo.value.to_payload()
    assert json.dumps(payload, sort_keys=True) == json.dumps(
        {
            "error_code": "BASINS_MODEL_NOT_FOUND",
            "message": "Basins model_id was not found in inventory.",
            "model_id": "basins_absent_shud",
            "version": "vbasins-test",
        },
        sort_keys=True,
    )


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


def test_publish_basins_rejects_extra_same_pattern_required_file_without_outputs(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    inventory_path, model_id = _write_valid_inventory(tmp_path)
    secret_file = tmp_path / "basins" / "basin-a" / "input" / "alias-a" / "secret.cfg.para"
    secret_file.write_text("do not publish\n", encoding="utf-8")
    inventory = json.loads(inventory_path.read_text(encoding="utf-8"))
    model = inventory["models"][0]
    model["status"] = "valid"
    model["default_publish_eligible"] = True
    model["missing_required_files"] = []
    model["required_files"]["cfg_para"] = ["alias-a.cfg.para", "secret.cfg.para"]
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
            "vbasins-extra-same-pattern-required",
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
    assert error["version"] == "vbasins-extra-same-pattern-required"
    assert "cfg_para:secret.cfg.para" in error["message"]
    assert not output.exists()
    assert not (
        object_root / "models" / model_id / "vbasins-extra-same-pattern-required" / "manifest.json"
    ).exists()
    assert not (
        object_root / "models" / model_id / "vbasins-extra-same-pattern-required" / "package" / "secret.cfg.para"
    ).exists()
