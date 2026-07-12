#!/usr/bin/env python3
"""Bounded, verify-before-delete product archive mover for node-27."""

from __future__ import annotations

import argparse
import ctypes
import errno
import fcntl
import hashlib
import io
import json
import os
import stat
import subprocess
import sys
import tarfile
import threading
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

import jsonschema

from packages.common.safe_fs import (
    SafeFilesystemError,
    atomic_write_bytes_no_follow,
    ensure_directory_no_follow,
    open_directory_no_follow,
    stat_no_follow,
    verify_directory_no_follow,
)
from packages.common.source_identity import normalize_source_id
from packages.common.storage import (
    DEFAULT_DB_RETENTION_DAYS,
    ArchiveConfigurationError,
    ArchiveIdentity,
    archive_provenance_paths,
    validate_archive_configuration,
    validate_product_archive_manifest_binding,
)

SCHEMA_VERSION = "1.0"
TOOL_VERSION = "node27-product-archive/1"
MAX_MANIFEST_BYTES = 16 * 1024 * 1024
MAX_DISCOVERY = 100_000
MAX_TREE_ENTRIES = 100_000
MAX_TREE_DEPTH = 16
MAX_FILE_BYTES = 256 * 1024**3
MAX_SOURCE_BYTES = 1024**4
MAX_ARCHIVE_BYTES = 1024**4
MAX_TAR_BYTES = 2 * 1024**4
TOOL_TIMEOUT_SECONDS = 3_600
MAX_STDERR_BYTES = 64 * 1024
_ROOT = Path(__file__).resolve().parents[1]
MANIFEST_SCHEMA_PATH = _ROOT / "schemas/product_archive_manifest.schema.json"
RECEIPT_SCHEMA_PATH = _ROOT / "schemas/product_archive_receipt.schema.json"
_DIR_FLAGS = os.O_RDONLY | os.O_DIRECTORY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0)
_READ_FLAGS = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0)
_CREATE_FLAGS = (
    os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0)
)
_CREATE_RDWR_FLAGS = (
    os.O_RDWR | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0)
)


class ArchiveMoverError(RuntimeError):
    """A deterministic safety or contract failure."""

    def __init__(self, message: str, *, indeterminate: bool = False, residue: Sequence[str] = ()) -> None:
        super().__init__(message)
        self.indeterminate = indeterminate
        self.residue = tuple(residue)


@dataclass(frozen=True)
class FileRecord:
    path: str
    size_bytes: int
    sha256: str
    device: int
    inode: int
    mode: int
    mtime_ns: int

    def manifest_entry(self) -> dict[str, Any]:
        return {"path": self.path, "sha256": self.sha256, "size_bytes": self.size_bytes}


@dataclass(frozen=True)
class DirectoryRecord:
    path: str
    device: int
    inode: int
    mode: int
    mtime_ns: int


@dataclass(frozen=True)
class Candidate:
    identity: ArchiveIdentity
    source_path: Path
    source_relative: str
    source_bytes: int
    files: tuple[FileRecord, ...]
    directories: tuple[DirectoryRecord, ...]
    source_root_stat: tuple[int, int, int]
    source_mount_id: int

    @property
    def sort_key(self) -> tuple[str, str, str]:
        return self.identity.cycle_time, self.identity.lane, _identity_key(self.identity)

    def receipt_value(self, archive_root: Path) -> dict[str, Any]:
        archive_path = archive_provenance_paths(archive_root, identity=self.identity).archive
        return {
            "identity": _identity_dict(self.identity),
            "source_path": self.source_relative,
            "archive_path": archive_path.relative_to(archive_root).as_posix(),
            "source_bytes": self.source_bytes,
        }


@dataclass(frozen=True)
class DiscoveryFailure:
    lane_hint: str
    locator: str
    reason: str

    def value(self) -> dict[str, str]:
        return {"lane_hint": self.lane_hint, "locator": self.locator, "reason": self.reason}


@dataclass(frozen=True)
class MoverConfig:
    object_store_root: Path
    archive_root: Path
    receipt_path: Path
    lock_path: Path
    zstd_path: Path
    minimum_age_days: int = 45
    per_tick_bound: int = 10
    enforce: bool = False


MountIdProvider = Callable[[int], int]
RenameNoReplace = Callable[[int, str, int, str], None]


def fd_mount_id(fd: int) -> int:
    """Return the Linux mount ID for an opened descriptor, failing closed elsewhere."""
    path = Path(f"/proc/self/fdinfo/{fd}")
    try:
        text = path.read_text(encoding="ascii")
    except OSError as error:
        raise ArchiveMoverError(f"cannot prove mount ID for fd {fd}: {error}") from error
    for line in text.splitlines():
        if line.startswith("mnt_id:"):
            try:
                return int(line.split(":", 1)[1].strip())
            except ValueError as error:
                raise ArchiveMoverError(f"invalid mount ID evidence for fd {fd}") from error
    raise ArchiveMoverError(f"mount ID evidence is unavailable for fd {fd}")


def rename_no_replace(src_fd: int, src: str, dst_fd: int, dst: str) -> None:
    """Linux renameat2(RENAME_NOREPLACE), with no unsafe check-then-rename fallback."""
    libc = ctypes.CDLL(None, use_errno=True)
    function = getattr(libc, "renameat2", None)
    if function is None:
        raise ArchiveMoverError("native no-replace rename is unavailable")
    function.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p, ctypes.c_uint]
    function.restype = ctypes.c_int
    if function(src_fd, os.fsencode(src), dst_fd, os.fsencode(dst), 1) != 0:
        code = ctypes.get_errno()
        raise OSError(code, os.strerror(code), dst)


def _safe_names(fd: int, label: str, *, remaining: int) -> list[str]:
    names: list[str] = []
    try:
        with os.scandir(fd) as entries:
            for entry in entries:
                names.append(entry.name)
                if len(names) > remaining:
                    raise ArchiveMoverError(f"tree exceeds {MAX_TREE_ENTRIES} entries: {label}")
    except ArchiveMoverError:
        raise
    except OSError as error:
        raise ArchiveMoverError(f"cannot list {label}: {error}") from error
    return sorted(names)


def _open_child_dir(
    parent_fd: int,
    name: str,
    label: str,
    mount_id: int,
    provider: MountIdProvider,
    device: int | None = None,
) -> int:
    fd: int | None = None
    try:
        expected = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
        if stat.S_ISLNK(expected.st_mode) or not stat.S_ISDIR(expected.st_mode):
            raise ArchiveMoverError(f"non-directory or symlink in product tree: {label}")
        fd = os.open(name, _DIR_FLAGS, dir_fd=parent_fd)
        opened = os.fstat(fd)
        if (opened.st_dev, opened.st_ino) != (expected.st_dev, expected.st_ino):
            raise ArchiveMoverError(f"directory changed while opening: {label}")
        if device is not None and opened.st_dev != device:
            raise ArchiveMoverError(f"cross-device directory rejected: {label}")
        if provider(fd) != mount_id:
            raise ArchiveMoverError(f"cross-mount directory rejected: {label}")
        return fd
    except ArchiveMoverError:
        if fd is not None:
            os.close(fd)
        raise
    except OSError as error:
        if fd is not None:
            os.close(fd)
        raise ArchiveMoverError(f"cannot open directory {label}: {error}") from error


def _open_regular(
    parent_fd: int,
    name: str,
    label: str,
    mount_id: int,
    provider: MountIdProvider,
    device: int | None = None,
    max_file_bytes: int | None = None,
) -> tuple[int, os.stat_result]:
    fd: int | None = None
    try:
        expected = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
        if stat.S_ISLNK(expected.st_mode) or not stat.S_ISREG(expected.st_mode):
            raise ArchiveMoverError(f"unsupported product entry type: {label}")
        if expected.st_nlink != 1:
            raise ArchiveMoverError(f"hard-linked product file rejected: {label}")
        file_limit = MAX_FILE_BYTES if max_file_bytes is None else max_file_bytes
        if expected.st_size > file_limit:
            raise ArchiveMoverError(f"product file exceeds {file_limit} bytes: {label}")
        fd = os.open(name, _READ_FLAGS, dir_fd=parent_fd)
        opened = os.fstat(fd)
        if (opened.st_dev, opened.st_ino, opened.st_mode) != (expected.st_dev, expected.st_ino, expected.st_mode):
            raise ArchiveMoverError(f"file changed while opening: {label}")
        if device is not None and opened.st_dev != device:
            raise ArchiveMoverError(f"cross-device file rejected: {label}")
        if provider(fd) != mount_id:
            raise ArchiveMoverError(f"cross-mount file rejected: {label}")
        return fd, opened
    except ArchiveMoverError:
        if fd is not None:
            os.close(fd)
        raise
    except OSError as error:
        if fd is not None:
            os.close(fd)
        raise ArchiveMoverError(f"cannot open product file {label}: {error}") from error


