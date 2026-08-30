from __future__ import annotations

import hashlib
import os
import stat
import uuid
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, BinaryIO

from packages.common.object_store import LocalObjectStore, ObjectStoreError
from packages.common.storage import validate_object_path

from .basins_package_contracts import BasinsPackageError, ObjectStoreParent, SourceFile, _json_bytes
from .basins_package_source_io import _open_verified_source_file

_OS_OPEN_SUPPORTS_DIR_FD = os.open in os.supports_dir_fd
_OS_MKDIR_SUPPORTS_DIR_FD = os.mkdir in os.supports_dir_fd
_OS_RENAME_SUPPORTS_DIR_FD = os.rename in os.supports_dir_fd
_OS_UNLINK_SUPPORTS_DIR_FD = os.unlink in os.supports_dir_fd
_OS_STAT_SUPPORTS_DIR_FD = os.stat in os.supports_dir_fd
_OS_STAT_SUPPORTS_FOLLOW_SYMLINKS = os.stat in os.supports_follow_symlinks
_OS_OPENAT_OBJECT_STORE_AVAILABLE = (
    hasattr(os, "O_NOFOLLOW")
    and hasattr(os, "O_DIRECTORY")
    and _OS_OPEN_SUPPORTS_DIR_FD
    and _OS_MKDIR_SUPPORTS_DIR_FD
    and _OS_RENAME_SUPPORTS_DIR_FD
    and _OS_UNLINK_SUPPORTS_DIR_FD
    and _OS_STAT_SUPPORTS_DIR_FD
)


def _manifest_file_entry_for_source_file(source_file: SourceFile, *, size_bytes: int, sha256: str) -> dict[str, Any]:
    return {
        "relative_path": source_file.relative_path,
        "object_uri": source_file.object_uri,
        "size_bytes": size_bytes,
        "sha256": sha256,
        "role": source_file.role,
    }


def _object_store_from_env(*, model_id: str, version: str) -> LocalObjectStore:
    root = os.getenv("OBJECT_STORE_ROOT", "").strip()
    if not root:
        raise BasinsPackageError(
            "OBJECT_STORE_ROOT_MISSING",
            "OBJECT_STORE_ROOT is required for Basins package publication.",
            model_id=model_id,
            version=version,
        )
    return LocalObjectStore(root, os.getenv("OBJECT_STORE_PREFIX", ""))

def _directory_uri(object_store: LocalObjectStore, key: str) -> str:
    return object_store.uri_for_key(key).rstrip("/") + "/"

def _validate_object_key_segment(
    value: str,
    field_name: str,
    *,
    model_id: str,
    version: str,
) -> None:
    if value != value.strip():
        raise BasinsPackageError(
            "BASINS_PACKAGE_IDENTIFIER_INVALID",
            f"Basins package {field_name} must not contain leading or trailing whitespace.",
            model_id=model_id,
            version=version,
        )
    if value in {"", ".", ".."}:
        raise BasinsPackageError(
            "BASINS_PACKAGE_IDENTIFIER_INVALID",
            f"Basins package {field_name} must be a non-empty safe object-key segment.",
            model_id=model_id,
            version=version,
        )
    if not all(character.isascii() and (character.isalnum() or character in {"_", "-", "."}) for character in value):
        raise BasinsPackageError(
            "BASINS_PACKAGE_IDENTIFIER_INVALID",
            f"Basins package {field_name} must be a single safe object-key segment.",
            model_id=model_id,
            version=version,
        )

def _acquire_publish_lock(
    store: LocalObjectStore,
    lock_key: str,
    model_id: str,
    version: str,
    manifest_uri: str,
) -> None:
    lock_path = _object_path_for_key(store, lock_key)
    try:
        with _object_parent_for_write(
            store,
            lock_key,
            model_id=model_id,
            version=version,
            manifest_uri=manifest_uri,
        ) as target:
            flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
            if hasattr(os, "O_NOFOLLOW"):
                flags |= os.O_NOFOLLOW
            if hasattr(os, "O_CLOEXEC"):
                flags |= os.O_CLOEXEC
            fd = _object_os_open(target.name, flags, 0o666, target)
    except FileExistsError as error:
        raise BasinsPackageError(
            "BASINS_PACKAGE_PUBLISH_IN_PROGRESS",
            "Basins package publication is already in progress for this model/version.",
            model_id=model_id,
            version=version,
            manifest_uri=manifest_uri,
            path=str(lock_path),
        ) from error
    except OSError as error:
        raise BasinsPackageError(
            "BASINS_PACKAGE_WRITE_FAILED",
            f"Failed to acquire Basins package publish lock: {error}",
            model_id=model_id,
            version=version,
            manifest_uri=manifest_uri,
            path=str(lock_path),
        ) from error
    with os.fdopen(fd, "wb") as handle:
        handle.write(
            _json_bytes(
                {
                    "model_id": model_id,
                    "version": version,
                    "manifest_uri": manifest_uri,
                    "created_at": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
                }
            )
        )

