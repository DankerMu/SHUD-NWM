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
import shutil
import stat
import sys
from collections import Counter
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Callable, Protocol

from packages.common.object_store import LocalObjectStore

# #1097: the audit contract lives in `packages/scheduler/registry_audit.py` so
# the CLI and the manifest publisher share one definition.  These names stay
# importable from this module (`CUTOVER_GATE_MODES` is re-exported only for
# import-path compatibility, hence the noqa).
from packages.scheduler.registry_audit import (
    CUTOVER_GATE_MODES,  # noqa: F401
    SchedulerRegistryPublishError,
    normalize_cutover_gate_audit,
)
from services.orchestrator.scheduler_file_providers import (
    ProviderPreimage,
    SchedulerFileProviderError,
    publish_scheduler_registry_manifest,
)
from workers.model_registry.basins_calibration_overrides import (
    DEFAULT_CALIBRATION_OVERRIDES_PATH,
    CalibrationOverride,
    CalibrationOverrideError,
    apply_calibration_overrides_for_basin,
    load_calibration_overrides,
    overrides_for_basin,
)
from workers.model_registry.basins_discovery import (
    BasinsDiscoveryError,
    discover_basins_inventory,
    resolve_basins_root,
    write_inventory,
)
from workers.model_registry.basins_package import (
    BasinsPackageError,
    basins_package_source_identity,
    publish_basins_package,
)
from workers.model_registry.basins_radiation_template import repair_missing_tsd_rl_for_basin, repair_performed
from workers.model_registry.basins_registry_import import (
    BasinsRegistryImportError,
    ImportSources,
    prepare_basins_import_sources,
    prepare_relocated_basins_import_sources_after_package_verification,
)

# v2 (#1080 round-2 R2-A1): summary now carries a required top-level
# `cutover_gate` audit block so a `--allow-uncovered-cutover` bypass leaves a
# persisted marker (versus the byte-identical v1 shape between gate-passing
# and bypass runs).  See design.md D7 sub-decision on `cutover_gate`.
SCHEMA_VERSION = "nhms.scheduler.basins_file_registry_publish.v2"
# Env name owned by scheduler_file_provider_refresh._registry_precommit_gate;
# hard-coded here so the CLI can audit it even when the refresh module is not
# imported (bootstrap path).
CUTOVER_DECLARATION_ENV_NAME = "NHMS_REGISTRY_CUTOVER_DECLARATION_PATH"
# #1104: `main()` does not populate `expected_preimage`, so this CLI has NO
# code-level protection against overwriting a provider refresh that commits
# between our snapshot and our own commit.  The protection is an operator
# prohibition documented in the runbook; every run announces it on stderr.
# Deliberately free of the substring `allow-uncovered-cutover` so it stays
# distinguishable from the bypass banner.
OPERATOR_GATE_WARNING = (
    "WARNING: manual publisher concurrency is operator-gated, not CAS-gated. "
    "Confirm nhms-scheduler-file-provider-refresh.timer is inactive/disabled "
    "AND nhms-scheduler-file-provider-refresh.service is not activating/active "
    "before publishing: systemctl --user status "
    "nhms-scheduler-file-provider-refresh.timer "
    "nhms-scheduler-file-provider-refresh.service --no-pager. "
    "See docs/runbooks/current-production-ops.md (manual publisher CLI)."
)
DEFAULT_PACKAGE_VERSION_TEMPLATE = "vbasins-{slug_id}-{content_hash}-{source_hash}"
DEFAULT_SOURCE_POLICY = {
    "forcing_source": "node27_raw_handoff",
    "allowed_cycle_hours_utc": [0, 12],
}
CALIBRATION_OVERRIDE_STAGING_DIR_NAME = "overridden-basins"
CALIBRATION_OVERRIDE_PATH_ENV_NAME = "NHMS_CALIBRATION_OVERRIDES_PATH"
REPAIR_STAGING_DIR_NAMES = ("repaired-basins", CALIBRATION_OVERRIDE_STAGING_DIR_NAME)
_SAFE_KEY_RE = re.compile(r"^[A-Za-z0-9_.-]+$")


