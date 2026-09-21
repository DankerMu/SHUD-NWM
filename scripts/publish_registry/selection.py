"""Inventory selection: which discovered Basins models this run publishes.

Split out of ``scripts/publish_scheduler_file_registry.py`` by #1100.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any, Callable

from packages.scheduler.registry_audit import SchedulerRegistryPublishError


def _record_skipped_model(
    sink: Callable[[Mapping[str, Mapping[str, Any]]], None] | None,
    model: Mapping[str, Any],
) -> None:
    """Hand one skipped inventory row to the caller's sink (#1433).

    A bulk run silently drops models it cannot publish.  When the model is
    already in the canonical registry that skip surfaces downstream as a
    registry removal, and the refresh gate needs the reason to tell "package
    turned invalid" from "the model directory is gone".  The keys mirror the
    not-publishable details this module already raises with, so operators read
    one vocabulary on both lanes.  Selection itself is unchanged: this is a
    read-only out-sink.
    """
    if sink is None:
        return
    model_id = str(model.get("model_id") or "")
    if not model_id:
        return
    sink(
        {
            model_id: {
                "status": model.get("status"),
                "missing_required_files": model.get("missing_required_files") or [],
                "invalid_required_files": model.get("invalid_required_files") or [],
                "unreadable_required_files": model.get("unreadable_required_files") or [],
            }
        }
    )

def _select_publishable_models(
    inventory: Mapping[str, Any],
    *,
    basin_slugs: Sequence[str],
    model_ids: Sequence[str],
    skipped_model_sink: Callable[[Mapping[str, Mapping[str, Any]]], None] | None = None,
) -> list[dict[str, Any]]:
    models = inventory.get("models")
    if not isinstance(models, Sequence) or isinstance(models, str | bytes | bytearray):
        raise SchedulerRegistryPublishError(
            "SCHEDULER_REGISTRY_INVENTORY_INVALID",
            "Basins inventory must contain a models array.",
        )
    requested_slugs = {str(value) for value in basin_slugs if str(value)}
    requested_model_ids = {str(value) for value in model_ids if str(value)}
    selected: list[dict[str, Any]] = []
    available_slugs: set[str] = set()
    available_model_ids: set[str] = set()
    for item in models:
        if not isinstance(item, Mapping):
            continue
        model = dict(item)
        basin_slug = str(model.get("basin_slug") or "")
        model_id = str(model.get("model_id") or "")
        if basin_slug:
            available_slugs.add(basin_slug)
        if model_id:
            available_model_ids.add(model_id)
        if requested_slugs and basin_slug not in requested_slugs:
            continue
        if requested_model_ids and model_id not in requested_model_ids:
            continue
        if model.get("status") != "valid" or model.get("default_publish_eligible") is not True:
            _record_skipped_model(skipped_model_sink, model)
            if _is_missing_tsd_rl_only(model):
                continue
            if requested_slugs or requested_model_ids:
                raise SchedulerRegistryPublishError(
                    "SCHEDULER_REGISTRY_MODEL_NOT_PUBLISHABLE",
                    "Requested Basins model is not valid/publishable.",
                    details={
                        "model_id": model_id,
                        "basin_slug": basin_slug,
                        "status": model.get("status"),
                        "missing_required_files": model.get("missing_required_files") or [],
                        "invalid_required_files": model.get("invalid_required_files") or [],
                        "unreadable_required_files": model.get("unreadable_required_files") or [],
                    },
                )
            continue
        selected.append(model)
    missing_slugs = sorted(requested_slugs - available_slugs)
    missing_model_ids = sorted(requested_model_ids - available_model_ids)
    if missing_slugs or missing_model_ids:
        raise SchedulerRegistryPublishError(
            "SCHEDULER_REGISTRY_REQUESTED_MODEL_NOT_FOUND",
            "Requested Basins model was not found in the inventory.",
            details={
                "missing_basin_slugs": missing_slugs,
                "missing_model_ids": missing_model_ids,
                "available_basin_slugs": sorted(available_slugs),
                "available_model_ids": sorted(available_model_ids),
            },
        )
    selected.sort(key=lambda model: (str(model.get("root_relative_resolved_path") or ""), str(model.get("model_id"))))
    return selected

def _repairable_missing_radiation_models(inventory: Mapping[str, Any]) -> list[dict[str, Any]]:
    models = inventory.get("models")
    if not isinstance(models, Sequence) or isinstance(models, str | bytes | bytearray):
        return []
    return [
        dict(model)
        for model in models
        if isinstance(model, Mapping)
        and model.get("status") == "partial"
        and model.get("default_publish_eligible") is not True
        and _is_missing_tsd_rl_only(model)
    ]

def _is_missing_tsd_rl_only(model: Mapping[str, Any]) -> bool:
    return set(model.get("missing_required_files") or []) == {"*.tsd.rl"}

def _find_inventory_model(inventory: Mapping[str, Any], model_id: str) -> dict[str, Any]:
    models = inventory.get("models")
    if not isinstance(models, Sequence) or isinstance(models, str | bytes | bytearray):
        raise SchedulerRegistryPublishError(
            "SCHEDULER_REGISTRY_INVENTORY_INVALID",
            "Basins inventory must contain a models array.",
        )
    matches = [dict(model) for model in models if isinstance(model, Mapping) and model.get("model_id") == model_id]
    if len(matches) != 1:
        raise SchedulerRegistryPublishError(
            "SCHEDULER_REGISTRY_REPAIRED_MODEL_NOT_FOUND",
            "Repaired Basins inventory did not contain exactly one requested model.",
            details={"model_id": model_id, "match_count": len(matches)},
        )
    return matches[0]