def scan_tree_snapshot(
    root_fd: int, *, mount_id: int, mount_id_provider: MountIdProvider = fd_mount_id
) -> tuple[tuple[FileRecord, ...], tuple[DirectoryRecord, ...]]:
    """Return a complete deterministic descriptor-bound tree snapshot."""
    root = os.fstat(root_fd)
    if not stat.S_ISDIR(root.st_mode) or mount_id_provider(root_fd) != mount_id:
        raise ArchiveMoverError("source root device/mount identity changed")
    records: list[FileRecord] = []
    directories: list[DirectoryRecord] = []
    count = 0
    total = 0

    def walk(fd: int, prefix: str, depth: int) -> None:
        nonlocal count, total
        if depth > MAX_TREE_DEPTH:
            raise ArchiveMoverError(f"tree exceeds depth {MAX_TREE_DEPTH}: {prefix}")
        for name in _safe_names(fd, prefix or ".", remaining=MAX_TREE_ENTRIES - count):
            count += 1
            if count > MAX_TREE_ENTRIES:
                raise ArchiveMoverError(f"tree exceeds {MAX_TREE_ENTRIES} entries")
            relative = f"{prefix}/{name}" if prefix else name
            try:
                info = os.stat(name, dir_fd=fd, follow_symlinks=False)
            except OSError as error:
                raise ArchiveMoverError(f"cannot stat product entry {relative}: {error}") from error
            if stat.S_ISDIR(info.st_mode):
                child = _open_child_dir(
                    fd,
                    name,
                    relative,
                    mount_id,
                    mount_id_provider,
                    root.st_dev,
                )
                try:
                    opened_directory = os.fstat(child)
                    directories.append(
                        DirectoryRecord(
                            relative,
                            opened_directory.st_dev,
                            opened_directory.st_ino,
                            opened_directory.st_mode,
                            opened_directory.st_mtime_ns,
                        )
                    )
                    walk(child, relative, depth + 1)
                finally:
                    os.close(child)
                continue
            file_fd, opened = _open_regular(
                fd,
                name,
                relative,
                mount_id,
                mount_id_provider,
                root.st_dev,
            )
            digest = hashlib.sha256()
            size = 0
            try:
                while chunk := os.read(file_fd, 1024 * 1024):
                    size += len(chunk)
                    digest.update(chunk)
                after = os.fstat(file_fd)
            except OSError as error:
                raise ArchiveMoverError(f"cannot read product file {relative}: {error}") from error
            finally:
                os.close(file_fd)
            if _stat_signature(opened) != _stat_signature(after) or size != opened.st_size:
                raise ArchiveMoverError(f"product file changed while hashing: {relative}")
            total += size
            if total > MAX_SOURCE_BYTES:
                raise ArchiveMoverError(f"tree exceeds {MAX_SOURCE_BYTES} source bytes")
            records.append(
                FileRecord(
                    relative, size, digest.hexdigest(), opened.st_dev, opened.st_ino, opened.st_mode, opened.st_mtime_ns
                )
            )

    walk(root_fd, "", 0)
    if not records:
        raise ArchiveMoverError("product tree contains no regular files")
    return tuple(records), tuple(directories)


def scan_tree(
    root_fd: int, *, mount_id: int, mount_id_provider: MountIdProvider = fd_mount_id
) -> tuple[FileRecord, ...]:
    """Compatibility helper returning the file portion of a complete snapshot."""
    return scan_tree_snapshot(root_fd, mount_id=mount_id, mount_id_provider=mount_id_provider)[0]


def _parse_hour(value: Any, *, label: str) -> datetime:
    if not isinstance(value, str):
        raise ArchiveMoverError(f"{label} must be a timestamp")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as error:
        raise ArchiveMoverError(f"{label} is invalid: {value!r}") from error
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ArchiveMoverError(f"{label} must be timezone-aware")
    parsed = parsed.astimezone(UTC)
    if parsed.minute or parsed.second or parsed.microsecond:
        raise ArchiveMoverError(f"{label} must be an exact UTC hour")
    return parsed


def _parse_instant(value: Any, *, label: str) -> datetime:
    if not isinstance(value, str):
        raise ArchiveMoverError(f"{label} must be a timestamp")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as error:
        raise ArchiveMoverError(f"{label} is invalid: {value!r}") from error
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ArchiveMoverError(f"{label} must be timezone-aware")
    return parsed.astimezone(UTC)


def _forcing_identity(manifest: Mapping[str, Any], parts: Sequence[str]) -> ArchiveIdentity:
    if len(parts) != 5 or parts[0] != "forcing":
        raise ArchiveMoverError("forcing locator shape is invalid")
    _, source_segment, cycle, basin, model = parts
    source = normalize_source_id(str(manifest.get("source_id") or ""))
    time = _parse_hour(manifest.get("cycle_time"), label="forcing cycle_time")
    if (source.lower(), time.strftime("%Y%m%d%H"), manifest.get("basin_version_id"), manifest.get("model_id")) != (
        source_segment,
        cycle,
        basin,
        model,
    ):
        raise ArchiveMoverError("forcing package identity does not bind its leaf path")
    return ArchiveIdentity(
        "forcing", source, cycle, time.strftime("%Y-%m-%dT%H:00:00Z"), basin_version_id=basin, model_id=model
    )


def _run_identity(manifest: Mapping[str, Any], run_id: str) -> ArchiveIdentity:
    model = manifest.get("model")
    outputs = manifest.get("outputs")
    if not isinstance(model, Mapping) or not isinstance(outputs, Mapping):
        raise ArchiveMoverError("run manifest lacks model/outputs identity")
    source = normalize_source_id(str(manifest.get("source_id") or ""))
    time = _parse_hour(manifest.get("cycle_time"), label="run cycle_time")
    start_time = _parse_instant(manifest.get("start_time"), label="run start_time")
    end_time = _parse_instant(manifest.get("end_time"), label="run end_time")
    if start_time > end_time:
        raise ArchiveMoverError("run manifest window is inverted")
    required = {
        "run_id": run_id,
        "run_manifest_uri": f"runs/{run_id}/input/manifest.json",
        "output_uri": f"runs/{run_id}/output",
    }
    if (
        manifest.get("run_id") != required["run_id"]
        or _object_key(outputs.get("run_manifest_uri")) != required["run_manifest_uri"]
        or _object_key(outputs.get("output_uri")).rstrip("/") != required["output_uri"]
    ):
        raise ArchiveMoverError("run manifest identity/outputs do not bind run directory")
    basin = model.get("basin_version_id")
    model_id = model.get("model_id")
    if not isinstance(basin, str) or not isinstance(model_id, str):
        raise ArchiveMoverError("run manifest model identity is incomplete")
    if not _safe_segment(basin) or not _safe_segment(model_id):
        raise ArchiveMoverError("run manifest model identity contains an unsafe segment")
    duplicated = manifest.get("identity")
    if duplicated is not None:
        if not isinstance(duplicated, Mapping):
            raise ArchiveMoverError("run duplicated identity must be an object")
        expected = {
            "run_id": run_id,
            "cycle_time": time.strftime("%Y-%m-%dT%H:00:00Z"),
            "model_id": model_id,
            "basin_version_id": basin,
            "start_time": _time(start_time),
            "end_time": _time(end_time),
        }
        for source_field in ("source", "source_id"):
            if source_field in duplicated and normalize_source_id(str(duplicated[source_field])) != source:
                raise ArchiveMoverError(f"run duplicated identity drift: {source_field}")
        for field, expected_value in expected.items():
            if field in duplicated:
                actual = duplicated[field]
                if field == "cycle_time":
                    actual = _parse_hour(actual, label="run duplicated cycle_time").strftime("%Y-%m-%dT%H:00:00Z")
                elif field in {"start_time", "end_time"}:
                    actual = _time(_parse_instant(actual, label=f"run duplicated {field}"))
                if actual != expected_value:
                    raise ArchiveMoverError(f"run duplicated identity drift: {field}")
    return ArchiveIdentity(
        "runs", source, time.strftime("%Y%m%d%H"), time.strftime("%Y-%m-%dT%H:00:00Z"), run_id=run_id
    )


def _state_identity(parts: Sequence[str]) -> ArchiveIdentity:
    if len(parts) == 4:
        _, source_segment, model, cycle = parts
        try:
            source = normalize_source_id(source_segment)
        except ValueError as error:
            raise ArchiveMoverError(f"unknown provider state layout: {source_segment}") from error
        if source_segment != source:
            raise ArchiveMoverError(f"provider state layout must use canonical source ID: {source_segment}")
    elif len(parts) == 3:
        _, model, cycle = parts
        source = "legacy-unqualified"
        try:
            normalize_source_id(model)
        except ValueError:
            pass
        else:
            raise ArchiveMoverError("ambiguous provider/legacy state layout")
    else:
        raise ArchiveMoverError("state locator shape is invalid")
    try:
        time = datetime.strptime(cycle, "%Y%m%d%H").replace(tzinfo=UTC)
    except ValueError as error:
        raise ArchiveMoverError("state valid-time directory is not canonical") from error
    return ArchiveIdentity("states", source, cycle, time.strftime("%Y-%m-%dT%H:00:00Z"), model_id=model)