@dataclass(frozen=True)
class PublishContext:
    model: dict[str, Any]
    inventory_path: Path
    repair: dict[str, Any] | None = None
    source_lineage_model: dict[str, Any] | None = None
    # #1832: the declared calibration overrides that were applied to THIS
    # context's staging copy, in manifest shape.  ``None`` (never ``[]``) when
    # the basin is absent from the declaration.
    calibration_overrides: tuple[dict[str, Any], ...] | None = None


class WorkspaceBudget(Protocol):
    def ensure_directory(self, path: Path) -> None: ...

    def write_json(self, path: Path, payload: Mapping[str, Any]) -> None: ...

    def copy_tree(self, source: Path, target: Path) -> None: ...

    def copy_file(self, source: Path, target: Path) -> None: ...

    def write_bytes(self, path: Path, content: bytes) -> None: ...

    def reserve_external_write(self, path: Path, size: int) -> None: ...

    def finalize_external_write(self, path: Path, size: int) -> None: ...

    def verify_external_write(self, path: Path) -> None: ...

    def rescan(self) -> None: ...


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
    repair_missing_radiation: bool = True,
    retain_repair_staging: bool = False,
    calibration_overrides_path: str | Path | None = DEFAULT_CALIBRATION_OVERRIDES_PATH,
    dry_run: bool = False,
    output_path: str | Path | None = None,
    expected_preimage: ProviderPreimage | Mapping[str, object] | None = None,
    registry_generated_at: datetime | None = None,
    registry_commit_observer: Callable[[ProviderPreimage], None] | None = None,
    precommit_validator: Callable[
        [Path, Sequence[Mapping[str, Any]], Sequence[Mapping[str, Any]]], None
    ]
    | None = None,
    resource_validator: Callable[[Path], None] | None = None,
    workspace_budget: WorkspaceBudget | None = None,
    max_contexts: int | None = None,
    cutover_gate: Mapping[str, Any] | None = None,
    skipped_model_sink: Callable[[Mapping[str, Mapping[str, Any]]], None] | None = None,
) -> dict[str, Any]:
    audited_cutover_gate = normalize_cutover_gate_audit(cutover_gate)
    # #1832: the checked-in declaration loads by default, on BOTH lanes -- an
    # override that only applies on one lane is worse than no override, because
    # the other lane republishes the source value and re-derives the original
    # `model_id`.  An explicit ``None``/`""` is the test/rehearsal escape hatch.
    declared_overrides: tuple[CalibrationOverride, ...] = (
        load_calibration_overrides(calibration_overrides_path)
        if calibration_overrides_path not in (None, "")
        else ()
    )
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
    _ensure_workspace_directory(workspace, workspace_budget)
    package_manifest_dir = workspace / "package-manifests"
    _guard_resources(resource_validator, workspace)
    _ensure_workspace_directory(package_manifest_dir, workspace_budget)
    _guard_resources(resource_validator, workspace)

    inventory = discover_basins_inventory(root)
    # #1832 round-2 C2: a declared slug that the discovered inventory does not
    # contain at all refuses HERE, before anything is selected, staged or
    # written -- and on `dry_run` too.
    #
    # Refuse rather than report, because the two "not applied" situations are
    # categorically different and only one of them is benign.  A basin that
    # exists but was narrowed out of this run is legitimate (`--basin-slug`).  A
    # basin that exists NOWHERE is a typo or a stale rename in checked-in
    # config: the declaration will never bite, forever, with no signal.  After
    # the hetianhe rollout that silence means the unattended refresh lane
    # republishes the SOURCE value, re-derives the ORIGINAL `model_id` and
    # reverts the registry straight back onto the NaN cliff the declaration
    # exists to avoid.  The refusal is fail-safe (nothing is committed, the
    # previous registry generation stays live) and the manual CLI lane catches a
    # bad slug long before the timer ever sees it, so a loud, diagnosable,
    # non-committing failure is strictly better than silently reverting one
    # basin to a NaNing calibration.
    _require_declared_basins_in_inventory(declared_overrides, inventory)
    inventory_path = workspace / "basins-inventory.json"
    _guard_resources(resource_validator, workspace)
    _write_workspace_inventory(inventory, inventory_path, workspace_budget)
    _guard_resources(resource_validator, workspace)

    selected_models = _select_publishable_models(
        inventory,
        basin_slugs=basin_slugs,
        model_ids=model_ids,
        skipped_model_sink=skipped_model_sink,
    )
    contexts = [PublishContext(model=model, inventory_path=inventory_path) for model in selected_models]
    if max_contexts is not None and len(contexts) > max_contexts:
        raise _context_limit_error(len(contexts), max_contexts)
    if repair_missing_radiation:
        repaired_radiation_contexts = (
            _repair_missing_radiation_contexts(
                inventory=inventory,
                basins_root=root,
                workspace=workspace,
                basin_slugs=basin_slugs,
                model_ids=model_ids,
                already_selected_model_ids={str(model.get("model_id")) for model in selected_models},
                resource_validator=resource_validator,
                workspace_budget=workspace_budget,
                skipped_model_sink=skipped_model_sink,
            )
        )
        if max_contexts is not None and len(contexts) + len(repaired_radiation_contexts) > max_contexts:
            raise _context_limit_error(len(contexts) + len(repaired_radiation_contexts), max_contexts)
        contexts.extend(repaired_radiation_contexts)
    if max_contexts is not None and len(contexts) > max_contexts:
        raise _context_limit_error(len(contexts), max_contexts)
    # #1832: applied AFTER the full context list is assembled, so a basin that
    # is both declared and radiation-repaired gets the override too.  Staging
    # from that context's own (already isolated) tree keeps both edits.
    contexts = _apply_calibration_override_contexts(
        contexts=contexts,
        declared_overrides=declared_overrides,
        workspace=workspace,
        resource_validator=resource_validator,
        workspace_budget=workspace_budget,
    )
    if not contexts:
        raise SchedulerRegistryPublishError(
            "SCHEDULER_REGISTRY_NO_PUBLISHABLE_MODELS",
            "No publishable Basins models were discovered.",
            details={"discovered_model_count": int(inventory.get("model_count") or 0)},
        )
    store = LocalObjectStore(resolved_object_root, object_store_prefix=resolved_object_prefix)
    registry_models: list[dict[str, Any]] = []
    package_results: list[dict[str, Any]] = []
    attempted_total = 0
    for context in contexts:
        attempted_total += 1
        manifest_key: str | None = None
        manifest_uri: str | None = None
        manifest_existed_before = True
        package_recorded = False
        try:
            _guard_resources(resource_validator, workspace)
            model = context.model
            model_id = _required_model_str(model, "model_id")
            source_identity = basins_package_source_identity(
                inventory_path=context.inventory_path,
                model_id=model_id,
            )
            version = package_version_for_model(
                model,
                package_version_template,
                source_identity=source_identity,
            )
            package_manifest_path = package_manifest_dir / f"{model_id}.manifest.json"
            manifest_key = f"models/{model_id}/{version}/manifest.json"
            manifest_uri = store.uri_for_key(manifest_key)
            manifest_existed_before = store.exists(manifest_key)
            if dry_run:
                package_result = {
                    "status": "dry_run",
                    "model_id": model_id,
                    "version": version,
                    "manifest_path": str(package_manifest_path),
                }
                suggested_ids = model.get("suggested_ids")
                if not isinstance(suggested_ids, Mapping):
                    raise SchedulerRegistryPublishError(
                        "SCHEDULER_REGISTRY_DRY_RUN_IDENTITY_INVALID",
                        "Dry-run model is missing bounded suggested identities.",
                        details={"model_id": model_id},
                    )
                registry_models.append(
                    {
                        "model_id": str(suggested_ids.get("model_id") or model_id),
                        "basin_id": str(suggested_ids.get("basin_id") or ""),
                    }
                )
            else:
                # Passed only when non-empty: `calibration_overrides` is an
                # added keyword, and an unconditional pass would break callers
                # that substitute their own publisher.
                override_kwargs: dict[str, Any] = (
                    {"calibration_overrides": list(context.calibration_overrides)}
                    if context.calibration_overrides
                    else {}
                )
                package_result = publish_basins_package(
                    inventory_path=context.inventory_path,
                    model_id=model_id,
                    version=version,
                    output_path=package_manifest_path,
                    copy_forcing=False,
                    object_store=store,
                    output_capacity_guard=(workspace_budget.reserve_external_write if workspace_budget else None),
                    output_write_guard=(workspace_budget.finalize_external_write if workspace_budget else None),
                    expected_source_identity=source_identity,
                    **override_kwargs,
                )
                if workspace_budget is not None:
                    workspace_budget.verify_external_write(package_manifest_path)
            package_results.append(dict(package_result))
            package_recorded = True
            _guard_resources(resource_validator, workspace)
            if dry_run:
                continue
            if package_result.get("status") == "already_done":
                sources = prepare_relocated_basins_import_sources_after_package_verification(
                    inventory_path=context.inventory_path,
                    package_manifest_path=package_manifest_path,
                    verified_package_checksum=str(package_result.get("package_checksum") or ""),
                )
            else:
                sources = prepare_basins_import_sources(
                    inventory_path=context.inventory_path,
                    package_manifest_path=package_manifest_path,
                )
            try:
                registry_row = scheduler_registry_row_from_sources(
                    sources,
                    shud_code_version=shud_code_version,
                    partition=partition,
                    cpus_per_task=cpus_per_task,
                    memory_mb=memory_mb,
                    walltime_minutes=walltime_minutes,
                    source_lineage_model=context.source_lineage_model,
                )
            finally:
                # Parsed geometry can be much larger than the registry row.
                # Release it before the next context starts parsing so two
                # basin geometries are never live at the same time.
                del sources
            registry_models.append(registry_row)
        except Exception as error:
            if (
                not dry_run
                and not package_recorded
                and not manifest_existed_before
                and manifest_key is not None
                and manifest_uri is not None
                and _object_exists_after_failure(store, manifest_key)
            ):
                package_results.append({"status": "published", "manifest_uri": manifest_uri})
            if workspace_budget is not None:
                workspace_budget.rescan()
            raise _publish_failure(
                error,
                discovered_total=len(contexts),
                attempted_total=attempted_total,
                package_results=package_results,
                error_code="SCHEDULER_REGISTRY_CONTEXT_PUBLISH_FAILED",
                message="Scheduler registry context publication failed before canonical replacement.",
            ) from error

    if precommit_validator is not None:
        try:
            precommit_validator(workspace, package_results, registry_models)
        except Exception as error:
            raise _publish_failure(
                error,
                discovered_total=len(contexts),
                attempted_total=attempted_total,
                package_results=package_results,
                error_code="SCHEDULER_REGISTRY_REFRESH_PRECOMMIT_FAILED",
                message="Registry/readiness precommit validation failed before canonical replacement.",
            ) from error

    registry_receipt: dict[str, Any] | None = None
    if not dry_run:
        try:
            registry_receipt = publish_scheduler_registry_manifest(
                registry_models,
                registry_manifest,
                object_store_root=resolved_object_root,
                object_store_prefix=resolved_object_prefix,
                generated_at=registry_generated_at,
                expected_preimage=expected_preimage,
                commit_observer=registry_commit_observer,
                # R2-A1: mirror the CLI summary audit into the manifest
                # publication receipt so operators reading
                # `manifest-last.json`'s companion receipt see the same
                # cutover_gate mode and declaration presence.
                cutover_gate=audited_cutover_gate,
            )
        except Exception as error:
            raise _publish_failure(
                error,
                discovered_total=len(contexts),
                attempted_total=attempted_total,
                package_results=package_results,
                error_code="SCHEDULER_REGISTRY_CANONICAL_PUBLISH_FAILED",
                message="Validated packages were not committed to the canonical registry.",
            ) from error

    package_status_counts = dict(Counter(str(item.get("status") or "unknown") for item in package_results))
    summary = {
        "schema_version": SCHEMA_VERSION,
        "status": "dry_run" if dry_run else "published",
        "generated_at": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
        "basins_root": str(root),
        "resolved_basins_root": str(root.resolve()),
        "inventory_path": str(inventory_path),
        "discovered_model_count": int(inventory.get("model_count") or 0),
        "selected_model_count": len(contexts),
        "selected_basin_slugs": [str(context.model.get("basin_slug")) for context in contexts],
        "selected_model_ids": [_required_model_str(context.model, "model_id") for context in contexts],
        "repairs": [context.repair for context in contexts if context.repair is not None],
        # #1832: workspace-side echo of the authoritative record.  The record
        # that matters is `calibration.overrides` in each package manifest;
        # this one only exists so an operator reading the run receipt sees the
        # same facts without opening the object store.
        "calibration_overrides": [
            item for context in contexts for item in (context.calibration_overrides or ())
        ],
        # A declared basin that IS discovered but this run does not publish is
        # REPORTED, not refused: it publishes nothing, so it can tell no lie.
        # (A declared basin the tree does not contain AT ALL never gets here --
        # `_require_declared_basins_in_inventory` refused it above.)
        "calibration_overrides_not_applied": _declared_entries_not_applied(declared_overrides, contexts),
        "calibration_overrides_declaration": (
            str(calibration_overrides_path) if calibration_overrides_path not in (None, "") else None
        ),
        "registry_manifest": str(registry_manifest),
        "registry": registry_receipt,
        "package_status_counts": package_status_counts,
        "packages": package_results,
        # R2-A1: persist the cutover_gate audit block on every summary
        # (dry_run/published/bypassed).  Same shape in every path so a later
        # auditor can grep for `cutover_gate.mode` and see how the gate ran.
        "cutover_gate": audited_cutover_gate,
    }
    summary["repair_staging_cleanup"] = (
        {"status": "retained", "reason": "retain_repair_staging"}
        if retain_repair_staging
        else _cleanup_repair_staging(workspace)
    )
    if output_path is not None:
        _write_json(output_path, summary)
    return summary


