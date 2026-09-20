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
from types import ModuleType
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
from scripts.publish_registry import calibration as _mod_calibration
from scripts.publish_registry import cli as _mod_cli
from scripts.publish_registry import constants as _mod_constants
from scripts.publish_registry import model_version as _mod_model_version
from scripts.publish_registry import publisher as _mod_publisher
from scripts.publish_registry import radiation as _mod_radiation
from scripts.publish_registry import registry_rows as _mod_registry_rows
from scripts.publish_registry import selection as _mod_selection
from scripts.publish_registry import workspace as _mod_workspace
from scripts.publish_registry.calibration import (
    _apply_calibration_override_contexts,
    _declared_entries_not_applied,
    _require_declared_basins_in_inventory,
)
from scripts.publish_registry.cli import (
    _build_manual_cutover_gate,
    _cutover_declaration_present,
    _default_registry_manifest,
    _default_work_dir,
)
from scripts.publish_registry.constants import (
    _SAFE_KEY_RE,
    CALIBRATION_OVERRIDE_NOT_SELECTED_REASON,
    CALIBRATION_OVERRIDE_PATH_ENV_NAME,
    CALIBRATION_OVERRIDE_STAGING_DIR_NAME,
    CUTOVER_DECLARATION_ENV_NAME,
    DEFAULT_PACKAGE_VERSION_TEMPLATE,
    DEFAULT_SOURCE_POLICY,
    OPERATOR_GATE_WARNING,
    REPAIR_STAGING_DIR_NAMES,
    SCHEMA_VERSION,
    PublishContext,
    WorkspaceBudget,
)
from scripts.publish_registry.model_version import (
    _required_model_str,
    _required_path,
    _required_source_identity_hash,
    _slug_id,
    package_version_for_model,
)
from scripts.publish_registry.publisher import (
    _context_limit_error,
    _object_exists_after_failure,
    _publish_failure,
    publish_all_basin_scheduler_registry,
)
from scripts.publish_registry.radiation import (
    _repair_missing_radiation_contexts,
)
from scripts.publish_registry.registry_rows import (
    scheduler_registry_row_from_sources,
)
from scripts.publish_registry.selection import (
    _find_inventory_model,
    _is_missing_tsd_rl_only,
    _record_skipped_model,
    _repairable_missing_radiation_models,
    _select_publishable_models,
)
from scripts.publish_registry.workspace import (
    _cleanup_repair_staging,
    _copy_workspace_tree,
    _dir_size,
    _ensure_workspace_directory,
    _guard_resources,
    _strip_synology_sidecars,
    _write_json,
    _write_workspace_inventory,
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

__all__ = [
    "Any",
    "BasinsDiscoveryError",
    "BasinsPackageError",
    "BasinsRegistryImportError",
    "CALIBRATION_OVERRIDE_NOT_SELECTED_REASON",
    "CALIBRATION_OVERRIDE_PATH_ENV_NAME",
    "CALIBRATION_OVERRIDE_STAGING_DIR_NAME",
    "CUTOVER_DECLARATION_ENV_NAME",
    "CUTOVER_GATE_MODES",
    "CalibrationOverride",
    "CalibrationOverrideError",
    "Callable",
    "Counter",
    "DEFAULT_CALIBRATION_OVERRIDES_PATH",
    "DEFAULT_PACKAGE_VERSION_TEMPLATE",
    "DEFAULT_SOURCE_POLICY",
    "ImportSources",
    "LocalObjectStore",
    "Mapping",
    "OPERATOR_GATE_WARNING",
    "Path",
    "Protocol",
    "ProviderPreimage",
    "PublishContext",
    "REPAIR_STAGING_DIR_NAMES",
    "SCHEMA_VERSION",
    "SchedulerFileProviderError",
    "SchedulerRegistryPublishError",
    "Sequence",
    "UTC",
    "WorkspaceBudget",
    "_SAFE_KEY_RE",
    "_apply_calibration_override_contexts",
    "_build_manual_cutover_gate",
    "_cleanup_repair_staging",
    "_context_limit_error",
    "_copy_workspace_tree",
    "_cutover_declaration_present",
    "_declared_entries_not_applied",
    "_default_registry_manifest",
    "_default_work_dir",
    "_dir_size",
    "_ensure_workspace_directory",
    "_find_inventory_model",
    "_guard_resources",
    "_is_missing_tsd_rl_only",
    "_object_exists_after_failure",
    "_parse_args",
    "_publish_failure",
    "_record_skipped_model",
    "_repair_missing_radiation_contexts",
    "_repairable_missing_radiation_models",
    "_require_declared_basins_in_inventory",
    "_required_model_str",
    "_required_path",
    "_required_source_identity_hash",
    "_select_publishable_models",
    "_slug_id",
    "_strip_synology_sidecars",
    "_write_json",
    "_write_workspace_inventory",
    "annotations",
    "apply_calibration_overrides_for_basin",
    "argparse",
    "basins_package_source_identity",
    "dataclass",
    "datetime",
    "discover_basins_inventory",
    "hashlib",
    "json",
    "load_calibration_overrides",
    "main",
    "normalize_cutover_gate_audit",
    "os",
    "overrides_for_basin",
    "package_version_for_model",
    "prepare_basins_import_sources",
    "prepare_relocated_basins_import_sources_after_package_verification",
    "publish_all_basin_scheduler_registry",
    "publish_basins_package",
    "publish_scheduler_registry_manifest",
    "re",
    "repair_missing_tsd_rl_for_basin",
    "repair_performed",
    "replace",
    "resolve_basins_root",
    "scheduler_registry_row_from_sources",
    "shutil",
    "stat",
    "sys",
    "write_inventory",
]


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


# --- #1100 compatibility facade ------------------------------------------
# Same mechanism, same reason as the #1099 refresh facade
# (`scripts/scheduler_file_provider_refresh.py`).  Pre-split this file WAS the
# namespace: one binding per name, so `monkeypatch.setattr(registry_script,
# name, stub)` was observed by every call site.  Post-split the same logical
# binding is replicated across the `scripts/publish_registry/` modules that
# import it, and a plain re-export here would leave those replicas stale -- the
# patch would succeed, the stub would never be called, and the negative tests
# built on it (15 sites on `discover_basins_inventory`, 15 on
# `publish_basins_package`, 13 on `prepare_basins_import_sources`, plus the
# single-site refusal probes) would pass vacuously.  The module class below
# restores the single-namespace semantics by broadcasting every attribute write
# on this module to each package module that currently binds the SAME object
# under that name (identity-matched once, at import).  Undo restores travel the
# same path.
_PUBLISH_PACKAGE_MODULES: tuple[ModuleType, ...] = (
    _mod_calibration,
    _mod_cli,
    _mod_constants,
    _mod_model_version,
    _mod_publisher,
    _mod_radiation,
    _mod_registry_rows,
    _mod_selection,
    _mod_workspace,
)


def _mirrored_bindings() -> dict[str, tuple[ModuleType, ...]]:
    """Map facade attribute -> package modules holding the identical binding."""

    bindings: dict[str, tuple[ModuleType, ...]] = {}
    for name, value in list(globals().items()):
        if name.startswith("__"):
            continue
        owners = tuple(
            module
            for module in _PUBLISH_PACKAGE_MODULES
            if name in module.__dict__ and module.__dict__[name] is value
        )
        if owners:
            bindings[name] = owners
    return bindings


_MIRRORED_BINDINGS: dict[str, tuple[ModuleType, ...]] = _mirrored_bindings()


class _PublishFacadeModule(ModuleType):
    """Facade module type that mirrors attribute writes onto the real owners."""

    def __setattr__(self, name: str, value: Any) -> None:
        ModuleType.__setattr__(self, name, value)
        for module in _MIRRORED_BINDINGS.get(name, ()):
            ModuleType.__setattr__(module, name, value)

    def __delattr__(self, name: str) -> None:
        ModuleType.__delattr__(self, name)
        for module in _MIRRORED_BINDINGS.get(name, ()):
            if name in module.__dict__:
                ModuleType.__delattr__(module, name)


sys.modules[__name__].__class__ = _PublishFacadeModule


if __name__ == "__main__":
    raise SystemExit(main())
