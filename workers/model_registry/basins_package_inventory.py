from __future__ import annotations

import json
from collections.abc import Callable, Iterator
from fnmatch import fnmatchcase
from pathlib import Path
from typing import Any

from packages.common.object_store import LocalObjectStore

from .basins_discovery import GIS_REQUIRED_FILES, SHUD_REQUIRED_PATTERNS
from .basins_discovery import (
    _slug_id as _basins_slug_id,
)
from .basins_package_contracts import (
    BASINS_PACKAGE_SCHEMA_VERSION,
    BASINS_PACKAGE_SOURCE_IDENTITY_SCHEMA_VERSION,
    BasinsPackageError,
    SourceFile,
    _forcing_checksum_material,
    _sha256_json,
)
from .basins_package_source_io import (
    _ensure_under_root,
    _ensure_under_source_root,
    _normalize_relative_path,
    _reject_source_symlink_path,
    _resolve_package_path,
    _safe_source_dir,
    _safe_source_file,
    _source_file_evidence,
)


def _read_inventory(path: str | Path) -> tuple[dict[str, Any], bytes]:
    inventory_path = Path(path).expanduser()
    try:
        content = inventory_path.read_bytes()
        payload = json.loads(content.decode("utf-8"))
    except OSError as error:
        raise BasinsPackageError(
            "BASINS_INVENTORY_NOT_FOUND",
            f"Basins inventory cannot be read: {inventory_path}",
            path=str(inventory_path),
        ) from error
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise BasinsPackageError(
            "BASINS_INVENTORY_INVALID",
            f"Basins inventory is not valid JSON: {inventory_path}",
            path=str(inventory_path),
        ) from error
    if not isinstance(payload, dict):
        raise BasinsPackageError(
            "BASINS_INVENTORY_INVALID",
            "Basins inventory JSON must be an object.",
            path=str(inventory_path),
        )
    return payload, content

def _find_publishable_model(inventory: dict[str, Any], model_id: str, version: str) -> dict[str, Any]:
    models = inventory.get("models")
    if not isinstance(models, list):
        raise BasinsPackageError(
            "BASINS_INVENTORY_INVALID",
            "Basins inventory JSON must contain a models array.",
            model_id=model_id,
            version=version,
        )
    matches = [model for model in models if isinstance(model, dict) and model.get("model_id") == model_id]
    if len(matches) > 1:
        raise BasinsPackageError(
            "BASINS_MODEL_ID_DUPLICATE",
            "Basins inventory contains duplicate records for the requested model_id.",
            model_id=model_id,
            version=version,
        )
    if matches:
        model = matches[0]
        _verify_model_id_matches_canonical_identity(model, model_id, version)
        if model.get("status") != "valid" or model.get("default_publish_eligible") is not True:
            raise BasinsPackageError(
                "BASINS_MODEL_NOT_PUBLISHABLE",
                "Basins model is not publishable from this inventory.",
                model_id=model_id,
                version=version,
                path=str(model.get("source_path") or ""),
                details={
                    "status": model.get("status"),
                    "missing_required_files": model.get("missing_required_files") or [],
                    "invalid_required_files": model.get("invalid_required_files") or [],
                    "unreadable_required_files": model.get("unreadable_required_files") or [],
                },
            )
        return model
    raise BasinsPackageError(
        "BASINS_MODEL_NOT_FOUND",
        "Basins model_id was not found in inventory.",
        model_id=model_id,
        version=version,
    )

def _verify_model_id_matches_canonical_identity(model: dict[str, Any], model_id: str, version: str) -> None:
    basin_slug = model.get("basin_slug")
    suggested_ids = model.get("suggested_ids")
    suggested_model_id = suggested_ids.get("model_id") if isinstance(suggested_ids, dict) else None
    if not isinstance(basin_slug, str) or not basin_slug:
        raise BasinsPackageError(
            "BASINS_INVENTORY_INVALID",
            "Basins model record is missing basin_slug.",
            model_id=model_id,
            version=version,
            path=str(model.get("source_path") or ""),
        )

    canonical_basin_slug = _canonical_basin_slug_from_source_path(model, model_id, version)
    expected_model_id = f"basins_{_basins_slug_id(canonical_basin_slug)}_shud"
    if (
        basin_slug != canonical_basin_slug
        or model.get("model_id") != expected_model_id
        or model_id != expected_model_id
        or suggested_model_id != expected_model_id
    ):
        raise BasinsPackageError(
            "BASINS_MODEL_ID_MISMATCH",
            "Basins inventory model_id does not match the selected model's canonical source identity.",
            model_id=model_id,
            version=version,
            path=str(model.get("source_path") or ""),
        )