def _release_publish_lock(store: LocalObjectStore, lock_key: str) -> None:
    try:
        with _object_parent_for_write(store, lock_key) as target:
            try:
                _object_os_unlink(target.name, target)
            except FileNotFoundError:
                pass
    except (BasinsPackageError, OSError, ValueError):
        pass

def _write_bytes_to_store_atomic(
    store: LocalObjectStore,
    key: str,
    content: bytes,
    *,
    model_id: str | None = None,
    version: str | None = None,
    manifest_uri: str | None = None,
) -> str:
    target_path = _object_path_for_key(store, key)
    temp_path = target_path.with_name(f".{target_path.name}.{uuid.uuid4().hex}.part")
    try:
        with _object_parent_for_write(
            store,
            key,
            model_id=model_id,
            version=version,
            manifest_uri=manifest_uri,
        ) as target:
            temp_fd = _object_os_open(
                temp_path.name,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL | _object_no_follow_flag() | _object_cloexec_flag(),
                0o666,
                target,
            )
        with os.fdopen(temp_fd, "wb") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        with _object_parent_for_existing_write(
            store,
            key,
            model_id=model_id,
            version=version,
            manifest_uri=manifest_uri,
        ) as target:
            _object_os_replace(temp_path.name, target.name, target)
    except OSError as error:
        try:
            _remove_object_temp_path(store, key, temp_path.name)
        except OSError as cleanup_error:
            raise ObjectStoreError(
                f"Failed to write object {key}: {error}; cleanup also failed: {cleanup_error}"
            ) from cleanup_error
        raise ObjectStoreError(f"Failed to write object {key}: {error}") from error
    return store.uri_for_key(store.normalize_key(key))

def _preflight_object_store_keys(
    store: LocalObjectStore,
    keys: list[str],
    *,
    model_id: str,
    version: str,
    manifest_uri: str,
) -> None:
    for key in keys:
        _object_path_rejecting_symlinks(
            store,
            key,
            model_id=model_id,
            version=version,
            manifest_uri=manifest_uri,
        )

def _object_exists_no_symlinks(
    store: LocalObjectStore,
    key: str,
    *,
    model_id: str,
    version: str,
    manifest_uri: str,
) -> bool:
    try:
        with _object_parent_for_existing_read(
            store,
            key,
            model_id=model_id,
            version=version,
            manifest_uri=manifest_uri,
        ) as target:
            stat_result = _object_os_stat(target.name, target)
            if stat.S_ISLNK(stat_result.st_mode):
                raise _object_path_unsafe_error(
                    target.path,
                    model_id=model_id,
                    version=version,
                    manifest_uri=manifest_uri,
                )
    except FileNotFoundError:
        return False
    except BasinsPackageError:
        raise
    except OSError as error:
        raise ObjectStoreError(f"Failed to check object existence for {key}: {error}") from error
    return True

def _read_object_bytes_no_symlinks(
    store: LocalObjectStore,
    key: str,
    *,
    model_id: str,
    version: str,
    manifest_uri: str,
    max_bytes: int | None = None,
) -> bytes:
    try:
        with _open_object_file_no_symlinks(
            store,
            key,
            model_id=model_id,
            version=version,
            manifest_uri=manifest_uri,
        ) as handle:
            if max_bytes is None:
                return handle.read()
            if max_bytes < 0:
                raise ValueError("max_bytes must be non-negative.")
            chunks = []
            remaining = max_bytes + 1
            while remaining > 0:
                chunk = handle.read(min(1024 * 1024, remaining))
                if not chunk:
                    break
                chunks.append(chunk)
                remaining -= len(chunk)
            content = b"".join(chunks)
            if len(content) > max_bytes:
                raise ObjectStoreError(
                    f"Object {key} exceeds read limit: {len(content)} bytes > {max_bytes} bytes"
                )
            return content
    except OSError as error:
        raise ObjectStoreError(f"Failed to read object {key}: {error}") from error

