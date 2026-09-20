"""Repository file access: scan walk, bounded text reads, naming, hashes, git.

Split out of ``scripts/governance/audit_repo_entropy.py`` by #1842. Every check
family reaches the working tree through this module: ``_iter_text_files`` and
``_read_repo_text`` enforce the skip-dir/extension/size bounds, ``_rel`` and
``_module_for_relative`` derive the heatmap module, and the ``_git_*`` helpers
are the only ``subprocess`` call sites in the package."""

from __future__ import annotations

import hashlib
import os
import stat
import subprocess
from pathlib import Path
from typing import Iterable

from scripts.governance.entropy_audit.constants import (
    HASH_CHUNK_BYTES,
    MAX_ARTIFACT_FINGERPRINT_BYTES,
    MAX_SCANNED_TEXT_FILE_BYTES,
    SCAN_SKIP_DIRS,
    SCAN_SKIP_PREFIXES,
    SCAN_SKIP_ROOT_DIRS,
    TEXT_EXTENSIONS,
)


def _existing_files(paths: Iterable[Path]) -> list[Path]:
    return sorted({path for path in paths if path.is_file()})


def _iter_text_files(root: Path, roots: Iterable[Path]) -> Iterable[Path]:
    root = root.resolve(strict=False)
    for scan_root in roots:
        if scan_root.is_file():
            if _repo_text_rejection_reason(root, scan_root) is None:
                yield scan_root
            continue
        if not scan_root.exists():
            continue
        if not _is_scannable_dir(root, scan_root):
            continue
        for current, dirnames, filenames in os.walk(scan_root):
            current_path = Path(current)
            dirnames[:] = [
                dirname
                for dirname in dirnames
                if _is_scannable_dir(root, current_path / dirname)
            ]
            for filename in filenames:
                path = current_path / filename
                if _repo_text_rejection_reason(root, path) is None:
                    yield path


def _iter_python_files(root: Path, roots: Iterable[Path]) -> Iterable[Path]:
    for path in _iter_text_files(root, roots):
        if path.suffix == ".py":
            yield path


def _is_scannable_dir(root: Path, path: Path) -> bool:
    try:
        file_stat = path.lstat()
        if stat.S_ISLNK(file_stat.st_mode) or not stat.S_ISDIR(file_stat.st_mode):
            return False
        relative = path.resolve(strict=False).relative_to(root.resolve(strict=False))
    except ValueError:
        return False
    except OSError:
        return False
    if any(part in SCAN_SKIP_DIRS for part in relative.parts):
        return False
    if any(part.startswith(SCAN_SKIP_PREFIXES) for part in relative.parts):
        return False
    return not (relative.parts and relative.parts[0] in SCAN_SKIP_ROOT_DIRS)


def _read_repo_text(root: Path, path: Path) -> str:
    if _repo_text_rejection_reason(root, path) is not None:
        return ""
    try:
        with path.open("r", encoding="utf-8", errors="replace") as handle:
            return handle.read(MAX_SCANNED_TEXT_FILE_BYTES)
    except OSError:
        return ""


def _repo_text_rejection_reason(root: Path, path: Path) -> str | None:
    root_resolved = root.resolve(strict=False)
    try:
        file_stat = path.lstat()
    except OSError:
        return "stat-error"
    if stat.S_ISLNK(file_stat.st_mode):
        return "symlink"
    if not stat.S_ISREG(file_stat.st_mode):
        return "not-regular-file"
    try:
        relative = path.resolve(strict=False).relative_to(root_resolved)
    except (OSError, ValueError):
        return "outside-repo"
    if _repo_relative_path_is_skipped(relative):
        return "skipped-path"
    if not _has_scannable_text_name(path):
        return "unsupported-extension"
    if file_stat.st_size > MAX_SCANNED_TEXT_FILE_BYTES:
        return f"exceeds-{MAX_SCANNED_TEXT_FILE_BYTES}-bytes"
    return None


def _repo_relative_path_is_skipped(relative: Path) -> bool:
    if any(part in SCAN_SKIP_DIRS for part in relative.parts):
        return True
    if any(part.startswith(SCAN_SKIP_PREFIXES) for part in relative.parts):
        return True
    return bool(relative.parts and relative.parts[0] in SCAN_SKIP_ROOT_DIRS)


def _has_scannable_text_name(path: Path) -> bool:
    return (
        path.suffix in TEXT_EXTENSIONS
        or path.name in {"Makefile", ".gitignore", ".dockerignore", ".env"}
        or path.name.startswith(".env.")
    )