def discover_candidates(
    config: MoverConfig, *, now: datetime, mount_id_provider: MountIdProvider = fd_mount_id
) -> tuple[list[Candidate], list[DiscoveryFailure]]:
    """Discover lane-specific physical leaves; malformed leaves remain locator failures."""
    root = verify_directory_no_follow(config.object_store_root)
    root_fd = open_directory_no_follow(root)
    root_stat = os.fstat(root_fd)
    root_mount = mount_id_provider(root_fd)
    candidates: list[Candidate] = []
    failures: list[DiscoveryFailure] = []
    observed = 0
    cutoff = now - timedelta(days=config.minimum_age_days)

    def lane_exists(lane: str, path: Path) -> bool:
        nonlocal observed
        try:
            return _entry_exists(path, root)
        except Exception as error:
            observed += 1
            if observed > MAX_DISCOVERY:
                raise ArchiveMoverError(f"discovery exceeds {MAX_DISCOVERY} candidates/failures") from error
            failures.append(DiscoveryFailure(lane, lane, str(error)))
            return False

    def add_leaf(lane: str, relative: str, manifest_rel: str | None) -> None:
        nonlocal observed
        observed += 1
        if observed > MAX_DISCOVERY:
            raise ArchiveMoverError(f"discovery exceeds {MAX_DISCOVERY} candidates/failures")
        try:
            leaf = root / relative
            leaf_fd = open_directory_no_follow(leaf, containment_root=root)
            try:
                opened = os.fstat(leaf_fd)
                if opened.st_dev != root_stat.st_dev or mount_id_provider(leaf_fd) != root_mount:
                    raise ArchiveMoverError(f"cross-device/mount candidate rejected: {relative}")
                if manifest_rel is None:
                    identity = _state_identity(relative.split("/"))
                else:
                    manifest, manifest_record = _read_json_relative_fd(
                        leaf_fd,
                        manifest_rel,
                        label=f"{relative}/{manifest_rel}",
                        mount_id=root_mount,
                        mount_id_provider=mount_id_provider,
                    )
                    identity = (
                        _forcing_identity(manifest, relative.split("/"))
                        if lane == "forcing"
                        else _run_identity(manifest, relative.split("/")[-1])
                    )
                try:
                    archive_provenance_paths(config.archive_root, identity=identity)
                except ArchiveConfigurationError as error:
                    raise ArchiveMoverError(f"candidate archive identity is unsafe: {error}") from error
                files, directories = scan_tree_snapshot(
                    leaf_fd, mount_id=root_mount, mount_id_provider=mount_id_provider
                )
                if manifest_rel is not None:
                    scanned_manifest = next((item for item in files if item.path == manifest_rel), None)
                    if scanned_manifest != manifest_record:
                        raise ArchiveMoverError(
                            f"manifest changed between identity read and tree snapshot: {relative}/{manifest_rel}"
                        )
                if _parse_hour(identity.cycle_time, label="candidate cycle_time") >= cutoff:
                    return
                candidates.append(
                    Candidate(
                        identity,
                        leaf,
                        relative,
                        sum(item.size_bytes for item in files),
                        files,
                        directories,
                        (opened.st_dev, opened.st_ino, opened.st_mtime_ns),
                        root_mount,
                    )
                )
            finally:
                os.close(leaf_fd)
        except Exception as error:
            failures.append(DiscoveryFailure(lane, relative, str(error)))

    try:
        # A deliberately shallow locator pass; full leaf safety is proven by scan_tree.
        forcing = root / "forcing"
        if lane_exists("forcing", forcing):
            try:
                for source in _path_dirs(forcing, root):
                    for cycle in _path_dirs(forcing / source, root):
                        for basin in _path_dirs(forcing / source / cycle, root):
                            for model in _path_entries(forcing / source / cycle / basin, root):
                                add_leaf("forcing", f"forcing/{source}/{cycle}/{basin}/{model}", "forcing_package.json")
            except Exception as error:
                failures.append(DiscoveryFailure("forcing", "forcing", str(error)))
        runs = root / "runs"
        if lane_exists("runs", runs):
            try:
                for run_id in _path_entries(runs, root):
                    add_leaf("runs", f"runs/{run_id}", "input/manifest.json")
            except Exception as error:
                failures.append(DiscoveryFailure("runs", "runs", str(error)))
        states = root / "states"
        if lane_exists("states", states):
            try:
                for first in _path_dirs(states, root):
                    first_path = states / first
                    try:
                        provider = normalize_source_id(first)
                    except ValueError:
                        provider = None
                    if provider is not None:
                        for model in _path_dirs(first_path, root):
                            for cycle in _path_entries(first_path / model, root):
                                add_leaf("states", f"states/{first}/{model}/{cycle}", None)
                    else:
                        for cycle in _path_entries(first_path, root):
                            add_leaf("states", f"states/{first}/{cycle}", None)
            except Exception as error:
                failures.append(DiscoveryFailure("states", "states", str(error)))
    finally:
        os.close(root_fd)
    candidates.sort(key=lambda value: value.sort_key)
    failures.sort(key=lambda value: (value.lane_hint, value.locator, value.reason))
    return candidates, failures


def _path_entries(path: Path, root: Path) -> list[str]:
    try:
        fd = open_directory_no_follow(path, containment_root=root)
        try:
            return _safe_names(fd, path.relative_to(root).as_posix(), remaining=MAX_DISCOVERY)
        finally:
            os.close(fd)
    except Exception as error:
        if "tree exceeds" in str(error):
            raise ArchiveMoverError(f"discovery exceeds {MAX_DISCOVERY} candidates/failures") from error
        raise ArchiveMoverError(f"cannot enumerate {path}: {error}") from error


def _read_json_relative_fd(
    root_fd: int,
    relative: str,
    *,
    label: str,
    mount_id: int,
    mount_id_provider: MountIdProvider,
) -> tuple[dict[str, Any], FileRecord]:
    if not _safe_relative(relative):
        raise ArchiveMoverError(f"unsafe relative manifest path: {relative!r}")
    current_fd = os.dup(root_fd)
    root_device = os.fstat(root_fd).st_dev
    try:
        parts = relative.split("/")
        for part in parts[:-1]:
            child = _open_child_dir(
                current_fd,
                part,
                label,
                mount_id,
                mount_id_provider,
                root_device,
            )
            os.close(current_fd)
            current_fd = child
        file_fd, opened = _open_regular(
            current_fd,
            parts[-1],
            label,
            mount_id,
            mount_id_provider,
            root_device,
        )
        try:
            content = bytearray()
            while len(content) <= MAX_MANIFEST_BYTES:
                chunk = os.read(file_fd, MAX_MANIFEST_BYTES + 1 - len(content))
                if not chunk:
                    break
                content.extend(chunk)
            if len(content) > MAX_MANIFEST_BYTES:
                raise ArchiveMoverError(f"manifest exceeds {MAX_MANIFEST_BYTES} bytes: {label}")
            after = os.fstat(file_fd)
            if _stat_signature(opened) != _stat_signature(after) or len(content) != opened.st_size:
                raise ArchiveMoverError(f"manifest changed while reading: {label}")
        finally:
            os.close(file_fd)
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ArchiveMoverError(f"cannot read bounded manifest {label}: {error}") from error
    finally:
        os.close(current_fd)
    try:
        value = json.loads(content)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ArchiveMoverError(f"cannot parse bounded manifest {label}: {error}") from error
    if not isinstance(value, dict):
        raise ArchiveMoverError(f"manifest must be a JSON object: {label}")
    return value, FileRecord(
        relative,
        len(content),
        hashlib.sha256(content).hexdigest(),
        opened.st_dev,
        opened.st_ino,
        opened.st_mode,
        opened.st_mtime_ns,
    )


def _entry_exists(path: Path, root: Path) -> bool:
    try:
        info = stat_no_follow(path, containment_root=root)
    except FileNotFoundError:
        return False
    if not stat.S_ISDIR(info.st_mode):
        raise ArchiveMoverError(f"namespace entry must be a directory: {path}")
    return True


def _path_dirs(path: Path, root: Path) -> list[str]:
    return _path_entries(path, root)


def _object_key(value: Any) -> str:
    if not isinstance(value, str) or not value:
        raise ArchiveMoverError("object URI must be non-empty")
    if value.startswith("s3://"):
        parts = value.split("/", 3)
        return parts[3].strip("/") if len(parts) == 4 else ""
    return value.strip("/")


def _write_tar_from_tree(
    candidate: Candidate,
    root_fd: int,
    output_fd: int,
    mount_id_provider: MountIdProvider,
) -> None:
    expected = {record.path: record for record in candidate.files}
    with os.fdopen(os.dup(output_fd), "wb") as output:
        with tarfile.open(fileobj=output, mode="w", format=tarfile.PAX_FORMAT) as archive:
            for path in sorted(expected):
                record = expected[path]
                current_fd = os.dup(root_fd)
                file_fd: int | None = None
                try:
                    parts = path.split("/")
                    for part in parts[:-1]:
                        child = _open_child_dir(
                            current_fd,
                            part,
                            path,
                            candidate.source_mount_id,
                            mount_id_provider,
                            candidate.source_root_stat[0],
                        )
                        os.close(current_fd)
                        current_fd = child
                    file_fd, opened = _open_regular(
                        current_fd,
                        parts[-1],
                        path,
                        candidate.source_mount_id,
                        mount_id_provider,
                        candidate.source_root_stat[0],
                    )
                    digest = hashlib.sha256()
                    stream = _HashingReader(file_fd, digest)
                    info = tarfile.TarInfo(path)
                    info.size = record.size_bytes
                    info.mode = stat.S_IMODE(record.mode)
                    info.mtime = 0
                    info.uid = info.gid = 0
                    info.uname = info.gname = ""
                    archive.addfile(info, stream)
                    after = os.fstat(file_fd)
                    os.close(file_fd)
                    file_fd = None
                    if (
                        stream.bytes_read != record.size_bytes
                        or digest.hexdigest() != record.sha256
                        or _stat_signature(opened) != _stat_signature(after)
                    ):
                        raise ArchiveMoverError(f"source changed while feeding tar: {path}")
                finally:
                    if file_fd is not None:
                        os.close(file_fd)
                    os.close(current_fd)
        output.flush()
    os.fsync(output_fd)
    if os.fstat(output_fd).st_size > MAX_TAR_BYTES:
        raise ArchiveMoverError(f"uncompressed tar exceeds {MAX_TAR_BYTES} bytes")


class _HashingReader(io.RawIOBase):
    def __init__(self, fd: int, digest: Any) -> None:
        self.fd = fd
        self.digest = digest
        self.bytes_read = 0

    def readable(self) -> bool:
        return True

    def readinto(self, buffer: Any) -> int:
        chunk = os.read(self.fd, len(buffer))
        if not chunk:
            return 0
        buffer[: len(chunk)] = chunk
        self.digest.update(chunk)
        self.bytes_read += len(chunk)
        return len(chunk)