def _object_path_rejecting_symlinks(
    store: LocalObjectStore,
    key_or_uri: str,
    *,
    model_id: str | None = None,
    version: str | None = None,
    manifest_uri: str | None = None,
) -> Path:
    key, parts = _object_key_parts(store, key_or_uri)
    root = Path(store.root)
    candidate = root
    for part in parts:
        candidate = candidate / part
        try:
            mode = candidate.lstat().st_mode
        except FileNotFoundError:
            continue
        except OSError as error:
            raise ObjectStoreError(f"Failed to inspect object path {key}: {error}") from error
        if stat.S_ISLNK(mode):
            raise BasinsPackageError(
                "BASINS_PACKAGE_OBJECT_PATH_UNSAFE",
                "Basins package publication does not follow object-store symlink components.",
                model_id=model_id,
                version=version,
                path=str(candidate),
                manifest_uri=manifest_uri,
            )
    return root.joinpath(*parts)

def _object_key_parts(store: LocalObjectStore, key_or_uri: str) -> tuple[str, tuple[str, ...]]:
    key = store.normalize_key(key_or_uri)
    validation = validate_object_path(key)
    if not validation.valid:
        raise ValueError(validation.error)
    parts = Path(key).parts
    if not parts or ".." in parts:
        raise ValueError(f"Object key must not contain '..': {key_or_uri}")
    return key, parts

def _object_path_for_key(store: LocalObjectStore, key_or_uri: str) -> Path:
    _, parts = _object_key_parts(store, key_or_uri)
    return Path(store.root).joinpath(*parts)

