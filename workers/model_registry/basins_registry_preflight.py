"""Basins registry import preflight: source preparation, import-policy gates
and inventory/manifest identity, checksum and included-file validation (#2490
split of ``basins_registry_import``).

``workers.model_registry.basins_registry_import`` stays the stable import path
and re-exports every name defined here.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from packages.common.auth_policy import PolicyDecision, require_policy_evidence, trusted_internal_policy_decision

from .basins_geometry import (
    SHAPEFILE_REQUIRED_SUFFIXES,
    SHUD_CANONICAL_SUFFIXES,
    BasinsGeometryError,
    TrustedBasinsRoot,
    parse_basins_geometry,
    safe_basins_file_sha256,
    trusted_basins_root,
)
from .basins_package import SUPPORTED_BASINS_PACKAGE_SCHEMA_VERSIONS
from .basins_registry_support import (
    PUBLIC_REGISTRY_IMPORT_UNKNOWN_TARGET_ID,
    BasinsRegistryImportError,
    ImportSources,
    _ensure_under_root,
    _normalize_relative,
    _raise_geometry_import_error,
    _recorded_path_matches_expected,
    _recorded_relative_inventory_root,
    _reject_directory_symlink,
    _required_mapping_str,
    _required_model_str,
    _required_str,
    _safe_relative,
    _sha256_json,
    _slug_id,
)


def _prepare_sources(
    inventory: dict[str, Any],
    manifest: dict[str, Any],
    *,
    inventory_raw_checksum: str | None = None,
    allow_source_relocation: bool = False,
) -> ImportSources:
    model_id = _required_str(manifest, "model_id", "BASINS_REGISTRY_PACKAGE_MANIFEST_INVALID")
    model = _find_inventory_model(inventory, model_id)
    if manifest.get("schema_version") not in SUPPORTED_BASINS_PACKAGE_SCHEMA_VERSIONS:
        raise BasinsRegistryImportError(
            "BASINS_REGISTRY_PACKAGE_MANIFEST_INVALID",
            "Basins package manifest schema_version must be one of "
            + ", ".join(SUPPORTED_BASINS_PACKAGE_SCHEMA_VERSIONS)
            + ".",
            model_id=model_id,
        )
    if (
        manifest.get("package_checksum") in (None, "")
        or manifest.get("model_package_uri") in (None, "")
        or manifest.get("manifest_uri") in (None, "")
    ):
        raise BasinsRegistryImportError(
            "BASINS_REGISTRY_PACKAGE_MANIFEST_INVALID",
            "Basins package manifest must include package_checksum, model_package_uri, and manifest_uri.",
            model_id=model_id,
        )
    if model.get("status") != "valid" or model.get("default_import_eligible") is not True:
        raise BasinsRegistryImportError(
            "BASINS_REGISTRY_MODEL_NOT_IMPORTABLE",
            "Basins model is not importable from this inventory.",
            model_id=model_id,
            path=str(model.get("source_path") or ""),
            details={
                "status": model.get("status"),
                "missing_required_files": model.get("missing_required_files") or [],
                "invalid_required_files": model.get("invalid_required_files") or [],
                "unreadable_required_files": model.get("unreadable_required_files") or [],
            },
        )
    if manifest.get("basin_slug") not in (None, model.get("basin_slug")):
        raise BasinsRegistryImportError(
            "BASINS_REGISTRY_SOURCE_MISMATCH",
            "Basins package manifest basin_slug does not match inventory.",
            model_id=model_id,
        )

    inventory_root = _inventory_root(inventory, model_id)
    inventory_relative_root = _recorded_relative_inventory_root(inventory)
    source_root = _source_root(inventory_root, inventory_relative_root, model, model_id)
    input_dir = _input_dir(inventory_root, inventory_relative_root, source_root, model, model_id)
    _validate_manifest_source_identity(
        inventory,
        manifest,
        model,
        source_root,
        model_id,
        inventory_raw_checksum=inventory_raw_checksum,
        allow_source_relocation=allow_source_relocation,
    )
    _verify_model_id_matches_canonical_identity(model, model_id)
    required_files = model.get("required_files")
    if not isinstance(required_files, dict):
        raise BasinsRegistryImportError(
            "BASINS_REGISTRY_INVENTORY_INVALID",
            "Basins inventory model record is missing required_files.",
            model_id=model_id,
        )
    expected_checksums = _expected_manifest_checksums(manifest, model, model_id)
    _verify_inventory_checksums_match_manifest(expected_checksums, model, model_id)
    try:
        geometry = parse_basins_geometry(
            model_id=model_id,
            input_dir=input_dir,
            shud_input_name=_required_model_str(model, "shud_input_name", model_id),
            required_files=required_files,
            expected_checksums=expected_checksums,
        )
    except BasinsGeometryError as error:
        _raise_geometry_import_error(error, model_id)
    _validate_manifest_included_files(manifest, model, input_dir, model_id, expected_checksums=expected_checksums)
    return ImportSources(
        inventory=inventory,
        manifest=manifest,
        model=model,
        input_dir=input_dir,
        source_root=source_root,
        ids=_registry_ids(model, model_id),
        geometry=geometry,
        manifest_checksums=expected_checksums,
    )


def _require_import_policy(
    model_id: str,
    *,
    policy_decision: PolicyDecision | None = None,
    trusted_internal: bool = False,
) -> PolicyDecision:
    if trusted_internal:
        policy_decision = trusted_internal_policy_decision(
            "models.switch_version",
            target_type="model_registry",
            target_id=model_id,
            actor_id="trusted-internal:basins-registry-import",
            roles=("sys_admin",),
        )
    decision = require_policy_evidence(
        policy_decision,
        action_id="models.switch_version",
        target_type="model_registry",
        target_id=model_id,
    )
    if decision.decision != "allow":
        raise BasinsRegistryImportError(
            decision.reason_code,
            decision.reason,
            model_id=model_id,
            details={"policy_decision": decision.to_dict(), "no_mutation_expected": True},
        )
    return decision


def _require_public_import_preflight_policy(
    *,
    policy_decision: PolicyDecision | None = None,
    trusted_internal: bool = False,
) -> None:
    if trusted_internal:
        return
    decision = require_policy_evidence(
        policy_decision,
        action_id="models.switch_version",
        target_type="model_registry",
        target_id=PUBLIC_REGISTRY_IMPORT_UNKNOWN_TARGET_ID,
    )
    if decision.decision != "allow":
        raise BasinsRegistryImportError(
            decision.reason_code,
            decision.reason,
            model_id=PUBLIC_REGISTRY_IMPORT_UNKNOWN_TARGET_ID,
            details={"policy_decision": decision.to_dict(), "no_mutation_expected": True},
        )


def _find_inventory_model(inventory: dict[str, Any], model_id: str) -> dict[str, Any]:
    models = inventory.get("models")
    if not isinstance(models, list):
        raise BasinsRegistryImportError(
            "BASINS_REGISTRY_INVENTORY_INVALID",
            "Basins inventory JSON must contain a models array.",
            model_id=model_id,
        )
    matches = [model for model in models if isinstance(model, dict) and model.get("model_id") == model_id]
    if len(matches) > 1:
        raise BasinsRegistryImportError(
            "BASINS_REGISTRY_MODEL_ID_DUPLICATE",
            "Basins inventory contains duplicate records for model_id.",
            model_id=model_id,
        )
    if not matches:
        raise BasinsRegistryImportError(
            "BASINS_REGISTRY_MODEL_NOT_FOUND",
            "Basins model_id was not found in inventory.",
            model_id=model_id,
        )
    return matches[0]


def _inventory_root(inventory: dict[str, Any], model_id: str) -> Path:
    value = inventory.get("resolved_root")
    if not isinstance(value, str) or not value:
        raise BasinsRegistryImportError(
            "BASINS_REGISTRY_INVENTORY_INVALID",
            "Basins inventory is missing resolved_root.",
            model_id=model_id,
        )
    path = Path(value).expanduser().resolve()
    if not path.is_dir():
        raise BasinsRegistryImportError(
            "BASINS_REGISTRY_SOURCE_MISSING",
            "Basins inventory resolved_root does not exist.",
            model_id=model_id,
            path=str(path),
        )
    return path


def _source_root(
    inventory_root: Path,
    inventory_relative_root: Path | None,
    model: dict[str, Any],
    model_id: str,
) -> Path:
    relative = _safe_relative(_required_model_str(model, "root_relative_resolved_path", model_id), model_id)
    source_root = (inventory_root / relative).resolve()
    _ensure_under_root(source_root, inventory_root, model_id)
    recorded_value = _required_model_str(model, "resolved_source_path", model_id)
    if not _recorded_path_matches_expected(recorded_value, source_root, inventory_root, inventory_relative_root):
        raise BasinsRegistryImportError(
            "BASINS_REGISTRY_SOURCE_MISMATCH",
            "Basins inventory resolved_source_path does not match root_relative_resolved_path.",
            model_id=model_id,
            path=recorded_value,
        )
    source_path_value = _required_model_str(model, "source_path", model_id)
    if not _recorded_path_matches_expected(source_path_value, source_root, inventory_root, inventory_relative_root):
        raise BasinsRegistryImportError(
            "BASINS_REGISTRY_SOURCE_MISMATCH",
            "Basins inventory source_path does not match root_relative_resolved_path.",
            model_id=model_id,
            path=source_path_value,
        )
    if not source_root.is_dir():
        raise BasinsRegistryImportError(
            "BASINS_REGISTRY_SOURCE_MISSING",
            "Basins source directory does not exist.",
            model_id=model_id,
            path=str(source_root),
        )
    return source_root


def _input_dir(
    inventory_root: Path,
    inventory_relative_root: Path | None,
    source_root: Path,
    model: dict[str, Any],
    model_id: str,
) -> TrustedBasinsRoot:
    shud_input_name = _required_model_str(model, "shud_input_name", model_id)
    expected = source_root / "input" / shud_input_name
    for role, path in (
        ("input", source_root / "input"),
        ("shud_input_name", expected),
    ):
        _reject_directory_symlink(path, role, model_id)
    gis_dir = expected / "gis"
    if gis_dir.exists() or gis_dir.is_symlink():
        _reject_directory_symlink(gis_dir, "gis", model_id)
    final_resolved = expected.resolve()
    _ensure_under_root(final_resolved, inventory_root, model_id)
    _ensure_under_root(final_resolved, source_root, model_id)
    recorded_value = _required_model_str(model, "input_dir", model_id)
    if not _recorded_path_matches_expected(recorded_value, final_resolved, inventory_root, inventory_relative_root):
        raise BasinsRegistryImportError(
            "BASINS_REGISTRY_SOURCE_MISMATCH",
            "Basins inventory input_dir does not match canonical source root and shud_input_name.",
            model_id=model_id,
            path=recorded_value,
        )
    gis_dir_value = model.get("gis_dir")
    if isinstance(gis_dir_value, str) and gis_dir_value:
        gis_resolved = gis_dir.resolve()
        _ensure_under_root(gis_resolved, inventory_root, model_id)
        _ensure_under_root(gis_resolved, source_root, model_id)
        if not _recorded_path_matches_expected(gis_dir_value, gis_resolved, inventory_root, inventory_relative_root):
            raise BasinsRegistryImportError(
                "BASINS_REGISTRY_SOURCE_MISMATCH",
                "Basins inventory gis_dir does not match canonical source root and shud_input_name.",
                model_id=model_id,
                path=gis_dir_value,
            )
    if not expected.is_dir():
        raise BasinsRegistryImportError(
            "BASINS_REGISTRY_SOURCE_MISSING",
            "Basins input directory does not exist.",
            model_id=model_id,
            path=str(expected),
        )
    try:
        return trusted_basins_root(expected, role="shud_input_name")
    except BasinsGeometryError as error:
        _raise_geometry_import_error(error, model_id)
        raise AssertionError("unreachable") from error


def _registry_ids(model: dict[str, Any], model_id: str) -> dict[str, str]:
    suggested = model.get("suggested_ids")
    if not isinstance(suggested, dict):
        raise BasinsRegistryImportError(
            "BASINS_REGISTRY_INVENTORY_INVALID",
            "Basins inventory model record is missing suggested_ids.",
            model_id=model_id,
        )
    ids = {
        "basin_id": _required_mapping_str(suggested, "basin_id", model_id),
        "basin_version_id": _required_mapping_str(suggested, "basin_version_id", model_id),
        "river_network_version_id": _required_mapping_str(suggested, "river_network_version_id", model_id),
        "mesh_version_id": _required_mapping_str(suggested, "mesh_version_id", model_id),
        "model_id": model_id,
    }
    if suggested.get("model_id") not in (None, model_id):
        raise BasinsRegistryImportError(
            "BASINS_REGISTRY_SOURCE_MISMATCH",
            "Basins suggested model_id does not match package manifest model_id.",
            model_id=model_id,
        )
    return ids


def _validate_manifest_source_identity(
    inventory: dict[str, Any],
    manifest: dict[str, Any],
    model: dict[str, Any],
    source_root: Path,
    model_id: str,
    inventory_raw_checksum: str | None,
    allow_source_relocation: bool = False,
) -> None:
    expected = {
        "basin_slug": model.get("basin_slug"),
        "shud_input_name": model.get("shud_input_name"),
        "source_path": model.get("source_path"),
        "resolved_source_path": str(source_root),
        "source_is_symlink": bool(model.get("source_is_symlink", False)),
        "source_inventory_schema_version": inventory.get("schema_version"),
    }
    missing = [key for key in expected if key not in manifest or manifest.get(key) in (None, "")]
    # Relocation re-presents an already byte-verified historical package against
    # a freshly regenerated inventory.  The recorded absolute paths and the
    # whole-inventory checksum are workspace-scoped facts, and since #1813 the
    # inventory schema generation is too: a pre-migration manifest records the
    # generation it was published under, while the regenerated inventory carries
    # the current one.  Selected model identity stays fully validated.
    relocation_exempt_fields = {"source_path", "resolved_source_path", "source_inventory_schema_version"}
    mismatches = [
        key
        for key, expected_value in expected.items()
        if key not in missing
        and manifest.get(key) != expected_value
        and not (allow_source_relocation and key in relocation_exempt_fields)
    ]
    source_inventory_checksum = manifest.get("source_inventory_checksum")
    if not isinstance(source_inventory_checksum, str) or not source_inventory_checksum:
        missing.append("source_inventory_checksum")
    if missing or mismatches:
        raise BasinsRegistryImportError(
            "BASINS_REGISTRY_SOURCE_MISMATCH",
            "Basins package manifest source identity does not match selected inventory model.",
            model_id=model_id,
            details={"fields": sorted({*missing, *mismatches})},
        )
    actual_inventory_checksum = inventory_raw_checksum or _sha256_json(inventory)
    if not allow_source_relocation and source_inventory_checksum != actual_inventory_checksum:
        raise BasinsRegistryImportError(
            "BASINS_REGISTRY_SOURCE_MISMATCH",
            "Basins package manifest source inventory checksum does not match selected inventory.",
            model_id=model_id,
            details={
                "fields": ["source_inventory_checksum"],
                "expected": actual_inventory_checksum,
                "actual": source_inventory_checksum,
            },
        )


def _validate_manifest_included_files(
    manifest: dict[str, Any],
    model: dict[str, Any],
    input_dir: TrustedBasinsRoot,
    model_id: str,
    *,
    expected_checksums: dict[str, str],
) -> None:
    shud_input_name = _required_model_str(model, "shud_input_name", model_id)
    canonical_paths = [f"{shud_input_name}{suffix}" for suffix in SHUD_CANONICAL_SUFFIXES.values()]
    canonical_paths.extend(
        f"gis/{layer}.{suffix}"
        for layer in ("domain", "river", "seg")
        for suffix in SHAPEFILE_REQUIRED_SUFFIXES
    )
    missing = [relative_path for relative_path in canonical_paths if relative_path not in expected_checksums]
    if missing:
        raise BasinsRegistryImportError(
            "BASINS_REGISTRY_SOURCE_MISMATCH",
            "Basins package manifest is missing canonical runtime/GIS package entries.",
            model_id=model_id,
            details={"missing_included_files": missing},
        )

    conflicts: list[str] = []
    for relative_path in canonical_paths:
        manifest_sha = expected_checksums[relative_path]
        try:
            actual_sha = safe_basins_file_sha256(input_dir.path / relative_path, input_dir)
        except BasinsGeometryError as error:
            _raise_geometry_import_error(error, model_id, relative_path=relative_path)
        if actual_sha != manifest_sha:
            conflicts.append(relative_path)
    if conflicts:
        raise BasinsRegistryImportError(
            "BASINS_REGISTRY_CHECKSUM_CONFLICT",
            "Basins package manifest file checksums do not match selected inventory/source.",
            model_id=model_id,
            details={"relative_paths": sorted(set(conflicts))},
        )


def _expected_manifest_checksums(
    manifest: dict[str, Any],
    model: dict[str, Any],
    model_id: str,
) -> dict[str, str]:
    included_files = manifest.get("included_files")
    if not isinstance(included_files, list):
        raise BasinsRegistryImportError(
            "BASINS_REGISTRY_PACKAGE_MANIFEST_INVALID",
            "Basins package manifest included_files must be an array.",
            model_id=model_id,
        )
    expected: dict[str, str] = {}
    conflicts: list[str] = []
    for entry in included_files:
        if not isinstance(entry, dict) or not isinstance(entry.get("relative_path"), str):
            raise BasinsRegistryImportError(
                "BASINS_REGISTRY_PACKAGE_MANIFEST_INVALID",
                "Basins package manifest included_files entries must include relative_path.",
                model_id=model_id,
            )
        relative_path = _normalize_relative(entry["relative_path"], model_id)
        manifest_sha = entry.get("sha256")
        if not isinstance(manifest_sha, str) or not manifest_sha:
            conflicts.append(relative_path)
            continue
        expected.setdefault(relative_path, manifest_sha)

    shud_input_name = _required_model_str(model, "shud_input_name", model_id)
    canonical_paths = [f"{shud_input_name}{suffix}" for suffix in SHUD_CANONICAL_SUFFIXES.values()]
    canonical_paths.extend(
        f"gis/{layer}.{suffix}"
        for layer in ("domain", "river", "seg")
        for suffix in SHAPEFILE_REQUIRED_SUFFIXES
    )
    missing = [relative_path for relative_path in canonical_paths if relative_path not in expected]
    if missing:
        raise BasinsRegistryImportError(
            "BASINS_REGISTRY_SOURCE_MISMATCH",
            "Basins package manifest is missing canonical runtime/GIS package entries.",
            model_id=model_id,
            details={"missing_included_files": missing},
        )
    if conflicts:
        raise BasinsRegistryImportError(
            "BASINS_REGISTRY_CHECKSUM_CONFLICT",
            "Basins package manifest file checksums do not match selected inventory/source.",
            model_id=model_id,
            details={"relative_paths": sorted(set(conflicts))},
        )
    return {relative_path: expected[relative_path] for relative_path in canonical_paths}


def _verify_inventory_checksums_match_manifest(
    expected_checksums: dict[str, str],
    model: dict[str, Any],
    model_id: str,
) -> None:
    checksums = model.get("checksums")
    known_checksums = checksums if isinstance(checksums, dict) else {}
    conflicts = [
        relative_path
        for relative_path, manifest_sha in expected_checksums.items()
        if isinstance(known_checksums.get(relative_path), str)
        and known_checksums[relative_path]
        and known_checksums[relative_path] != manifest_sha
    ]
    if conflicts:
        raise BasinsRegistryImportError(
            "BASINS_REGISTRY_CHECKSUM_CONFLICT",
            "Basins package manifest file checksums do not match selected inventory/source.",
            model_id=model_id,
            details={"relative_paths": sorted(set(conflicts))},
        )


def _verify_model_id_matches_canonical_identity(model: dict[str, Any], model_id: str) -> None:
    basin_slug = _required_model_str(model, "basin_slug", model_id)
    root_relative = model.get("root_relative_resolved_path") or model.get("root_relative_path")
    if not isinstance(root_relative, str) or not root_relative:
        raise BasinsRegistryImportError(
            "BASINS_REGISTRY_INVENTORY_INVALID",
            "Basins inventory model record is missing root-relative source path.",
            model_id=model_id,
        )
    canonical_slug = _normalize_relative(root_relative, model_id)
    expected_model_id = f"basins_{_slug_id(canonical_slug)}_shud"
    suggested = model.get("suggested_ids")
    suggested_model_id = suggested.get("model_id") if isinstance(suggested, dict) else None
    if (
        basin_slug != canonical_slug
        or model.get("model_id") != expected_model_id
        or suggested_model_id != expected_model_id
    ):
        raise BasinsRegistryImportError(
            "BASINS_REGISTRY_SOURCE_MISMATCH",
            "Basins inventory model_id does not match canonical source identity.",
            model_id=model_id,
        )