def _run_tool(
    argv: list[str],
    *,
    input_fd: int,
    stdout_fd: int,
    max_output_bytes: int,
) -> str:
    stderr = bytearray()
    stderr_overflow = False
    output_overflow = False
    output_error: Exception | None = None
    input_position = os.lseek(input_fd, 0, os.SEEK_CUR)
    stdin_fd = os.dup(input_fd)
    try:
        os.lseek(stdin_fd, 0, os.SEEK_SET)
        with os.fdopen(os.dup(stdout_fd), "wb") as output:
            os.fchmod(stdout_fd, 0o600)
            process = subprocess.Popen(
                argv,
                stdin=stdin_fd,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                shell=False,
            )

            def drain_stderr() -> None:
                nonlocal stderr_overflow
                assert process.stderr is not None
                while chunk := process.stderr.read(8192):
                    room = MAX_STDERR_BYTES - len(stderr)
                    if room > 0:
                        stderr.extend(chunk[:room])
                    if len(chunk) > room:
                        stderr_overflow = True

            def drain_stdout() -> None:
                nonlocal output_overflow, output_error
                assert process.stdout is not None
                written = 0
                try:
                    while chunk := process.stdout.read(1024 * 1024):
                        written += len(chunk)
                        if written > max_output_bytes:
                            output_overflow = True
                            process.kill()
                            break
                        output.write(chunk)
                    output.flush()
                    os.fsync(stdout_fd)
                except Exception as error:  # surfaced deterministically in the caller thread
                    output_error = error
                    process.kill()

            stderr_thread = threading.Thread(target=drain_stderr, daemon=True)
            stdout_thread = threading.Thread(target=drain_stdout, daemon=True)
            stderr_thread.start()
            stdout_thread.start()
            try:
                return_code = process.wait(timeout=TOOL_TIMEOUT_SECONDS)
            except subprocess.TimeoutExpired as error:
                process.kill()
                process.wait()
                stderr_thread.join()
                stdout_thread.join()
                raise ArchiveMoverError(f"compression tool timed out after {TOOL_TIMEOUT_SECONDS}s") from error
            stderr_thread.join()
            stdout_thread.join()
    finally:
        os.close(stdin_fd)
        os.lseek(input_fd, input_position, os.SEEK_SET)
    if output_error is not None:
        raise ArchiveMoverError(f"cannot capture compression output safely: {output_error}")
    if output_overflow:
        raise ArchiveMoverError(f"compression output exceeds {max_output_bytes} bytes")
    if stderr_overflow:
        raise ArchiveMoverError(f"compression stderr exceeds {MAX_STDERR_BYTES} bytes")
    message = stderr.decode("utf-8", "replace")
    if return_code:
        raise ArchiveMoverError(f"compression tool failed ({return_code}): {message}")
    return message


def _validate_zstd(path: Path) -> Path:
    if not path.is_absolute():
        raise ArchiveMoverError("zstd path must be absolute")
    try:
        info = path.lstat()
    except OSError as error:
        raise ArchiveMoverError(f"zstd executable is unavailable: {path}: {error}") from error
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode) or not os.access(path, os.X_OK):
        raise ArchiveMoverError(f"zstd must be an executable regular non-symlink file: {path}")
    return path


def verify_archive_pair(
    leaf: Path,
    archive_root: Path,
    *,
    zstd_path: Path,
    require_canonical_location: bool = True,
    mount_id_provider: MountIdProvider = fd_mount_id,
) -> dict[str, Any]:
    """Verify exact leaf shape, schema/binding, compressed checksum and tar members."""
    root_fd = open_directory_no_follow(archive_root)
    root_info = os.fstat(root_fd)
    root_mount = mount_id_provider(root_fd)
    leaf_fd = open_directory_no_follow(leaf, containment_root=archive_root)
    try:
        leaf_info = os.fstat(leaf_fd)
        if leaf_info.st_dev != root_info.st_dev or mount_id_provider(leaf_fd) != root_mount:
            raise ArchiveMoverError(f"archive leaf is on a different device/mount: {leaf}")
        names = _safe_names(leaf_fd, str(leaf), remaining=2)
        if names != ["archive.tar.zst", "manifest.json"]:
            raise ArchiveMoverError(f"archive leaf does not contain exact expected pair: {leaf}")
        manifest, manifest_record = _read_json_relative_fd(
            leaf_fd,
            "manifest.json",
            label=f"{leaf}/manifest.json",
            mount_id=root_mount,
            mount_id_provider=mount_id_provider,
        )
        try:
            jsonschema.Draft7Validator(
                _load_schema(MANIFEST_SCHEMA_PATH),
                format_checker=jsonschema.FormatChecker(),
            ).validate(manifest)
            paths = validate_product_archive_manifest_binding(archive_root, manifest)
        except (jsonschema.ValidationError, ArchiveConfigurationError) as error:
            raise ArchiveMoverError(f"archive manifest contract failed: {error}") from error
        _validate_manifest_resource_bounds(manifest)
        if require_canonical_location and paths.manifest != leaf / "manifest.json":
            raise ArchiveMoverError("archive manifest is installed at the wrong canonical leaf")
        archive_fd, archive_info = _open_regular(
            leaf_fd,
            "archive.tar.zst",
            f"{leaf}/archive.tar.zst",
            root_mount,
            mount_id_provider,
            root_info.st_dev,
            MAX_ARCHIVE_BYTES,
        )
        try:
            size, digest = _size_digest_fd(archive_fd, max_bytes=MAX_ARCHIVE_BYTES)
            archive_after = os.fstat(archive_fd)
            if _stat_signature(archive_info) != _stat_signature(archive_after) or size != archive_info.st_size:
                raise ArchiveMoverError("archive tarball changed while verifying")
            if size != manifest["archive"]["size_bytes"] or digest != manifest["archive"]["sha256"]:
                raise ArchiveMoverError("archive tarball size/sha256 mismatch")
            os.lseek(archive_fd, 0, os.SEEK_SET)
            expected = {entry["path"]: entry for entry in manifest["files"]}
            if len(expected) != len(manifest["files"]):
                raise ArchiveMoverError("archive manifest has duplicate file paths")
            actual: dict[str, tuple[int, str]] = {}
            with _decompressed_tar_stream(archive_fd, zstd_path) as archive:
                for member in archive:
                    if not member.isfile() or not _safe_relative(member.name) or member.name in actual:
                        raise ArchiveMoverError(f"unsafe/duplicate/non-regular tar member: {member.name!r}")
                    source = archive.extractfile(member)
                    if source is None:
                        raise ArchiveMoverError(f"cannot read tar member: {member.name}")
                    digest_obj = hashlib.sha256()
                    count = 0
                    while chunk := source.read(1024 * 1024):
                        count += len(chunk)
                        digest_obj.update(chunk)
                    actual[member.name] = (count, digest_obj.hexdigest())
            if set(actual) != set(expected):
                raise ArchiveMoverError("tar member set differs from manifest")
            for name, (member_size, member_digest) in actual.items():
                if member_size != expected[name]["size_bytes"] or member_digest != expected[name]["sha256"]:
                    raise ArchiveMoverError(f"tar member differs from manifest: {name}")
            archive_final = os.fstat(archive_fd)
            if _stat_signature(archive_info) != _stat_signature(archive_final):
                raise ArchiveMoverError("archive tarball changed during internal verification")
        finally:
            os.close(archive_fd)
        manifest_after, manifest_record_after = _read_json_relative_fd(
            leaf_fd,
            "manifest.json",
            label=f"{leaf}/manifest.json",
            mount_id=root_mount,
            mount_id_provider=mount_id_provider,
        )
        if manifest_after != manifest or manifest_record_after != manifest_record:
            raise ArchiveMoverError("archive manifest changed during verification")
        _verify_open_directory_entry(leaf_fd, leaf, archive_root)
    finally:
        os.close(leaf_fd)
        os.close(root_fd)
    return manifest


def _validate_manifest_resource_bounds(manifest: Mapping[str, Any]) -> None:
    files = manifest["files"]
    if len(files) > MAX_TREE_ENTRIES:
        raise ArchiveMoverError(f"archive manifest exceeds {MAX_TREE_ENTRIES} file entries")
    total = 0
    for entry in files:
        path = entry["path"]
        if len(path.split("/")) > MAX_TREE_DEPTH + 1:
            raise ArchiveMoverError(f"archive manifest member exceeds depth {MAX_TREE_DEPTH}: {path}")
        size = entry["size_bytes"]
        if size > MAX_FILE_BYTES:
            raise ArchiveMoverError(f"archive manifest member exceeds {MAX_FILE_BYTES} bytes: {path}")
        total += size
        if total > MAX_SOURCE_BYTES:
            raise ArchiveMoverError(f"archive manifest exceeds {MAX_SOURCE_BYTES} source bytes")


def _size_digest_fd(source_fd: int, *, max_bytes: int) -> tuple[int, str]:
    digest = hashlib.sha256()
    size = 0
    while chunk := os.read(source_fd, 1024 * 1024):
        size += len(chunk)
        if size > max_bytes:
            raise ArchiveMoverError(f"evidence file exceeds {max_bytes} bytes")
        digest.update(chunk)
    return size, digest.hexdigest()


class _LimitedReader(io.RawIOBase):
    def __init__(self, stream: Any, limit: int, process: subprocess.Popen[bytes]) -> None:
        self.stream = stream
        self.limit = limit
        self.process = process
        self.count = 0

    def readable(self) -> bool:
        return True

    def read(self, size: int = -1) -> bytes:
        chunk = self.stream.read(size)
        self.count += len(chunk)
        if self.count > self.limit:
            self.process.kill()
            raise ArchiveMoverError(f"decompressed tar exceeds {self.limit} bytes")
        return chunk