@contextmanager
def _object_parent_for_write(
    store: LocalObjectStore,
    key_or_uri: str,
    *,
    model_id: str | None = None,
    version: str | None = None,
    manifest_uri: str | None = None,
) -> Iterator[ObjectStoreParent]:
    if _OS_OPENAT_OBJECT_STORE_AVAILABLE:
        target = _open_object_parent_at(
            store,
            key_or_uri,
            create=True,
            model_id=model_id,
            version=version,
            manifest_uri=manifest_uri,
        )
        try:
            yield target
        finally:
            if target.parent_fd is not None:
                os.close(target.parent_fd)
        return

    path = _object_path_rejecting_symlinks(
        store,
        key_or_uri,
        model_id=model_id,
        version=version,
        manifest_uri=manifest_uri,
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    path = _object_path_rejecting_symlinks(
        store,
        key_or_uri,
        model_id=model_id,
        version=version,
        manifest_uri=manifest_uri,
    )
    yield ObjectStoreParent(path=path, name=path.name)

@contextmanager
def _object_parent_for_existing_write(
    store: LocalObjectStore,
    key_or_uri: str,
    *,
    model_id: str | None = None,
    version: str | None = None,
    manifest_uri: str | None = None,
) -> Iterator[ObjectStoreParent]:
    if _OS_OPENAT_OBJECT_STORE_AVAILABLE:
        target = _open_object_parent_at(
            store,
            key_or_uri,
            create=False,
            model_id=model_id,
            version=version,
            manifest_uri=manifest_uri,
        )
        try:
            yield target
        finally:
            if target.parent_fd is not None:
                os.close(target.parent_fd)
        return

    path = _object_path_rejecting_symlinks(
        store,
        key_or_uri,
        model_id=model_id,
        version=version,
        manifest_uri=manifest_uri,
    )
    yield ObjectStoreParent(path=path, name=path.name)

@contextmanager
def _object_parent_for_existing_read(
    store: LocalObjectStore,
    key_or_uri: str,
    *,
    model_id: str | None = None,
    version: str | None = None,
    manifest_uri: str | None = None,
) -> Iterator[ObjectStoreParent]:
    with _object_parent_for_existing_write(
        store,
        key_or_uri,
        model_id=model_id,
        version=version,
        manifest_uri=manifest_uri,
    ) as target:
        yield target

def _open_object_parent_at(
    store: LocalObjectStore,
    key_or_uri: str,
    *,
    create: bool,
    model_id: str | None = None,
    version: str | None = None,
    manifest_uri: str | None = None,
) -> ObjectStoreParent:
    _, parts = _object_key_parts(store, key_or_uri)
    root = Path(store.root)
    directory_flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | _object_cloexec_flag()
    current_fd: int | None = None
    try:
        if create:
            root.mkdir(parents=True, exist_ok=True)
        current_fd = os.open(root, directory_flags)
        root_stat = os.fstat(current_fd)
        if not stat.S_ISDIR(root_stat.st_mode):
            raise BasinsPackageError(
                "BASINS_PACKAGE_OBJECT_PATH_UNSAFE",
                "Basins package object-store root is not a directory.",
                model_id=model_id,
                version=version,
                path=str(root),
                manifest_uri=manifest_uri,
            )

        parent_path = root
        for component in parts[:-1]:
            try:
                next_fd = os.open(component, directory_flags, dir_fd=current_fd)
            except FileNotFoundError:
                if not create:
                    raise
                os.mkdir(component, 0o777, dir_fd=current_fd)
                next_fd = os.open(component, directory_flags, dir_fd=current_fd)
            except OSError as error:
                if _object_path_component_is_symlink(parent_path / component):
                    raise _object_path_unsafe_error(
                        parent_path / component,
                        model_id=model_id,
                        version=version,
                        manifest_uri=manifest_uri,
                    ) from error
                raise

            try:
                next_stat = os.fstat(next_fd)
                if not stat.S_ISDIR(next_stat.st_mode):
                    raise _object_path_unsafe_error(
                        parent_path / component,
                        model_id=model_id,
                        version=version,
                        manifest_uri=manifest_uri,
                    )
            except Exception:
                os.close(next_fd)
                raise
            os.close(current_fd)
            current_fd = next_fd
            parent_path = parent_path / component

        target = ObjectStoreParent(path=root.joinpath(*parts), name=parts[-1], parent_fd=current_fd)
        current_fd = None
        return target
    except (BasinsPackageError, FileNotFoundError):
        raise
    except OSError as error:
        raise ObjectStoreError(f"Failed to inspect object-store path {key_or_uri}: {error}") from error
    finally:
        if current_fd is not None:
            try:
                os.close(current_fd)
            except OSError:
                pass

def _object_os_open(name: str, flags: int, mode: int, target: ObjectStoreParent) -> int:
    if target.parent_fd is not None:
        return os.open(name, flags, mode, dir_fd=target.parent_fd)
    return os.open(target.path.with_name(name), flags, mode)

def _object_os_replace(source_name: str, target_name: str, target: ObjectStoreParent) -> None:
    if target.parent_fd is not None:
        os.rename(source_name, target_name, src_dir_fd=target.parent_fd, dst_dir_fd=target.parent_fd)
        return
    os.replace(target.path.with_name(source_name), target.path.with_name(target_name))

def _object_os_unlink(name: str, target: ObjectStoreParent) -> None:
    if target.parent_fd is not None:
        os.unlink(name, dir_fd=target.parent_fd)
        return
    target.path.with_name(name).unlink()

def _object_os_stat(name: str, target: ObjectStoreParent) -> os.stat_result:
    if target.parent_fd is not None and _OS_STAT_SUPPORTS_FOLLOW_SYMLINKS:
        return os.stat(name, dir_fd=target.parent_fd, follow_symlinks=False)
    return target.path.with_name(name).lstat()

def _remove_object_temp_path(store: LocalObjectStore, key_or_uri: str, temp_name: str) -> None:
    with _object_parent_for_existing_write(store, key_or_uri) as target:
        try:
            _object_os_unlink(temp_name, target)
        except FileNotFoundError:
            pass

@contextmanager
def _open_object_file_no_symlinks(
    store: LocalObjectStore,
    key_or_uri: str,
    *,
    model_id: str | None = None,
    version: str | None = None,
    manifest_uri: str | None = None,
) -> Iterator[BinaryIO]:
    with _object_parent_for_existing_read(
        store,
        key_or_uri,
        model_id=model_id,
        version=version,
        manifest_uri=manifest_uri,
    ) as target:
        flags = os.O_RDONLY | _object_no_follow_flag() | _object_cloexec_flag()
        try:
            fd = _object_os_open(target.name, flags, 0o666, target)
        except OSError as error:
            if _object_path_component_is_symlink(target.path):
                raise _object_path_unsafe_error(
                    target.path,
                    model_id=model_id,
                    version=version,
                    manifest_uri=manifest_uri,
                ) from error
            raise
        try:
            stat_result = os.fstat(fd)
            if not stat.S_ISREG(stat_result.st_mode):
                raise _object_path_unsafe_error(
                    target.path,
                    model_id=model_id,
                    version=version,
                    manifest_uri=manifest_uri,
                )
            with os.fdopen(fd, "rb") as handle:
                fd = -1
                yield handle
        finally:
            if fd >= 0:
                os.close(fd)

def _object_path_component_is_symlink(path: Path) -> bool:
    try:
        return stat.S_ISLNK(path.lstat().st_mode)
    except OSError:
        return False

def _object_path_unsafe_error(
    path: Path,
    *,
    model_id: str | None = None,
    version: str | None = None,
    manifest_uri: str | None = None,
) -> BasinsPackageError:
    return BasinsPackageError(
        "BASINS_PACKAGE_OBJECT_PATH_UNSAFE",
        "Basins package publication does not follow object-store symlink components.",
        model_id=model_id,
        version=version,
        path=str(path),
        manifest_uri=manifest_uri,
    )

def _object_no_follow_flag() -> int:
    return os.O_NOFOLLOW if hasattr(os, "O_NOFOLLOW") else 0

def _object_cloexec_flag() -> int:
    return os.O_CLOEXEC if hasattr(os, "O_CLOEXEC") else 0

def _write_file_to_store_streaming(
    store: LocalObjectStore,
    key: str,
    source_path: Path,
    source_root: Path,
    *,
    model_id: str | None = None,
    version: str | None = None,
    manifest_uri: str | None = None,
) -> tuple[int, str]:
    target_path = _object_path_for_key(store, key)
    temp_path = target_path.with_name(f".{target_path.name}.{uuid.uuid4().hex}.part")
    digest = hashlib.sha256()
    size_bytes = 0
    try:
        with _object_parent_for_write(
            store,
            key,
            model_id=model_id,
            version=version,
            manifest_uri=manifest_uri,
        ) as target:
            temp_name = temp_path.name
            temp_fd = _object_os_open(
                temp_name,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL | _object_no_follow_flag() | _object_cloexec_flag(),
                0o666,
                target,
            )
        with (
            _open_verified_source_file(
                source_path,
                source_root,
                model_id=model_id,
                version=version,
                manifest_uri=manifest_uri,
            ) as source,
            os.fdopen(temp_fd, "wb") as target_handle,
        ):
            for chunk in iter(lambda: source.read(1024 * 1024), b""):
                target_handle.write(chunk)
                digest.update(chunk)
                size_bytes += len(chunk)
            target_handle.flush()
            os.fsync(target_handle.fileno())
        with _object_parent_for_existing_write(
            store,
            key,
            model_id=model_id,
            version=version,
            manifest_uri=manifest_uri,
        ) as target:
            _object_os_replace(temp_path.name, target.name, target)
    except OSError as error:
        try:
            _remove_object_temp_path(store, key, temp_path.name)
        except OSError as cleanup_error:
            raise ObjectStoreError(
                f"Failed to write object {key}: {error}; cleanup also failed: {cleanup_error}"
            ) from cleanup_error
        raise ObjectStoreError(f"Failed to write object {key}: {error}") from error
    return size_bytes, digest.hexdigest()

def _verify_object_bytes(
    store: LocalObjectStore,
    key: str,
    *,
    expected_size: int,
    expected_sha256: str,
    model_id: str | None = None,
    version: str | None = None,
    manifest_uri: str | None = None,
    object_size_and_checksum_streaming: Callable[..., tuple[int, str]]
) -> None:
    actual_size, actual_sha256 = object_size_and_checksum_streaming(
        store,
        key,
        model_id=model_id,
        version=version,
        manifest_uri=manifest_uri,
    )
    if actual_size != expected_size or actual_sha256 != expected_sha256:
        raise ObjectStoreError(
            f"Object verification failed for {key}: expected {expected_size}/{expected_sha256}, "
            f"got {actual_size}/{actual_sha256}"
        )

def _object_size_and_checksum_streaming(
    store: LocalObjectStore,
    key: str,
    *,
    model_id: str | None = None,
    version: str | None = None,
    manifest_uri: str | None = None,
) -> tuple[int, str]:
    digest = hashlib.sha256()
    size_bytes = 0
    try:
        with _open_object_file_no_symlinks(
            store,
            key,
            model_id=model_id,
            version=version,
            manifest_uri=manifest_uri,
        ) as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
                size_bytes += len(chunk)
    except BasinsPackageError:
        raise
    except OSError as error:
        raise ObjectStoreError(f"Failed to verify object {key}: {error}") from error
    return size_bytes, digest.hexdigest()

def _write_source_file_to_store(
    source_file: SourceFile,
    store: LocalObjectStore,
    *,
    model_id: str,
    version: str,
    manifest_uri: str,
    write_file_to_store_streaming: Callable[..., tuple[int, str]],
    verify_object_bytes: Callable[..., None]
) -> dict[str, Any]:
    size_bytes, sha256 = write_file_to_store_streaming(
        store,
        source_file.object_key,
        source_file.source_path,
        source_file.source_root,
        model_id=model_id,
        version=version,
        manifest_uri=manifest_uri,
    )
    verify_object_bytes(
        store,
        source_file.object_key,
        expected_size=size_bytes,
        expected_sha256=sha256,
        model_id=model_id,
        version=version,
        manifest_uri=manifest_uri,
    )
    return _manifest_file_entry_for_source_file(source_file, size_bytes=size_bytes, sha256=sha256)
