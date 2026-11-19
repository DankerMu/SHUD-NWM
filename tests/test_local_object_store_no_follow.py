"""``LocalObjectStore`` bounds limited reads and refuses FIFOs, symlinked roots, key parents and parent swaps.

The store resolves every key through the no-follow primitives in
``packages.common.safe_fs``; these cases pin that a symlink or a FIFO anywhere on
the path is a stable ``ObjectStoreError`` and that nothing outside the store root
is read, written or deleted.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from packages.common import safe_fs
from packages.common.object_store import LocalObjectStore, ObjectStoreError


def test_local_object_store_read_bytes_limited_uses_bounded_read(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = LocalObjectStore(tmp_path)
    key = "runs/m10_148_bounded/input/manifest.json"
    store.write_bytes_atomic(key, b"abcdef")
    path = store.resolve_path(key)
    read_sizes: list[int] = []
    original_os_read = safe_fs.os.read

    def fake_os_read(fd: int, size: int) -> bytes:
        read_sizes.append(size)
        return original_os_read(fd, size)

    original_read_bytes = type(path).read_bytes

    def forbidden_read_bytes(self: Path) -> bytes:
        if self != path:
            return original_read_bytes(self)
        raise AssertionError("read_bytes() must not be used for limited reads")

    monkeypatch.setattr(safe_fs.os, "read", fake_os_read)
    monkeypatch.setattr(type(path), "read_bytes", forbidden_read_bytes)

    with pytest.raises(ObjectStoreError):
        store.read_bytes_limited(key, max_bytes=5)

    assert read_sizes == [6]


@pytest.mark.parametrize("operation", ["read_bytes_limited", "iter_bytes", "size_and_checksum"])
def test_local_object_store_rejects_fifo_entry_without_blocking(
    tmp_path: Path,
    operation: str,
) -> None:
    store = LocalObjectStore(tmp_path / "object-store", "s3://nhms-prod/m10")
    key = "runs/m10_148_fifo/input/manifest.json"
    fifo_path = store.resolve_path(key)
    fifo_path.parent.mkdir(parents=True)
    os.mkfifo(fifo_path)

    with pytest.raises(ObjectStoreError):
        if operation == "read_bytes_limited":
            store.read_bytes_limited(key, max_bytes=1024)
        elif operation == "iter_bytes":
            next(store.iter_bytes(key))
        else:
            store.size_and_checksum(key)


@pytest.mark.parametrize("case", ["parent", "root"])
def test_local_object_store_rejects_symlinked_root_identity(
    tmp_path: Path,
    case: str,
) -> None:
    external = tmp_path / "external-store"
    external.mkdir()
    symlink = tmp_path / "store-link"
    symlink.symlink_to(external, target_is_directory=True)
    root = symlink / "child" if case == "parent" else symlink

    with pytest.raises(ObjectStoreError):
        LocalObjectStore(root, "s3://nhms-prod/m10")

    assert sorted(path.name for path in external.iterdir()) == []


@pytest.mark.parametrize(
    "operation",
    ["read_bytes_limited", "size_and_checksum", "write_bytes_atomic", "delete"],
)
def test_local_object_store_rejects_symlinked_object_key_parent_identity(
    tmp_path: Path,
    operation: str,
) -> None:
    store = LocalObjectStore(tmp_path / "object-store", "s3://nhms-prod/m10")
    stale_dir = tmp_path / "object-store" / "runs" / "stale" / "input"
    stale_dir.mkdir(parents=True)
    stale_file = stale_dir / "manifest.json"
    stale_file.write_bytes(b"stale sibling content\n")
    current_parent = tmp_path / "object-store" / "runs" / "current"
    current_parent.parent.mkdir(parents=True, exist_ok=True)
    current_parent.symlink_to(tmp_path / "object-store" / "runs" / "stale", target_is_directory=True)
    key = "runs/current/input/manifest.json"

    with pytest.raises(ObjectStoreError):
        if operation == "read_bytes_limited":
            store.read_bytes_limited(key, max_bytes=1024)
        elif operation == "size_and_checksum":
            store.size_and_checksum(key)
        elif operation == "write_bytes_atomic":
            store.write_bytes_atomic(key, b"new content\n")
        else:
            store.delete(key)

    assert stale_file.read_bytes() == b"stale sibling content\n"


def test_local_object_store_rejects_parent_symlink_swap_before_write_or_delete(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = LocalObjectStore(tmp_path / "object-store", "s3://nhms-prod/m10")
    external = tmp_path / "external-store"
    external.mkdir()
    original_verify = safe_fs._verify_fd_matches_path
    swapped = False

    def swap_run_parent(fd: int, path: Path) -> None:
        nonlocal swapped
        if path.name == "input" and path.parent.name == "run_001" and not swapped:
            swapped = True
            run_dir = path.parent
            run_dir.rmdir()
            run_dir.symlink_to(external, target_is_directory=True)
        original_verify(fd, path)

    monkeypatch.setattr(safe_fs, "_verify_fd_matches_path", swap_run_parent)
    with pytest.raises(ObjectStoreError):
        store.write_bytes_atomic("runs/run_001/input/manifest.json", b"manifest\n")
    assert not (external / "input" / "manifest.json").exists()

    monkeypatch.setattr(safe_fs, "_verify_fd_matches_path", original_verify)
    run_dir = tmp_path / "object-store" / "runs" / "run_001"
    if run_dir.is_symlink():
        run_dir.unlink()
    safe_fs.rmtree_no_follow(run_dir, containment_root=tmp_path / "object-store", missing_ok=True)
    (run_dir / "input").mkdir(parents=True)
    target = run_dir / "input" / "manifest.json"
    target.write_text("internal\n", encoding="utf-8")
    external_target = external / "input" / "manifest.json"
    external_target.parent.mkdir(parents=True)
    external_target.write_text("external must remain\n", encoding="utf-8")
    original_open_parent = safe_fs._open_parent_dir
    swapped_for_delete = False

    def swap_delete_parent(path: Path, *, containment_root: Path | None, create: bool):
        nonlocal swapped_for_delete
        if path == target and not swapped_for_delete:
            swapped_for_delete = True
            safe_fs.rmtree_no_follow(run_dir, containment_root=tmp_path / "object-store")
            run_dir.symlink_to(external, target_is_directory=True)
        return original_open_parent(path, containment_root=containment_root, create=create)

    monkeypatch.setattr(safe_fs, "_open_parent_dir", swap_delete_parent)
    with pytest.raises(ObjectStoreError):
        store.delete("runs/run_001/input/manifest.json")
    assert external_target.read_text(encoding="utf-8") == "external must remain\n"