def _guard_resources(validator: Callable[[Path], None] | None, workspace: Path) -> None:
    if validator is not None:
        validator(workspace)


def _object_exists_after_failure(store: LocalObjectStore, manifest_key: str) -> bool:
    try:
        return store.exists(manifest_key)
    except (OSError, ValueError, RuntimeError):
        return False


def _publish_failure(
    error: Exception,
    *,
    discovered_total: int,
    attempted_total: int,
    package_results: Sequence[Mapping[str, Any]],
    error_code: str,
    message: str,
) -> SchedulerRegistryPublishError:
    published = [item for item in package_results if item.get("status") == "published"]
    source_details = getattr(error, "details", {})
    source_reason = getattr(error, "reason", None)
    source_phase = getattr(error, "phase", None)
    if isinstance(source_details, Mapping):
        source_reason = source_details.get("provider_reason", source_reason)
        source_phase = source_details.get("provider_phase", source_phase)
    allowed_reasons = {
        "workspace_limit_exceeded",
        "orphan_limit_exceeded",
        "provider_preimage_changed",
        "provider_replace_failed",
        "provider_replace_uncertain",
        "provider_postread_failed",
        # #1080 registry-cutover refusal tokens flow through the same details
        # channel as other precommit rejections; keep them out of the generic
        # provider_invalid collapse so operators see the actual reason.
        "registry_cutover_undeclared",
        "registry_cutover_removal_refused",
        "registry_cutover_declaration_invalid",
    }
    provider_reason = (
        str(source_reason)
        if isinstance(source_reason, str) and source_reason in allowed_reasons
        else "provider_invalid"
    )
    provider_phase = str(source_phase) if source_phase in {"precommit", "replace", "replace_uncertain"} else "precommit"
    return SchedulerRegistryPublishError(
        error_code,
        message,
        details={
            "provider_reason": provider_reason,
            "provider_phase": provider_phase,
            "discovered_total": discovered_total,
            "attempted_total": attempted_total,
            "created_total": len(published),
            "packages": [
                {
                    "status": "published",
                    "orphan_id": hashlib.sha256(str(item.get("manifest_uri") or "").encode("utf-8")).hexdigest()[:32],
                }
                for item in published[:256]
            ],
        },
    )


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


