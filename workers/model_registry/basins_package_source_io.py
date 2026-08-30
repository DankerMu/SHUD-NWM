from __future__ import annotations

import hashlib
import os
import stat
from collections.abc import Callable, Iterator
from errno import ENOENT
from pathlib import Path
from typing import Any, BinaryIO

from .basins_package_contracts import (
    FORCING_SAMPLE_BYTE_LIMIT,
    FORCING_SAMPLE_LINE_LIMIT,
    BasinsPackageError,
    _json_bytes,
    _sha256_handle,
)

_OS_OPEN_SUPPORTS_DIR_FD = os.open in os.supports_dir_fd


def _source_file_evidence(
    path: Path,
    source_root: Path,
    *,
    model_id: str | None = None,
    version: str | None = None,
    manifest_uri: str | None = None,
) -> tuple[int, str]:
    return _verified_source_file_evidence(
        path,
        source_root,
        read_error_code="BASINS_PACKAGE_WRITE_FAILED",
        read_error_message="Failed to read Basins package source file",
        model_id=model_id,
        version=version,
        manifest_uri=manifest_uri,
    )

def _migration_source_file_evidence(path: Path, source_root: Path) -> tuple[int, str]:
    return _verified_source_file_evidence(
        path,
        source_root,
        read_error_code="BASINS_MIGRATION_EVIDENCE_READ_FAILED",
        read_error_message="Failed to read Basins migration evidence source file",
    )

def _source_file_size(
    path: Path,
    source_root: Path,
    *,
    model_id: str | None = None,
    version: str | None = None,
    manifest_uri: str | None = None,
) -> int:
    """Size of a source file under the same symlink/regular-file guards as hashing, without reading it."""

    try:
        with _open_verified_source_file(
            path,
            source_root,
            model_id=model_id,
            version=version,
            manifest_uri=manifest_uri,
        ) as source:
            return os.fstat(source.fileno()).st_size
    except OSError as error:
        raise BasinsPackageError(
            "BASINS_PACKAGE_WRITE_FAILED",
            f"Failed to read Basins package source file: {path}: {error}",
            model_id=model_id,
            version=version,
            path=str(path),
            manifest_uri=manifest_uri,
        ) from error

def _verified_source_file_evidence(
    path: Path,
    source_root: Path,
    *,
    read_error_code: str,
    read_error_message: str,
    model_id: str | None = None,
    version: str | None = None,
    manifest_uri: str | None = None,
) -> tuple[int, str]:
    try:
        with _open_verified_source_file(
            path,
            source_root,
            model_id=model_id,
            version=version,
            manifest_uri=manifest_uri,
        ) as source:
            stat_result = os.fstat(source.fileno())
            size_bytes = stat_result.st_size
            sha256 = _sha256_handle(source)
    except OSError as error:
        raise BasinsPackageError(
            read_error_code,
            f"{read_error_message}: {path}: {error}",
            model_id=model_id,
            version=version,
            path=str(path),
            manifest_uri=manifest_uri,
        ) from error
    return size_bytes, sha256

def _open_verified_source_file(
    path: Path,
    source_root: Path,
    *,
    model_id: str | None = None,
    version: str | None = None,
    manifest_uri: str | None = None,
) -> BinaryIO:
    _reject_source_symlink_path(path, source_root, model_id=model_id, version=version, manifest_uri=manifest_uri)
    resolved = _resolve_package_path(path, model_id=model_id, version=version)
    _ensure_under_source_root(resolved, source_root, model_id=model_id, version=version, manifest_uri=manifest_uri)
    if (
        hasattr(os, "O_NOFOLLOW")
        and hasattr(os, "O_DIRECTORY")
        and _OS_OPEN_SUPPORTS_DIR_FD
    ):
        return _open_verified_source_file_at(
            resolved,
            source_root,
            model_id=model_id,
            version=version,
            manifest_uri=manifest_uri,
        )
    try:
        flags = os.O_RDONLY
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        fd = os.open(path, flags)
        stat_result = os.fstat(fd)
        if not stat.S_ISREG(stat_result.st_mode):
            os.close(fd)
            raise BasinsPackageError(
                "BASINS_PACKAGE_PATH_UNSAFE",
                "Basins package source path is not a regular file.",
                model_id=model_id,
                version=version,
                path=str(path),
                manifest_uri=manifest_uri,
            )
        return os.fdopen(fd, "rb")
    except BasinsPackageError:
        raise
    except OSError as error:
        if path.is_symlink() or resolved.is_symlink():
            raise BasinsPackageError(
                "BASINS_PACKAGE_PATH_UNSAFE",
                "Basins package publication does not follow symlink descendants.",
                model_id=model_id,
                version=version,
                path=str(path),
                manifest_uri=manifest_uri,
            ) from error
        raise

