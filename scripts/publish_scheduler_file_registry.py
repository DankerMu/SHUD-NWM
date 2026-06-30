#!/usr/bin/env python
"""Publish a DB-free scheduler registry manifest from the Basins source tree.

The node-22 production scheduler reads a file registry, not node-27's live
database. This script bridges that gap: discover every publishable SHUD model
under NHMS_BASINS_ROOT, publish immutable model packages when needed, derive
the scheduler-ready rows from the same package/source validation path used by
registry import, and atomically replace the scheduler registry manifest.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
from collections import Counter
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from packages.common.object_store import LocalObjectStore
from services.orchestrator.scheduler_file_providers import (
    SchedulerFileProviderError,
    publish_scheduler_registry_manifest,
)
from workers.model_registry.basins_discovery import (
    BasinsDiscoveryError,
    discover_basins_inventory,
    resolve_basins_root,
    write_inventory,
)
from workers.model_registry.basins_package import BasinsPackageError, publish_basins_package
from workers.model_registry.basins_registry_import import (
    BasinsRegistryImportError,
    ImportSources,
    prepare_basins_import_sources,
)

SCHEMA_VERSION = "nhms.scheduler.basins_file_registry_publish.v1"
DEFAULT_PACKAGE_VERSION_TEMPLATE = "vbasins-{slug_id}-{content_hash}"
DEFAULT_SOURCE_POLICY = {
    "forcing_source": "node27_raw_handoff",
    "allowed_cycle_hours_utc": [0, 12],
}
_SAFE_KEY_RE = re.compile(r"^[A-Za-z0-9_.-]+$")


class SchedulerRegistryPublishError(RuntimeError):
    def __init__(self, error_code: str, message: str, *, details: Mapping[str, Any] | None = None) -> None:
        super().__init__(message)
        self.error_code = error_code
        self.details = dict(details or {})

    def to_payload(self) -> dict[str, Any]:
        return {"error_code": self.error_code, "message": str(self), **self.details}


def publish_all_basin_scheduler_registry(
    *,
    basins_root: str | Path | None,
    registry_manifest: str | Path,
    object_store_root: str | Path | None,
    object_store_prefix: str | None,
    work_dir: str | Path,
    package_version_template: str = DEFAULT_PACKAGE_VERSION_TEMPLATE,
    basin_slugs: Sequence[str] = (),
    model_ids: Sequence[str] = (),
    shud_code_version: str = "basins-shud",
    partition: str = "standard",
    cpus_per_task: int = 4,
    memory_mb: int = 8192,
    walltime_minutes: int = 720,
    enable_return_periods: bool = False,
    dry_run: bool = False,
    output_path: str | Path | None = None,
) -> dict[str, Any]:
    root = resolve_basins_root(str(basins_root) if basins_root not in (None, "") else None)
    resolved_object_root = _required_path(
        object_store_root or os.getenv("OBJECT_STORE_ROOT"),
        "OBJECT_STORE_ROOT",
    )
    resolved_object_prefix = (object_store_prefix or os.getenv("OBJECT_STORE_PREFIX", "")).strip()
    if not resolved_object_prefix:
        raise SchedulerRegistryPublishError(
            "SCHEDULER_REGISTRY_OBJECT_STORE_PREFIX_MISSING",
            "OBJECT_STORE_PREFIX or --object-store-prefix is required.",
        )
    workspace = Path(work_dir).expanduser()
    workspace.mkdir(parents=True, exist_ok=True)
    package_manifest_dir = workspace / "package-manifests"
    package_manifest_dir.mkdir(parents=True, exist_ok=True)

    inventory = discover_basins_inventory(root)
    inventory_path = workspace / "basins-inventory.json"
    write_inventory(inventory, inventory_path)

    selected_models = _select_publishable_models(
        inventory,
        basin_slugs=basin_slugs,
        model_ids=model_ids,
    )
    store = LocalObjectStore(resolved_object_root, object_store_prefix=resolved_object_prefix)
    registry_models: list[dict[str, Any]] = []
    package_results: list[dict[str, Any]] = []
    for model in selected_models:
        model_id = _required_model_str(model, "model_id")
        version = package_version_for_model(model, package_version_template)
        package_manifest_path = package_manifest_dir / f"{model_id}.manifest.json"
        if dry_run:
            package_result = {
                "status": "dry_run",
                "model_id": model_id,
                "version": version,
                "manifest_path": str(package_manifest_path),
            }
        else:
            package_result = publish_basins_package(
                inventory_path=inventory_path,
                model_id=model_id,
                version=version,
                output_path=package_manifest_path,
                copy_forcing=False,
                object_store=store,
            )
        package_results.append(dict(package_result))
        if dry_run:
            continue
        sources = prepare_basins_import_sources(
            inventory_path=inventory_path,
            package_manifest_path=package_manifest_path,
        )
        registry_models.append(
            scheduler_registry_row_from_sources(
                sources,
                shud_code_version=shud_code_version,
                partition=partition,
                cpus_per_task=cpus_per_task,
                memory_mb=memory_mb,
                walltime_minutes=walltime_minutes,
                enable_return_periods=enable_return_periods,
            )
        )

    registry_receipt: dict[str, Any] | None = None
    if not dry_run:
        registry_receipt = publish_scheduler_registry_manifest(
            registry_models,
            registry_manifest,
            object_store_root=resolved_object_root,
            object_store_prefix=resolved_object_prefix,
        )

    package_status_counts = dict(Counter(str(item.get("status") or "unknown") for item in package_results))
    summary = {
        "schema_version": SCHEMA_VERSION,
        "status": "dry_run" if dry_run else "published",
        "generated_at": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
        "basins_root": str(root),
        "resolved_basins_root": str(root.resolve()),
        "inventory_path": str(inventory_path),
        "discovered_model_count": int(inventory.get("model_count") or 0),
        "selected_model_count": len(selected_models),
        "selected_basin_slugs": [str(model.get("basin_slug")) for model in selected_models],
        "selected_model_ids": [_required_model_str(model, "model_id") for model in selected_models],
        "registry_manifest": str(registry_manifest),
        "registry": registry_receipt,
        "package_status_counts": package_status_counts,
        "packages": package_results,
    }
    if output_path is not None:
        _write_json(output_path, summary)
    return summary


def scheduler_registry_row_from_sources(
    sources: ImportSources,
    *,
    shud_code_version: str,
    partition: str,
    cpus_per_task: int,
    memory_mb: int,
    walltime_minutes: int,
    enable_return_periods: bool,
) -> dict[str, Any]:
    model = sources.model
    manifest = sources.manifest
    ids = sources.ids
    geometry = sources.geometry
    display_capabilities = {"q_down": True, "tiles": True}
    frequency_capabilities = {"return_periods": bool(enable_return_periods)}
    resource_profile = {
        "runnable": True,
        "scheduler": "slurm",
        "partition": partition,
        "nodes": 1,
        "ntasks": 1,
        "cpus_per_task": int(cpus_per_task),
        "memory_mb": int(memory_mb),
        "walltime_minutes": int(walltime_minutes),
        "lineage": "basins_scheduler_file_registry",
        "basin_slug": model.get("basin_slug"),
        "project_name": model.get("shud_input_name") or model.get("basin_slug"),
        "shud_input_name": model.get("shud_input_name"),
        "manifest_uri": manifest["manifest_uri"],
        "package_checksum": manifest["package_checksum"],
        "model_package_uri": manifest["model_package_uri"],
        "source_inventory_checksum": manifest.get("source_inventory_checksum"),
        "source_inventory_schema_version": manifest.get("source_inventory_schema_version"),
        "source_path": model.get("source_path"),
        "resolved_source_path": model.get("resolved_source_path"),
        "source_is_symlink": bool(model.get("source_is_symlink", False)),
        "root_relative_path": model.get("root_relative_path"),
        "root_relative_resolved_path": model.get("root_relative_resolved_path"),
        "segment_count": geometry.segment_count,
        "output_segment_count": geometry.output_segment_count,
        "shud_evidence_counts": dict(geometry.evidence_counts),
    }
    return {
        "model_id": ids["model_id"],
        "basin_id": ids["basin_id"],
        "basin_version_id": ids["basin_version_id"],
        "river_network_version_id": ids["river_network_version_id"],
        "segment_count": geometry.segment_count,
        "output_segment_count": geometry.output_segment_count,
        "model_package_uri": manifest["model_package_uri"],
        "manifest_uri": manifest["manifest_uri"],
        "package_checksum": manifest["package_checksum"],
        "shud_code_version": shud_code_version,
        "active_flag": True,
        "lifecycle_state": "active",
        "resource_profile": resource_profile,
        "display_capabilities": display_capabilities,
        "frequency_capabilities": frequency_capabilities,
        "source_policy": dict(DEFAULT_SOURCE_POLICY),
    }


def package_version_for_model(model: Mapping[str, Any], template: str = DEFAULT_PACKAGE_VERSION_TEMPLATE) -> str:
    basin_slug = str(model.get("basin_slug") or "")
    model_id = _required_model_str(model, "model_id")
    slug_id = _slug_id(basin_slug)
    content_hash = _model_content_hash(model)
    try:
        version = template.format(
            slug=basin_slug.replace("/", "_"),
            slug_id=slug_id,
            model_id=model_id,
            content_hash=content_hash,
        )
    except KeyError as error:
        raise SchedulerRegistryPublishError(
            "SCHEDULER_REGISTRY_VERSION_TEMPLATE_INVALID",
            "Package version template contains an unsupported placeholder.",
            details={"placeholder": str(error), "template": template},
        ) from error
    if not _SAFE_KEY_RE.fullmatch(version) or version in {".", ".."}:
        raise SchedulerRegistryPublishError(
            "SCHEDULER_REGISTRY_PACKAGE_VERSION_UNSAFE",
            "Package version must be a safe object-store path segment.",
            details={"model_id": model_id, "version": version},
        )
    return version


def _select_publishable_models(
    inventory: Mapping[str, Any],
    *,
    basin_slugs: Sequence[str],
    model_ids: Sequence[str],
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
            if requested_slugs or requested_model_ids:
                raise SchedulerRegistryPublishError(
                    "SCHEDULER_REGISTRY_MODEL_NOT_PUBLISHABLE",
                    "Requested Basins model is not valid/publishable.",
                    details={
                        "model_id": model_id,
                        "basin_slug": basin_slug,
                        "status": model.get("status"),
                        "missing_required_files": model.get("missing_required_files") or [],
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
    if not selected:
        raise SchedulerRegistryPublishError(
            "SCHEDULER_REGISTRY_NO_PUBLISHABLE_MODELS",
            "No publishable Basins models were discovered.",
            details={
                "discovered_model_count": int(inventory.get("model_count") or 0),
                "available_basin_slugs": sorted(available_slugs),
            },
        )
    return selected


def _model_content_hash(model: Mapping[str, Any]) -> str:
    material = {
        "model_id": model.get("model_id"),
        "basin_slug": model.get("basin_slug"),
        "shud_input_name": model.get("shud_input_name"),
        "root_relative_resolved_path": model.get("root_relative_resolved_path"),
        "required_files": model.get("required_files") or {},
        "checksums": model.get("checksums") or {},
    }
    return hashlib.sha256(
        json.dumps(material, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str).encode("utf-8")
    ).hexdigest()[:12]


def _slug_id(value: str) -> str:
    normalized = re.sub(r"[^0-9a-zA-Z]+", "_", value).strip("_").lower()
    return normalized or "unknown"


def _required_model_str(model: Mapping[str, Any], field: str) -> str:
    value = model.get(field)
    if value in (None, ""):
        raise SchedulerRegistryPublishError(
            "SCHEDULER_REGISTRY_MODEL_FIELD_MISSING",
            "Basins model is missing a required field.",
            details={"field": field, "model": dict(model)},
        )
    return str(value)


def _required_path(value: str | Path | None, env_name: str) -> str:
    if value in (None, ""):
        raise SchedulerRegistryPublishError(
            "SCHEDULER_REGISTRY_REQUIRED_PATH_MISSING",
            f"{env_name} or the matching CLI option is required.",
            details={"env": env_name},
        )
    return str(value)


def _write_json(path: str | Path, payload: Mapping[str, Any]) -> None:
    output = Path(path).expanduser()
    output.parent.mkdir(parents=True, exist_ok=True)
    content = json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True, default=str) + "\n"
    temp = output.with_suffix(output.suffix + ".tmp")
    temp.write_text(content, encoding="utf-8")
    temp.replace(output)


def _default_registry_manifest() -> str:
    value = os.getenv("NHMS_SCHEDULER_REGISTRY_MANIFEST", "").strip()
    if not value:
        raise SchedulerRegistryPublishError(
            "SCHEDULER_REGISTRY_MANIFEST_MISSING",
            "NHMS_SCHEDULER_REGISTRY_MANIFEST or --registry-manifest is required.",
        )
    return value


def _default_work_dir() -> str:
    root = os.getenv("WORKSPACE_ROOT") or os.getenv("NHMS_SCHEDULER_TEMP_ROOT") or ".nhms-work"
    return str(Path(root) / "scheduler" / "basins-file-registry-publish")


def _parse_args(argv: Sequence[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--basins-root", default=None, help="Basins root. Defaults to NHMS_BASINS_ROOT.")
    parser.add_argument(
        "--registry-manifest",
        default=None,
        help="Destination scheduler registry manifest. Defaults to NHMS_SCHEDULER_REGISTRY_MANIFEST.",
    )
    parser.add_argument("--object-store-root", default=None, help="Defaults to OBJECT_STORE_ROOT.")
    parser.add_argument("--object-store-prefix", default=None, help="Defaults to OBJECT_STORE_PREFIX.")
    parser.add_argument("--work-dir", default=None, help="Operational work directory for inventory/package manifests.")
    parser.add_argument(
        "--package-version-template",
        default=DEFAULT_PACKAGE_VERSION_TEMPLATE,
        help="Template using {slug}, {slug_id}, {model_id}, and {content_hash}.",
    )
    parser.add_argument("--basin-slug", action="append", default=[], help="Optional basin slug filter; repeatable.")
    parser.add_argument("--model-id", action="append", default=[], help="Optional model id filter; repeatable.")
    parser.add_argument("--shud-code-version", default="basins-shud")
    parser.add_argument("--partition", default=os.getenv("NHMS_BASINS_DEFAULT_PARTITION", "standard"))
    parser.add_argument("--cpus-per-task", type=int, default=int(os.getenv("NHMS_BASINS_DEFAULT_CPUS", "4")))
    parser.add_argument("--memory-mb", type=int, default=int(os.getenv("NHMS_BASINS_DEFAULT_MEMORY_MB", "8192")))
    parser.add_argument(
        "--walltime-minutes",
        type=int,
        default=int(os.getenv("NHMS_BASINS_DEFAULT_WALLTIME_MINUTES", "720")),
    )
    parser.add_argument(
        "--enable-return-periods",
        action="store_true",
        help="Set frequency_capabilities.return_periods=true for all published models.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Discover/select only; do not publish packages/registry.",
    )
    parser.add_argument("--output", default=None, help="Optional path for the aggregate publication receipt.")
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = _parse_args(argv)
    try:
        summary = publish_all_basin_scheduler_registry(
            basins_root=args.basins_root,
            registry_manifest=args.registry_manifest or _default_registry_manifest(),
            object_store_root=args.object_store_root,
            object_store_prefix=args.object_store_prefix,
            work_dir=args.work_dir or _default_work_dir(),
            package_version_template=args.package_version_template,
            basin_slugs=args.basin_slug,
            model_ids=args.model_id,
            shud_code_version=args.shud_code_version,
            partition=args.partition,
            cpus_per_task=args.cpus_per_task,
            memory_mb=args.memory_mb,
            walltime_minutes=args.walltime_minutes,
            enable_return_periods=args.enable_return_periods,
            dry_run=args.dry_run,
            output_path=args.output,
        )
    except SchedulerRegistryPublishError as error:
        print(json.dumps(error.to_payload(), ensure_ascii=False, sort_keys=True), file=sys.stderr)
        return 1
    except (BasinsDiscoveryError, BasinsPackageError, BasinsRegistryImportError) as error:
        print(json.dumps(error.to_payload(), ensure_ascii=False, sort_keys=True), file=sys.stderr)
        return 1
    except SchedulerFileProviderError as error:
        print(
            json.dumps(
                {
                    "error_code": "SCHEDULER_REGISTRY_MANIFEST_INVALID",
                    "message": str(error),
                    "reason": error.reason,
                    "field": error.field,
                    "evidence": error.evidence,
                },
                ensure_ascii=False,
                sort_keys=True,
            ),
            file=sys.stderr,
        )
        return 1
    print(json.dumps(summary, ensure_ascii=False, sort_keys=True, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
