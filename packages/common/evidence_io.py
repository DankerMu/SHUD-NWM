"""Bounded, descriptor-pinned readers for governance evidence.

Evidence consumers must never hash one pathname read and parse a later one.
These helpers pin a no-follow descriptor, enforce the byte ceiling from
``fstat`` before allocating, read exactly that file, and bound decoded JSON
complexity before a caller performs semantic validation.
"""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import re
import stat
import time
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from packages.common.safe_fs import SafeFilesystemError, open_file_no_follow


class BoundedEvidenceError(RuntimeError):
    """An evidence input is unsafe, oversized, malformed, or too complex."""


def acquire_exclusive_flock_until(
    fd: int,
    *,
    deadline_monotonic: float,
    label: str,
    poll_seconds: float = 0.01,
) -> None:
    """Acquire ``LOCK_EX`` without ever waiting beyond a monotonic deadline."""

    if poll_seconds <= 0:
        raise ValueError("flock poll interval must be positive")
    while True:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return
        except BlockingIOError as error:
            remaining = deadline_monotonic - time.monotonic()
            if remaining <= 0:
                raise BoundedEvidenceError(f"{label} lock acquisition exceeded its deadline") from error
            time.sleep(min(poll_seconds, remaining))


@dataclass(frozen=True)
class FileIdentity:
    """One no-follow descriptor observation suitable for later revalidation."""

    path: Path
    normalized_path: Path
    device: int
    inode: int
    size: int
    sha256: str


@dataclass(frozen=True)
class ArtifactClosure:
    """A descriptor-pinned, transitively resolved evidence graph."""

    identities: tuple[FileIdentity, ...]
    manifest: tuple[dict[str, Any], ...]
    total_bytes: int


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
                or (before.st_dev, before.st_ino, before.st_size) != (after.st_dev, after.st_ino, after.st_size)
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


def artifact_references(value: Any) -> list[dict[str, Any]]:
    """Return syntactic artifact references from an arbitrary JSON value.

    Only objects whose key set is exactly ``path/sha256/bytes`` are refs.  The
    traversal is iterative so hostile nesting is bounded by the caller's prior
    JSON-complexity gate rather than the Python recursion limit.
    """

    found: list[dict[str, Any]] = []
    stack = [value]
    while stack:
        current = stack.pop()
        if isinstance(current, Mapping):
            if set(current) == {"path", "sha256", "bytes"}:
                found.append(dict(current))
            else:
                stack.extend(current.values())
        elif isinstance(current, list):
            stack.extend(current)
    return found