class _TarStreamContext:
    def __init__(self, archive_fd: int, zstd_path: Path) -> None:
        self.archive_fd = archive_fd
        self.archive_position = os.lseek(archive_fd, 0, os.SEEK_CUR)
        stdin_fd = os.dup(archive_fd)
        try:
            os.lseek(stdin_fd, 0, os.SEEK_SET)
            self.process = subprocess.Popen(
                [str(zstd_path), "-q", "-d", "-c"],
                stdin=stdin_fd,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                shell=False,
            )
        except Exception:
            os.lseek(archive_fd, self.archive_position, os.SEEK_SET)
            raise
        finally:
            os.close(stdin_fd)
        self.stderr = bytearray()
        self.stderr_overflow = False
        self.timed_out = False
        self.thread = threading.Thread(target=self._drain_stderr, daemon=True)
        self.thread.start()
        self.timer = threading.Timer(TOOL_TIMEOUT_SECONDS, self._kill_on_timeout)
        self.timer.daemon = True
        self.timer.start()
        assert self.process.stdout is not None
        self.reader = _LimitedReader(self.process.stdout, MAX_TAR_BYTES, self.process)
        try:
            self.archive = tarfile.open(fileobj=self.reader, mode="r|")
        except Exception:
            self.timer.cancel()
            if self.process.poll() is None:
                self.process.kill()
            self.process.wait()
            self.thread.join()
            self.process.stdout.close()
            assert self.process.stderr is not None
            self.process.stderr.close()
            os.lseek(self.archive_fd, self.archive_position, os.SEEK_SET)
            raise

    def _drain_stderr(self) -> None:
        assert self.process.stderr is not None
        while chunk := self.process.stderr.read(8192):
            room = MAX_STDERR_BYTES - len(self.stderr)
            if room > 0:
                self.stderr.extend(chunk[:room])
            if len(chunk) > room:
                self.stderr_overflow = True

    def _kill_on_timeout(self) -> None:
        if self.process.poll() is None:
            self.timed_out = True
            self.process.kill()

    def __enter__(self) -> tarfile.TarFile:
        return self.archive

    def __exit__(self, exc_type: Any, exc: Any, traceback: Any) -> None:
        try:
            self.archive.close()
            self.timer.cancel()
            if exc_type is not None and self.process.poll() is None:
                self.process.kill()
            try:
                return_code = self.process.wait(timeout=TOOL_TIMEOUT_SECONDS)
            except subprocess.TimeoutExpired as error:
                self.process.kill()
                self.process.wait()
                self.thread.join()
                raise ArchiveMoverError(f"decompressor timed out after {TOOL_TIMEOUT_SECONDS}s") from error
            self.thread.join()
            if self.timed_out:
                raise ArchiveMoverError(f"decompressor timed out after {TOOL_TIMEOUT_SECONDS}s")
            if exc_type is None:
                if self.stderr_overflow:
                    raise ArchiveMoverError(f"compression stderr exceeds {MAX_STDERR_BYTES} bytes")
                if return_code:
                    message = self.stderr.decode("utf-8", "replace")
                    raise ArchiveMoverError(f"decompressor failed ({return_code}): {message}")
        finally:
            if self.process.stdout is not None:
                self.process.stdout.close()
            if self.process.stderr is not None:
                self.process.stderr.close()
            os.lseek(self.archive_fd, self.archive_position, os.SEEK_SET)


def _decompressed_tar_stream(archive_fd: int, zstd_path: Path) -> _TarStreamContext:
    return _TarStreamContext(archive_fd, zstd_path)


def _safe_relative(value: str) -> bool:
    return (
        bool(value)
        and not value.startswith("/")
        and "\\" not in value
        and all(part not in {"", ".", ".."} for part in value.split("/"))
        and not any(ord(c) < 32 or ord(c) == 127 for c in value)
    )


def _safe_segment(value: str) -> bool:
    return (
        bool(value)
        and value == value.strip()
        and value not in {".", ".."}
        and "/" not in value
        and "\\" not in value
        and not any(ord(character) < 32 or ord(character) == 127 for character in value)
    )


def _manifest(
    candidate: Candidate, archive_root: Path, *, archive_size: int, archive_digest: str, now: datetime
) -> dict[str, Any]:
    paths = archive_provenance_paths(archive_root, identity=candidate.identity)
    return {
        "schema_version": SCHEMA_VERSION,
        "provenance": "product-archive",
        "identity": _identity_dict(candidate.identity),
        "archive": {
            "path": paths.archive.relative_to(archive_root).as_posix(),
            "manifest_path": paths.manifest.relative_to(archive_root).as_posix(),
            "sha256": archive_digest,
            "size_bytes": archive_size,
        },
        "files": [record.manifest_entry() for record in candidate.files],
        "created_at": _time(now),
        "tool_version": TOOL_VERSION,
    }


def _same_snapshot(candidate: Candidate, root_fd: int, provider: MountIdProvider) -> bool:
    current_root = os.fstat(root_fd)
    if (current_root.st_dev, current_root.st_ino, current_root.st_mtime_ns) != candidate.source_root_stat:
        return False
    files, directories = scan_tree_snapshot(
        root_fd,
        mount_id=candidate.source_mount_id,
        mount_id_provider=provider,
    )
    return files == candidate.files and directories == candidate.directories