def _context_limit_error(total: int, maximum: int) -> SchedulerRegistryPublishError:
    return SchedulerRegistryPublishError(
        "SCHEDULER_REGISTRY_CONTEXT_LIMIT_EXCEEDED",
        "Publishable scheduler registry contexts exceed the configured hard limit.",
        details={
            "provider_reason": "orphan_limit_exceeded",
            "provider_phase": "precommit",
            "context_total": total,
            "context_limit": maximum,
            "attempted_total": 0,
            "created_total": 0,
            "packages": [],
        },
    )


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


def package_version_for_model(
    model: Mapping[str, Any],
    template: str = DEFAULT_PACKAGE_VERSION_TEMPLATE,
    *,
    source_identity: Mapping[str, Any],
) -> str:
    basin_slug = str(model.get("basin_slug") or "")
    model_id = _required_model_str(model, "model_id")
    slug_id = _slug_id(basin_slug)
    content_hash = _required_source_identity_hash(source_identity, "content_sha256", model_id)[:12]
    source_hash = _required_source_identity_hash(source_identity, "source_sha256", model_id)[:8]
    try:
        version = template.format(
            slug=basin_slug.replace("/", "_"),
            slug_id=slug_id,
            model_id=model_id,
            content_hash=content_hash,
            source_hash=source_hash,
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


# The single "declared but not applied" reason token.  Two other places pin the
# same vocabulary and must move with it: `CALIBRATION_OVERRIDE_NOT_APPLIED_REASONS`
# in `scripts/scheduler_file_provider_refresh.py` (which imports this constant)
# and the `reason_not_applied` enum in
# `schemas/scheduler_file_provider_refresh_receipt.schema.json`, which the
# refresh receipt is validated against before it is published.
CALIBRATION_OVERRIDE_NOT_SELECTED_REASON = "basin_not_selected_for_this_run"


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


def _required_source_identity_hash(identity: Mapping[str, Any], field: str, model_id: str) -> str:
    value = identity.get(field)
    if not isinstance(value, str) or re.fullmatch(r"[0-9a-f]{64}", value) is None:
        raise SchedulerRegistryPublishError(
            "SCHEDULER_REGISTRY_SOURCE_IDENTITY_INVALID",
            "Package source identity is missing a canonical SHA-256 digest.",
            details={"model_id": model_id, "field": field},
        )
    return value


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
        help="Template using {slug}, {slug_id}, {model_id}, {content_hash}, and {source_hash}.",
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
        "--no-repair-missing-radiation",
        action="store_true",
        help="Do not synthesize missing *.tsd.rl files in private scratch copies.",
    )
    parser.add_argument(
        "--calibration-overrides",
        default=(
            os.getenv(CALIBRATION_OVERRIDE_PATH_ENV_NAME, "").strip()
            or str(DEFAULT_CALIBRATION_OVERRIDES_PATH)
        ),
        help=(
            "Path to the declared calibration-override file.  The checked-in "
            f"{DEFAULT_CALIBRATION_OVERRIDES_PATH.name} loads by default without anyone naming it; "
            f"this flag (or ${CALIBRATION_OVERRIDE_PATH_ENV_NAME}) only redirects the path, for "
            "rehearsal against an alternative declaration."
        ),
    )
    parser.add_argument(
        "--retain-repair-staging",
        action="store_true",
        help="Keep repaired basin staging directories after publishing for manual debugging.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Discover/select only; do not publish packages/registry.",
    )
    parser.add_argument("--output", default=None, help="Optional path for the aggregate publication receipt.")
    parser.add_argument(
        "--allow-uncovered-cutover",
        action="store_true",
        help=(
            "Bypass the #1080 registry cutover gate.  Only intended for bootstrap "
            "(no previous canonical manifest) or one-off operator recovery; regular "
            "operators must file a cutover declaration and let the gate run."
        ),
    )
    return parser.parse_args(argv)