def _open_verified_source_file_at(
    resolved: Path,
    source_root: Path,
    *,
    model_id: str | None = None,
    version: str | None = None,
    manifest_uri: str | None = None,
) -> BinaryIO:
    try:
        relative_parts = resolved.relative_to(source_root).parts
    except ValueError as error:
        raise BasinsPackageError(
            "BASINS_PACKAGE_PATH_UNSAFE",
            "Basins package source path resolves outside the model source directory.",
            model_id=model_id,
            version=version,
            path=str(resolved),
            manifest_uri=manifest_uri,
        ) from error
    if not relative_parts:
        raise BasinsPackageError(
            "BASINS_PACKAGE_PATH_UNSAFE",
            "Basins package source path is not a regular file.",
            model_id=model_id,
            version=version,
            path=str(resolved),
            manifest_uri=manifest_uri,
        )

    directory_flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
    file_flags = os.O_RDONLY | os.O_NOFOLLOW
    if hasattr(os, "O_CLOEXEC"):
        directory_flags |= os.O_CLOEXEC
        file_flags |= os.O_CLOEXEC

    open_dirs: list[int] = []
    file_fd: int | None = None
    try:
        root_fd = os.open(source_root, directory_flags)
        open_dirs.append(root_fd)
        root_stat = os.fstat(root_fd)
        if not stat.S_ISDIR(root_stat.st_mode):
            raise BasinsPackageError(
                "BASINS_PACKAGE_PATH_UNSAFE",
                "Basins package source root is not a directory.",
                model_id=model_id,
                version=version,
                path=str(source_root),
                manifest_uri=manifest_uri,
            )

        current_fd = root_fd
        for component in relative_parts[:-1]:
            next_fd = os.open(component, directory_flags, dir_fd=current_fd)
            open_dirs.append(next_fd)
            next_stat = os.fstat(next_fd)
            if not stat.S_ISDIR(next_stat.st_mode):
                raise BasinsPackageError(
                    "BASINS_PACKAGE_PATH_UNSAFE",
                    "Basins package source ancestor is not a directory.",
                    model_id=model_id,
                    version=version,
                    path=str(resolved),
                    manifest_uri=manifest_uri,
                )
            current_fd = next_fd

        file_fd = os.open(relative_parts[-1], file_flags, dir_fd=current_fd)
        file_stat = os.fstat(file_fd)
        if not stat.S_ISREG(file_stat.st_mode):
            os.close(file_fd)
            file_fd = None
            raise BasinsPackageError(
                "BASINS_PACKAGE_PATH_UNSAFE",
                "Basins package source path is not a regular file.",
                model_id=model_id,
                version=version,
                path=str(resolved),
                manifest_uri=manifest_uri,
            )
        _reject_source_symlink_path(
            resolved,
            source_root,
            model_id=model_id,
            version=version,
            manifest_uri=manifest_uri,
            error_path=resolved,
        )
        fresh_resolved = _resolve_package_path(resolved, model_id=model_id, version=version)
        try:
            fresh_resolved.relative_to(source_root)
        except ValueError as error:
            os.close(file_fd)
            file_fd = None
            raise BasinsPackageError(
                "BASINS_PACKAGE_PATH_UNSAFE",
                "Basins package source path was replaced outside the model source directory.",
                model_id=model_id,
                version=version,
                path=str(resolved),
                manifest_uri=manifest_uri,
            ) from error
        fresh_stat = os.stat(fresh_resolved)
        if (fresh_stat.st_dev, fresh_stat.st_ino) != (file_stat.st_dev, file_stat.st_ino):
            os.close(file_fd)
            file_fd = None
            raise BasinsPackageError(
                "BASINS_PACKAGE_PATH_UNSAFE",
                "Basins package source path was replaced during verified open.",
                model_id=model_id,
                version=version,
                path=str(resolved),
                manifest_uri=manifest_uri,
            )
        handle = os.fdopen(file_fd, "rb")
        file_fd = None
        return handle
    except BasinsPackageError:
        raise
    except FileNotFoundError:
        raise
    except OSError as error:
        raise BasinsPackageError(
            "BASINS_PACKAGE_PATH_UNSAFE",
            "Basins package publication does not follow symlink or replaced source descendants.",
            model_id=model_id,
            version=version,
            path=str(resolved),
            manifest_uri=manifest_uri,
        ) from error
    finally:
        if file_fd is not None:
            try:
                os.close(file_fd)
            except OSError:
                pass
        for directory_fd in reversed(open_dirs):
            try:
                os.close(directory_fd)
            except OSError:
                pass

