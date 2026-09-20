"""The bulk publish transaction: discover, stage, package, commit, summarize.

Split out of ``scripts/publish_scheduler_file_registry.py`` by #1100.
``publish_all_basin_scheduler_registry`` is the one entry point both lanes
drive -- the manual CLI at the historical path and the unattended refresh
runner (``scripts/scheduler_refresh/runner.py``) -- so the structured failure
translation lives beside it.
"""

from __future__ import annotations

import hashlib
import os
from collections import Counter
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Callable

from packages.common.object_store import LocalObjectStore
from packages.scheduler.registry_audit import SchedulerRegistryPublishError, normalize_cutover_gate_audit
from scripts.publish_registry.calibration import (
    _apply_calibration_override_contexts,
    _declared_entries_not_applied,
    _require_declared_basins_in_inventory,
)
from scripts.publish_registry.constants import (
    DEFAULT_PACKAGE_VERSION_TEMPLATE,
    SCHEMA_VERSION,
    PublishContext,
    WorkspaceBudget,
)
from scripts.publish_registry.model_version import (
    _required_model_str,
    _required_path,
    package_version_for_model,
)
from scripts.publish_registry.radiation import _repair_missing_radiation_contexts
from scripts.publish_registry.registry_rows import scheduler_registry_row_from_sources
from scripts.publish_registry.selection import _select_publishable_models
from scripts.publish_registry.workspace import (
    _cleanup_repair_staging,
    _ensure_workspace_directory,
    _guard_resources,
    _write_json,
    _write_workspace_inventory,
)
from services.orchestrator.scheduler_file_providers import ProviderPreimage, publish_scheduler_registry_manifest
from workers.model_registry.basins_calibration_overrides import (
    DEFAULT_CALIBRATION_OVERRIDES_PATH,
    CalibrationOverride,
    load_calibration_overrides,
)
from workers.model_registry.basins_discovery import discover_basins_inventory, resolve_basins_root
from workers.model_registry.basins_package import basins_package_source_identity, publish_basins_package
from workers.model_registry.basins_registry_import import (
    prepare_basins_import_sources,
    prepare_relocated_basins_import_sources_after_package_verification,
)


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