def _canonical_basin_slug_from_source_path(model: dict[str, Any], model_id: str, version: str) -> str:
    root_relative = model.get("root_relative_resolved_path") or model.get("root_relative_path")
    if not isinstance(root_relative, str) or not root_relative:
        raise BasinsPackageError(
            "BASINS_INVENTORY_INVALID",
            "Basins model record is missing root-relative source path.",
            model_id=model_id,
            version=version,
            path=str(model.get("source_path") or ""),
        )
    try:
        canonical_slug = _normalize_relative_path(root_relative)
    except BasinsPackageError as error:
        raise BasinsPackageError(
            "BASINS_PACKAGE_PATH_UNSAFE",
            "Basins model root-relative source path is unsafe.",
            model_id=model_id,
            version=version,
            path=root_relative,
        ) from error
    if Path(canonical_slug).is_absolute() or ".." in Path(canonical_slug).parts:
        raise BasinsPackageError(
            "BASINS_PACKAGE_PATH_UNSAFE",
            "Basins model root-relative source path is unsafe.",
            model_id=model_id,
            version=version,
            path=root_relative,
        )
    return canonical_slug

def _resolved_inventory_root(inventory: dict[str, Any], model_id: str, version: str) -> Path:
    resolved = inventory.get("resolved_root")
    if not isinstance(resolved, str) or not resolved:
        raise BasinsPackageError(
            "BASINS_INVENTORY_INVALID",
            "Basins inventory is missing resolved_root.",
            model_id=model_id,
            version=version,
        )
    inventory_root = _resolve_package_path(Path(resolved).expanduser(), model_id=model_id, version=version)
    if not inventory_root.is_dir():
        raise BasinsPackageError(
            "BASINS_SOURCE_NOT_FOUND",
            f"Basins inventory root directory does not exist: {inventory_root}",
            model_id=model_id,
            version=version,
            path=str(inventory_root),
        )
    return inventory_root

def _recorded_relative_inventory_root(inventory: dict[str, Any]) -> Path | None:
    root = inventory.get("root")
    if not isinstance(root, str) or not root:
        return None
    root_path = Path(root).expanduser()
    if root_path.is_absolute():
        return None
    try:
        normalized = Path(_normalize_relative_path(root_path.as_posix()))
    except BasinsPackageError:
        return None
    if normalized == Path("."):
        return None
    return normalized

def _resolved_source_root(model: dict[str, Any], inventory_root: Path, model_id: str, version: str) -> Path:
    root_relative = model.get("root_relative_resolved_path") or model.get("root_relative_path")
    if not isinstance(root_relative, str) or not root_relative:
        raise BasinsPackageError(
            "BASINS_INVENTORY_INVALID",
            "Basins model record is missing root-relative source path.",
            model_id=model_id,
            version=version,
            path=str(model.get("source_path") or ""),
        )
    root_relative_path = Path(root_relative)
    if root_relative_path.is_absolute() or ".." in root_relative_path.parts:
        raise BasinsPackageError(
            "BASINS_PACKAGE_PATH_UNSAFE",
            "Basins model root-relative source path is unsafe.",
            model_id=model_id,
            version=version,
            path=root_relative,
        )

    source_root = _resolve_package_path(inventory_root / root_relative_path, model_id=model_id, version=version)
    _ensure_under_root(
        source_root,
        inventory_root,
        error_code="BASINS_PACKAGE_PATH_UNSAFE",
        message="Basins model source path resolves outside the inventory root.",
        model_id=model_id,
        version=version,
    )

    resolved = model.get("resolved_source_path")
    if not isinstance(resolved, str) or not resolved:
        raise BasinsPackageError(
            "BASINS_INVENTORY_INVALID",
            "Basins model record is missing resolved_source_path.",
            model_id=model_id,
            version=version,
            path=str(model.get("source_path") or ""),
        )
    recorded_source_root = _resolve_package_path(Path(resolved).expanduser(), model_id=model_id, version=version)
    if recorded_source_root != source_root:
        raise BasinsPackageError(
            "BASINS_INVENTORY_PATH_MISMATCH",
            "Basins inventory model source path does not match its inventory root-relative path.",
            model_id=model_id,
            version=version,
            path=str(recorded_source_root),
        )
    if not source_root.is_dir():
        raise BasinsPackageError(
            "BASINS_SOURCE_NOT_FOUND",
            f"Basins model source directory does not exist: {source_root}",
            model_id=model_id,
            version=version,
            path=str(source_root),
        )
    return source_root

