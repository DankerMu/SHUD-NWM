from __future__ import annotations

import os
import re
import shutil
import stat
import uuid
from pathlib import Path
from typing import Any, Iterable

from packages.common.safe_fs import SafeFilesystemError, ensure_directory_no_follow, rmtree_no_follow

SAFE_RUN_ID_RE = re.compile(r"^[A-Za-z0-9_.-]+$")
REQUIRED_RUN_FILES = ("input/manifest.json",)


class RunTreeCopybackError(RuntimeError):
    def __init__(self, code: str, message: str, details: dict[str, Any]) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.details = details


def copyback_run_trees(
    *,
    object_store_root: str | Path,
    copyback_root: str | Path | None,
    run_ids: Iterable[str],
) -> dict[str, Any] | None:
    if copyback_root is None or not str(copyback_root).strip():
        return None

    unique_run_ids = sorted({str(run_id).strip() for run_id in run_ids if str(run_id).strip()})
    object_root = _existing_directory(Path(object_store_root), "object_store_root")
    target_root = ensure_directory_no_follow(Path(copyback_root)).resolve()

    if object_root == target_root:
        for run_id in unique_run_ids:
            _validate_run_tree(object_root / _run_key(run_id), run_id=run_id)
        return {
            "status": "skipped",
            "reason": "copyback_root_matches_object_store_root",
            "root": str(target_root),
            "run_ids": unique_run_ids,
        }
    if _paths_overlap(object_root, target_root):
        raise RunTreeCopybackError(
            "OBJECT_STORE_COPYBACK_ROOT_OVERLAP",
            "Object-store copyback root must not overlap OBJECT_STORE_ROOT.",
            {"object_store_root": str(object_root), "copyback_root": str(target_root)},
        )

    copied: list[dict[str, Any]] = []
    total_files = 0
    total_bytes = 0
    for run_id in unique_run_ids:
        source = _validate_run_tree(object_root / _run_key(run_id), run_id=run_id)
        summary = _replace_tree(source=source, target=target_root / _run_key(run_id), containment_root=target_root)
        copied.append({"run_id": run_id, "object_key": _run_key(run_id), **summary})
        total_files += int(summary["file_count"])
        total_bytes += int(summary["byte_count"])

    return {
        "status": "copied",
        "root": str(target_root),
        "run_ids": unique_run_ids,
        "file_count": total_files,
        "byte_count": total_bytes,
        "runs": copied,
    }


def _run_key(run_id: str) -> str:
    if not SAFE_RUN_ID_RE.fullmatch(run_id) or run_id in {".", ".."}:
        raise RunTreeCopybackError(
            "OBJECT_STORE_COPYBACK_UNSAFE_RUN_ID",
            "Run id is unsafe for object-store copyback.",
            {"run_id": run_id},
        )
    return f"runs/{run_id}"


def _existing_directory(path: Path, field: str) -> Path:
    try:
        resolved = path.expanduser().resolve(strict=False)
        if not resolved.is_dir():
            raise FileNotFoundError(str(resolved))
        _reject_symlink_ancestors(resolved)
        return resolved
    except (OSError, SafeFilesystemError) as error:
        raise RunTreeCopybackError(
            "OBJECT_STORE_COPYBACK_ROOT_UNAVAILABLE",
            "Object-store root is unavailable for run-tree copyback.",
            {field: str(path), "error": str(error)},
        ) from error


def _validate_run_tree(source: Path, *, run_id: str) -> Path:
    try:
        if not source.is_dir():
            raise FileNotFoundError(str(source))
        _reject_symlink_ancestors(source)
        for required in REQUIRED_RUN_FILES:
            path = source / required
            if not path.is_file() or path.is_symlink():
                raise FileNotFoundError(str(path))
    except (OSError, SafeFilesystemError) as error:
        raise RunTreeCopybackError(
            "OBJECT_STORE_COPYBACK_SOURCE_MISSING",
            "Run products are missing or unsafe in the object-store staging root.",
            {"run_id": run_id, "object_key": _run_key(run_id), "source": str(source), "error": str(error)},
        ) from error
    return source


def _replace_tree(*, source: Path, target: Path, containment_root: Path) -> dict[str, Any]:
    parent = ensure_directory_no_follow(target.parent, containment_root=containment_root)
    temp = parent / f".{target.name}.copyback-{uuid.uuid4().hex}.tmp"
    backup = parent / f".{target.name}.copyback-{uuid.uuid4().hex}.backup"
    try:
        summary = _copy_tree_no_symlinks(source, temp)
        if target.exists():
            os.replace(target, backup)
        os.replace(temp, target)
        if backup.exists():
            rmtree_no_follow(backup, containment_root=containment_root, missing_ok=True)
        return summary
    except Exception:
        rmtree_no_follow(temp, containment_root=containment_root, missing_ok=True)
        if backup.exists() and not target.exists():
            os.replace(backup, target)
        raise
    finally:
        rmtree_no_follow(backup, containment_root=containment_root, missing_ok=True)


def _copy_tree_no_symlinks(source: Path, target: Path) -> dict[str, Any]:
    file_count = 0
    byte_count = 0
    for current_root, dirs, files in os.walk(source, followlinks=False):
        current = Path(current_root)
        _reject_symlink(current)
        relative = current.relative_to(source)
        destination_dir = target / relative
        destination_dir.mkdir(parents=True, exist_ok=True)
        for dirname in list(dirs):
            _reject_symlink(current / dirname)
        for filename in files:
            src = current / filename
            _reject_symlink(src)
            info = src.stat()
            if not stat.S_ISREG(info.st_mode):
                raise RunTreeCopybackError(
                    "OBJECT_STORE_COPYBACK_UNSAFE_SOURCE",
                    "Run product source contains a non-regular file.",
                    {"source": str(src)},
                )
            shutil.copy2(src, destination_dir / filename)
            file_count += 1
            byte_count += int(info.st_size)
    return {"file_count": file_count, "byte_count": byte_count}


def _reject_symlink(path: Path) -> None:
    if path.is_symlink():
        raise RunTreeCopybackError(
            "OBJECT_STORE_COPYBACK_UNSAFE_SOURCE",
            "Run product source contains a symlink.",
            {"source": str(path)},
        )


def _reject_symlink_ancestors(path: Path) -> None:
    current = Path(path.anchor)
    for part in path.parts[1:]:
        current = current / part
        _reject_symlink(current)


def _paths_overlap(left: Path, right: Path) -> bool:
    try:
        left.relative_to(right)
        return True
    except ValueError:
        pass
    try:
        right.relative_to(left)
        return True
    except ValueError:
        return False