def process_candidate(
    candidate: Candidate,
    config: MoverConfig,
    *,
    now: datetime,
    mount_id_provider: MountIdProvider = fd_mount_id,
    rename_impl: RenameNoReplace = rename_no_replace,
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """Process one independent candidate and return one terminal plus side events."""
    identity = _identity_dict(candidate.identity)
    events: list[dict[str, Any]] = []
    paths = archive_provenance_paths(config.archive_root, identity=candidate.identity)
    final_leaf = paths.archive.parent
    source_fd: int | None = None
    stage_fd: int | None = None
    final_guard_fd: int | None = None
    archive_bytes = 0
    residue: list[str] = []
    try:
        source_fd = open_directory_no_follow(candidate.source_path, containment_root=config.object_store_root)
        if not _same_snapshot(candidate, source_fd, mount_id_provider):
            raise ArchiveMoverError("source differs from discovered preimage")
        existing = _lexists_no_follow(final_leaf, config.archive_root)
        if existing:
            verification_error: ArchiveMoverError | None = None
            try:
                final_guard_fd = open_directory_no_follow(final_leaf, containment_root=config.archive_root)
                existing_manifest = verify_archive_pair(
                    final_leaf,
                    config.archive_root,
                    zstd_path=config.zstd_path,
                    mount_id_provider=mount_id_provider,
                )
                _verify_open_directory_entry(final_guard_fd, final_leaf, config.archive_root)
                existing_files = tuple(
                    FileRecord(item["path"], item["size_bytes"], item["sha256"], 0, 0, 0, 0)
                    for item in existing_manifest["files"]
                )
                if [(x.path, x.size_bytes, x.sha256) for x in existing_files] != [
                    (x.path, x.size_bytes, x.sha256) for x in candidate.files
                ]:
                    raise ArchiveMoverError("verified existing archive conflicts with present source")
                archive_bytes = existing_manifest["archive"]["size_bytes"]
            except ArchiveMoverError as error:
                if "conflicts with present source" in str(error):
                    raise
                verification_error = error
            if verification_error is None:
                if not config.enforce:
                    return _terminal(
                        identity,
                        "planned",
                        "verified existing archive; would retire identical source",
                        candidate.source_bytes,
                        archive_bytes,
                        [],
                    ), events
                _retire_source(
                    candidate,
                    config,
                    source_fd,
                    events,
                    identity,
                    mount_id_provider,
                    rename_impl,
                    archive_guard=(final_guard_fd, final_leaf, config.archive_root),
                )
                return _terminal(
                    identity,
                    "retired-from-existing",
                    "verified existing archive and retired identical source",
                    candidate.source_bytes,
                    archive_bytes,
                    [],
                ), events
            else:
                if final_guard_fd is not None:
                    os.close(final_guard_fd)
                    final_guard_fd = None
                quarantine_relative = f".quarantine/{uuid.uuid4().hex}"
                if not config.enforce:
                    events.append(
                        _event(
                            len(events),
                            identity,
                            "would-quarantine",
                            final_leaf.relative_to(config.archive_root).as_posix(),
                            str(verification_error),
                        )
                    )
                else:
                    quarantine = config.archive_root / quarantine_relative
                    ensure_directory_no_follow(quarantine.parent, containment_root=config.archive_root)
                    _fsync_directory_chain(quarantine.parent, config.archive_root)
                    _rename_leaf(
                        final_leaf,
                        quarantine,
                        config.archive_root,
                        rename_impl,
                        mount_id_provider,
                    )
                    events.append(
                        _event(len(events), identity, "quarantined", quarantine_relative, str(verification_error))
                    )
        if not config.enforce:
            return _terminal(
                identity, "planned", "would create verified archive then retire source", candidate.source_bytes, 0, []
            ), events
        staging_parent = ensure_directory_no_follow(
            config.archive_root / ".staging", containment_root=config.archive_root
        )
        _fsync_directory_chain(staging_parent, config.archive_root)
        staging = staging_parent / uuid.uuid4().hex
        ensure_directory_no_follow(staging, containment_root=config.archive_root)
        _fsync_directory_chain(staging, config.archive_root)
        residue.append(staging.relative_to(config.archive_root).as_posix())
        stage_fd = open_directory_no_follow(staging, containment_root=config.archive_root)
        archive_root_fd = open_directory_no_follow(config.archive_root)
        try:
            archive_root_info = os.fstat(archive_root_fd)
            archive_mount_id = mount_id_provider(archive_root_fd)
            stage_info = os.fstat(stage_fd)
            if stage_info.st_dev != archive_root_info.st_dev or mount_id_provider(stage_fd) != archive_mount_id:
                raise ArchiveMoverError("staging directory is on a different device/mount")
        finally:
            os.close(archive_root_fd)
        uncompressed_fd = os.open(".archive.tar", _CREATE_RDWR_FLAGS, 0o600, dir_fd=stage_fd)
        try:
            _write_tar_from_tree(candidate, source_fd, uncompressed_fd, mount_id_provider)
            if not _same_snapshot(candidate, source_fd, mount_id_provider):
                raise ArchiveMoverError("source changed after tar stream")
            os.lseek(uncompressed_fd, 0, os.SEEK_SET)
            archive_output_fd = os.open("archive.tar.zst", _CREATE_FLAGS, 0o600, dir_fd=stage_fd)
            try:
                _run_tool(
                    [str(config.zstd_path), "-q", "-c"],
                    input_fd=uncompressed_fd,
                    stdout_fd=archive_output_fd,
                    max_output_bytes=MAX_ARCHIVE_BYTES,
                )
            finally:
                os.close(archive_output_fd)
        finally:
            os.close(uncompressed_fd)
        os.unlink(".archive.tar", dir_fd=stage_fd)
        archive_read_fd, archive_opened = _open_regular(
            stage_fd,
            "archive.tar.zst",
            f"{staging}/archive.tar.zst",
            archive_mount_id,
            mount_id_provider,
            archive_root_info.st_dev,
            MAX_ARCHIVE_BYTES,
        )
        try:
            archive_bytes, digest = _size_digest_fd(archive_read_fd, max_bytes=MAX_ARCHIVE_BYTES)
            archive_after = os.fstat(archive_read_fd)
            if _stat_signature(archive_opened) != _stat_signature(archive_after):
                raise ArchiveMoverError("compressed archive changed while hashing")
        finally:
            os.close(archive_read_fd)
        if archive_bytes > MAX_ARCHIVE_BYTES:
            raise ArchiveMoverError(f"compressed archive exceeds {MAX_ARCHIVE_BYTES} bytes")
        manifest = _manifest(candidate, config.archive_root, archive_size=archive_bytes, archive_digest=digest, now=now)
        manifest_raw = (json.dumps(manifest, indent=2, sort_keys=True) + "\n").encode()
        if len(manifest_raw) > MAX_MANIFEST_BYTES:
            raise ArchiveMoverError(f"generated manifest exceeds {MAX_MANIFEST_BYTES} bytes")
        fd = os.open("manifest.json", _CREATE_FLAGS, 0o600, dir_fd=stage_fd)
        try:
            view = memoryview(manifest_raw)
            while view:
                written = os.write(fd, view)
                if written <= 0:
                    raise ArchiveMoverError("short write while staging archive manifest")
                view = view[written:]
            os.fsync(fd)
        finally:
            os.close(fd)
        staged_root = os.fstat(stage_fd)
        os.fsync(stage_fd)
        _verify_open_directory_entry(stage_fd, staging, config.archive_root)
        verify_archive_pair(
            staging,
            config.archive_root,
            zstd_path=config.zstd_path,
            require_canonical_location=False,
            mount_id_provider=mount_id_provider,
        )
        if not _same_snapshot(candidate, source_fd, mount_id_provider):
            raise ArchiveMoverError("source changed before final publication")
        ensure_directory_no_follow(final_leaf.parent, containment_root=config.archive_root)
        _fsync_directory_chain(final_leaf.parent, config.archive_root)
        _rename_leaf(
            staging,
            final_leaf,
            config.archive_root,
            rename_impl,
            mount_id_provider,
            expected_source=(staged_root.st_dev, staged_root.st_ino),
        )
        residue.clear()
        events.append(
            _event(
                len(events),
                identity,
                "published",
                final_leaf.relative_to(config.archive_root).as_posix(),
                "verified staging leaf published without replacement",
            )
        )
        _verify_open_directory_entry(stage_fd, final_leaf, config.archive_root)
        verify_archive_pair(
            final_leaf,
            config.archive_root,
            zstd_path=config.zstd_path,
            mount_id_provider=mount_id_provider,
        )
        _verify_open_directory_entry(stage_fd, final_leaf, config.archive_root)
        if not _same_snapshot(candidate, source_fd, mount_id_provider):
            raise ArchiveMoverError("source changed after final verification")
        _retire_source(
            candidate,
            config,
            source_fd,
            events,
            identity,
            mount_id_provider,
            rename_impl,
            archive_guard=(stage_fd, final_leaf, config.archive_root),
        )
        return _terminal(
            identity,
            "archived",
            "published verified archive and retired source",
            candidate.source_bytes,
            archive_bytes,
            [],
        ), events
    except ArchiveMoverError as error:
        residue.extend(item for item in error.residue if item not in residue)
        status = "indeterminate" if error.indeterminate else "failed"
        return _terminal(identity, status, str(error), candidate.source_bytes, archive_bytes, residue), events
    except Exception as error:
        return _terminal(identity, "failed", str(error), candidate.source_bytes, archive_bytes, residue), events
    finally:
        if final_guard_fd is not None:
            os.close(final_guard_fd)
        if stage_fd is not None:
            os.close(stage_fd)
        if source_fd is not None:
            os.close(source_fd)


def _rename_leaf(
    source: Path,
    destination: Path,
    containment_root: Path,
    rename_impl: RenameNoReplace,
    mount_id_provider: MountIdProvider,
    *,
    expected_source: tuple[int, int] | None = None,
) -> None:
    src_fd = open_directory_no_follow(source.parent, containment_root=containment_root)
    dst_fd = open_directory_no_follow(destination.parent, containment_root=containment_root)
    root_fd = open_directory_no_follow(containment_root)
    source_entry_fd: int | None = None
    try:
        root_info = os.fstat(root_fd)
        root_mount = mount_id_provider(root_fd)
        if os.fstat(src_fd).st_dev != os.fstat(dst_fd).st_dev:
            raise ArchiveMoverError("cross-device rename rejected")
        if mount_id_provider(src_fd) != mount_id_provider(dst_fd):
            raise ArchiveMoverError("cross-mount rename rejected")
        if mount_id_provider(src_fd) != root_mount:
            raise ArchiveMoverError("rename parent is not on the pinned root mount")
        source_entry_fd = _open_child_dir(
            src_fd,
            source.name,
            source.relative_to(containment_root).as_posix(),
            root_mount,
            mount_id_provider,
            root_info.st_dev,
        )
        current = os.fstat(source_entry_fd)
        if expected_source is not None and (current.st_dev, current.st_ino) != expected_source:
            raise ArchiveMoverError("rename source namespace identity changed")
        expected_source = (current.st_dev, current.st_ino)
        try:
            rename_impl(src_fd, source.name, dst_fd, destination.name)
        except OSError as error:
            if error.errno == errno.EXDEV:
                raise ArchiveMoverError("cross-device rename rejected") from error
            raise ArchiveMoverError(f"no-replace rename failed: {error}") from error
        if expected_source is not None:
            installed = os.stat(destination.name, dir_fd=dst_fd, follow_symlinks=False)
            if (installed.st_dev, installed.st_ino) != expected_source:
                raise ArchiveMoverError(
                    "renamed destination identity is indeterminate",
                    indeterminate=True,
                    residue=(destination.relative_to(containment_root).as_posix(),),
                )
        try:
            os.fsync(src_fd)
            if dst_fd != src_fd:
                os.fsync(dst_fd)
        except OSError as error:
            raise ArchiveMoverError(f"rename completed but parent fsync failed: {error}", indeterminate=True) from error
    finally:
        if source_entry_fd is not None:
            os.close(source_entry_fd)
        os.close(src_fd)
        os.close(dst_fd)
        os.close(root_fd)


def _verify_open_directory_entry(fd: int, path: Path, containment_root: Path) -> None:
    """Prove a held directory still names the configured no-follow leaf."""
    parent_fd = open_directory_no_follow(path.parent, containment_root=containment_root)
    try:
        current = os.stat(path.name, dir_fd=parent_fd, follow_symlinks=False)
        opened = os.fstat(fd)
        if not stat.S_ISDIR(current.st_mode) or (current.st_dev, current.st_ino) != (opened.st_dev, opened.st_ino):
            raise ArchiveMoverError(f"directory namespace identity changed: {path}")
    except OSError as error:
        raise ArchiveMoverError(f"cannot verify directory namespace {path}: {error}") from error
    finally:
        os.close(parent_fd)


def _lexists_no_follow(path: Path, containment_root: Path) -> bool:
    try:
        parent_fd = open_directory_no_follow(path.parent, containment_root=containment_root)
    except FileNotFoundError:
        return False
    try:
        try:
            os.stat(path.name, dir_fd=parent_fd, follow_symlinks=False)
            return True
        except FileNotFoundError:
            return False
        except OSError as error:
            raise ArchiveMoverError(f"cannot stat namespace entry {path}: {error}") from error
    finally:
        os.close(parent_fd)


def _retire_source(
    candidate: Candidate,
    config: MoverConfig,
    source_fd: int,
    events: list[dict[str, Any]],
    identity: dict[str, Any],
    provider: MountIdProvider,
    rename_impl: RenameNoReplace,
    archive_guard: tuple[int, Path, Path],
) -> None:
    _verify_open_directory_entry(*archive_guard)
    if not _same_snapshot(candidate, source_fd, provider):
        raise ArchiveMoverError("source preimage changed immediately before retirement")
    tombstone = candidate.source_path.parent / f".archive-delete-{candidate.source_path.name}-{uuid.uuid4().hex}"
    parent_fd = open_directory_no_follow(
        candidate.source_path.parent,
        containment_root=config.object_store_root,
    )
    try:
        expected = os.fstat(source_fd)
        current = os.stat(
            candidate.source_path.name,
            dir_fd=parent_fd,
            follow_symlinks=False,
        )
        if (current.st_dev, current.st_ino) != (expected.st_dev, expected.st_ino):
            raise ArchiveMoverError("source namespace root changed before tombstone rename")
        if provider(parent_fd) != candidate.source_mount_id:
            raise ArchiveMoverError("source parent mount changed before tombstone rename")
        try:
            rename_impl(
                parent_fd,
                candidate.source_path.name,
                parent_fd,
                tombstone.name,
            )
        except OSError as error:
            if error.errno == errno.EXDEV:
                raise ArchiveMoverError("cross-device source tombstone rename rejected") from error
            raise ArchiveMoverError(f"source tombstone no-replace rename failed: {error}") from error
        try:
            os.fsync(parent_fd)
        except OSError as error:
            raise ArchiveMoverError(
                f"source tombstone rename completed but parent fsync failed: {error}",
                indeterminate=True,
                residue=(tombstone.relative_to(config.object_store_root).as_posix(),),
            ) from error
    finally:
        os.close(parent_fd)
    tombstone_relative = tombstone.relative_to(config.object_store_root).as_posix()
    events.append(
        _event(len(events), identity, "tombstoned", tombstone_relative, "source leaf renamed to delete tombstone")
    )
    tomb_fd = open_directory_no_follow(tombstone, containment_root=config.object_store_root)
    removal_started = False
    try:
        renamed = os.fstat(tomb_fd)
        original = os.fstat(source_fd)
        if (renamed.st_dev, renamed.st_ino) != (original.st_dev, original.st_ino):
            raise ArchiveMoverError(
                "tombstone namespace does not bind the verified source inode",
                indeterminate=True,
                residue=(tombstone_relative,),
            )
        if not _same_snapshot(candidate, tomb_fd, provider):
            events.append(
                _event(
                    len(events),
                    identity,
                    "tombstone-preserved",
                    tombstone.relative_to(config.object_store_root).as_posix(),
                    "producer-contract violation: tombstone drift before unlink",
                )
            )
            raise ArchiveMoverError(
                "producer-contract violation: tombstone changed before unlink",
                residue=(tombstone_relative,),
            )
        parent_fd = open_directory_no_follow(tombstone.parent, containment_root=config.object_store_root)
        try:
            removal_started = True
            _remove_tree_contents_fd(
                tomb_fd,
                tombstone_relative,
                device=candidate.source_root_stat[0],
                mount_id=candidate.source_mount_id,
                mount_id_provider=provider,
            )
            current = os.stat(tombstone.name, dir_fd=parent_fd, follow_symlinks=False)
            if (current.st_dev, current.st_ino) != (renamed.st_dev, renamed.st_ino):
                raise ArchiveMoverError("tombstone root changed during removal", indeterminate=True)
            os.rmdir(tombstone.name, dir_fd=parent_fd)
            os.fsync(parent_fd)
        finally:
            os.close(parent_fd)
    except Exception as error:
        if isinstance(error, ArchiveMoverError) and not removal_started:
            raise
        if isinstance(error, ArchiveMoverError) and error.indeterminate:
            message = str(error)
        else:
            message = f"tombstone removal incomplete: {tombstone}: {error}"
        raise ArchiveMoverError(
            message,
            indeterminate=True,
            residue=(tombstone_relative,),
        ) from error
    finally:
        os.close(tomb_fd)
    events.append(
        _event(len(events), identity, "retired", candidate.source_relative, "verified tombstone recursively removed")
    )


def _remove_tree_contents_fd(
    directory_fd: int,
    label: str,
    *,
    device: int,
    mount_id: int,
    mount_id_provider: MountIdProvider,
) -> None:
    """Remove one verified tombstone tree without crossing a device or mount."""
    if os.fstat(directory_fd).st_dev != device or mount_id_provider(directory_fd) != mount_id:
        raise ArchiveMoverError(f"tombstone directory crossed device/mount during removal: {label}")
    for name in _safe_names(directory_fd, label, remaining=MAX_TREE_ENTRIES):
        child_label = f"{label}/{name}"
        entry = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
        if stat.S_ISDIR(entry.st_mode):
            child_fd = _open_child_dir(
                directory_fd,
                name,
                child_label,
                mount_id,
                mount_id_provider,
                device,
            )
            try:
                _remove_tree_contents_fd(
                    child_fd,
                    child_label,
                    device=device,
                    mount_id=mount_id,
                    mount_id_provider=mount_id_provider,
                )
                current = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
                opened = os.fstat(child_fd)
                if (current.st_dev, current.st_ino) != (opened.st_dev, opened.st_ino):
                    raise ArchiveMoverError(f"tombstone child changed during removal: {child_label}")
                os.rmdir(name, dir_fd=directory_fd)
            finally:
                os.close(child_fd)
            continue
        file_fd, opened = _open_regular(
            directory_fd,
            name,
            child_label,
            mount_id,
            mount_id_provider,
            device,
        )
        try:
            current = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
            if (current.st_dev, current.st_ino) != (opened.st_dev, opened.st_ino):
                raise ArchiveMoverError(f"tombstone file changed during removal: {child_label}")
            os.unlink(name, dir_fd=directory_fd)
        finally:
            os.close(file_fd)
    os.fsync(directory_fd)


def run(
    config: MoverConfig,
    *,
    now: datetime | None = None,
    mount_id_provider: MountIdProvider = fd_mount_id,
    rename_impl: RenameNoReplace = rename_no_replace,
) -> tuple[dict[str, Any], int]:
    now = (now or datetime.now(UTC)).astimezone(UTC)
    _validate_config(config)
    candidates, failures = discover_candidates(config, now=now, mount_id_provider=mount_id_provider)
    discovery_incomplete = any(failure.reason.startswith("discovery exceeds") for failure in failures)
    selected = [] if discovery_incomplete else candidates[: config.per_tick_bound]
    deferred = candidates if discovery_incomplete else candidates[config.per_tick_bound :]
    terminals: list[dict[str, Any]] = []
    events: list[dict[str, Any]] = []
    for candidate in selected:
        terminal, side_events = process_candidate(
            candidate, config, now=now, mount_id_provider=mount_id_provider, rename_impl=rename_impl
        )
        for event in side_events:
            event["sequence"] = len(events)
            events.append(event)
        terminals.append(terminal)
    has_indeterminate = any(item["status"] == "indeterminate" for item in terminals)
    has_failure = bool(failures) or any(item["status"] == "failed" for item in terminals)
    receipt = {
        "schema_version": SCHEMA_VERSION,
        "generated_at": _time(now),
        "mode": "enforce" if config.enforce else "dry-run",
        "cutoff": _time(now - timedelta(days=config.minimum_age_days)),
        "minimum_age_days": config.minimum_age_days,
        "per_tick_bound": config.per_tick_bound,
        "outcome": "indeterminate" if has_indeterminate else "failed" if has_failure else "success",
        "candidates": [item.receipt_value(config.archive_root) for item in candidates],
        "selected": [item.receipt_value(config.archive_root) for item in selected],
        "deferred": [item.receipt_value(config.archive_root) for item in deferred],
        "terminals": terminals,
        "events": events,
        "discovery_failures": [failure.value() for failure in failures],
        "bytes": {
            "source": sum(item["source_bytes"] for item in terminals),
            "archived": sum(item["archive_bytes"] for item in terminals),
        },
    }
    validate_receipt_semantics(receipt)
    jsonschema.Draft7Validator(_load_schema(RECEIPT_SCHEMA_PATH), format_checker=jsonschema.FormatChecker()).validate(
        receipt
    )
    _publish_receipt(config.receipt_path, receipt)
    return receipt, 1 if has_failure or has_indeterminate else 0


def validate_receipt_semantics(receipt: Mapping[str, Any]) -> None:
    candidates = receipt["candidates"]
    selected = receipt["selected"]
    deferred = receipt["deferred"]

    def key(item: Mapping[str, Any]) -> str:
        return json.dumps(item["identity"], sort_keys=True)

    candidate_keys = [key(item) for item in candidates]
    selected_keys = [key(item) for item in selected]
    deferred_keys = [key(item) for item in deferred]
    if (
        len(set(candidate_keys)) != len(candidate_keys)
        or set(candidate_keys) != set(selected_keys) | set(deferred_keys)
        or set(selected_keys) & set(deferred_keys)
    ):
        raise ArchiveMoverError("receipt candidate/selected/deferred partition is invalid")
    if candidates != [*selected, *deferred]:
        raise ArchiveMoverError("receipt selection must be the stable candidate prefix")
    if len(selected) > receipt["per_tick_bound"]:
        raise ArchiveMoverError("receipt selected set exceeds per-tick bound")
    sortable: list[tuple[str, str, str]] = []
    for item in candidates:
        try:
            identity = ArchiveIdentity.from_mapping(item["identity"])
        except ArchiveConfigurationError as error:
            raise ArchiveMoverError(f"receipt candidate identity is invalid: {error}") from error
        expected_archive = archive_provenance_paths(Path("/archive"), identity=identity).archive
        if item["archive_path"] != expected_archive.relative_to("/archive").as_posix():
            raise ArchiveMoverError("receipt candidate archive path does not bind its identity")
        if identity.lane == "forcing":
            expected_source = (
                f"forcing/{identity.source.lower()}/{identity.cycle_identity}/"
                f"{identity.basin_version_id}/{identity.model_id}"
            )
        elif identity.lane == "runs":
            expected_source = f"runs/{identity.run_id}"
        elif identity.source == "legacy-unqualified":
            expected_source = f"states/{identity.model_id}/{identity.cycle_identity}"
        else:
            expected_source = f"states/{identity.source}/{identity.model_id}/{identity.cycle_identity}"
        if item["source_path"] != expected_source:
            raise ArchiveMoverError("receipt candidate source path does not bind its identity")
        sortable.append((identity.cycle_time, identity.lane, _identity_key(identity)))
    if sortable != sorted(sortable):
        raise ArchiveMoverError("receipt candidates are not in stable order")
    terminal_keys = [key(item) for item in receipt["terminals"]]
    if terminal_keys != selected_keys or len(set(terminal_keys)) != len(terminal_keys):
        raise ArchiveMoverError("receipt must contain exactly one terminal per selected identity")
    if any(event["sequence"] != index for index, event in enumerate(receipt["events"])):
        raise ArchiveMoverError("receipt event sequence is not contiguous")
    if any(key(event) not in set(selected_keys) for event in receipt["events"]):
        raise ArchiveMoverError("receipt event references an unselected identity")
    if receipt["mode"] == "dry-run" and any(
        terminal["status"] not in {"planned", "failed", "indeterminate"} for terminal in receipt["terminals"]
    ):
        raise ArchiveMoverError("dry-run receipt claims a mutation terminal")
    if receipt["mode"] == "enforce" and any(terminal["status"] == "planned" for terminal in receipt["terminals"]):
        raise ArchiveMoverError("enforce receipt contains a planned terminal")
    failure_keys = [(item["lane_hint"], item["locator"]) for item in receipt["discovery_failures"]]
    if len(failure_keys) != len(set(failure_keys)):
        raise ArchiveMoverError("receipt discovery failures must be unique by lane/locator")
    expected = (
        "indeterminate"
        if any(item["status"] == "indeterminate" for item in receipt["terminals"])
        else "failed"
        if receipt["discovery_failures"] or any(item["status"] == "failed" for item in receipt["terminals"])
        else "success"
    )
    if receipt["outcome"] != expected:
        raise ArchiveMoverError("receipt overall outcome disagrees with terminals/discovery failures")
    if receipt["bytes"] != {
        "source": sum(item["source_bytes"] for item in receipt["terminals"]),
        "archived": sum(item["archive_bytes"] for item in receipt["terminals"]),
    }:
        raise ArchiveMoverError("receipt byte totals disagree with terminals")
    generated = _parse_hour_or_instant(receipt["generated_at"], label="receipt generated_at")
    cutoff = _parse_hour_or_instant(receipt["cutoff"], label="receipt cutoff")
    if cutoff != generated - timedelta(days=receipt["minimum_age_days"]):
        raise ArchiveMoverError("receipt cutoff does not match generated_at/minimum age")


def _parse_hour_or_instant(value: Any, *, label: str) -> datetime:
    if not isinstance(value, str):
        raise ArchiveMoverError(f"{label} must be a timestamp")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as error:
        raise ArchiveMoverError(f"{label} is invalid") from error
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ArchiveMoverError(f"{label} must be timezone-aware")
    return parsed.astimezone(UTC)


def _fsync_directory_chain(path: Path, root: Path) -> None:
    """Durably record every directory component from a trusted root through path."""
    try:
        relative = path.relative_to(root)
    except ValueError as error:
        raise ArchiveMoverError(f"directory fsync path escapes trusted root: {path}") from error
    current = root
    targets = [root]
    for part in relative.parts:
        current /= part
        targets.append(current)
    for target in targets:
        fd = open_directory_no_follow(target, containment_root=root if target != root else None)
        try:
            os.fsync(fd)
        except OSError as error:
            raise ArchiveMoverError(f"directory fsync failed for {target}: {error}") from error
        finally:
            os.close(fd)


def _publish_receipt(path: Path, receipt: Mapping[str, Any]) -> None:
    if not path.is_absolute():
        raise ArchiveMoverError("receipt path must be absolute")
    raw = (json.dumps(receipt, indent=2, sort_keys=True) + "\n").encode()
    try:
        ensure_directory_no_follow(path.parent)
        _fsync_directory_chain(path.parent, Path(path.anchor))
        atomic_write_bytes_no_follow(path, raw, mode=0o600, require_durable_replace=True)
    except SafeFilesystemError as error:
        raise ArchiveMoverError(
            f"receipt publication failed: {error}", indeterminate=error.kind == "indeterminate"
        ) from error


def _validate_config(config: MoverConfig) -> None:
    if not config.object_store_root.is_absolute() or not config.archive_root.is_absolute():
        raise ArchiveMoverError("object-store and archive roots must be absolute")
    if not config.receipt_path.is_absolute() or not config.lock_path.is_absolute():
        raise ArchiveMoverError("receipt and lock paths must be absolute")
    if config.receipt_path == config.lock_path:
        raise ArchiveMoverError("receipt and lock paths must be distinct")
    if config.minimum_age_days < DEFAULT_DB_RETENTION_DAYS:
        raise ArchiveMoverError(f"minimum age must be at least {DEFAULT_DB_RETENTION_DAYS} days")
    if config.per_tick_bound <= 0:
        raise ArchiveMoverError("per-tick bound must be positive")
    if config.per_tick_bound > MAX_DISCOVERY:
        raise ArchiveMoverError(f"per-tick bound must not exceed {MAX_DISCOVERY}")
    validate_archive_configuration(
        archive_root=config.archive_root,
        cleanup_roots={"object_store_root": config.object_store_root},
        archive_min_age_days=config.minimum_age_days,
    )
    verify_directory_no_follow(config.object_store_root)
    verify_directory_no_follow(config.archive_root)
    _validate_zstd(config.zstd_path)
    try:
        ensure_directory_no_follow(config.receipt_path.parent)
        _fsync_directory_chain(config.receipt_path.parent, Path(config.receipt_path.anchor))
        try:
            receipt_info = stat_no_follow(config.receipt_path)
        except FileNotFoundError:
            pass
        else:
            if not stat.S_ISREG(receipt_info.st_mode):
                raise ArchiveMoverError("receipt target must be a regular non-symlink file")
    except SafeFilesystemError as error:
        raise ArchiveMoverError(f"receipt target preflight failed: {error}") from error


def acquire_lock(path: Path) -> int | None:
    """Open/create safe mode-0600 lock metadata and take a nonblocking flock."""
    if not path.is_absolute():
        raise ArchiveMoverError("lock path must be absolute")
    ensure_directory_no_follow(path.parent)
    _fsync_directory_chain(path.parent, Path(path.anchor))
    common_flags = os.O_RDWR | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0)
    created = False
    parent_fd = open_directory_no_follow(path.parent)
    fd: int | None = None
    try:
        try:
            fd = os.open(path.name, common_flags | os.O_CREAT | os.O_EXCL, 0o600, dir_fd=parent_fd)
            created = True
        except FileExistsError:
            fd = os.open(path.name, common_flags, dir_fd=parent_fd)
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or stat.S_IMODE(info.st_mode) != 0o600:
            raise ArchiveMoverError("lock file must be a mode-0600 regular file")
        current = os.stat(path.name, dir_fd=parent_fd, follow_symlinks=False)
        if (current.st_dev, current.st_ino) != (info.st_dev, info.st_ino):
            raise ArchiveMoverError("lock file namespace identity changed while opening")
        _verify_directory_namespace(parent_fd, path.parent)
        if created:
            os.fsync(fd)
            os.fsync(parent_fd)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            os.close(fd)
            fd = None
            return None
        return fd
    except ArchiveMoverError:
        if fd is not None:
            os.close(fd)
        raise
    except OSError as error:
        if fd is not None:
            os.close(fd)
        raise ArchiveMoverError(f"cannot safely open lock file: {error}") from error
    finally:
        os.close(parent_fd)