def _safe_source_dir(
    value: Any,
    inventory_root: Path,
    inventory_relative_root: Path | None,
    source_root: Path,
    field_name: str,
    *,
    expected_path: Path,
    model_id: str | None = None,
    version: str | None = None,
    manifest_uri: str | None = None,
) -> Path:
    if not isinstance(value, str) or not value:
        raise BasinsPackageError(
            "BASINS_INVENTORY_INVALID",
            f"Basins model record is missing {field_name}.",
            model_id=model_id,
            version=version,
            manifest_uri=manifest_uri,
        )
    path = Path(value).expanduser()
    if not path.is_absolute():
        path = _source_dir_from_relative_inventory_value(
            path,
            inventory_root,
            inventory_relative_root,
            source_root,
            expected_path,
            field_name,
            model_id=model_id,
            version=version,
            manifest_uri=manifest_uri,
        )
    _reject_source_symlink_path(path, source_root, model_id=model_id, version=version, manifest_uri=manifest_uri)
    resolved = _resolve_package_path(path)
    _ensure_under_root(
        resolved,
        inventory_root,
        error_code="BASINS_INVENTORY_PATH_MISMATCH",
        message=f"Basins model {field_name} resolves outside the inventory root.",
        model_id=model_id,
        version=version,
        manifest_uri=manifest_uri,
    )
    _ensure_under_source_root(resolved, source_root, model_id=model_id, version=version, manifest_uri=manifest_uri)
    if not resolved.is_dir():
        raise BasinsPackageError(
            "BASINS_SOURCE_NOT_FOUND",
            f"Basins source directory does not exist: {path}",
            model_id=model_id,
            version=version,
            path=str(path),
            manifest_uri=manifest_uri,
        )
    return resolved

def _source_dir_from_relative_inventory_value(
    path: Path,
    inventory_root: Path,
    inventory_relative_root: Path | None,
    source_root: Path,
    expected_path: Path,
    field_name: str,
    *,
    model_id: str | None = None,
    version: str | None = None,
    manifest_uri: str | None = None,
) -> Path:
    normalized = Path(_normalize_relative_path(path.as_posix()))
    if _relative_inventory_path_matches_expected(
        normalized,
        inventory_root,
        inventory_relative_root,
        source_root,
        expected_path,
    ):
        return expected_path
    candidate = inventory_root / normalized
    raise BasinsPackageError(
        "BASINS_INVENTORY_PATH_MISMATCH",
        f"Basins inventory {field_name} does not match the selected model's canonical source path.",
        model_id=model_id,
        version=version,
        path=str(candidate),
        manifest_uri=manifest_uri,
    )

def _relative_inventory_path_matches_expected(
    relative_path: Path,
    inventory_root: Path,
    inventory_relative_root: Path | None,
    source_root: Path,
    expected_path: Path,
) -> bool:
    expected_relative_paths: set[Path] = set()
    for base in (source_root, inventory_root):
        try:
            expected_relative_paths.add(expected_path.relative_to(base))
        except ValueError:
            continue
    if inventory_relative_root is not None:
        try:
            expected_relative_paths.add(inventory_relative_root / expected_path.relative_to(inventory_root))
        except ValueError:
            pass
    return relative_path in expected_relative_paths

def _safe_source_file(
    path: Path,
    source_root: Path,
    *,
    model_id: str | None = None,
    version: str | None = None,
    manifest_uri: str | None = None,
) -> Path:
    _reject_source_symlink_path(path, source_root, model_id=model_id, version=version, manifest_uri=manifest_uri)
    resolved = _resolve_package_path(path)
    _ensure_under_source_root(resolved, source_root, model_id=model_id, version=version, manifest_uri=manifest_uri)
    if not resolved.is_file():
        raise BasinsPackageError(
            "BASINS_SOURCE_NOT_FOUND",
            f"Basins source file does not exist: {path}",
            model_id=model_id,
            version=version,
            path=str(path),
            manifest_uri=manifest_uri,
        )
    return resolved

