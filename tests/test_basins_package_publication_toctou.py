"""Basins publication TOCTOU: symlink swaps, ancestors and streaming verification.

Partition 4 of 6 of the former monolith ``tests/test_basins_package_publication.py``
(issue #1912).  Shared test support lives in the non-collectible
``tests/basins_package_helpers.py``.
"""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path

import pytest

import workers.model_registry.basins_package as basins_package
from tests.basins_package_helpers import _object_store_env, _write_valid_inventory
from workers.model_registry.cli import _argparse_main


def test_publish_basins_rejects_existing_object_store_package_symlink_without_modifying_target(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    inventory_path, model_id = _write_valid_inventory(tmp_path)
    object_root = _object_store_env(tmp_path, monkeypatch)
    output = tmp_path / "manifest.json"
    version = "vbasins-object-symlink"
    target_object = object_root / "models" / model_id / "shared-target.txt"
    target_object.parent.mkdir(parents=True)
    target_object.write_text("do not modify\n", encoding="utf-8")
    package_object = object_root / "models" / model_id / version / "package" / "alias-a.cfg.para"
    package_object.parent.mkdir(parents=True)
    try:
        package_object.symlink_to(target_object)
    except (NotImplementedError, OSError) as error:
        pytest.skip(f"symlink support unavailable: {error}")

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
    assert error["error_code"] == "BASINS_PACKAGE_OBJECT_PATH_UNSAFE"
    assert error["model_id"] == model_id
    assert error["version"] == version
    assert error["path"] == str(package_object)
    assert error["manifest_uri"] == f"s3://nhms/models/{model_id}/{version}/manifest.json"
    assert "Traceback" not in captured.err
    assert package_object.is_symlink()
    assert target_object.read_text(encoding="utf-8") == "do not modify\n"
    assert not output.exists()
    assert not (object_root / "models" / model_id / version / "manifest.json").exists()


def test_publish_basins_rejects_object_store_ancestor_replaced_with_symlink_before_final_write(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    inventory_path, model_id = _write_valid_inventory(tmp_path)
    object_root = _object_store_env(tmp_path, monkeypatch)
    output = tmp_path / "manifest.json"
    version = "vbasins-object-ancestor-race"
    escaped_dir = tmp_path / "escaped-object-target"
    escaped_dir.mkdir()
    escaped_target = escaped_dir / "alias-a.cfg.para"
    escaped_target.write_text("do not modify\n", encoding="utf-8")
    package_dir = object_root / "models" / model_id / version / "package"
    original_replace = basins_package.os.rename
    mutated = False

    def swapping_rename(
        src: str | bytes | os.PathLike[str] | os.PathLike[bytes],
        dst: str | bytes | os.PathLike[str] | os.PathLike[bytes],
        *,
        src_dir_fd: int | None = None,
        dst_dir_fd: int | None = None,
    ) -> None:
        nonlocal mutated
        if not mutated and os.fspath(dst) == "alias-a.cfg.para" and src_dir_fd is not None and dst_dir_fd is not None:
            mutated = True
            moved_package_dir = package_dir.with_name("package.original")
            package_dir.rename(moved_package_dir)
            try:
                package_dir.symlink_to(escaped_dir, target_is_directory=True)
            except (NotImplementedError, OSError) as error:
                pytest.skip(f"symlink support unavailable: {error}")
        return original_replace(src, dst, src_dir_fd=src_dir_fd, dst_dir_fd=dst_dir_fd)

    monkeypatch.setattr(basins_package.os, "rename", swapping_rename)

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
    assert mutated is True
    assert error["error_code"] == "BASINS_PACKAGE_OBJECT_PATH_UNSAFE"
    assert error["model_id"] == model_id
    assert error["version"] == version
    assert error["manifest_uri"] == f"s3://nhms/models/{model_id}/{version}/manifest.json"
    assert "Traceback" not in captured.err
    assert package_dir.is_symlink()
    assert escaped_target.read_text(encoding="utf-8") == "do not modify\n"
    assert not output.exists()
    assert not (object_root / "models" / model_id / version / "manifest.json").exists()


def test_publish_basins_rejects_object_store_ancestor_replaced_with_symlink_during_verify_open(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    inventory_path, model_id = _write_valid_inventory(tmp_path)
    object_root = _object_store_env(tmp_path, monkeypatch)
    output = tmp_path / "manifest.json"
    version = "vbasins-object-verify-ancestor-race"
    escaped_dir = tmp_path / "escaped-object-verify-target"
    escaped_dir.mkdir()
    package_dir = object_root / "models" / model_id / version / "package"
    original_size_and_checksum = basins_package._object_size_and_checksum_streaming
    mutated = False

    def swapping_size_and_checksum(
        store: object,
        key: str,
        *,
        model_id: str | None = None,
        version: str | None = None,
        manifest_uri: str | None = None,
    ) -> tuple[int, str]:
        nonlocal mutated
        if not mutated and key.endswith("/package/alias-a.cfg.para"):
            mutated = True
            moved_package_dir = package_dir.with_name("package.original")
            package_dir.rename(moved_package_dir)
            try:
                package_dir.symlink_to(escaped_dir, target_is_directory=True)
            except (NotImplementedError, OSError) as error:
                pytest.skip(f"symlink support unavailable: {error}")
        return original_size_and_checksum(
            store,  # type: ignore[arg-type]
            key,
            model_id=model_id,
            version=version,
            manifest_uri=manifest_uri,
        )

    monkeypatch.setattr(basins_package, "_object_size_and_checksum_streaming", swapping_size_and_checksum)

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
    assert mutated is True
    assert error["error_code"] == "BASINS_PACKAGE_OBJECT_PATH_UNSAFE"
    assert error["model_id"] == model_id
    assert error["version"] == version
    assert error["manifest_uri"] == f"s3://nhms/models/{model_id}/{version}/manifest.json"
    assert "Traceback" not in captured.err
    assert package_dir.is_symlink()
    assert not (escaped_dir / "alias-a.cfg.para").exists()
    assert not output.exists()
    assert not (object_root / "models" / model_id / version / "manifest.json").exists()


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


def test_publish_basins_rejects_runtime_ancestor_replaced_with_symlink_before_final_open(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    inventory_path, model_id = _write_valid_inventory(tmp_path)
    object_root = _object_store_env(tmp_path, monkeypatch)
    output = tmp_path / "manifest.json"
    source_file = tmp_path / "basins" / "basin-a" / "input" / "alias-a" / "alias-a.cfg.para"
    outside_dir = tmp_path / "outside-input"
    outside_dir.mkdir()
    (outside_dir / source_file.name).write_text("outside\n", encoding="utf-8")
    original_writer = basins_package._write_file_to_store_streaming
    original_open = basins_package.os.open
    enabled = False
    mutated = False

    def swapping_open(
        path: str | bytes | os.PathLike[str] | os.PathLike[bytes],
        flags: int,
        mode: int = 0o777,
        *,
        dir_fd: int | None = None,
    ) -> int:
        nonlocal mutated
        if enabled and not mutated and dir_fd is not None and os.fspath(path) == source_file.name:
            mutated = True
            moved_dir = source_file.parent.with_name("alias-a.original")
            source_file.parent.rename(moved_dir)
            try:
                source_file.parent.symlink_to(outside_dir, target_is_directory=True)
            except (NotImplementedError, OSError) as error:
                pytest.skip(f"symlink support unavailable: {error}")
        return original_open(path, flags, mode, dir_fd=dir_fd)

    def enabling_writer(
        store: object,
        key: str,
        path: Path,
        source_root: Path,
        *,
        model_id: str | None = None,
        version: str | None = None,
        manifest_uri: str | None = None,
    ) -> tuple[int, str]:
        nonlocal enabled
        if path == source_file:
            enabled = True
        return original_writer(
            store,  # type: ignore[arg-type]
            key,
            path,
            source_root,
            model_id=model_id,
            version=version,
            manifest_uri=manifest_uri,
        )

    monkeypatch.setattr(basins_package.os, "open", swapping_open)
    monkeypatch.setattr(basins_package, "_write_file_to_store_streaming", enabling_writer)

    exit_code = _argparse_main(
        [
            "publish-basins",
            "--inventory",
            str(inventory_path),
            "--model-id",
            model_id,
            "--version",
            "vbasins-runtime-ancestor-symlink",
            "--output",
            str(output),
        ]
    )

    captured = capsys.readouterr()
    error = json.loads(captured.err)
    assert exit_code == 1
    assert captured.out == ""
    assert mutated is True
    assert error["error_code"] == "BASINS_PACKAGE_PATH_UNSAFE"
    assert error["model_id"] == model_id
    assert error["version"] == "vbasins-runtime-ancestor-symlink"
    assert error["path"] == str(source_file)
    assert "Traceback" not in captured.err
    assert not output.exists()
    assert not (object_root / "models" / model_id / "vbasins-runtime-ancestor-symlink" / "manifest.json").exists()
    assert not (
        object_root / "models" / model_id / "vbasins-runtime-ancestor-symlink" / "package" / source_file.name
    ).exists()


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
    # #1813: excluded forcing is stat-ed, not hashed, so the pre-sampling seam
    # for this TOCTOU is now _source_file_size.  The guarantee under test is
    # unchanged: a CSV swapped for a symlink before sampling must be refused.
    original_source_file_size = basins_package._source_file_size
    mutated = False

    def symlink_swapping_source_file_size(
        path: Path,
        source_root: Path,
        *,
        model_id: str | None = None,
        version: str | None = None,
        manifest_uri: str | None = None,
    ) -> int:
        nonlocal mutated
        size_bytes = original_source_file_size(
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
        return size_bytes

    monkeypatch.setattr(basins_package, "_source_file_size", symlink_swapping_source_file_size)

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


def test_publish_basins_rejects_forcing_ancestor_replaced_with_symlink_before_sampling_open(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    inventory_path, model_id = _write_valid_inventory(tmp_path, forcing_count=1)
    object_root = _object_store_env(tmp_path, monkeypatch)
    output = tmp_path / "manifest.json"
    forcing_file = tmp_path / "basins" / "basin-a" / "forcing" / "X000001.csv"
    outside_dir = tmp_path / "outside-forcing"
    outside_dir.mkdir()
    (outside_dir / forcing_file.name).write_text("time,value\n2026-01-01,999\n", encoding="utf-8")
    original_source_file_size = basins_package._source_file_size
    original_open = basins_package.os.open
    enabled = False
    mutated = False

    def swapping_open(
        path: str | bytes | os.PathLike[str] | os.PathLike[bytes],
        flags: int,
        mode: int = 0o777,
        *,
        dir_fd: int | None = None,
    ) -> int:
        nonlocal mutated
        if enabled and not mutated and dir_fd is not None and os.fspath(path) == forcing_file.name:
            mutated = True
            moved_dir = forcing_file.parent.with_name("forcing.original")
            forcing_file.parent.rename(moved_dir)
            try:
                forcing_file.parent.symlink_to(outside_dir, target_is_directory=True)
            except (NotImplementedError, OSError) as error:
                pytest.skip(f"symlink support unavailable: {error}")
        return original_open(path, flags, mode, dir_fd=dir_fd)

    def enabling_source_file_size(
        path: Path,
        source_root: Path,
        *,
        model_id: str | None = None,
        version: str | None = None,
        manifest_uri: str | None = None,
    ) -> int:
        nonlocal enabled
        if path == forcing_file:
            enabled = True
        return original_source_file_size(
            path,
            source_root,
            model_id=model_id,
            version=version,
            manifest_uri=manifest_uri,
        )

    monkeypatch.setattr(basins_package.os, "open", swapping_open)
    monkeypatch.setattr(basins_package, "_source_file_size", enabling_source_file_size)

    exit_code = _argparse_main(
        [
            "publish-basins",
            "--inventory",
            str(inventory_path),
            "--model-id",
            model_id,
            "--version",
            "vbasins-forcing-ancestor-symlink",
            "--output",
            str(output),
        ]
    )

    captured = capsys.readouterr()
    error = json.loads(captured.err)
    assert exit_code == 1
    assert captured.out == ""
    assert mutated is True
    assert error["error_code"] == "BASINS_PACKAGE_PATH_UNSAFE"
    assert error["model_id"] == model_id
    assert error["version"] == "vbasins-forcing-ancestor-symlink"
    assert error["path"] == str(forcing_file)
    assert "Traceback" not in captured.err
    assert not output.exists()
    assert not (object_root / "models" / model_id / "vbasins-forcing-ancestor-symlink" / "manifest.json").exists()
    assert not (object_root / "models" / model_id / "vbasins-forcing-ancestor-symlink" / "package").exists()


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
