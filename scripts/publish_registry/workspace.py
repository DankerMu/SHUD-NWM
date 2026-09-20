"""Workspace-scoped filesystem primitives shared by every publish phase.

Split out of ``scripts/publish_scheduler_file_registry.py`` by #1100. Each
helper is the single seam through which an optional ``WorkspaceBudget``
intercepts a write, so the staging paths and the publisher share one accounting
surface.
"""

from __future__ import annotations

import json
import shutil
from collections.abc import Mapping
from pathlib import Path
from typing import Any, Callable

from scripts.publish_registry.constants import REPAIR_STAGING_DIR_NAMES, WorkspaceBudget
from workers.model_registry.basins_discovery import write_inventory


def _guard_resources(validator: Callable[[Path], None] | None, workspace: Path) -> None:
    if validator is not None:
        validator(workspace)

def _ensure_workspace_directory(path: Path, budget: WorkspaceBudget | None) -> None:
    if budget is None:
        path.mkdir(parents=True, exist_ok=True)
    else:
        budget.ensure_directory(path)

def _write_workspace_inventory(
    inventory: dict[str, Any],
    path: Path,
    budget: WorkspaceBudget | None,
) -> None:
    if budget is None:
        write_inventory(inventory, path)
    else:
        budget.write_json(path, inventory)

def _copy_workspace_tree(source: Path, target: Path, budget: WorkspaceBudget | None) -> None:
    if budget is None:
        shutil.copytree(source, target, symlinks=False)
    else:
        budget.copy_tree(source, target)

def _strip_synology_sidecars(root: Path) -> None:
    for sidecar in root.rglob("@eaDir"):
        shutil.rmtree(sidecar, ignore_errors=True)

def _cleanup_repair_staging(workspace: Path) -> dict[str, Any]:
    removed: list[dict[str, Any]] = []
    failures: list[dict[str, str]] = []
    for name in REPAIR_STAGING_DIR_NAMES:
        path = workspace / name
        if not path.exists():
            continue
        try:
            size_bytes = _dir_size(path)
            shutil.rmtree(path)
        except OSError as error:
            failures.append({"name": name, "path": str(path), "error": str(error)})
            continue
        removed.append({"name": name, "path": str(path), "size_bytes": size_bytes})
    if failures:
        return {"status": "failed", "removed": removed, "failures": failures}
    return {"status": "cleaned", "removed": removed}

def _dir_size(path: Path) -> int:
    total = 0
    for child in path.rglob("*"):
        try:
            if child.is_file() and not child.is_symlink():
                total += child.stat().st_size
        except OSError:
            continue
    return total

def _write_json(path: str | Path, payload: Mapping[str, Any]) -> None:
    output = Path(path).expanduser()
    output.parent.mkdir(parents=True, exist_ok=True)
    content = json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True, default=str) + "\n"
    temp = output.with_suffix(output.suffix + ".tmp")
    temp.write_text(content, encoding="utf-8")
    temp.replace(output)
