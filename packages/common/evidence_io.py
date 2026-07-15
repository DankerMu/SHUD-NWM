"""Bounded, descriptor-pinned readers for governance evidence.

Evidence consumers must never hash one pathname read and parse a later one.
These helpers pin a no-follow descriptor, enforce the byte ceiling from
``fstat`` before allocating, read exactly that file, and bound decoded JSON
complexity before a caller performs semantic validation.
"""

from __future__ import annotations

import json
import os
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from packages.common.safe_fs import SafeFilesystemError, open_file_no_follow


class BoundedEvidenceError(RuntimeError):
    """An evidence input is unsafe, oversized, malformed, or too complex."""


def read_bounded_bytes_no_follow(path: Path, *, max_bytes: int, label: str) -> bytes:
    """Return one descriptor-bound byte sequence after a pre-read size gate."""

    if max_bytes < 1:
        raise ValueError("max_bytes must be positive")
    try:
        fd = open_file_no_follow(path)
        try:
            size = os.fstat(fd).st_size
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
            if len(raw) != size or os.read(fd, 1):
                raise BoundedEvidenceError(f"{label} changed while being read")
            return bytes(raw)
        finally:
            os.close(fd)
    except BoundedEvidenceError:
        raise
    except (OSError, SafeFilesystemError) as error:
        raise BoundedEvidenceError(f"{label} is unavailable or unsafe") from error


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

    raw = read_bounded_bytes_no_follow(path, max_bytes=max_bytes, label=label)
    try:
        value = json.loads(raw.decode("utf-8"))
    except (UnicodeError, json.JSONDecodeError) as error:
        raise BoundedEvidenceError(f"{label} is not valid UTF-8 JSON") from error
    validate_json_complexity(
        value,
        label=label,
        max_depth=max_depth,
        max_nodes=max_nodes,
        max_array_items=max_array_items,
    )
    return raw, value