def _verify_directory_namespace(fd: int, path: Path) -> None:
    try:
        current = path.lstat()
    except OSError as error:
        raise ArchiveMoverError(f"cannot verify directory namespace {path}: {error}") from error
    opened = os.fstat(fd)
    if stat.S_ISLNK(current.st_mode) or not stat.S_ISDIR(current.st_mode):
        raise ArchiveMoverError(f"directory namespace is unsafe: {path}")
    if (current.st_dev, current.st_ino) != (opened.st_dev, opened.st_ino):
        raise ArchiveMoverError(f"directory namespace identity changed: {path}")


def _config_from_args(args: argparse.Namespace) -> MoverConfig:
    env = os.environ
    raw_age = (
        str(args.minimum_age_days) if args.minimum_age_days is not None else env.get("NHMS_ARCHIVE_MIN_AGE_DAYS", "45")
    )
    raw_bound = (
        str(args.per_tick_bound)
        if args.per_tick_bound is not None
        else env.get("NODE27_PRODUCT_ARCHIVE_PER_TICK_BOUND", "10")
    )
    try:
        age = int(raw_age)
        bound = int(raw_bound)
    except ValueError as error:
        raise ArchiveMoverError("minimum age and per-tick bound must be integers") from error
    return MoverConfig(
        object_store_root=Path(args.object_store_root or env.get("NODE27_PRODUCT_ARCHIVE_OBJECT_STORE_ROOT", "")),
        archive_root=Path(
            args.archive_root or env.get("NODE27_PRODUCT_ARCHIVE_ARCHIVE_ROOT") or env.get("NHMS_ARCHIVE_ROOT", "")
        ),
        receipt_path=Path(args.receipt or env.get("NODE27_PRODUCT_ARCHIVE_RECEIPT", "")),
        lock_path=Path(args.lock_file or env.get("NODE27_PRODUCT_ARCHIVE_LOCK_FILE", "")),
        zstd_path=Path(args.zstd or env.get("NODE27_PRODUCT_ARCHIVE_ZSTD", "/usr/bin/zstd")),
        minimum_age_days=age,
        per_tick_bound=bound,
        enforce=args.enforce,
    )


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--object-store-root")
    parser.add_argument("--archive-root")
    parser.add_argument("--receipt")
    parser.add_argument("--lock-file")
    parser.add_argument("--zstd")
    parser.add_argument("--minimum-age-days", type=int)
    parser.add_argument("--per-tick-bound", type=int)
    parser.add_argument("--enforce", action="store_true", help="perform archive and retirement mutations")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    try:
        config = _config_from_args(_parser().parse_args(argv))
        lock_fd = acquire_lock(config.lock_path)
        if lock_fd is None:
            print(json.dumps({"status": "skipped", "reason": "lock-contended"}, sort_keys=True), file=sys.stderr)
            return 0
        try:
            _receipt, code = run(config)
            return code
        finally:
            os.close(lock_fd)
    except Exception as error:
        status = "indeterminate" if isinstance(error, ArchiveMoverError) and error.indeterminate else "failed"
        print(json.dumps({"status": status, "reason": str(error)}, sort_keys=True), file=sys.stderr)
        return 1