def _optional_shud_runtime_files(
    input_dir: Path,
    source_root: Path,
    object_store: LocalObjectStore | None,
    package_key: str,
    *,
    model_id: str,
    version: str,
    manifest_uri: str | None,
) -> list[SourceFile]:
    optional_names = (
        f"{input_dir.name}.lake.sp",
        f"{input_dir.name}.lake.bathy",
        f"{input_dir.name}.lake.ic",
    )
    files: list[SourceFile] = []
    for relative_path in optional_names:
        candidate = input_dir / relative_path
        if not candidate.exists():
            continue
        source_path = _safe_source_file(
            candidate,
            source_root,
            model_id=model_id,
            version=version,
            manifest_uri=manifest_uri,
        )
        files.append(
            _source_file_for_package(
                source_path,
                relative_path,
                object_store,
                package_key,
                source_root=source_root,
                role="runtime_input",
            )
        )
    return files

def _validated_canonical_required_source_files(
    required_files: dict[str, Any],
    input_dir: Path,
    source_root: Path,
    object_store: LocalObjectStore | None,
    package_key: str,
    *,
    model_id: str,
    version: str,
    manifest_uri: str | None,
) -> list[SourceFile]:
    missing: list[str] = []
    extras: list[str] = []
    files: list[SourceFile] = []
    canonical_roles = {role for role, _ in SHUD_REQUIRED_PATTERNS} | {role for role, _ in GIS_REQUIRED_FILES}
    direct_same_pattern_extras: list[str] = []

    for role, pattern in SHUD_REQUIRED_PATTERNS:
        relative_names = required_files.get(role)
        if not isinstance(relative_names, list) or not relative_names:
            missing.append(role)
            continue
        expected_path = _canonical_shud_required_file_name(input_dir.name, pattern)
        normalized_names = [_normalize_relative_path(str(name)) for name in relative_names]
        expected_count = normalized_names.count(expected_path)
        role_extras = [name for name in normalized_names if name != expected_path]
        extras.extend(f"{role}:{name}" for name in role_extras)
        direct_same_pattern_extras.extend(
            f"{role}:{name}"
            for name in role_extras
            if len(Path(name).parts) == 1 and fnmatchcase(name, pattern)
        )
        if expected_count == 0:
            missing.append(role)
            continue
        if expected_count > 1:
            duplicate_extras = [expected_path for _ in range(expected_count - 1)]
            extras.extend(f"{role}:{name}" for name in duplicate_extras)
            direct_same_pattern_extras.extend(f"{role}:{name}" for name in duplicate_extras)
        source_path = _safe_source_file(
            input_dir / expected_path,
            source_root,
            model_id=model_id,
            version=version,
            manifest_uri=manifest_uri,
        )
        files.append(
            _source_file_for_package(
                source_path,
                expected_path,
                object_store,
                package_key,
                source_root=source_root,
                role="runtime_input",
            )
        )

    for role, file_name in GIS_REQUIRED_FILES:
        expected_path = _normalize_relative_path(f"gis/{file_name}")
        relative_names = required_files.get(role)
        if not isinstance(relative_names, list) or not relative_names:
            missing.append(role)
            continue
        normalized_names = [_normalize_relative_path(str(name)) for name in relative_names]
        extras.extend(f"{role}:{name}" for name in normalized_names if name != expected_path)
        if expected_path not in normalized_names:
            missing.append(role)
            continue
        source_path = _safe_source_file(
            input_dir / expected_path,
            source_root,
            model_id=model_id,
            version=version,
            manifest_uri=manifest_uri,
        )
        files.append(
            _source_file_for_package(
                source_path,
                expected_path,
                object_store,
                package_key,
                source_root=source_root,
                role="gis",
            )
        )

    for role, relative_names in required_files.items():
        role_name = str(role)
        if role_name in canonical_roles:
            continue
        if isinstance(relative_names, list):
            extras.extend(f"{role_name}:{_normalize_relative_path(str(name))}" for name in relative_names)
        else:
            extras.append(f"{role_name}:<non-list>")

    if direct_same_pattern_extras:
        entries = ", ".join(sorted(set(extras)))
        raise BasinsPackageError(
            "BASINS_REQUIRED_FILES_NON_CANONICAL",
            f"Basins inventory includes non-canonical required file entries: {entries}",
            model_id=model_id or None,
            version=version,
            path=str(input_dir),
        )
    if missing:
        roles = ", ".join(sorted(missing))
        raise BasinsPackageError(
            "BASINS_REQUIRED_FILES_MISSING",
            f"Basins inventory is missing canonical required file roles or paths: {roles}",
            model_id=model_id or None,
            version=version,
            path=str(input_dir),
        )
    if extras:
        entries = ", ".join(sorted(extras))
        raise BasinsPackageError(
            "BASINS_REQUIRED_FILES_NON_CANONICAL",
            f"Basins inventory includes non-canonical required file entries: {entries}",
            model_id=model_id or None,
            version=version,
            path=str(input_dir),
        )
    return files