def _cutover_declaration_present(env_value: str | None) -> bool:
    """Return True when the cutover declaration env resolves to a readable file.

    R2-A1: the CLI records this fact on the summary so a later auditor can tell
    whether the operator staged a declaration at all before running the
    publisher — a "gate enforced but no declaration" run is materially
    different from a "gate enforced and declaration file was found" run.

    Any error (missing env, not a regular file, permission denied) collapses
    to ``False``.  The gate itself does the strict schema validation; this
    helper only proves the operator staged a file we could read.
    """
    if not env_value:
        return False
    path = Path(env_value).expanduser()
    try:
        # Explicit no-follow: symlinks are already rejected by the gate; the
        # audit fact should reflect the same rejection.
        stat_result = path.lstat()
    except OSError:
        return False
    if not stat.S_ISREG(stat_result.st_mode):
        return False
    return os.access(str(path), os.R_OK)


def _build_manual_cutover_gate(
    *,
    registry_manifest: str | Path,
    dry_run: bool,
) -> Callable[
    [Path, Sequence[Mapping[str, Any]], Sequence[Mapping[str, Any]]], None
]:
    """Wire the #1080 registry cutover gate for the manual CLI (finding C-D1).

    Lazy import of the refresh module keeps this file free of a top-level
    dependency cycle (`scheduler_file_provider_refresh` already imports from
    this module).  The manual CLI runs outside the refresh runner's own lock
    coordination, but the gate itself is stateless — it snapshots the
    previous canonical bytes inside the gate call and returns the same
    classification refusal that the runner path would produce.
    """
    from datetime import UTC, datetime  # local — CLI-only path.

    from scripts import scheduler_file_provider_refresh as refresh

    manifest_path = Path(registry_manifest).expanduser()

    def gate(
        workspace: Path,
        packages: Sequence[Mapping[str, Any]],
        registry_models: Sequence[Mapping[str, Any]],
    ) -> None:
        # Load the previous canonical bytes; missing file legitimately
        # bootstraps and returns None.  We deliberately hand only the raw
        # bytes forward so the gate's own parser stays the source of truth.
        try:
            previous = refresh._load_previous_canonical_registry(
                str(manifest_path),
                containment_root=manifest_path.parent,
            )
        except refresh.RefreshError as error:
            raise SchedulerRegistryPublishError(
                "SCHEDULER_REGISTRY_REFRESH_PRECOMMIT_FAILED",
                "Previous canonical registry could not be read for the cutover gate.",
                details={
                    "provider_reason": error.reason,
                    "provider_phase": "precommit",
                },
            ) from error
        previous_sha: str | None
        previous_bytes: bytes | None
        if previous is None:
            previous_sha = None
            previous_bytes = None
        else:
            previous_sha, _previous_models, previous_bytes = previous
        cutover_env = os.getenv(refresh.CUTOVER_DECLARATION_ENV, "").strip() or None
        # Manual CLI does not need to bind classification to a receipt; the
        # sink still needs to be a no-op callable.
        def classification_sink(payload: dict[str, Any]) -> None:
            del payload
        refresh._registry_precommit_gate(
            workspace,
            packages,
            registry_models,
            previous_registry_bytes=previous_bytes,
            previous_registry_sha256=previous_sha,
            prospective_generated_at=datetime.now(UTC),
            cutover_declaration_env=cutover_env,
            dry_run=dry_run,
            classification_sink=classification_sink,
        )

    return gate