def _reject_source_symlink_path(
    path: Path,
    source_root: Path,
    *,
    model_id: str | None = None,
    version: str | None = None,
    manifest_uri: str | None = None,
    error_path: Path | None = None,
) -> None:
    current = path if path.is_absolute() else Path.cwd() / path
    parts: list[Path] = []
    while True:
        parts.append(current)
        if current == current.parent:
            break
        current = current.parent

    for candidate in reversed(parts):
        try:
            resolved_parent = _resolve_package_path(candidate.parent)
        except BasinsPackageError:
            continue
        try:
            resolved_parent.relative_to(source_root)
        except ValueError:
            continue
        if candidate.is_symlink():
            raise BasinsPackageError(
                "BASINS_PACKAGE_PATH_UNSAFE",
                "Basins package publication does not follow symlink descendants.",
                model_id=model_id,
                version=version,
                path=str(error_path or candidate),
                manifest_uri=manifest_uri,
            )

def _ensure_under_source_root(
    path: Path,
    source_root: Path,
    *,
    model_id: str | None = None,
    version: str | None = None,
    manifest_uri: str | None = None,
) -> None:
    _ensure_under_root(
        path,
        source_root,
        error_code="BASINS_PACKAGE_PATH_UNSAFE",
        message="Basins package source path resolves outside the model source directory.",
        model_id=model_id,
        version=version,
        manifest_uri=manifest_uri,
    )

def _ensure_under_root(
    path: Path,
    root: Path,
    *,
    error_code: str,
    message: str,
    model_id: str | None = None,
    version: str | None = None,
    manifest_uri: str | None = None,
) -> None:
    try:
        path.relative_to(root)
    except ValueError as error:
        raise BasinsPackageError(
            error_code,
            message,
            model_id=model_id,
            version=version,
            path=str(path),
            manifest_uri=manifest_uri,
        ) from error

def _resolve_package_path(path: Path, *, model_id: str | None = None, version: str | None = None) -> Path:
    # Strict resolution + errno split: non-strict resolution stopped raising
    # on symlink loops in CPython 3.13+, so the loop verdict must come from
    # the kernel errno. ENOENT keeps pre-change parity (missing paths resolve
    # non-strictly and are classified by the callers downstream).
    try:
        return Path(os.path.realpath(path, strict=True))
    except OSError as error:
        if getattr(error, "errno", None) == ENOENT:
            # Non-strict os.path.realpath() never raises on 3.11-3.14;
            # Path.resolve() would raise an errno-less RuntimeError on <=3.12
            # when the `..`-collapsed tail meets a symlink loop behind the
            # missing component (e.g. `gone/../loopdir`).
            return Path(os.path.realpath(path))
        raise BasinsPackageError(
            "BASINS_PACKAGE_PATH_UNRESOLVABLE",
            "Basins package source path cannot be resolved.",
            model_id=model_id,
            version=version,
            path=str(path),
        ) from error

def _normalize_relative_path(value: str) -> str:
    path = Path(value)
    if path.is_absolute() or ".." in path.parts:
        raise BasinsPackageError("BASINS_PACKAGE_PATH_UNSAFE", f"Unsafe package relative path: {value}", path=value)
    normalized = path.as_posix().strip("/")
    if not normalized:
        raise BasinsPackageError("BASINS_PACKAGE_PATH_UNSAFE", "Package relative path is empty.")
    return normalized

def _walk_source_files(root: Path, source_root: Path) -> Iterator[Path]:
    resolved_root = _resolve_package_path(root)
    _ensure_under_source_root(resolved_root, source_root)
    stack = [resolved_root]
    visited_dirs = {resolved_root}
    while stack:
        directory = stack.pop()
        try:
            children = sorted(directory.iterdir(), key=lambda path: path.name.lower())
        except OSError as error:
            raise BasinsPackageError(
                "BASINS_PACKAGE_PATH_UNRESOLVABLE",
                "Basins package source directory cannot be traversed.",
                path=str(directory),
            ) from error
        for child in children:
            if _is_ignored_source_path(child):
                continue
            if child.is_symlink():
                raise BasinsPackageError(
                    "BASINS_PACKAGE_PATH_UNSAFE",
                    "Basins package publication does not follow symlink descendants.",
                    path=str(child),
                )
            resolved = _resolve_package_path(child)
            _ensure_under_source_root(resolved, source_root)
            if child.is_dir():
                if resolved in visited_dirs:
                    continue
                visited_dirs.add(resolved)
                stack.append(resolved)
            elif child.is_file():
                yield resolved

