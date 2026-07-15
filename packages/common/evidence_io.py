"""Bounded, descriptor-pinned readers for governance evidence.

Evidence consumers must never hash one pathname read and parse a later one.
These helpers pin a no-follow descriptor, enforce the byte ceiling from
``fstat`` before allocating, read exactly that file, and bound decoded JSON
complexity before a caller performs semantic validation.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import stat
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from packages.common.safe_fs import SafeFilesystemError, open_file_no_follow


class BoundedEvidenceError(RuntimeError):
    """An evidence input is unsafe, oversized, malformed, or too complex."""


@dataclass(frozen=True)
class FileIdentity:
    """One no-follow descriptor observation suitable for later revalidation."""

    path: Path
    normalized_path: Path
    device: int
    inode: int
    size: int
    sha256: str


_SECRET_KEY = re.compile(
    r"(?i)(?:^|[_-])(?:password|passwd|pwd|token|secret|api[_-]?key|private[_-]?key|"
    r"client[_-]?secret|credential|database[_-]?url|dsn)(?:$|[_-])"
)
_SECRET_TEXT = re.compile(
    r"(?i)(?:"
    r"postgres(?:ql)?://[^\s\"']+|"
    r"-----BEGIN [A-Z0-9 ]*PRIVATE KEY-----|"
    r"\bbearer\s+[a-z0-9._~+/=-]+|"
    r"(?:password|passwd|pwd|token|secret|api[_-]?key|client[_-]?secret|"
    r"authorization|credential|database[_-]?url|dsn)\s*[:=]\s*[^\s,;]+"
    r")"
)


def normalized_absolute_path(path: Path) -> Path:
    """Normalize spelling without resolving symlinks or requiring existence."""

    return Path(os.path.normpath(os.path.abspath(os.fspath(path))))


def inspect_bounded_file_no_follow(
    path: Path,
    *,
    max_bytes: int,
    label: str,
    chunk_bytes: int = 1024 * 1024,
) -> FileIdentity:
    """Stream-hash one descriptor without materializing the file in memory."""

    if max_bytes < 1 or chunk_bytes < 1:
        raise ValueError("byte ceilings must be positive")
    try:
        fd = open_file_no_follow(path)
        try:
            before = os.fstat(fd)
            if not stat.S_ISREG(before.st_mode):
                raise BoundedEvidenceError(f"{label} is not a regular file")
            if before.st_size > max_bytes:
                raise BoundedEvidenceError(f"{label} exceeds the byte ceiling")
            digest = hashlib.sha256()
            consumed = 0
            while consumed < before.st_size:
                chunk = os.read(fd, min(chunk_bytes, before.st_size - consumed))
                if not chunk:
                    break
                consumed += len(chunk)
                digest.update(chunk)
            after = os.fstat(fd)
            if (
                consumed != before.st_size
                or os.read(fd, 1)
                or (before.st_dev, before.st_ino, before.st_size)
                != (after.st_dev, after.st_ino, after.st_size)
            ):
                raise BoundedEvidenceError(f"{label} changed while being read")
            return FileIdentity(
                path=path,
                normalized_path=normalized_absolute_path(path),
                device=before.st_dev,
                inode=before.st_ino,
                size=before.st_size,
                sha256=digest.hexdigest(),
            )
        finally:
            os.close(fd)
    except BoundedEvidenceError:
        raise
    except (OSError, SafeFilesystemError) as error:
        raise BoundedEvidenceError(f"{label} is unavailable or unsafe") from error


def assert_paths_disjoint(
    output: Path,
    inputs: list[Path],
    *,
    label: str,
) -> None:
    """Reject lexical, normalized, symlink, and hardlink output/input aliases."""

    output_normalized = normalized_absolute_path(output)
    try:
        output_stat = os.lstat(output)
    except FileNotFoundError:
        output_stat = None
    except OSError as error:
        raise BoundedEvidenceError(f"{label} output is unsafe") from error
    if output_stat is not None and stat.S_ISLNK(output_stat.st_mode):
        raise BoundedEvidenceError(f"{label} output must not be a symlink")
    for input_path in inputs:
        if output_normalized == normalized_absolute_path(input_path):
            raise BoundedEvidenceError(f"{label} output aliases an input")
        if output_stat is None:
            continue
        try:
            input_stat = os.stat(input_path, follow_symlinks=False)
        except FileNotFoundError:
            continue
        except OSError as error:
            raise BoundedEvidenceError(f"{label} input identity is unavailable") from error
        if stat.S_ISLNK(input_stat.st_mode) or (
            output_stat.st_dev,
            output_stat.st_ino,
        ) == (input_stat.st_dev, input_stat.st_ino):
            raise BoundedEvidenceError(f"{label} output aliases an input")


def reject_secret_material(value: Any, *, label: str) -> None:
    """Scan every JSON key/string iteratively and never include values in errors."""

    stack: list[Any] = [value]
    while stack:
        current = stack.pop()
        if isinstance(current, Mapping):
            for key, item in current.items():
                if _SECRET_KEY.search(str(key)):
                    raise BoundedEvidenceError(
                        f"{label} contains forbidden credential field or material"
                    )
                stack.append(item)
        elif isinstance(current, list):
            stack.extend(current)
        elif isinstance(current, str) and _SECRET_TEXT.search(current):
            raise BoundedEvidenceError(
                f"{label} contains forbidden credential field or material"
            )


def read_bounded_bytes_with_identity_no_follow(
    path: Path, *, max_bytes: int, label: str
) -> tuple[bytes, FileIdentity]:
    """Return bytes and identity from the same pinned descriptor read."""

    if max_bytes < 1:
        raise ValueError("max_bytes must be positive")
    try:
        fd = open_file_no_follow(path)
        try:
            before = os.fstat(fd)
            size = before.st_size
            if size > max_bytes:
                raise BoundedEvidenceError(f"{label} exceeds the byte ceiling")
            raw = bytearray()
            while len(raw) < size:
                chunk = os.read(fd, min(1024 * 1024, size - len(raw)))
                if not chunk:
                    break
                raw.extend(chunk)
            # A regular file can be replaced by pathname but not through the
            # pinned descriptor. Detect in-place growth/shrink as well.
            after = os.fstat(fd)
            if (
                len(raw) != size
                or os.read(fd, 1)
                or (before.st_dev, before.st_ino, before.st_size)
                != (after.st_dev, after.st_ino, after.st_size)
            ):
                raise BoundedEvidenceError(f"{label} changed while being read")
            immutable = bytes(raw)
            return immutable, FileIdentity(
                path=path,
                normalized_path=normalized_absolute_path(path),
                device=before.st_dev,
                inode=before.st_ino,
                size=size,
                sha256=hashlib.sha256(immutable).hexdigest(),
            )
        finally:
            os.close(fd)
    except BoundedEvidenceError:
        raise
    except (OSError, SafeFilesystemError) as error:
        raise BoundedEvidenceError(f"{label} is unavailable or unsafe") from error


def read_bounded_bytes_no_follow(path: Path, *, max_bytes: int, label: str) -> bytes:
    """Return one descriptor-bound byte sequence after a pre-read size gate."""

    raw, _ = read_bounded_bytes_with_identity_no_follow(
        path, max_bytes=max_bytes, label=label
    )
    return raw


def validate_json_complexity(
    value: Any,
    *,
    label: str,
    max_depth: int,
    max_nodes: int,
    max_array_items: int,
) -> None:
    """Bound nesting, aggregate nodes, and every array without recursion."""

    stack: list[tuple[Any, int]] = [(value, 1)]
    nodes = 0
    while stack:
        current, depth = stack.pop()
        nodes += 1
        if nodes > max_nodes:
            raise BoundedEvidenceError(f"{label} exceeds the JSON node ceiling")
        if depth > max_depth:
            raise BoundedEvidenceError(f"{label} exceeds the JSON depth ceiling")
        if isinstance(current, Mapping):
            stack.extend((item, depth + 1) for item in current.values())
        elif isinstance(current, list):
            if len(current) > max_array_items:
                raise BoundedEvidenceError(f"{label} exceeds the array item ceiling")
            stack.extend((item, depth + 1) for item in current)


def read_bounded_json_no_follow(
    path: Path,
    *,
    max_bytes: int,
    label: str,
    max_depth: int = 32,
    max_nodes: int = 100_000,
    max_array_items: int = 10_000,
) -> tuple[bytes, Any]:
    """Read/hash/decode/parse one pinned byte sequence and bound complexity."""

    raw, _, value = read_bounded_json_with_identity_no_follow(
        path,
        max_bytes=max_bytes,
        label=label,
        max_depth=max_depth,
        max_nodes=max_nodes,
        max_array_items=max_array_items,
    )
    return raw, value


def read_bounded_json_with_identity_no_follow(
    path: Path,
    *,
    max_bytes: int,
    label: str,
    max_depth: int = 32,
    max_nodes: int = 100_000,
    max_array_items: int = 10_000,
) -> tuple[bytes, FileIdentity, Any]:
    """Read JSON and preserve the exact descriptor identity used to parse it."""

    raw, identity = read_bounded_bytes_with_identity_no_follow(
        path, max_bytes=max_bytes, label=label
    )
    try:
        value = json.loads(raw.decode("utf-8"))
    except (UnicodeError, json.JSONDecodeError, RecursionError) as error:
        raise BoundedEvidenceError(f"{label} is not valid UTF-8 JSON") from error
    validate_json_complexity(
        value,
        label=label,
        max_depth=max_depth,
        max_nodes=max_nodes,
        max_array_items=max_array_items,
    )
    return raw, identity, value