def _canonical_shud_required_file_name(input_name: str, pattern: str) -> str:
    if not pattern.startswith("*"):
        raise BasinsPackageError(
            "BASINS_INVENTORY_INVALID",
            f"Unsupported SHUD required file pattern: {pattern}",
        )
    return f"{input_name}{pattern.removeprefix('*')}"

def _source_file_for_package(
    source_path: Path,
    relative_path: str,
    object_store: LocalObjectStore | None,
    package_key: str,
    *,
    source_root: Path,
    role: str,
) -> SourceFile:
    object_key = f"{package_key}/{relative_path}" if object_store is not None else ""
    return SourceFile(
        source_path=source_path,
        source_root=source_root,
        relative_path=relative_path,
        object_key=object_key,
        object_uri=object_store.uri_for_key(object_key) if object_store is not None else "",
        role=role,
    )

def _expected_input_dir(
    model: dict[str, Any],
    source_root: Path,
    *,
    model_id: str,
    version: str,
) -> Path:
    shud_input_name = model.get("shud_input_name")
    if not isinstance(shud_input_name, str) or not shud_input_name:
        raise BasinsPackageError(
            "BASINS_INVENTORY_INVALID",
            "Basins model record is missing shud_input_name.",
            model_id=model_id,
            version=version,
        )
    try:
        safe_name = _normalize_relative_path(shud_input_name)
    except BasinsPackageError as error:
        raise BasinsPackageError(
            "BASINS_INVENTORY_PATH_MISMATCH",
            "Basins inventory shud_input_name is not a safe canonical input directory name.",
            model_id=model_id,
            version=version,
            path=shud_input_name,
        ) from error
    if Path(safe_name).parts != (safe_name,):
        raise BasinsPackageError(
            "BASINS_INVENTORY_PATH_MISMATCH",
            "Basins inventory shud_input_name is not a single canonical input directory name.",
            model_id=model_id,
            version=version,
            path=shud_input_name,
        )
    return _resolve_package_path(source_root / "input" / safe_name, model_id=model_id, version=version)

def _expected_forcing_dir(
    model: dict[str, Any],
    source_root: Path,
    *,
    model_id: str,
    version: str,
) -> Path:
    forcing_dir_original_name = model.get("forcing_dir_original_name")
    if forcing_dir_original_name not in {"forcing", "focing"}:
        raise BasinsPackageError(
            "BASINS_INVENTORY_PATH_MISMATCH",
            "Basins inventory forcing_dir_original_name is not an accepted canonical forcing directory name.",
            model_id=model_id,
            version=version,
            path=str(forcing_dir_original_name or ""),
        )
    return _resolve_package_path(source_root / forcing_dir_original_name, model_id=model_id, version=version)

def _ensure_inventory_path_matches_expected(
    actual: Path,
    expected: Path,
    field_name: str,
    *,
    model_id: str | None = None,
    version: str | None = None,
) -> None:
    resolved_expected = _resolve_package_path(expected, model_id=model_id, version=version)
    if actual != resolved_expected:
        raise BasinsPackageError(
            "BASINS_INVENTORY_PATH_MISMATCH",
            f"Basins inventory {field_name} does not match the selected model's canonical source path.",
            model_id=model_id,
            version=version,
            path=str(actual),
        )

def _planned_file_entry(
    source_file: SourceFile,
    *,
    model_id: str,
    version: str,
    manifest_uri: str | None,
) -> dict[str, Any]:
    size_bytes, sha256 = _source_file_evidence(
        source_file.source_path,
        source_file.source_root,
        model_id=model_id,
        version=version,
        manifest_uri=manifest_uri,
    )
    return {
        "relative_path": source_file.relative_path,
        "role": source_file.role,
        "size_bytes": size_bytes,
        "sha256": sha256,
    }