def _directory_evidence(
    root: Path,
    *,
    walk_source_files: Callable[[Path, Path], Iterator[Path]],
    migration_source_file_evidence: Callable[[Path, Path], tuple[int, str]],
) -> tuple[int, int, str]:
    resolved_root = _resolve_package_path(root)
    digest = hashlib.sha256()
    file_count = 0
    byte_count = 0
    for path in walk_source_files(resolved_root, resolved_root):
        relative_path = path.relative_to(resolved_root).as_posix()
        size_bytes, sha256 = migration_source_file_evidence(path, resolved_root)
        digest.update(relative_path.encode("utf-8"))
        digest.update(b"\0")
        digest.update(str(size_bytes).encode("ascii"))
        digest.update(b"\0")
        digest.update(sha256.encode("ascii"))
        digest.update(b"\0")
        file_count += 1
        byte_count += size_bytes
    return file_count, byte_count, digest.hexdigest()


def _csv_time_evidence(
    path: Path,
    source_root: Path,
    *,
    model_id: str | None = None,
    version: str | None = None,
    manifest_uri: str | None = None,
) -> tuple[str | None, str | None, str | None, int]:
    try:
        with (
            _open_verified_source_file(
                path,
                source_root,
                model_id=model_id,
                version=version,
                manifest_uri=manifest_uri,
            ) as source,
            open(source.fileno(), "r", encoding="utf-8", errors="replace", newline="", closefd=False) as handle,
        ):
            header = handle.readline(FORCING_SAMPLE_BYTE_LIMIT).strip()
            first_time: str | None = None
            last_time: str | None = None
            row_count = 0
            consumed_bytes = len(header.encode("utf-8", errors="replace"))
            for line in handle:
                consumed_bytes += len(line.encode("utf-8", errors="replace"))
                if consumed_bytes > FORCING_SAMPLE_BYTE_LIMIT or row_count >= FORCING_SAMPLE_LINE_LIMIT:
                    break
                stripped = line.strip()
                if not stripped:
                    continue
                value = stripped.split(",", 1)[0].strip()
                if not value:
                    continue
                first_time = value if first_time is None else first_time
                last_time = value
                row_count += 1
            return header or None, first_time, last_time, row_count
    except OSError as error:
        raise BasinsPackageError(
            "BASINS_PACKAGE_WRITE_FAILED",
            f"Failed to read Basins forcing sample file: {path}: {error}",
            model_id=model_id,
            version=version,
            path=str(path),
            manifest_uri=manifest_uri,
        ) from error

def _is_ignored_source_path(path: Path) -> bool:
    return any(part == ".DS_Store" or part == "@eaDir" or part.endswith("@SynoEAStream") for part in path.parts)

def _write_json_file(
    path: str | Path,
    payload: dict[str, Any],
    *,
    error_code: str,
    model_id: str | None = None,
    version: str | None = None,
    manifest_uri: str | None = None,
    before_write: Callable[[Path, int], None] | None = None,
) -> None:
    output = Path(path).expanduser()
    try:
        content = _json_bytes(payload)
        if before_write is not None:
            before_write(output, len(content))
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_bytes(content)
    except OSError as error:
        raise BasinsPackageError(
            error_code,
            f"Failed to write Basins output JSON: {output}: {error}",
            model_id=model_id,
            version=version,
            path=str(output),
            manifest_uri=manifest_uri,
        ) from error

def _preflight_json_output_path(
    path: str | Path,
    *,
    error_code: str,
    model_id: str | None = None,
    version: str | None = None,
    manifest_uri: str | None = None,
) -> None:
    output = Path(path).expanduser()
    try:
        output.parent.mkdir(parents=True, exist_ok=True)
    except OSError as error:
        raise BasinsPackageError(
            error_code,
            f"Failed to prepare Basins output JSON path: {output}: {error}",
            model_id=model_id,
            version=version,
            path=str(output),
            manifest_uri=manifest_uri,
        ) from error
