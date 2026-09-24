"""Basins discovery filesystem primitives: verdict types, errno-aware metadata
classifiers and bounded directory scanners (#2460 split of ``basins_discovery``).

``workers.model_registry.basins_discovery`` stays the stable import path and
re-exports every name defined here.
"""

from __future__ import annotations

import os
import stat
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Any

IGNORED_SIDE_NAMES = {".DS_Store", "@eaDir"}
IGNORED_SIDE_SUFFIXES = ("@SynoEAStream",)


class BasinsDiscoveryError(RuntimeError):
    """Raised when Basins discovery cannot produce an importable inventory."""

    def __init__(self, error_code: str, message: str, *, path: str | None = None) -> None:
        super().__init__(message)
        self.error_code = error_code
        self.path = path

    def to_payload(self) -> dict[str, Any]:
        payload: dict[str, Any] = {"error_code": self.error_code, "message": str(self)}
        if self.path is not None:
            payload["path"] = self.path
        return payload


@dataclass(frozen=True)
class DiscoveryWarning:
    code: str
    message: str
    path: str | None = None

    def as_dict(self) -> dict[str, str]:
        payload = {"code": self.code, "message": self.message}
        if self.path is not None:
            payload["path"] = self.path
        return payload


class _ResolveState(Enum):
    """First-class per-call verdict for a path under the Basins root.

    Owners map the state -- never a shared-warning scan -- to their lane
    semantics: hard directory refusal, matched-file unreadable third state,
    optional skip, or blocking outside-root/unresolvable refusal.
    """

    RESOLVED = "resolved"
    MISSING = "missing"
    UNREADABLE = "unreadable"
    OUTSIDE = "outside"
    UNRESOLVABLE = "unresolvable"


@dataclass(frozen=True)
class _ResolvedPath:
    state: _ResolveState
    path: Path | None = None


class _FileKind(Enum):
    """Errno-aware final-file metadata verdict after containment passes."""

    REGULAR = "regular"
    MISSING = "missing"
    UNREADABLE = "unreadable"
    OTHER = "other"


@dataclass
class DiscoveryBudget:
    max_depth: int
    max_entries: int
    error_code_prefix: str = "BASINS"
    root: Path | None = None
    entries_seen: int = 0

    def enter(self, path: Path, *, depth: int | None = None) -> None:
        if depth is not None and depth > self.max_depth:
            raise BasinsDiscoveryError(
                f"{self.error_code_prefix}_DISCOVERY_DEPTH_EXCEEDED",
                "Basins discovery exceeded the allowed directory depth.",
                path=str(path),
            )
        self.entries_seen += 1
        if self.entries_seen > self.max_entries:
            raise BasinsDiscoveryError(
                f"{self.error_code_prefix}_DISCOVERY_ENTRY_LIMIT_EXCEEDED",
                "Basins discovery exceeded the allowed entry count.",
                path=str(self.root or path),
            )


def _count_sidecars(root: Path, *, budget: DiscoveryBudget | None = None, depth: int = 0) -> int:
    count = 0
    stack: list[tuple[Path, int]] = [(root, depth)]
    while stack:
        directory, directory_depth = stack.pop()
        if budget is not None:
            budget.enter(directory, depth=directory_depth)
        try:
            with os.scandir(directory) as entries:
                for entry in entries:
                    entry_path = Path(entry.path)
                    if budget is not None:
                        budget.enter(entry_path, depth=directory_depth + 1)
                    if _is_sidecar_name(entry.name):
                        count += 1
                        continue
                    if entry.is_dir(follow_symlinks=False):
                        stack.append((entry_path, directory_depth + 1))
        except OSError:
            continue
    return count


def _iter_child_dirs(root: Path, *, budget: DiscoveryBudget | None = None, depth: int = 0) -> list[Path]:
    try:
        with os.scandir(root) as entries:
            paths: list[Path] = []
            for entry in entries:
                path = Path(entry.path)
                if budget is not None:
                    budget.enter(path, depth=depth)
                if entry.is_dir(follow_symlinks=False) or entry.is_symlink():
                    paths.append(path)
            return sorted(paths, key=lambda path: path.name.lower())
    except PermissionError as error:
        raise BasinsDiscoveryError(
            "BASINS_DIRECTORY_UNREADABLE",
            f"Basins directory is not readable: {root}",
            path=str(root),
        ) from error


def _append_warning_once(warnings: list[DiscoveryWarning], warning: DiscoveryWarning) -> None:
    if any(existing.code == warning.code and existing.path == warning.path for existing in warnings):
        return
    warnings.append(warning)


def _ensure_readable_directory(path: Path, error_code: str) -> None:
    try:
        mode = path.stat().st_mode
    except FileNotFoundError:
        raise BasinsDiscoveryError(error_code, f"Basins directory does not exist: {path}", path=str(path))
    except NotADirectoryError:
        raise BasinsDiscoveryError(error_code, f"Basins directory does not exist: {path}", path=str(path))
    except OSError as error:
        # One structured boundary for the kind/stat probes (#1554): the old
        # ``is_dir()`` pre-check swallowed a different OSError set per CPython
        # (EACCES on 3.11 raised, on 3.12+ returned False and was then
        # misreported as missing).  Permission denial stays on the caller's
        # unreadable code, never leaks as a bare PermissionError and never
        # becomes a NOT_FOUND mislabel.
        raise BasinsDiscoveryError(error_code, f"Basins directory cannot be stat'ed: {path}", path=str(path)) from error
    if not stat.S_ISDIR(mode):
        raise BasinsDiscoveryError(error_code, f"Basins directory does not exist: {path}", path=str(path))
    if not mode & (stat.S_IRUSR | stat.S_IRGRP | stat.S_IROTH):
        raise BasinsDiscoveryError(error_code, f"Basins directory is not readable: {path}", path=str(path))
    if not mode & (stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH):
        raise BasinsDiscoveryError(error_code, f"Basins directory is not searchable: {path}", path=str(path))