def main(argv: Sequence[str] | None = None) -> int:
    args = _parse_args(argv)
    # #1104: this CLI never populates `expected_preimage`, so a provider
    # refresh committing between our snapshot and our commit would be silently
    # overwritten.  Concurrency with the refresh timer is operator-gated by the
    # runbook, not by code -- say so before any I/O happens.
    print(OPERATOR_GATE_WARNING, file=sys.stderr)
    resolved_registry_manifest = args.registry_manifest or _default_registry_manifest()
    precommit_validator: Callable[
        [Path, Sequence[Mapping[str, Any]], Sequence[Mapping[str, Any]]], None
    ] | None = None
    # R2-A1: compute the cutover_gate audit block BEFORE calling the publisher
    # so the summary always records how the gate ran even when the publisher
    # fails/short-circuits.  Only the CLI-controlled seam changes mode.
    cutover_gate_audit: dict[str, Any]
    if args.allow_uncovered_cutover:
        # Loud stderr warning: operators must NOT default to bypass; the
        # gate refusal is the point of #1080.
        print(
            "WARNING: --allow-uncovered-cutover disables the #1080 registry "
            "cutover gate. Only use for bootstrap or explicit one-off recovery; "
            "regular refreshes MUST file a valid cutover declaration.",
            file=sys.stderr,
        )
        cutover_gate_audit = {
            "mode": "bypassed_allow_uncovered_cutover",
            "declaration_env": None,
            "declaration_present": False,
        }
    else:
        precommit_validator = _build_manual_cutover_gate(
            registry_manifest=resolved_registry_manifest,
            dry_run=args.dry_run,
        )
        cutover_gate_audit = {
            "mode": "enforced",
            "declaration_env": CUTOVER_DECLARATION_ENV_NAME,
            "declaration_present": _cutover_declaration_present(
                os.getenv(CUTOVER_DECLARATION_ENV_NAME, "").strip() or None
            ),
        }
    # #1132: normalize once, outside the try, so the success summary and all
    # three stderr failure payloads share one audited block.  The normalizer
    # raises, so it must never run inside an except handler.
    cutover_gate_audit = normalize_cutover_gate_audit(cutover_gate_audit)
    try:
        summary = publish_all_basin_scheduler_registry(
            basins_root=args.basins_root,
            registry_manifest=resolved_registry_manifest,
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
            repair_missing_radiation=not args.no_repair_missing_radiation,
            retain_repair_staging=args.retain_repair_staging,
            calibration_overrides_path=args.calibration_overrides,
            dry_run=args.dry_run,
            output_path=args.output,
            precommit_validator=precommit_validator,
            cutover_gate=cutover_gate_audit,
        )
    except SchedulerRegistryPublishError as error:
        # R2-A1: attach the cutover_gate audit to the stderr error payload so
        # a refusal (or bootstrap/deploy failure) leaves the same audit fact
        # a successful summary would.  Otherwise bypass runs would be
        # byte-identical to gate-passing runs in every persisted artifact and
        # a later auditor could not tell them apart.
        payload = {**error.to_payload(), "cutover_gate": cutover_gate_audit}
        print(json.dumps(payload, ensure_ascii=False, sort_keys=True), file=sys.stderr)
        return 1
    except CalibrationOverrideError as error:
        payload = {**error.to_payload(), "cutover_gate": cutover_gate_audit}
        print(json.dumps(payload, ensure_ascii=False, sort_keys=True), file=sys.stderr)
        return 1
    except (BasinsDiscoveryError, BasinsPackageError, BasinsRegistryImportError) as error:
        payload = {**error.to_payload(), "cutover_gate": cutover_gate_audit}
        print(json.dumps(payload, ensure_ascii=False, sort_keys=True), file=sys.stderr)
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
                    "cutover_gate": cutover_gate_audit,
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
