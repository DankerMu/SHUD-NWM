#!/usr/bin/env python
"""Refresh all expiring node-22 scheduler file providers without a database."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import stat
import sys
import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from dataclasses import field as dataclass_field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import ModuleType
from typing import Any, Callable

import jsonschema

from packages.common.libpq_env import LIBPQ_CONNECTION_ENV_KEYS
from packages.common.provider_atomic import (
    ProviderAtomicError,
    ProviderPreimage,
    atomic_replace_provider_bytes,
    provider_destination_lock,
)
from packages.common.safe_fs import (
    SafeFilesystemError,
    atomic_write_bytes_no_follow,
    ensure_directory_no_follow,
    open_directory_no_follow,
    read_bytes_limited_no_follow,
    verify_directory_no_follow,
)
from packages.common.state_manager import (
    MAX_STATE_SNAPSHOT_INDEX_BYTES,
    FileStateSnapshotIndexRepository,
    StateManagerError,
    publish_state_snapshot_index,
)
from packages.scheduler.registry_audit import (
    CUTOVER_GATE_MODES,
    normalize_cutover_gate_audit,
)
from scripts.publish_scheduler_file_registry import (
    CALIBRATION_OVERRIDE_NOT_SELECTED_REASON,
    CALIBRATION_OVERRIDE_PATH_ENV_NAME,
    SchedulerRegistryPublishError,
    publish_all_basin_scheduler_registry,
)
from scripts.scheduler_refresh import classification as _mod_classification
from scripts.scheduler_refresh import config as _mod_config
from scripts.scheduler_refresh import constants as _mod_constants
from scripts.scheduler_refresh import cutover_declaration as _mod_cutover_declaration
from scripts.scheduler_refresh import identity as _mod_identity
from scripts.scheduler_refresh import precommit_gate as _mod_precommit_gate
from scripts.scheduler_refresh import providers as _mod_providers
from scripts.scheduler_refresh import receipt as _mod_receipt
from scripts.scheduler_refresh import receipt_validation as _mod_receipt_validation
from scripts.scheduler_refresh import runner as _mod_runner
from scripts.scheduler_refresh.classification import (
    _CLASSIFICATION_GROUP_KEYS,
    _CLASSIFICATION_OPTIONAL_KEYS,
    _SKIP_CAUSE_KEYS,
    _SKIP_CAUSE_LIST_KEYS,
    CLASSIFICATION_MODES,
    _enforce_registry_classification_reconciliation,
    _validate_group_totals,
    _validate_object_group,
    _validate_registry_classification_field,
)
from scripts.scheduler_refresh.config import (
    EmergencySlot,
    RefreshConfig,
    _absolute_env_path,
    _apply_environment_file,
    _cleanup_run_workspace,
    _enforce_workspace_bounds,
    _ensure_private_directory,
    _env_flag,
    _iso_utc,
    _optional_absolute_env_path,
    _preflight_config,
    _provider_failure_reason,
    _ProviderRollbackRecord,
    _required_env,
    _WorkspaceBudget,
)
from scripts.scheduler_refresh.constants import (
    CALIBRATION_OVERRIDE_NOT_APPLIED_REASONS,
    CALIBRATION_OVERRIDE_REFUSAL_REASON,
    CUTOVER_CYCLE_HOURS,
    CUTOVER_DECLARATION_ENV,
    CUTOVER_FUTURE_TOLERANCE,
    CUTOVER_PAST_TOLERANCE,
    CUTOVER_REPLACE_TRANSITION_MODES,
    CUTOVER_RETIRE_TRANSITION_MODES,
    CUTOVER_SCHEMA_VERSION,
    CUTOVER_TRANSITION_MODES,
    MAX_COLLECTION_ITEMS,
    MAX_CUTOVER_DECLARATION_BYTES,
    MAX_HISTORY,
    MAX_ORPHAN_EVIDENCE,
    MAX_ORPHANS,
    MAX_RECEIPT_BYTES,
    MAX_RESIDUES,
    MAX_STRING_LENGTH,
    MAX_WORKSPACE_BYTES,
    MAX_WORKSPACE_DEPTH,
    MAX_WORKSPACE_ENTRIES,
    OUTCOMES,
    REASONS,
    RECEIPT_KEYS,
    RECEIPT_OPTIONAL_KEYS,
    REGISTRY_CUTOVER_REFUSAL_REASONS,
    REGISTRY_MANIFEST_SCHEMA_VERSION,
    SCHEMA_VERSION,
    RefreshError,
)
from scripts.scheduler_refresh.cutover_declaration import (
    _CUTOVER_DECLARATION_FORMAT_CHECKER,
    _CUTOVER_DECLARATION_SCHEMA,
    _CUTOVER_DECLARATION_SCHEMA_PATH,
    _CUTOVER_DECLARATION_VALIDATOR,
    _cutover_datetime_format_check,
    _cutover_declaration_env_resolves_to_file,
    _load_cutover_declaration,
    _load_previous_canonical_registry,
    _prospective_registry_content,
    _prospective_registry_generation,
)
from scripts.scheduler_refresh.identity import (
    _MISSING_IDENTITY,
    GENERATION_PATTERN,
    MAX_GENERATION_LENGTH,
    MAX_MODEL_ID_LENGTH,
    MODEL_ID_PATTERN,
    REGISTRY_MODEL_IDENTITY_FIELDS,
    REGISTRY_MODEL_NESTED_IDENTITY_FIELDS,
    _extract_nested_identity,
    _rows_have_identical_identity,
)
from scripts.scheduler_refresh.precommit_gate import (
    _classify_registry,
    _registry_precommit_gate,
    _RegistryClassification,
    _skip_cause_evidence,
)
from scripts.scheduler_refresh.providers import (
    _restore_provider_path,
    _restore_worker_registry_mirror,
    _rollback_provider_transaction,
    _tracked_provider_publish,
    reconstruct_primary_receipt,
    validate_current_receipt,
)
from scripts.scheduler_refresh.receipt import (
    _discard_emergency_slot,
    _finalize_emergency_slot,
    _lenient_receipt_order,
    _provider_evidence,
    _publish_primary_receipt,
    _read_provider_header,
    _receipt,
    _receipt_order,
    _reserve_emergency_slot,
    _verify_emergency_slot,
)
from scripts.scheduler_refresh.receipt_validation import (
    _CALIBRATION_OVERRIDE_BLOCK_KEYS,
    _CALIBRATION_OVERRIDE_BLOCK_REQUIRED_KEYS,
    _CALIBRATION_OVERRIDE_ENTRY_KEYS,
    _CALIBRATION_OVERRIDE_ERROR_KEYS,
    _CALIBRATION_OVERRIDE_NOT_APPLIED_KEYS,
    _CUTOVER_GATE_KEYS,
    _bounded_receipt_text,
    _calibration_entry_digest,
    _calibration_entry_list,
    _calibration_overrides_audit_from_error,
    _calibration_overrides_audit_from_summary,
    _parse_receipt_datetime,
    _receipt_bytes,
    _require_calibration_entry_strings,
    _validate_calibration_overrides_field,
    _validate_cutover_gate_field,
    _validate_receipt,
    _validate_value_bounds,
)
from scripts.scheduler_refresh.runner import refresh_scheduler_file_providers
from services.orchestrator.scheduler_file_providers import (
    MAX_READINESS_INDEX_BYTES,
    MAX_REGISTRY_MANIFEST_BYTES,
    SchedulerFileProviderError,
    capture_scheduler_provider_preimage,
    derive_catalog_bound_readiness_entries,
    publish_canonical_readiness_index,
    publish_scheduler_registry_manifest,
    validate_catalog_bound_readiness_entries,
)
from workers.model_registry.basins_calibration_overrides import (
    DEFAULT_CALIBRATION_OVERRIDES_PATH,
    CalibrationOverrideError,
)

__all__ = [
    "Any",
    "CALIBRATION_OVERRIDE_NOT_APPLIED_REASONS",
    "CALIBRATION_OVERRIDE_NOT_SELECTED_REASON",
    "CALIBRATION_OVERRIDE_PATH_ENV_NAME",
    "CALIBRATION_OVERRIDE_REFUSAL_REASON",
    "CLASSIFICATION_MODES",
    "CUTOVER_CYCLE_HOURS",
    "CUTOVER_DECLARATION_ENV",
    "CUTOVER_FUTURE_TOLERANCE",
    "CUTOVER_GATE_MODES",
    "CUTOVER_PAST_TOLERANCE",
    "CUTOVER_REPLACE_TRANSITION_MODES",
    "CUTOVER_RETIRE_TRANSITION_MODES",
    "CUTOVER_SCHEMA_VERSION",
    "CUTOVER_TRANSITION_MODES",
    "CalibrationOverrideError",
    "Callable",
    "DEFAULT_CALIBRATION_OVERRIDES_PATH",
    "EmergencySlot",
    "FileStateSnapshotIndexRepository",
    "GENERATION_PATTERN",
    "LIBPQ_CONNECTION_ENV_KEYS",
    "MAX_COLLECTION_ITEMS",
    "MAX_CUTOVER_DECLARATION_BYTES",
    "MAX_GENERATION_LENGTH",
    "MAX_HISTORY",
    "MAX_MODEL_ID_LENGTH",
    "MAX_ORPHANS",
    "MAX_ORPHAN_EVIDENCE",
    "MAX_READINESS_INDEX_BYTES",
    "MAX_RECEIPT_BYTES",
    "MAX_REGISTRY_MANIFEST_BYTES",
    "MAX_RESIDUES",
    "MAX_STATE_SNAPSHOT_INDEX_BYTES",
    "MAX_STRING_LENGTH",
    "MAX_WORKSPACE_BYTES",
    "MAX_WORKSPACE_DEPTH",
    "MAX_WORKSPACE_ENTRIES",
    "MODEL_ID_PATTERN",
    "Mapping",
    "OUTCOMES",
    "Path",
    "ProviderAtomicError",
    "ProviderPreimage",
    "REASONS",
    "RECEIPT_KEYS",
    "RECEIPT_OPTIONAL_KEYS",
    "REGISTRY_CUTOVER_REFUSAL_REASONS",
    "REGISTRY_MANIFEST_SCHEMA_VERSION",
    "REGISTRY_MODEL_IDENTITY_FIELDS",
    "REGISTRY_MODEL_NESTED_IDENTITY_FIELDS",
    "RefreshConfig",
    "RefreshError",
    "SCHEMA_VERSION",
    "SafeFilesystemError",
    "SchedulerFileProviderError",
    "SchedulerRegistryPublishError",
    "Sequence",
    "StateManagerError",
    "UTC",
    "_CALIBRATION_OVERRIDE_BLOCK_KEYS",
    "_CALIBRATION_OVERRIDE_BLOCK_REQUIRED_KEYS",
    "_CALIBRATION_OVERRIDE_ENTRY_KEYS",
    "_CALIBRATION_OVERRIDE_ERROR_KEYS",
    "_CALIBRATION_OVERRIDE_NOT_APPLIED_KEYS",
    "_CLASSIFICATION_GROUP_KEYS",
    "_CLASSIFICATION_OPTIONAL_KEYS",
    "_CUTOVER_DECLARATION_FORMAT_CHECKER",
    "_CUTOVER_DECLARATION_SCHEMA",
    "_CUTOVER_DECLARATION_SCHEMA_PATH",
    "_CUTOVER_DECLARATION_VALIDATOR",
    "_CUTOVER_GATE_KEYS",
    "_MISSING_IDENTITY",
    "_ProviderRollbackRecord",
    "_RegistryClassification",
    "_SKIP_CAUSE_KEYS",
    "_SKIP_CAUSE_LIST_KEYS",
    "_WorkspaceBudget",
    "_absolute_env_path",
    "_apply_environment_file",
    "_bounded_receipt_text",
    "_build_parser",
    "_calibration_entry_digest",
    "_calibration_entry_list",
    "_calibration_overrides_audit_from_error",
    "_calibration_overrides_audit_from_summary",
    "_classify_registry",
    "_cleanup_run_workspace",
    "_cutover_datetime_format_check",
    "_cutover_declaration_env_resolves_to_file",
    "_discard_emergency_slot",
    "_enforce_registry_classification_reconciliation",
    "_enforce_workspace_bounds",
    "_ensure_private_directory",
    "_env_flag",
    "_extract_nested_identity",
    "_finalize_emergency_slot",
    "_iso_utc",
    "_lenient_receipt_order",
    "_load_cutover_declaration",
    "_load_previous_canonical_registry",
    "_optional_absolute_env_path",
    "_parse_receipt_datetime",
    "_preflight_config",
    "_prospective_registry_content",
    "_prospective_registry_generation",
    "_provider_evidence",
    "_provider_failure_reason",
    "_publish_primary_receipt",
    "_read_provider_header",
    "_receipt",
    "_receipt_bytes",
    "_receipt_order",
    "_registry_precommit_gate",
    "_require_calibration_entry_strings",
    "_required_env",
    "_reserve_emergency_slot",
    "_restore_provider_path",
    "_restore_worker_registry_mirror",
    "_rollback_provider_transaction",
    "_rows_have_identical_identity",
    "_skip_cause_evidence",
    "_tracked_provider_publish",
    "_validate_calibration_overrides_field",
    "_validate_cutover_gate_field",
    "_validate_group_totals",
    "_validate_object_group",
    "_validate_receipt",
    "_validate_registry_classification_field",
    "_validate_value_bounds",
    "_verify_emergency_slot",
    "annotations",
    "argparse",
    "atomic_replace_provider_bytes",
    "atomic_write_bytes_no_follow",
    "capture_scheduler_provider_preimage",
    "dataclass",
    "dataclass_field",
    "datetime",
    "derive_catalog_bound_readiness_entries",
    "ensure_directory_no_follow",
    "hashlib",
    "json",
    "jsonschema",
    "main",
    "normalize_cutover_gate_audit",
    "open_directory_no_follow",
    "os",
    "provider_destination_lock",
    "publish_all_basin_scheduler_registry",
    "publish_canonical_readiness_index",
    "publish_scheduler_registry_manifest",
    "publish_state_snapshot_index",
    "re",
    "read_bytes_limited_no_follow",
    "reconstruct_primary_receipt",
    "refresh_scheduler_file_providers",
    "shutil",
    "stat",
    "sys",
    "timedelta",
    "uuid",
    "validate_catalog_bound_readiness_entries",
    "validate_current_receipt",
    "verify_directory_no_follow",
]


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry-run", action="store_true")
    operation = parser.add_mutually_exclusive_group()
    operation.add_argument("--recover-emergency", type=Path)
    operation.add_argument("--validate-current-receipt", type=Path)
    parser.add_argument("--env-file", type=Path)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    try:
        restore_environment = _apply_environment_file(args.env_file) if args.env_file is not None else None
        try:
            config = RefreshConfig.from_env()
        finally:
            if restore_environment is not None:
                restore_environment()
        if args.validate_current_receipt is not None:
            receipt = validate_current_receipt(config, args.validate_current_receipt)
        elif args.recover_emergency is not None:
            receipt = reconstruct_primary_receipt(config, args.recover_emergency)
        else:
            receipt = refresh_scheduler_file_providers(config, dry_run=args.dry_run)
    except RefreshError as error:
        print(json.dumps({"status": error.outcome, "reason": error.reason}, sort_keys=True), file=sys.stderr)
        return 2
    print(json.dumps(receipt, sort_keys=True))
    return 0 if receipt["outcome"] in {"dry_run", "published"} else 2

# --- #1099 compatibility facade -------------------------------------------
# Pre-split this file WAS the namespace: one binding per name, so
# ``monkeypatch.setattr(refresh, name, stub)`` was observed by every call site.
# Post-split the same logical binding is replicated across the package modules
# that import it, and a plain re-export here would leave those replicas stale --
# the patch would succeed, the stub would never be called, and the negative
# tests built on it would pass vacuously.  The module class below restores the
# single-namespace semantics by broadcasting every attribute write on this
# module to each package module that currently binds the SAME object under that
# name (identity-matched once, at import).  Undo restores travel the same path.
_REFRESH_PACKAGE_MODULES: tuple[ModuleType, ...] = (
    _mod_constants,
    _mod_identity,
    _mod_config,
    _mod_classification,
    _mod_receipt_validation,
    _mod_cutover_declaration,
    _mod_receipt,
    _mod_precommit_gate,
    _mod_providers,
    _mod_runner,
)


def _mirrored_bindings() -> dict[str, tuple[ModuleType, ...]]:
    """Map facade attribute -> package modules holding the identical binding."""

    bindings: dict[str, tuple[ModuleType, ...]] = {}
    for name, value in list(globals().items()):
        if name.startswith("__"):
            continue
        owners = tuple(
            module
            for module in _REFRESH_PACKAGE_MODULES
            if name in module.__dict__ and module.__dict__[name] is value
        )
        if owners:
            bindings[name] = owners
    return bindings


_MIRRORED_BINDINGS: dict[str, tuple[ModuleType, ...]] = _mirrored_bindings()


class _RefreshFacadeModule(ModuleType):
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


sys.modules[__name__].__class__ = _RefreshFacadeModule


if __name__ == "__main__":
    raise SystemExit(main())