def _identity_dict(identity: ArchiveIdentity) -> dict[str, Any]:
    value = {
        "lane": identity.lane,
        "source": identity.source,
        "cycle_identity": identity.cycle_identity,
        "cycle_time": identity.cycle_time,
    }
    if identity.basin_version_id is not None:
        value["basin_version_id"] = identity.basin_version_id
    if identity.model_id is not None:
        value["model_id"] = identity.model_id
    if identity.run_id is not None:
        value["run_id"] = identity.run_id
    return value


def _identity_key(identity: ArchiveIdentity) -> str:
    return json.dumps(_identity_dict(identity), sort_keys=True, separators=(",", ":"))


def _terminal(
    identity: dict[str, Any], status: str, reason: str, source_bytes: int, archive_bytes: int, residue: list[str]
) -> dict[str, Any]:
    return {
        "identity": identity,
        "status": status,
        "reason": reason,
        "source_bytes": source_bytes,
        "archive_bytes": archive_bytes,
        "residue": sorted(residue),
    }


def _event(sequence: int, identity: dict[str, Any], kind: str, path: str, detail: str) -> dict[str, Any]:
    return {"sequence": sequence, "identity": identity, "kind": kind, "path": path, "detail": detail}


def _stat_signature(value: os.stat_result) -> tuple[int, int, int, int, int]:
    return value.st_dev, value.st_ino, value.st_mode, value.st_size, value.st_mtime_ns


def _time(value: datetime) -> str:
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _load_schema(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ArchiveMoverError(f"schema is not an object: {path}")
    return value


if __name__ == "__main__":
    raise SystemExit(main())
