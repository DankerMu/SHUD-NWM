"""#1832 declared calibration overrides: refusal, reporting and re-staging.

Split out of ``scripts/publish_scheduler_file_registry.py`` by #1100.
"""

from __future__ import annotations

import shutil
from collections.abc import Mapping, Sequence
from dataclasses import replace
from pathlib import Path
from typing import Any, Callable

from scripts.publish_registry.constants import (
    CALIBRATION_OVERRIDE_NOT_SELECTED_REASON,
    CALIBRATION_OVERRIDE_STAGING_DIR_NAME,
    PublishContext,
    WorkspaceBudget,
)
from scripts.publish_registry.model_version import _required_model_str, _slug_id
from scripts.publish_registry.selection import _find_inventory_model
from scripts.publish_registry.workspace import (
    _copy_workspace_tree,
    _ensure_workspace_directory,
    _guard_resources,
    _strip_synology_sidecars,
    _write_workspace_inventory,
)
from workers.model_registry.basins_calibration_overrides import (
    CalibrationOverride,
    CalibrationOverrideError,
    apply_calibration_overrides_for_basin,
    overrides_for_basin,
)
from workers.model_registry.basins_discovery import discover_basins_inventory


def _require_declared_basins_in_inventory(
    declared_overrides: Sequence[CalibrationOverride],
    inventory: Mapping[str, Any],
) -> None:
    """#1832 round-2 C2: refuse a declared slug that exists nowhere in the tree.

    The discriminator between "typo/stale rename" and "narrowed out of this
    run" is the DISCOVERED inventory, not the publish set: see the rationale at
    the call site.  Only the former reaches here; the latter is reported by
    ``_declared_entries_not_applied``.
    """
    if not declared_overrides:
        return
    discovered_slugs = {
        str(model.get("basin_slug") or "") for model in inventory.get("models") or ()
    }
    missing = [
        override for override in declared_overrides if override.basin_slug not in discovered_slugs
    ]
    if not missing:
        return
    labels = sorted(override.entry_label for override in missing)
    raise CalibrationOverrideError(
        "CALIBRATION_OVERRIDE_BASIN_NOT_IN_INVENTORY",
        (
            "Declared calibration override(s) name a basin the discovered Basins inventory does not "
            f"contain: {', '.join(labels)}."
        ),
        details={
            "entries": [
                {"basin_slug": override.basin_slug, "parameter": override.parameter}
                for override in sorted(missing, key=lambda item: item.entry_label)
            ],
            "basin_slugs": sorted({override.basin_slug for override in missing}),
            "discovered_basin_count": len(discovered_slugs),
        },
    )

def _declared_entries_not_applied(
    declared_overrides: Sequence[CalibrationOverride],
    contexts: Sequence[PublishContext],
) -> list[dict[str, Any]]:
    """#1832: a declared basin this run does not publish is reported, not refused.

    The lie the refusal exists to prevent is a PUBLISHED package carrying the
    original value while the declaration claims otherwise.  A basin this run
    does not publish cannot tell that lie, so keying the refusal on
    "declared but not published" would make every narrowed publish -- a
    ``--basin-slug`` run, a partial Basins mirror -- fail on a declaration that
    is doing nothing wrong.  It is still a fact worth persisting, so it lands on
    the run summary (and, on the unattended lane, on the refresh receipt).

    By the time this runs, ``_require_declared_basins_in_inventory`` has already
    refused every declared basin the tree does not contain, so the only case
    left here is "discovered, but not selected for this run" -- hence the
    reason token, which is deliberately NOT the pre-C2
    ``basin_not_in_publish_set``: that string covered both cases and therefore
    means something different on receipts written before this change.
    """
    if not declared_overrides:
        return []
    published_slugs = {str(context.model.get("basin_slug") or "") for context in contexts}
    return [
        {**override.as_entry(), "reason_not_applied": CALIBRATION_OVERRIDE_NOT_SELECTED_REASON}
        for override in declared_overrides
        if override.basin_slug not in published_slugs
    ]

