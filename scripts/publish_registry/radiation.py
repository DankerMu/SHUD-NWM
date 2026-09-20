"""Missing-``*.tsd.rl`` radiation repair staged into private scratch copies.

Split out of ``scripts/publish_scheduler_file_registry.py`` by #1100.
"""

from __future__ import annotations

import shutil
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any, Callable

from packages.scheduler.registry_audit import SchedulerRegistryPublishError
from scripts.publish_registry.constants import PublishContext, WorkspaceBudget
from scripts.publish_registry.model_version import _slug_id
from scripts.publish_registry.selection import (
    _find_inventory_model,
    _record_skipped_model,
    _repairable_missing_radiation_models,
)
from scripts.publish_registry.workspace import (
    _copy_workspace_tree,
    _ensure_workspace_directory,
    _guard_resources,
    _strip_synology_sidecars,
    _write_workspace_inventory,
)
from workers.model_registry.basins_discovery import discover_basins_inventory
from workers.model_registry.basins_radiation_template import repair_missing_tsd_rl_for_basin, repair_performed


def _repair_missing_radiation_contexts(
    *,
    inventory: Mapping[str, Any],
    basins_root: Path,
    workspace: Path,
    basin_slugs: Sequence[str],
    model_ids: Sequence[str],
    already_selected_model_ids: set[str],
    resource_validator: Callable[[Path], None] | None = None,
    workspace_budget: WorkspaceBudget | None = None,
    skipped_model_sink: Callable[[Mapping[str, Mapping[str, Any]]], None] | None = None,
) -> list[PublishContext]:
    requested_slugs = {str(value) for value in basin_slugs if str(value)}
    requested_model_ids = {str(value) for value in model_ids if str(value)}
    contexts: list[PublishContext] = []
    repaired_root_base = workspace / "repaired-basins"
    repaired_inventory_dir = workspace / "repaired-inventories"
    _guard_resources(resource_validator, workspace)
    _ensure_workspace_directory(repaired_inventory_dir, workspace_budget)
    _guard_resources(resource_validator, workspace)
    for model in _repairable_missing_radiation_models(inventory):
        basin_slug = str(model.get("basin_slug") or "")
        model_id = str(model.get("model_id") or "")
        if model_id in already_selected_model_ids:
            continue
        if requested_slugs and basin_slug not in requested_slugs:
            continue
        if requested_model_ids and model_id not in requested_model_ids:
            continue
        source_path = Path(str(model.get("source_path") or ""))
        if not source_path.is_dir():
            continue
        repaired_root = repaired_root_base / _slug_id(basin_slug)
        if repaired_root.exists():
            shutil.rmtree(repaired_root, ignore_errors=True)
            if workspace_budget is not None:
                workspace_budget.rescan()
        repaired_target = repaired_root / basin_slug
        _guard_resources(resource_validator, workspace)
        _ensure_workspace_directory(repaired_target.parent, workspace_budget)
        _copy_workspace_tree(source_path, repaired_target, workspace_budget)
        _guard_resources(resource_validator, workspace)
        _strip_synology_sidecars(repaired_target)
        if workspace_budget is not None:
            workspace_budget.rescan()
        _guard_resources(resource_validator, workspace)
        repair = repair_missing_tsd_rl_for_basin(
            isolated_root=repaired_root,
            basin_slug=basin_slug,
            template_search_root=basins_root,
            copy_file=(workspace_budget.copy_file if workspace_budget else None),
        )
        if workspace_budget is not None:
            workspace_budget.rescan()
        if not repair_performed(repair):
            _record_skipped_model(skipped_model_sink, model)
            if requested_slugs or requested_model_ids:
                raise SchedulerRegistryPublishError(
                    "SCHEDULER_REGISTRY_MISSING_RADIATION_REPAIR_FAILED",
                    "Requested Basins model is missing *.tsd.rl and no matching template was found.",
                    details={"model_id": model_id, "basin_slug": basin_slug, "repair": repair},
                )
            continue
        repaired_inventory = discover_basins_inventory(repaired_root)
        repaired_model = _find_inventory_model(repaired_inventory, model_id)
        if repaired_model.get("status") != "valid" or repaired_model.get("default_publish_eligible") is not True:
            # The repaired row is the accurate one: it already reflects the
            # rescued *.tsd.rl, so what it still reports is what actually
            # blocks publication (#1433 evidence).
            _record_skipped_model(skipped_model_sink, repaired_model)
            # Same split as the missing-template branch above, and as
            # ``_select_publishable_models``: an EXPLICITLY requested model that
            # cannot be published fails closed, but on a bulk (unfiltered) run
            # this model is simply not selected. The repair is a best-effort
            # rescue of models the plain selection already dropped, so aborting
            # the whole run over one of them would let an unrelated malformed
            # package (e.g. #1197's `23106\t6` IC on a basin that also lacks
            # *.tsd.rl) block every healthy basin from publishing.
            #
            # Scope of that relief: it only reaches models NOT already in the
            # canonical registry. A skipped model that IS registered leaves the
            # prospective registry short a row, which #1080's cutover gate
            # classifies as a removal and refuses
            # (``registry_cutover_removal_refused``) before canonical
            # replacement. So for registered models the run still fails; what
            # changed is that it fails at the gate, with the previous registry
            # intact, instead of mid-publish.
            # (Pinned by
            # ``test_bulk_skip_of_an_already_registered_model_is_refused_by_the_cutover_gate``.)
            # #1433 gave that refusal a declared way out: a
            # ``transition_mode: "retire"`` declaration entry admits the removal
            # on the refresh lane, so this CLI's ``--allow-uncovered-cutover``
            # is no longer the only exit.
            #
            # On the manual CLI lane the skip is not silent: with a persistent
            # ``--work-dir`` the run's ``basins-inventory.json`` keeps the
            # model's ``invalid_required_files`` / ``missing_required_files``.
            # The refresh lane gets the same rows through
            # ``skipped_model_sink`` (#1433), which the gate copies onto the
            # removal refusal.
            if requested_slugs or requested_model_ids:
                raise SchedulerRegistryPublishError(
                    "SCHEDULER_REGISTRY_REPAIRED_MODEL_NOT_PUBLISHABLE",
                    "Repaired Basins model is still not publishable.",
                    details={
                        "model_id": model_id,
                        "basin_slug": basin_slug,
                        "status": repaired_model.get("status"),
                        "missing_required_files": repaired_model.get("missing_required_files") or [],
                        "invalid_required_files": repaired_model.get("invalid_required_files") or [],
                        "unreadable_required_files": repaired_model.get("unreadable_required_files") or [],
                        "repair": repair,
                    },
                )
            continue
        repaired_inventory_path = repaired_inventory_dir / f"{model_id}.inventory.json"
        _guard_resources(resource_validator, workspace)
        _write_workspace_inventory(repaired_inventory, repaired_inventory_path, workspace_budget)
        _guard_resources(resource_validator, workspace)
        contexts.append(
            PublishContext(
                model=repaired_model,
                inventory_path=repaired_inventory_path,
                repair=repair,
                source_lineage_model=model,
            )
        )
    return contexts