def _classify_basins_root_metadata(root: Path, *, error_prefix: str = "BASINS_ROOT") -> bool:
    """Classify the explicit root's metadata once, by errno; return source-symlink identity.

    ``Path.exists()`` / ``is_dir()`` swallow different ``OSError`` sets on
    different CPython versions (EACCES on a denied ancestor raises on 3.11 but
    returns False from 3.12 on), so they cannot decide root classification.
    One no-follow ``lstat`` probe identifies a source symlink and one
    follow-target ``stat`` probe confirms the directory this classifier
    admits.  Missing (``ENOENT``/``ENOTDIR``) is the ``*_NOT_FOUND`` verdict,
    permission denial (``EACCES``/``EPERM``) and any other unreadable metadata
    failure is the ``*_UNREADABLE`` verdict, and no raw ``PermissionError``
    escapes.  Callers consume the returned symlink identity instead of
    re-probing the root with a bare ``Path.is_symlink()`` after this
    classifier has run (#1554).
    """

    try:
        lstat_mode = root.lstat().st_mode
    except FileNotFoundError:
        raise BasinsDiscoveryError(
            f"{error_prefix}_NOT_FOUND",
            f"Basins root does not exist: {root}",
            path=str(root),
        )
    except NotADirectoryError:
        raise BasinsDiscoveryError(
            f"{error_prefix}_NOT_FOUND",
            f"Basins root does not exist: {root}",
            path=str(root),
        )
    except PermissionError:
        raise BasinsDiscoveryError(
            f"{error_prefix}_UNREADABLE",
            f"Basins root is not readable: {root}",
            path=str(root),
        )
    except OSError:
        raise BasinsDiscoveryError(
            f"{error_prefix}_UNREADABLE",
            f"Basins root cannot be inspected: {root}",
            path=str(root),
        )
    is_symlink = stat.S_ISLNK(lstat_mode)
    try:
        mode = root.stat().st_mode
    except FileNotFoundError:
        raise BasinsDiscoveryError(
            f"{error_prefix}_NOT_FOUND",
            f"Basins root does not exist: {root}",
            path=str(root),
        )
    except NotADirectoryError:
        raise BasinsDiscoveryError(
            f"{error_prefix}_NOT_FOUND",
            f"Basins root does not exist: {root}",
            path=str(root),
        )
    except PermissionError:
        raise BasinsDiscoveryError(
            f"{error_prefix}_UNREADABLE",
            f"Basins root is not readable: {root}",
            path=str(root),
        )
    except OSError:
        raise BasinsDiscoveryError(
            f"{error_prefix}_UNREADABLE",
            f"Basins root cannot be inspected: {root}",
            path=str(root),
        )
    if not stat.S_ISDIR(mode):
        raise BasinsDiscoveryError(
            f"{error_prefix}_NOT_FOUND",
            f"Basins root is not a directory: {root}",
            path=str(root),
        )
    return is_symlink


def _classify_entry_kind(path: Path) -> tuple[_FileKind, int | None]:
    """Errno-aware single follow-stat verdict shared by file and directory owners.

    ``Path.is_file()`` / ``is_dir()`` each swallow a different OSError set per
    CPython (EACCES on the final follow-stat raises on 3.11-3.13 but returns
    False on 3.14), so neither predicate can decide final metadata.  One
    errno-aware stat yields a verdict every interpreter agrees on and returns
    the raw mode for the caller's type-bit check: missing / not-a-directory ->
    MISSING, permission denial -> UNREADABLE, any other metadata failure ->
    OTHER, otherwise REGULAR with the mode.
    """

    try:
        mode = path.stat().st_mode
    except FileNotFoundError:
        return _FileKind.MISSING, None
    except NotADirectoryError:
        return _FileKind.MISSING, None
    except PermissionError:
        return _FileKind.UNREADABLE, None
    except OSError:
        return _FileKind.OTHER, None
    return _FileKind.REGULAR, mode


def _classify_regular_file(path: Path) -> _FileKind:
    """Errno-aware final-file metadata verdict.

    ``Path.is_file()`` swallows a different OSError set per CPython: EACCES on
    the final follow-stat raises on 3.11-3.13 but returns False on 3.14
    (cand-r1-06).  This classifier yields one verdict on every interpreter:
    missing/not-a-dir -> MISSING, permission denial -> UNREADABLE, other
    metadata failure -> OTHER, regular file -> REGULAR.
    """

    kind, mode = _classify_entry_kind(path)
    if kind is _FileKind.REGULAR:
        return _FileKind.REGULAR if mode is not None and stat.S_ISREG(mode) else _FileKind.OTHER
    return kind


def _classify_directory_kind(path: Path) -> _FileKind:
    """Errno-aware final-directory metadata verdict for required-input ownership.

    Mirrors ``_classify_regular_file`` for directories: the final follow-stat
    EACCES/EPERM is UNREADABLE on every interpreter, missing/not-a-dir is
    MISSING, a regular file at the leaf is OTHER, and an actual directory is
    REGULAR.
    """

    kind, mode = _classify_entry_kind(path)
    if kind is _FileKind.REGULAR:
        return _FileKind.REGULAR if mode is not None and stat.S_ISDIR(mode) else _FileKind.OTHER
    return kind


def _is_sidecar_name(name: str) -> bool:
    return name in IGNORED_SIDE_NAMES or any(name.endswith(suffix) for suffix in IGNORED_SIDE_SUFFIXES)


def _is_ignored_path(path: Path) -> bool:
    return any(_is_sidecar_name(part) for part in path.parts)