def _apply_calibration_override_contexts(
    *,
    contexts: Sequence[PublishContext],
    declared_overrides: Sequence[CalibrationOverride],
    workspace: Path,
    resource_validator: Callable[[Path], None] | None = None,
    workspace_budget: WorkspaceBudget | None = None,
) -> list[PublishContext]:
    """Re-stage every declared context onto a private copy carrying its override.

    Mirrors ``_repair_missing_radiation_contexts``: copy into a workspace-owned
    staging root, edit only there, then RE-DISCOVER from the copy so the
    published content hash reflects the edit.  The Basins source tree is only
    ever read.
    """
    if not declared_overrides:
        return list(contexts)
    staging_base = workspace / CALIBRATION_OVERRIDE_STAGING_DIR_NAME
    inventory_dir = workspace / "overridden-inventories"
    result: list[PublishContext] = []
    staged_any = False
    for context in contexts:
        basin_slug = str(context.model.get("basin_slug") or "")
        basin_overrides = overrides_for_basin(declared_overrides, basin_slug)
        if not basin_overrides:
            result.append(context)
            continue
        if not staged_any:
            _guard_resources(resource_validator, workspace)
            _ensure_workspace_directory(inventory_dir, workspace_budget)
            _guard_resources(resource_validator, workspace)
            staged_any = True
        model_id = _required_model_str(context.model, "model_id")
        source_path = Path(str(context.model.get("source_path") or ""))
        if not source_path.is_dir():
            raise CalibrationOverrideError(
                "CALIBRATION_OVERRIDE_SOURCE_MISSING",
                f"Calibration override for '{basin_slug}' cannot stage: source path is not a directory.",
                details={
                    "basin_slug": basin_slug,
                    "model_id": model_id,
                    "source_path": str(source_path),
                    "entries": [override.as_entry() for override in basin_overrides],
                },
            )
        staged_root = staging_base / _slug_id(basin_slug)
        if staged_root.exists():
            shutil.rmtree(staged_root, ignore_errors=True)
            if workspace_budget is not None:
                workspace_budget.rescan()
        staged_target = staged_root / basin_slug
        _guard_resources(resource_validator, workspace)
        _ensure_workspace_directory(staged_target.parent, workspace_budget)
        _copy_workspace_tree(source_path, staged_target, workspace_budget)
        _guard_resources(resource_validator, workspace)
        _strip_synology_sidecars(staged_target)
        if workspace_budget is not None:
            workspace_budget.rescan()
        _guard_resources(resource_validator, workspace)
        applied = apply_calibration_overrides_for_basin(
            isolated_root=staged_root,
            basin_slug=basin_slug,
            overrides=basin_overrides,
            write_bytes=(workspace_budget.write_bytes if workspace_budget else None),
        )
        if workspace_budget is not None:
            workspace_budget.rescan()
        _guard_resources(resource_validator, workspace)
        staged_inventory = discover_basins_inventory(staged_root)
        staged_model = _find_inventory_model(staged_inventory, model_id)
        if staged_model.get("status") != "valid" or staged_model.get("default_publish_eligible") is not True:
            raise CalibrationOverrideError(
                "CALIBRATION_OVERRIDE_MODEL_NOT_PUBLISHABLE",
                f"Basin '{basin_slug}' is no longer publishable after applying its declared calibration override.",
                details={
                    "basin_slug": basin_slug,
                    "model_id": model_id,
                    "status": staged_model.get("status"),
                    "missing_required_files": staged_model.get("missing_required_files") or [],
                    "invalid_required_files": staged_model.get("invalid_required_files") or [],
                    "unreadable_required_files": staged_model.get("unreadable_required_files") or [],
                    "entries": [override.as_entry() for override in basin_overrides],
                },
            )
        staged_inventory_path = inventory_dir / f"{model_id}.overridden.inventory.json"
        _guard_resources(resource_validator, workspace)
        _write_workspace_inventory(staged_inventory, staged_inventory_path, workspace_budget)
        _guard_resources(resource_validator, workspace)
        result.append(
            replace(
                context,
                model=staged_model,
                inventory_path=staged_inventory_path,
                # Lineage keeps pointing at the real Basins tree (or, for a
                # radiation-repaired context, whatever it already recorded).
                source_lineage_model=context.source_lineage_model or context.model,
                calibration_overrides=tuple(applied),
            )
        )
    return result
