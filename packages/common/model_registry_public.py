"""Model registry public-payload shaping: list/detail sanitizers, audit
lineage details, public projections and the recursive sensitive-key JSON
redaction (#2617 split of ``packages.common.model_registry``).

``packages.common.model_registry`` stays the stable import path and re-exports
every name defined here.
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping, Sequence
from pathlib import PurePosixPath
from typing import Any
from urllib.parse import urlsplit, urlunsplit

from packages.common.model_registry_contracts import RiverSegmentGeoJsonBudgetError, _json_mapping


def sanitize_model_list_payload(payload: Mapping[str, Any]) -> dict[str, Any]:
    result = dict(payload)
    result["items"] = [_model_public_projection(item) for item in list(result.get("items") or [])]
    return result


def sanitize_model_detail_payload(payload: Mapping[str, Any]) -> dict[str, Any]:
    return _model_asset_detail(payload)


def sanitize_basin_version_list_payload(payload: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    return [_basin_version_public_projection(item) for item in payload]


BASINS_AUDIT_LINEAGE_KEYS = (
    "basin_slug",
    "shud_input_name",
    "manifest_uri",
    "package_checksum",
    "source_inventory_checksum",
)
BASINS_AUDIT_LINEAGE_URI_KEYS = frozenset({"manifest_uri"})


def _sanitize_audit_uri(value: Any) -> str | None:
    if value in (None, ""):
        return None
    parsed = urlsplit(str(value))
    netloc = parsed.netloc.rsplit("@", 1)[-1]
    return urlunsplit((parsed.scheme, netloc, parsed.path, "", ""))


def _is_uri_like(value: Any) -> bool:
    parsed = urlsplit(str(value))
    return bool(parsed.scheme or parsed.netloc)


def _basins_lineage_details(resource_profile: Any) -> dict[str, Any]:
    if isinstance(resource_profile, str):
        try:
            resource_profile = json.loads(resource_profile)
        except json.JSONDecodeError:
            return {}
    if not isinstance(resource_profile, Mapping):
        return {}
    details: dict[str, Any] = {}
    for key in BASINS_AUDIT_LINEAGE_KEYS:
        value = resource_profile.get(key)
        if value in (None, ""):
            continue
        details[key] = _sanitize_audit_uri(value) if key in BASINS_AUDIT_LINEAGE_URI_KEYS else value
    return details


MODEL_ASSET_LINEAGE_KEYS = (
    "manifest_uri",
    "source_inventory_checksum",
    "basin_slug",
    "shud_input_name",
    "package_checksum",
    "source_path",
    "resolved_source_path",
    "source_uri",
    "source_is_symlink",
)
MODEL_ASSET_URI_KEYS = frozenset(
    {
        "manifest_uri",
        "mesh_uri",
        "model_package_uri",
        "source_uri",
    }
)
MODEL_ASSET_URI_OR_PATH_KEYS = frozenset({"source_path", "resolved_source_path"})
PUBLIC_SENSITIVE_PATH_KEYS = frozenset(
    {
        "artifact_path",
        "copied_root",
        "copied_root_uri",
        "local_path",
        "local_root",
        "package_path",
        "path",
        "resolved_source_path",
        "root",
        "source_path",
        "source_root",
        "source_uri",
        "uri",
        "url",
    }
)
PUBLIC_SENSITIVE_DIGEST_KEYS = frozenset(
    {
        "checksum",
        "digest",
        "hash",
        "md5",
        "package_checksum",
        "sha",
        "sha1",
        "sha224",
        "sha256",
        "sha384",
        "sha512",
        "source_inventory_checksum",
        "stored_manifest_package_checksum",
    }
)
REDACTED_REASON = "[redacted]"
SUPPORTED_OBJECT_URI_SCHEMES = frozenset({"s3", "az", "gs", "https", "http", "integration", "memory"})
PUBLIC_JSON_SANITIZE_MAX_DEPTH = 24
PUBLIC_JSON_SANITIZE_MAX_NODES = 5000


def _model_asset_detail(row: Mapping[str, Any]) -> dict[str, Any]:
    detail = dict(row)
    resource_profile = _json_mapping(detail.get("resource_profile"))
    mesh_properties = _json_mapping(detail.pop("mesh_properties_json", None))
    detail["resource_profile"] = _sanitize_public_json_value(resource_profile)
    detail["lifecycle_state"] = str(
        detail.get("lifecycle_state") or ("active" if detail.get("active_flag") else "inactive")
    )

    for key in MODEL_ASSET_LINEAGE_KEYS:
        detail[key] = _first_non_empty(resource_profile.get(key), mesh_properties.get(key), detail.get(key))
    for key in MODEL_ASSET_URI_KEYS:
        if detail.get(key) not in (None, ""):
            detail[key] = _sanitize_public_json_value(detail[key])
    for key in MODEL_ASSET_URI_OR_PATH_KEYS:
        if detail.get(key) not in (None, ""):
            detail[key] = _sanitize_public_json_value(detail[key])

    model_name = _first_non_empty(
        resource_profile.get("model_name"),
        resource_profile.get("shud_input_name"),
        detail.get("model_name"),
        detail.get("model_id"),
    )
    detail["model_name"] = str(model_name) if model_name is not None else None
    detail["segment_count"] = int(detail["segment_count"]) if detail.get("segment_count") is not None else None
    for key in (
        "package_checksum",
        "source_inventory_checksum",
        "mesh_checksum",
        "basin_checksum",
        "river_network_checksum",
    ):
        if key in detail:
            detail[key] = None
    return detail


def _river_segment_detail(row: Mapping[str, Any]) -> dict[str, Any]:
    detail = dict(row)
    detail["river_segment_id"] = str(detail["river_segment_id"])
    detail["river_network_version_id"] = str(detail["river_network_version_id"])
    detail["segment_order"] = int(detail["segment_order"]) if detail.get("segment_order") is not None else None
    detail["length_m"] = float(detail["length_m"]) if detail.get("length_m") is not None else None
    detail["properties_json"] = _json_mapping(detail.get("properties_json"))
    return detail


def _enforce_river_segment_serialized_budget(payload: Mapping[str, Any], *, max_bytes: int, scope: str) -> None:
    serialized_bytes = len(json.dumps(payload, separators=(",", ":"), default=str).encode("utf-8"))
    if serialized_bytes > max_bytes:
        raise RiverSegmentGeoJsonBudgetError(
            limit_type="serialized_bytes",
            max_bytes=max_bytes,
            serialized_bytes=serialized_bytes,
            scope=scope,
        )


def _model_public_projection(row: Mapping[str, Any]) -> dict[str, Any]:
    detail = dict(row)
    # #2038: lifecycle rows alias ``mv.properties_json AS mesh_properties_json``
    # (raw source paths and package checksums).  Mirror ``_model_asset_detail``
    # and drop it; ``preflight.lineage.mesh_properties`` reads the raw row and
    # carries its own audit redaction.
    detail.pop("mesh_properties_json", None)
    detail["resource_profile"] = _sanitize_public_json_value(_json_mapping(detail.get("resource_profile")))
    detail["lifecycle_state"] = str(
        detail.get("lifecycle_state") or ("active" if detail.get("active_flag") else "inactive")
    )
    for key in MODEL_ASSET_URI_KEYS:
        if detail.get(key) not in (None, ""):
            detail[key] = _sanitize_public_json_value(detail[key])
    for key in MODEL_ASSET_URI_OR_PATH_KEYS:
        if detail.get(key) not in (None, ""):
            detail[key] = _sanitize_public_json_value(detail[key])
    for key in (
        "package_checksum",
        "source_inventory_checksum",
        "mesh_checksum",
        "basin_checksum",
        "river_network_checksum",
    ):
        if key in detail:
            detail[key] = None
    return detail


def _basin_version_public_projection(row: Mapping[str, Any]) -> dict[str, Any]:
    detail = dict(row)
    if "source_uri" in detail:
        detail["source_uri"] = None
    if "checksum" in detail:
        detail["checksum"] = None
    return detail


def _sanitize_public_json_value(
    value: Any,
    *,
    _depth: int = 0,
    _state: dict[str, Any] | None = None,
) -> Any:
    state = _state or {"nodes": 0, "seen": set()}
    state["nodes"] += 1
    if _depth > PUBLIC_JSON_SANITIZE_MAX_DEPTH or state["nodes"] > PUBLIC_JSON_SANITIZE_MAX_NODES:
        return None

    if isinstance(value, Mapping):
        object_id = id(value)
        if object_id in state["seen"]:
            return None
        state["seen"].add(object_id)
        sanitized: dict[str, Any] = {}
        for key, child in value.items():
            if state["nodes"] >= PUBLIC_JSON_SANITIZE_MAX_NODES:
                break
            if _is_sensitive_public_json_key(key):
                sanitized[key] = None
            elif (
                _is_sensitive_public_path_key(key)
                and isinstance(child, str)
                and _is_public_sensitive_path_or_file_uri(child)
            ):
                sanitized[key] = None
            else:
                sanitized[key] = _sanitize_public_json_value(child, _depth=_depth + 1, _state=state)
        state["seen"].remove(object_id)
        return sanitized
    if isinstance(value, list | tuple):
        object_id = id(value)
        if object_id in state["seen"]:
            return None
        state["seen"].add(object_id)
        sanitized_list = []
        for child in value:
            if state["nodes"] >= PUBLIC_JSON_SANITIZE_MAX_NODES:
                break
            sanitized_list.append(_sanitize_public_json_value(child, _depth=_depth + 1, _state=state))
        state["seen"].remove(object_id)
        return sanitized_list
    if isinstance(value, str):
        if _is_public_sensitive_path_or_file_uri(value):
            return None
        if _is_uri_like(value):
            return _sanitize_audit_uri(value)
        return value
    if value is None or isinstance(value, bool | int | float):
        return value
    return None
    return value


def _is_sensitive_public_json_key(key: str) -> bool:
    lowered = key.lower()
    if lowered in PUBLIC_SENSITIVE_DIGEST_KEYS:
        return True
    return (
        lowered.endswith("_checksum")
        or lowered.endswith("checksum")
        or lowered.endswith("_hash")
        or lowered.endswith("_digest")
    )


def _is_sensitive_public_path_key(key: str) -> bool:
    lowered = key.lower()
    return lowered in PUBLIC_SENSITIVE_PATH_KEYS or lowered.endswith("_path") or lowered.endswith("_root")


def _is_public_sensitive_path_or_file_uri(value: str) -> bool:
    parsed = urlsplit(value)
    scheme = parsed.scheme.lower()
    if scheme in SUPPORTED_OBJECT_URI_SCHEMES:
        return False
    if scheme == "file":
        return True
    if re.match(r"^[a-zA-Z]:[\\/]", value):
        return True
    if value.startswith("\\\\"):
        return True
    normalized = (parsed.path if parsed.scheme else value).replace("\\", "/")
    if not parsed.scheme and not parsed.netloc and normalized.startswith("/"):
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


def _first_non_empty(*values: Any) -> Any:
    for value in values:
        if value not in (None, ""):
            return value
    return None