def _matching_lines(text: str, tokens: tuple[str, ...]) -> Iterable[tuple[int, str]]:
    for line_no, line in enumerate(text.splitlines(), start=1):
        if any(token in line for token in tokens):
            yield line_no, line


def _module_for_path(root: Path, path: Path) -> str:
    return _module_for_relative(_rel(root, path))


def _module_for_relative(relative: str) -> str:
    parts = Path(relative).parts
    if not parts:
        return "repo"
    if parts[0] in {"apps", "services", "workers", "packages"} and len(parts) >= 2:
        return f"{parts[0]}/{parts[1]}"
    if parts[0] == "openspec" and len(parts) >= 3:
        return f"openspec/{parts[2]}"
    if parts[0] == "docs" and len(parts) >= 2:
        return f"docs/{parts[1]}"
    if parts[0] == ".github":
        return ".github/workflows"
    if parts[0] == "infra" and len(parts) >= 2:
        return f"infra/{parts[1]}"
    return parts[0]


def _rel(root: Path, path: Path) -> str:
    try:
        return path.absolute().relative_to(root.resolve(strict=False)).as_posix()
    except ValueError:
        return path.as_posix()


def _artifact_fingerprint_pair_reason(root: Path, left: Path, right: Path) -> tuple[str, bool]:
    left_fingerprint = _bounded_artifact_sha256(root, left, MAX_ARTIFACT_FINGERPRINT_BYTES)
    right_fingerprint = _bounded_artifact_sha256(root, right, MAX_ARTIFACT_FINGERPRINT_BYTES)
    failures = [
        fingerprint
        for fingerprint in (left_fingerprint, right_fingerprint)
        if fingerprint.startswith("skipped:")
    ]
    if failures:
        return f"report-only fingerprint skipped ({'; '.join(failures)})", False
    hash_pair = f"{left_fingerprint}:{right_fingerprint}"
    return f"report-only fingerprint {hash_pair[:24]}", True


def _bounded_artifact_sha256(root: Path, path: Path, max_bytes: int) -> str:
    try:
        root_resolved = root.resolve(strict=False)
        relative = path.absolute().relative_to(root_resolved)
    except (OSError, ValueError):
        return f"skipped:{path.as_posix()}:outside-repo"
    rel = relative.as_posix()
    try:
        file_stat = path.lstat()
    except OSError:
        return f"skipped:{rel}:stat-error"
    if stat.S_ISLNK(file_stat.st_mode):
        return f"skipped:{rel}:symlink"
    if not stat.S_ISREG(file_stat.st_mode):
        return f"skipped:{rel}:not-regular-file"
    if file_stat.st_size > max_bytes:
        return f"skipped:{rel}:exceeds-{max_bytes}-bytes"
    try:
        return _file_sha256(path, max_bytes)
    except OSError:
        return f"skipped:{rel}:read-error"


def _file_sha256(path: Path, max_bytes: int) -> str:
    digest = hashlib.sha256()
    remaining = max_bytes
    with path.open("rb") as handle:
        while remaining > 0:
            chunk = handle.read(min(HASH_CHUNK_BYTES, remaining))
            if not chunk:
                break
            digest.update(chunk)
            remaining -= len(chunk)
    return digest.hexdigest()


def _git_resolve_commit(root: Path, ref: str) -> str | None:
    try:
        result = subprocess.run(
            ["git", "rev-parse", "--verify", f"{ref}^{{commit}}"],
            cwd=root,
            check=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
        )
    except (OSError, subprocess.CalledProcessError):
        return None
    resolved = result.stdout.strip()
    return resolved or None


def _git_merge_base(root: Path, left_ref: str, right_ref: str) -> str | None:
    try:
        result = subprocess.run(
            ["git", "merge-base", left_ref, right_ref],
            cwd=root,
            check=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
        )
    except (OSError, subprocess.CalledProcessError):
        return None
    resolved = result.stdout.strip()
    return resolved or None


def _git_tracked_paths(root: Path, pathspecs: Iterable[str] = ()) -> list[str]:
    command = ["git", "ls-files", "-z"]
    scoped_pathspecs = list(pathspecs)
    if scoped_pathspecs:
        command.extend(["--", *scoped_pathspecs])
    try:
        result = subprocess.run(
            command,
            cwd=root,
            check=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
        )
    except (OSError, subprocess.CalledProcessError):
        return []
    return [os.fsdecode(path) for path in result.stdout.split(b"\0") if path]
