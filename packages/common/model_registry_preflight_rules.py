"""Model lifecycle preflight rules: blocker/warning records, activation
safety evidence, transition and source-root checks, and the audit references
used by the lifecycle preflight (#2617 split of
``packages.common.model_registry``).

``packages.common.model_registry`` stays the stable import path and re-exports
every name defined here.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from pathlib import PurePosixPath
from typing import Any
from urllib.parse import urlsplit

from packages.common.model_registry_contracts import (
    MODEL_LIFECYCLE_STATES,
    ModelLifecycleOperation,
    ModelLifecycleState,
    _json_mapping,
)
from packages.common.model_registry_public import (
    SUPPORTED_OBJECT_URI_SCHEMES,
    _first_non_empty,
    _model_public_projection,
)


def _preflight_blocker(code: str, message: str) -> dict[str, str]:
    return {"code": code, "message": message}


def _lifecycle_audit_persistence_failure_result(
    *,
    model: Mapping[str, Any],
    current_active: Mapping[str, Any] | None,
    operation: ModelLifecycleOperation,
    preflight: Mapping[str, Any],
) -> dict[str, Any]:
    blocked_preflight = dict(preflight)
    blocked_preflight["status"] = "blocked"
    blocked_preflight["blockers"] = [
        *list(preflight.get("blockers") or []),
        _preflight_blocker(
            "LIFECYCLE_AUDIT_PERSISTENCE_FAILED",
            "Lifecycle audit evidence could not be persisted; mutation was rolled back.",
        ),
    ]
    return {
        "status": "blocked",
        "operation": operation,
        "model": _model_public_projection(model),
        "previous_model": _model_public_projection(current_active) if current_active is not None else None,
        "preflight": blocked_preflight,
        "audit_reference": None,
    }


def _preflight_warning(code: str, message: str) -> dict[str, str]:
    return {"code": code, "message": message}


def _apply_idempotent_rollback_preflight(preflight: dict[str, Any], history: Mapping[str, Any]) -> None:
    preflight["status"] = "ready"
    preflight["blockers"] = []
    preflight["warnings"] = [
        *list(preflight.get("warnings") or []),
        {
            "code": "ROLLBACK_ALREADY_CURRENT",
            "message": "Rollback retry is already reflected by the current active model.",
        },
    ]
    preflight["prior_audit_log_id"] = history.get("prior_audit_log_id")
    preflight["rollback_history"] = _rollback_history_preflight_reference(history)


def _activation_safety_evidence(
    model: Mapping[str, Any],
    *,
    activation_class_operation: bool,
) -> tuple[list[dict[str, str]], list[dict[str, str]], str, Any]:
    resource_profile = _json_mapping(model.get("resource_profile"))
    blockers: list[dict[str, str]] = []
    warnings: list[dict[str, str]] = []
    if model.get("river_network_version_id") and model.get("basin_version_id") is None:
        blockers.append(_preflight_blocker("LINEAGE_MISSING_BASIN_VERSION", "Model lineage is missing basin version."))
    if model.get("mesh_version_id") in (None, ""):
        blockers.append(_preflight_blocker("LINEAGE_MISSING_MESH_VERSION", "Model lineage is missing mesh version."))
    if model.get("model_package_uri") in (None, ""):
        blockers.append(_preflight_blocker("PACKAGE_URI_MISSING", "Model package URI is missing."))
    package_checksum = _first_non_empty(resource_profile.get("package_checksum"), model.get("package_checksum"))
    if package_checksum in (None, ""):
        evidence = _preflight_blocker if activation_class_operation else _preflight_warning
        (blockers if activation_class_operation else warnings).append(
            evidence("PACKAGE_CHECKSUM_MISSING", "Package checksum evidence is not available.")
        )
    elif (
        activation_class_operation
        and _package_checksum_verification_status(resource_profile, package_checksum) == "blocked"
    ):
        blockers.append(
            _preflight_blocker(
                "PACKAGE_CHECKSUM_UNVERIFIED",
                "Package checksum evidence could not be reread from stored package evidence.",
            )
        )
    if _object_uri_prefix_status(model.get("model_package_uri")) == "invalid":
        blockers.append(_preflight_blocker("OBJECT_URI_PREFIX_INVALID", "Model package URI prefix is not supported."))
    copied_root = _copied_root_status(resource_profile)
    if copied_root == "unsafe":
        blockers.append(_preflight_blocker("COPIED_ROOT_UNSAFE", "Copied-root source evidence is unsafe."))
    elif copied_root == "missing":
        warnings.append(_preflight_warning("COPIED_ROOT_EVIDENCE_MISSING", "Copied-root evidence is not available."))
    if activation_class_operation and _has_unsafe_source_root(resource_profile):
        blockers.append(
            _preflight_blocker("SOURCE_ROOT_UNSAFE", "Model source root evidence points to an unsafe local source.")
        )
    return blockers, warnings, copied_root, package_checksum


def _object_uri_prefix_status(value: Any) -> str:
    if value in (None, ""):
        return "missing"
    parsed = urlsplit(str(value))
    if parsed.scheme in SUPPORTED_OBJECT_URI_SCHEMES:
        return "valid"
    return "invalid"


def _copied_root_status(resource_profile: Mapping[str, Any]) -> str:
    copied_root = _first_non_empty(
        resource_profile.get("copied_root"),
        resource_profile.get("copied_root_uri"),
        resource_profile.get("copied_root_status"),
    )
    if copied_root in (None, ""):
        return "missing"
    if str(resource_profile.get("source_is_symlink", "")).lower() == "true":
        return "unsafe"
    text = str(copied_root)
    if text.lower() in {"unsafe", "symlink", "raw", "local"}:
        return "unsafe"
    if text.lower() in {"present", "safe", "copied", "verified"}:
        return "present"
    if text.startswith("/") or re.match(r"^[A-Za-z]:[\\/]", text):
        return "unsafe"
    return "present"


def _canonical_lifecycle_state(model: Mapping[str, Any]) -> ModelLifecycleState:
    state = str(model.get("lifecycle_state") or ("active" if model.get("active_flag") else "inactive"))
    if state in MODEL_LIFECYCLE_STATES:
        return state  # type: ignore[return-value]
    return "active" if model.get("active_flag") else "inactive"


def _transition_blocker(
    *,
    operation: ModelLifecycleOperation,
    lifecycle_state: ModelLifecycleState,
    model_id: str,
    current_active_id: str | None,
) -> dict[str, str] | None:
    if operation == "activate":
        if lifecycle_state == "active" and current_active_id == model_id:
            return None
        if lifecycle_state not in {"inactive", "deprecated", "superseded"}:
            return _preflight_blocker("INVALID_TRANSITION", f"activate is not allowed from {lifecycle_state}.")
    elif operation == "switch_version":
        if lifecycle_state == "active" and current_active_id == model_id:
            return None
        if lifecycle_state not in {"inactive", "deprecated", "superseded"}:
            return _preflight_blocker("INVALID_TRANSITION", f"switch_version is not allowed from {lifecycle_state}.")
    elif operation == "deactivate":
        if lifecycle_state not in {"active", "inactive"}:
            return _preflight_blocker("INVALID_TRANSITION", f"deactivate is not allowed from {lifecycle_state}.")
    elif operation == "supersede":
        if lifecycle_state not in {"active", "inactive", "deprecated", "superseded"}:
            return _preflight_blocker("INVALID_TRANSITION", f"supersede is not allowed from {lifecycle_state}.")
    elif operation == "deprecate":
        if lifecycle_state not in {"inactive", "superseded", "deprecated"}:
            return _preflight_blocker("INVALID_TRANSITION", f"deprecate is not allowed from {lifecycle_state}.")
    return None


def _package_checksum_verification_status(resource_profile: Mapping[str, Any], package_checksum: Any) -> str:
    verification_fields = (
        resource_profile.get("package_checksum_confirmed_from_stored_manifest"),
        resource_profile.get("package_checksum_verified"),
        resource_profile.get("checksum_reread_verified"),
    )
    if any(value is True for value in verification_fields):
        return "verified"
    if any(value is False for value in verification_fields):
        return "blocked"
    for key in (
        "package_checksum_reread_status",
        "package_checksum_reconstruction_status",
        "checksum_reread_status",
    ):
        value = resource_profile.get(key)
        if value in (None, ""):
            continue
        if str(value).lower() in {"verified", "ready", "ok", "confirmed"}:
            return "verified"
        if str(value).lower() in {"blocked", "failed", "unreadable", "missing", "limited", "mismatch"}:
            return "blocked"
    if resource_profile.get("stored_manifest_package_checksum") not in (None, ""):
        return "verified" if resource_profile.get("stored_manifest_package_checksum") == package_checksum else "blocked"
    if resource_profile.get("manifest_uri") not in (None, ""):
        return "verified"
    return "blocked"


def _has_unsafe_source_root(resource_profile: Mapping[str, Any]) -> bool:
    for value in _iter_source_evidence_values(resource_profile):
        if _is_unsafe_source_value(value):
            return True
    return False


def _iter_source_evidence_values(value: Any) -> list[Any]:
    values: list[Any] = []
    if isinstance(value, Mapping):
        for key, child in value.items():
            if key in {"source_path", "resolved_source_path", "source_uri", "root", "source_root"}:
                values.append(child)
            if isinstance(child, (Mapping, list, tuple)):
                values.extend(_iter_source_evidence_values(child))
    elif isinstance(value, list | tuple):
        for child in value:
            values.extend(_iter_source_evidence_values(child))
    return values


def _is_unsafe_source_value(value: Any) -> bool:
    if value in (None, ""):
        return False
    text = str(value)
    parsed = urlsplit(text)
    scheme = parsed.scheme.lower()
    if scheme == "file":
        return True
    if scheme in SUPPORTED_OBJECT_URI_SCHEMES:
        return False
    path_text = parsed.path if scheme else text
    normalized = path_text.replace("\\", "/")
    if normalized.startswith("/") or re.match(r"^[A-Za-z]:[\\/]", text) or text.startswith("\\\\"):
        return True
    if normalized.startswith("/volume/"):
        return True
    parts = [part for part in PurePosixPath(normalized).parts if part not in {"/", ""}]
    for index, part in enumerate(parts):
        if part == "data" and index + 1 < len(parts) and parts[index + 1] == "Basins":
            return True
        if part == "Basins" and index > 0 and parts[index - 1] == "data":
            return True
    return False


def _model_audit_reference(model: Mapping[str, Any] | None) -> dict[str, Any] | None:
    if model is None:
        return None
    return {
        "model_id": model.get("model_id"),
        "basin_version_id": model.get("basin_version_id"),
        "lifecycle_state": model.get("lifecycle_state"),
        "active_flag": bool(model.get("active_flag")),
    }


def _rollback_history_preflight_reference(history: Mapping[str, Any] | None) -> dict[str, Any] | None:
    if history is None:
        return None
    return {
        "prior_audit_log_id": history.get("prior_audit_log_id") or history.get("log_id"),
        "action": history.get("action"),
        "entity_id": history.get("entity_id"),
        "trusted": bool(history.get("trusted")),
        "matched_previous_model_id": history.get("matched_previous_model_id"),
        "stale_reason": history.get("stale_reason"),
    }
