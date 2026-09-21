"""The scheduler registry row derived from one validated import source set.

Split out of ``scripts/publish_scheduler_file_registry.py`` by #1100.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from scripts.publish_registry.constants import DEFAULT_SOURCE_POLICY
from workers.model_registry.basins_registry_import import ImportSources


def scheduler_registry_row_from_sources(
    sources: ImportSources,
    *,
    shud_code_version: str,
    partition: str,
    cpus_per_task: int,
    memory_mb: int,
    walltime_minutes: int,
    source_lineage_model: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    model = sources.model
    lineage_model = source_lineage_model or model
    manifest = sources.manifest
    ids = sources.ids
    geometry = sources.geometry
    display_capabilities = {"q_down": True, "tiles": True}
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
        "basin_slug": lineage_model.get("basin_slug"),
        "project_name": lineage_model.get("shud_input_name") or lineage_model.get("basin_slug"),
        "shud_input_name": lineage_model.get("shud_input_name"),
        "manifest_uri": manifest["manifest_uri"],
        "package_checksum": manifest["package_checksum"],
        "model_package_uri": manifest["model_package_uri"],
        "source_inventory_checksum": manifest.get("source_inventory_checksum"),
        "source_inventory_schema_version": manifest.get("source_inventory_schema_version"),
        "source_path": lineage_model.get("source_path"),
        "resolved_source_path": lineage_model.get("resolved_source_path"),
        "source_is_symlink": bool(lineage_model.get("source_is_symlink", False)),
        "root_relative_path": lineage_model.get("root_relative_path"),
        "root_relative_resolved_path": lineage_model.get("root_relative_resolved_path"),
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
        "source_policy": dict(DEFAULT_SOURCE_POLICY),
    }