def resolve_artifact_closure(
    root: Any,
    *,
    max_depth: int = 16,
    max_nodes: int = 512,
    max_total_bytes: int = 1024**3,
    max_artifact_bytes: int = 512 * 1024**2,
    max_json_depth: int = 48,
    max_json_nodes: int = 250_000,
    max_json_array_items: int = 25_000,
) -> ArtifactClosure:
    """Freeze the complete artifact graph before an evidence output is written.

    JSON artifacts are followed recursively.  Every pathname and inode must be
    unique, declared hashes/sizes must match the pinned descriptor, and cycle,
    depth, node and aggregate-byte ceilings fail closed.  Non-JSON leaves are
    retained as opaque bytes.
    """

    if min(max_depth, max_nodes, max_total_bytes, max_artifact_bytes) < 1:
        raise ValueError("artifact closure ceilings must be positive")
    pending = [(ref, 1) for ref in reversed(artifact_references(root))]
    identities: list[FileIdentity] = []
    manifest: list[dict[str, Any]] = []
    paths: dict[Path, FileIdentity] = {}
    inodes: dict[tuple[int, int], Path] = {}
    total = 0
    while pending:
        ref, depth = pending.pop()
        if depth > max_depth:
            raise BoundedEvidenceError("artifact closure exceeds the depth ceiling")
        if len(identities) >= max_nodes:
            raise BoundedEvidenceError("artifact closure exceeds the node ceiling")
        if set(ref) != {"path", "sha256", "bytes"}:
            raise BoundedEvidenceError("artifact closure contains a malformed reference")
        path = Path(str(ref["path"]))
        digest = ref["sha256"]
        size = ref["bytes"]
        if (
            not path.is_absolute()
            or not isinstance(digest, str)
            or re.fullmatch(r"[0-9a-f]{64}", digest) is None
            or not isinstance(size, int)
            or isinstance(size, bool)
            or size < 0
            or size > max_artifact_bytes
        ):
            raise BoundedEvidenceError("artifact closure reference metadata is invalid")
        raw, identity = read_bounded_bytes_with_identity_no_follow(
            path, max_bytes=max_artifact_bytes, label="artifact closure node"
        )
        if identity.sha256 != digest or identity.size != size:
            raise BoundedEvidenceError("artifact closure reference identity differs")
        inode = (identity.device, identity.inode)
        previous = paths.get(identity.normalized_path)
        if previous is not None:
            if previous == identity:
                continue
            raise BoundedEvidenceError("artifact closure path identity changed")
        if inode in inodes:
            raise BoundedEvidenceError("artifact closure contains a path/inode alias or cycle")
        total += identity.size
        if total > max_total_bytes:
            raise BoundedEvidenceError("artifact closure exceeds the aggregate byte ceiling")
        paths[identity.normalized_path] = identity
        inodes[inode] = identity.normalized_path
        identities.append(identity)
        manifest.append({"path": str(path), "sha256": identity.sha256, "bytes": identity.size})
        try:
            nested = json.loads(raw)
        except (UnicodeDecodeError, json.JSONDecodeError):
            continue
        validate_json_complexity(
            nested,
            label="artifact closure JSON node",
            max_depth=max_json_depth,
            max_nodes=max_json_nodes,
            max_array_items=max_json_array_items,
        )
        pending.extend((nested_ref, depth + 1) for nested_ref in reversed(artifact_references(nested)))
    return ArtifactClosure(tuple(identities), tuple(manifest), total)


def assert_output_disjoint_from_closure(output: Path, closure: ArtifactClosure, *, label: str) -> None:
    """Reject normalized-path and inode aliases against a frozen closure."""

    assert_paths_disjoint(output, [item.path for item in closure.identities], label=label)


def reverify_artifact_closure(closure: ArtifactClosure) -> None:
    """Prove every retained graph node still has its frozen identity."""

    for identity in closure.identities:
        current = inspect_bounded_file_no_follow(
            identity.path,
            max_bytes=max(identity.size, 1),
            label="retained artifact closure node",
        )
        if current != identity:
            raise BoundedEvidenceError("retained artifact closure changed after publication")


def reject_secret_material(value: Any, *, label: str) -> None:
    """Scan every JSON key/string iteratively and never include values in errors."""

    stack: list[Any] = [value]
    while stack:
        current = stack.pop()
        if isinstance(current, Mapping):
            for key, item in current.items():
                if _SECRET_KEY.search(str(key)):
                    raise BoundedEvidenceError(f"{label} contains forbidden credential field or material")
                stack.append(item)
        elif isinstance(current, list):
            stack.extend(current)
        elif isinstance(current, str) and _SECRET_TEXT.search(current):
            raise BoundedEvidenceError(f"{label} contains forbidden credential field or material")


def read_bounded_bytes_with_identity_no_follow(path: Path, *, max_bytes: int, label: str) -> tuple[bytes, FileIdentity]:
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
                or (before.st_dev, before.st_ino, before.st_size) != (after.st_dev, after.st_ino, after.st_size)
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

    raw, _ = read_bounded_bytes_with_identity_no_follow(path, max_bytes=max_bytes, label=label)
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

    raw, identity = read_bounded_bytes_with_identity_no_follow(path, max_bytes=max_bytes, label=label)
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