def _source_identity_from_plan(
    *,
    model: dict[str, Any],
    package_files: list[SourceFile],
    forcing: dict[str, Any],
    copy_forcing: bool,
    model_id: str,
    version: str,
    manifest_uri: str | None,
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    planned_entries = sorted(
        [
            _planned_file_entry(
                source_file,
                model_id=model_id,
                version=version,
                manifest_uri=manifest_uri,
            )
            for source_file in package_files
        ],
        key=lambda item: (item["role"], item["relative_path"]),
    )
    content_material = {
        "package_schema_version": BASINS_PACKAGE_SCHEMA_VERSION,
        "included_files": planned_entries,
        "forcing": _forcing_checksum_material(forcing),
        "copy_forcing": copy_forcing,
    }
    source_material = {
        "model_id": model_id,
        "basin_slug": model.get("basin_slug"),
        "shud_input_name": model.get("shud_input_name"),
        "root_relative_resolved_path": model.get("root_relative_resolved_path"),
    }
    return (
        {
            "schema_version": BASINS_PACKAGE_SOURCE_IDENTITY_SCHEMA_VERSION,
            "content_sha256": _sha256_json(content_material),
            "source_sha256": _sha256_json(source_material),
        },
        planned_entries,
    )

def _verify_expected_source_identity(
    expected: dict[str, Any] | None,
    actual: dict[str, Any],
    *,
    model_id: str,
    version: str,
    manifest_uri: str,
) -> None:
    if expected is None:
        return
    required_fields = ("schema_version", "content_sha256", "source_sha256")
    expected_identity = {field: expected.get(field) for field in required_fields}
    actual_identity = {field: actual.get(field) for field in required_fields}
    if expected_identity != actual_identity:
        raise BasinsPackageError(
            "BASINS_PACKAGE_SOURCE_IDENTITY_CHANGED",
            "Basins package sources changed after the immutable version was planned.",
            model_id=model_id,
            version=version,
            manifest_uri=manifest_uri,
        )

def _package_source_files(
    model: dict[str, Any],
    inventory_root: Path,
    inventory_relative_root: Path | None,
    source_root: Path,
    object_store: LocalObjectStore | None,
    package_key: str,
    *,
    model_id: str,
    version: str,
    manifest_uri: str | None,
    walk_source_files: Callable[[Path, Path], Iterator[Path]]
) -> list[SourceFile]:
    expected_input_dir = _expected_input_dir(model, source_root, model_id=model_id, version=version)
    input_dir = _safe_source_dir(
        model.get("input_dir"),
        inventory_root,
        inventory_relative_root,
        source_root,
        "input_dir",
        expected_path=expected_input_dir,
        model_id=model_id,
        version=version,
        manifest_uri=manifest_uri,
    )
    _ensure_inventory_path_matches_expected(
        input_dir,
        expected_input_dir,
        "input_dir",
        model_id=model_id,
        version=version,
    )
    gis_dir_value = model.get("gis_dir")
    if isinstance(gis_dir_value, str) and gis_dir_value:
        gis_dir = _safe_source_dir(
            gis_dir_value,
            inventory_root,
            inventory_relative_root,
            source_root,
            "gis_dir",
            expected_path=expected_input_dir / "gis",
            model_id=model_id,
            version=version,
            manifest_uri=manifest_uri,
        )
        _ensure_inventory_path_matches_expected(
            gis_dir,
            expected_input_dir / "gis",
            "gis_dir",
            model_id=model_id,
            version=version,
        )
    required_files = model.get("required_files")
    if not isinstance(required_files, dict):
        raise BasinsPackageError("BASINS_INVENTORY_INVALID", "Basins model record is missing required_files.")
    files = _validated_canonical_required_source_files(
        required_files,
        input_dir,
        source_root,
        object_store,
        package_key,
        model_id=model_id,
        version=version,
        manifest_uri=manifest_uri,
    )
    files.extend(
        _optional_shud_runtime_files(
            input_dir,
            source_root,
            object_store,
            package_key,
            model_id=model_id,
            version=version,
            manifest_uri=manifest_uri,
        )
    )

    calib_path = source_root / "CALIB"
    _reject_source_symlink_path(calib_path, source_root, model_id=model_id, version=version, manifest_uri=manifest_uri)
    if calib_path.is_dir():
        calib_dir = _resolve_package_path(calib_path)
        _ensure_under_source_root(calib_dir, source_root)
        for path in walk_source_files(calib_path, source_root):
            relative_path = _normalize_relative_path(Path("CALIB", path.relative_to(calib_dir)).as_posix())
            files.append(
                SourceFile(
                    source_path=path,
                    source_root=source_root,
                    relative_path=relative_path,
                    object_key=f"{package_key}/{relative_path}" if object_store is not None else "",
                    object_uri=(
                        object_store.uri_for_key(f"{package_key}/{relative_path}")
                        if object_store is not None
                        else ""
                    ),
                    role="calibration",
                )
            )
    return sorted(files, key=lambda item: (item.role, item.relative_path))
