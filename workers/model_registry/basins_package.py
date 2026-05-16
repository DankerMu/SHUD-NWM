from __future__ import annotations

import hashlib
import json
import os
import stat
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from fnmatch import fnmatchcase
from pathlib import Path
from typing import Any, BinaryIO

from packages.common.object_store import LocalObjectStore, ObjectStoreError
from packages.common.storage import validate_object_path

from .basins_discovery import (
    GIS_REQUIRED_FILES,
    SHUD_REQUIRED_PATTERNS,
    BasinsDiscoveryError,
    discover_basins_inventory,
)
from .basins_discovery import (
    _slug_id as _basins_slug_id,
)

BASINS_PACKAGE_SCHEMA_VERSION = "basins.package.v1"
BASINS_MIGRATION_REPORT_SCHEMA_VERSION = "basins.migration.v1"
FORCING_SAMPLE_FILE_LIMIT = 5
FORCING_SAMPLE_BYTE_LIMIT = 64 * 1024
FORCING_SAMPLE_LINE_LIMIT = 1000
_OS_OPEN_SUPPORTS_DIR_FD = os.open in os.supports_dir_fd
_OS_MKDIR_SUPPORTS_DIR_FD = os.mkdir in os.supports_dir_fd
_OS_RENAME_SUPPORTS_DIR_FD = os.rename in os.supports_dir_fd
_OS_UNLINK_SUPPORTS_DIR_FD = os.unlink in os.supports_dir_fd
_OS_STAT_SUPPORTS_DIR_FD = os.stat in os.supports_dir_fd
_OS_STAT_SUPPORTS_FOLLOW_SYMLINKS = os.stat in os.supports_follow_symlinks
_OS_OPENAT_OBJECT_STORE_AVAILABLE = (
    hasattr(os, "O_NOFOLLOW")
    and hasattr(os, "O_DIRECTORY")
    and _OS_OPEN_SUPPORTS_DIR_FD
    and _OS_MKDIR_SUPPORTS_DIR_FD
    and _OS_RENAME_SUPPORTS_DIR_FD
    and _OS_UNLINK_SUPPORTS_DIR_FD
    and _OS_STAT_SUPPORTS_DIR_FD
)


class BasinsPackageError(RuntimeError):
    """Raised when Basins package publication or migration evidence fails."""

    def __init__(
        self,
        error_code: str,
        message: str,
        *,
        model_id: str | None = None,
        version: str | None = None,
        path: str | None = None,
        manifest_uri: str | None = None,
    ) -> None:
        super().__init__(message)
        self.error_code = error_code
        self.model_id = model_id
        self.version = version
        self.path = path
        self.manifest_uri = manifest_uri

    def to_payload(self) -> dict[str, Any]:
        payload: dict[str, Any] = {"error_code": self.error_code, "message": str(self)}
        if self.model_id is not None:
            payload["model_id"] = self.model_id
        if self.version is not None:
            payload["version"] = self.version
        if self.path is not None:
            payload["path"] = self.path
        if self.manifest_uri is not None:
            payload["manifest_uri"] = self.manifest_uri
        return payload


@dataclass(frozen=True)
class SourceFile:
    source_path: Path
    source_root: Path
    relative_path: str
    object_key: str
    object_uri: str
    role: str


@dataclass(frozen=True)
class ObjectStoreParent:
    path: Path
    name: str
    parent_fd: int | None = None


def publish_basins_package(
    *,
    inventory_path: str | Path,
    model_id: str,
    version: str,
    output_path: str | Path,
    copy_forcing: bool = False,
    object_store: LocalObjectStore | None = None,
) -> dict[str, Any]:
    _validate_object_key_segment(model_id, "model_id", model_id=model_id, version=version)
    _validate_object_key_segment(version, "version", model_id=model_id, version=version)
    inventory, inventory_bytes = _read_inventory(inventory_path)
    model = _find_publishable_model(inventory, model_id, version)
    store = object_store or _object_store_from_env(model_id=model_id, version=version)

    base_key = f"models/{model_id}/{version}"
    package_key = f"{base_key}/package"
    forcing_key = f"{base_key}/forcing"
    manifest_key = f"{base_key}/manifest.json"
    lock_key = f"{base_key}/.publish.lock"
    model_package_uri = _directory_uri(store, package_key)
    manifest_uri = store.uri_for_key(manifest_key)
    inventory_root = _resolved_inventory_root(inventory, model_id, version)
    inventory_relative_root = _recorded_relative_inventory_root(inventory)
    source_root = _resolved_source_root(model, inventory_root, model_id, version)

    package_files = _package_source_files(
        model,
        inventory_root,
        inventory_relative_root,
        source_root,
        store,
        package_key,
        model_id=model_id,
        version=version,
        manifest_uri=manifest_uri,
    )
    forcing, forcing_files = _forcing_metadata(
        model=model,
        inventory_root=inventory_root,
        inventory_relative_root=inventory_relative_root,
        source_root=source_root,
        object_store=store,
        forcing_key=forcing_key,
        copy_forcing=copy_forcing,
        model_id=model_id,
        version=version,
        manifest_uri=manifest_uri,
    )
    source_files = [*package_files, *(forcing_files if copy_forcing else [])]
    _preflight_object_store_keys(
        store,
        [source_file.object_key for source_file in source_files] + [manifest_key, lock_key],
        model_id=model_id,
        version=version,
        manifest_uri=manifest_uri,
    )
    planned_included_files = sorted(
        [
            _planned_file_entry(
                source_file,
                model_id=model_id,
                version=version,
                manifest_uri=manifest_uri,
            )
            for source_file in source_files
        ],
        key=lambda item: (item["role"], item["relative_path"]),
    )
    checksum_material = {
        "schema_version": BASINS_PACKAGE_SCHEMA_VERSION,
        "model_id": model_id,
        "version": version,
        "included_files": planned_included_files,
        "forcing": _forcing_checksum_material(forcing),
        "copy_forcing": copy_forcing,
        "source_model_identity": {
            "basin_slug": model.get("basin_slug"),
            "shud_input_name": model.get("shud_input_name"),
            "root_relative_resolved_path": model.get("root_relative_resolved_path"),
        },
    }
    package_checksum = _sha256_json(checksum_material)

    if _object_exists_no_symlinks(store, manifest_key, model_id=model_id, version=version, manifest_uri=manifest_uri):
        existing_manifest = _read_existing_manifest(store, manifest_key, model_id, version, manifest_uri)
        if existing_manifest.get("package_checksum") != package_checksum:
            raise BasinsPackageError(
                "BASINS_PACKAGE_CHECKSUM_CONFLICT",
                "Existing Basins package manifest has a different package checksum; publish a new version.",
                model_id=model_id,
                version=version,
                manifest_uri=manifest_uri,
            )
        _write_json_file(
            output_path,
            existing_manifest,
            error_code="BASINS_PACKAGE_OUTPUT_WRITE_FAILED",
            model_id=model_id,
            version=version,
            manifest_uri=manifest_uri,
        )
        return _success_payload("already_done", existing_manifest)

    created_at = datetime.now(UTC).isoformat().replace("+00:00", "Z")
    local_output_manifest: dict[str, Any] | None = None
    lock_acquired = False
    try:
        _acquire_publish_lock(store, lock_key, model_id, version, manifest_uri)
        lock_acquired = True
        if _object_exists_no_symlinks(
            store,
            manifest_key,
            model_id=model_id,
            version=version,
            manifest_uri=manifest_uri,
        ):
            existing_manifest = _read_existing_manifest(store, manifest_key, model_id, version, manifest_uri)
            if existing_manifest.get("package_checksum") != package_checksum:
                raise BasinsPackageError(
                    "BASINS_PACKAGE_CHECKSUM_CONFLICT",
                    "Existing Basins package manifest has a different package checksum; publish a new version.",
                    model_id=model_id,
                    version=version,
                    manifest_uri=manifest_uri,
                )
            _write_json_file(
                output_path,
                existing_manifest,
                error_code="BASINS_PACKAGE_OUTPUT_WRITE_FAILED",
                model_id=model_id,
                version=version,
                manifest_uri=manifest_uri,
            )
            return _success_payload("already_done", existing_manifest)

        _preflight_json_output_path(
            output_path,
            error_code="BASINS_PACKAGE_OUTPUT_WRITE_FAILED",
            model_id=model_id,
            version=version,
            manifest_uri=manifest_uri,
        )
        included_files = []
        for source_file in source_files:
            included_files.append(
                _write_source_file_to_store(
                    source_file,
                    store,
                    model_id=model_id,
                    version=version,
                    manifest_uri=manifest_uri,
                )
            )
        included_files = sorted(included_files, key=lambda item: (item["role"], item["relative_path"]))
        if copy_forcing:
            forcing = _forcing_metadata_from_written_entries(forcing, included_files)

        actual_checksum_material = {
            **checksum_material,
            "forcing": _forcing_checksum_material(forcing),
            "included_files": [
                {
                    "relative_path": item["relative_path"],
                    "role": item["role"],
                    "size_bytes": item["size_bytes"],
                    "sha256": item["sha256"],
                }
                for item in included_files
            ],
        }
        actual_package_checksum = _sha256_json(actual_checksum_material)
        if actual_package_checksum != package_checksum:
            package_checksum = actual_package_checksum

        manifest_without_self_entry = {
            "schema_version": BASINS_PACKAGE_SCHEMA_VERSION,
            "model_id": model_id,
            "version": version,
            "basin_slug": model.get("basin_slug"),
            "shud_input_name": model.get("shud_input_name"),
            "model_package_uri": model_package_uri,
            "manifest_uri": manifest_uri,
            "package_checksum": package_checksum,
            "source_inventory_checksum": _sha256_bytes(inventory_bytes),
            "source_inventory_schema_version": inventory.get("schema_version"),
            "source_path": model.get("source_path"),
            "resolved_source_path": str(source_root),
            "source_is_symlink": bool(model.get("source_is_symlink", False)),
            "included_files": included_files,
            "forcing": forcing,
            "calibration": _calibration_metadata(model, included_files),
            "created_at": created_at,
        }
        manifest, manifest_bytes = _manifest_with_manifest_entry(
            manifest_without_self_entry,
            included_files,
            object_store=store,
            manifest_key=manifest_key,
        )
        local_output_manifest = manifest
        _write_bytes_to_store_atomic(
            store,
            manifest_key,
            manifest_bytes,
            model_id=model_id,
            version=version,
            manifest_uri=manifest_uri,
        )
        _verify_object_bytes(
            store,
            manifest_key,
            expected_size=len(manifest_bytes),
            expected_sha256=_sha256_bytes(manifest_bytes),
            model_id=model_id,
            version=version,
            manifest_uri=manifest_uri,
        )
        _write_json_file(
            output_path,
            manifest,
            error_code="BASINS_PACKAGE_OUTPUT_WRITE_FAILED",
            model_id=model_id,
            version=version,
            manifest_uri=manifest_uri,
        )
    except (OSError, ObjectStoreError, ValueError) as error:
        raise BasinsPackageError(
            "BASINS_PACKAGE_WRITE_FAILED",
            f"Failed to publish Basins package: {error}",
            model_id=model_id,
            version=version,
            manifest_uri=manifest_uri,
        ) from error
    finally:
        if lock_acquired:
            _release_publish_lock(store, lock_key)

    if local_output_manifest is None:
        raise BasinsPackageError(
            "BASINS_PACKAGE_WRITE_FAILED",
            "Failed to publish Basins package: manifest was not prepared.",
            model_id=model_id,
            version=version,
            manifest_uri=manifest_uri,
        )
    return _success_payload("published", local_output_manifest)


def write_basins_migration_report(
    *,
    basins_root: str | Path,
    source_uri: str,
    output_path: str | Path,
) -> dict[str, Any]:
    root = Path(basins_root).expanduser()
    if not root.exists() or not root.is_dir():
        raise BasinsPackageError("BASINS_ROOT_NOT_FOUND", f"Basins root does not exist: {root}", path=str(root))
    if root.is_symlink():
        raise BasinsPackageError(
            "BASINS_MIGRATION_SYMLINK_TARGET",
            "Production migration evidence requires copied Basins data; symlink targets are not production-ready.",
            path=str(root),
        )

    try:
        inventory = discover_basins_inventory(root)
    except BasinsDiscoveryError as error:
        raise BasinsPackageError(error.error_code, str(error), path=error.path or str(root)) from error

    resolved_root = _resolve_package_path(root)
    file_count, byte_count, content_checksum = _directory_evidence(root)
    report = {
        "schema_version": BASINS_MIGRATION_REPORT_SCHEMA_VERSION,
        "source_uri": source_uri,
        "target_path": str(root),
        "resolved_target_path": str(resolved_root),
        "source_is_symlink": False,
        "file_count": file_count,
        "byte_count": byte_count,
        "content_checksum": content_checksum,
        "inventory_checksum": _sha256_json(inventory),
        "model_count": inventory.get("model_count", 0),
        "source_to_target": {
            "source_uri": source_uri,
            "target_path": str(root),
            "resolved_target_path": str(resolved_root),
            "copy_required": True,
            "symlink_allowed": False,
        },
        "production_ready": True,
        "created_at": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
    }
    _write_json_file(
        output_path,
        report,
        error_code="BASINS_MIGRATION_REPORT_WRITE_FAILED",
    )
    return report


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


def _object_store_from_env(*, model_id: str, version: str) -> LocalObjectStore:
    root = os.getenv("OBJECT_STORE_ROOT", "").strip()
    if not root:
        raise BasinsPackageError(
            "OBJECT_STORE_ROOT_MISSING",
            "OBJECT_STORE_ROOT is required for Basins package publication.",
            model_id=model_id,
            version=version,
        )
    return LocalObjectStore(root, os.getenv("OBJECT_STORE_PREFIX", ""))


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


def _package_source_files(
    model: dict[str, Any],
    inventory_root: Path,
    inventory_relative_root: Path | None,
    source_root: Path,
    object_store: LocalObjectStore,
    package_key: str,
    *,
    model_id: str,
    version: str,
    manifest_uri: str,
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

    calib_path = source_root / "CALIB"
    _reject_source_symlink_path(calib_path, source_root, model_id=model_id, version=version, manifest_uri=manifest_uri)
    if calib_path.is_dir():
        calib_dir = _resolve_package_path(calib_path)
        _ensure_under_source_root(calib_dir, source_root)
        for path in _walk_source_files(calib_path, source_root):
            relative_path = _normalize_relative_path(Path("CALIB", path.relative_to(calib_dir)).as_posix())
            files.append(
                SourceFile(
                    source_path=path,
                    source_root=source_root,
                    relative_path=relative_path,
                    object_key=f"{package_key}/{relative_path}",
                    object_uri=object_store.uri_for_key(f"{package_key}/{relative_path}"),
                    role="calibration",
                )
            )
    return sorted(files, key=lambda item: (item.role, item.relative_path))


def _validated_canonical_required_source_files(
    required_files: dict[str, Any],
    input_dir: Path,
    source_root: Path,
    object_store: LocalObjectStore,
    package_key: str,
    *,
    model_id: str,
    version: str,
    manifest_uri: str,
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
    object_store: LocalObjectStore,
    package_key: str,
    *,
    source_root: Path,
    role: str,
) -> SourceFile:
    object_key = f"{package_key}/{relative_path}"
    return SourceFile(
        source_path=source_path,
        source_root=source_root,
        relative_path=relative_path,
        object_key=object_key,
        object_uri=object_store.uri_for_key(object_key),
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


def _forcing_metadata(
    *,
    model: dict[str, Any],
    inventory_root: Path,
    inventory_relative_root: Path | None,
    source_root: Path,
    object_store: LocalObjectStore,
    forcing_key: str,
    copy_forcing: bool,
    model_id: str,
    version: str,
    manifest_uri: str | None = None,
) -> tuple[dict[str, Any], list[SourceFile]]:
    forcing_dir_value = model.get("forcing_dir")
    if not isinstance(forcing_dir_value, str) or not forcing_dir_value:
        return (
            {
                "policy": "excluded_by_default" if not copy_forcing else "copy_requested_no_source",
                "forcing_dir": None,
                "forcing_dir_original_name": model.get("forcing_dir_original_name"),
                "csv_count": 0,
                "byte_count": 0,
                "aggregate_checksum": None,
                "payload_copied": False,
            },
            [],
        )

    expected_forcing_dir = _expected_forcing_dir(model, source_root, model_id=model_id, version=version)
    forcing_dir = _safe_source_dir(
        forcing_dir_value,
        inventory_root,
        inventory_relative_root,
        source_root,
        "forcing_dir",
        expected_path=expected_forcing_dir,
        model_id=model_id,
        version=version,
        manifest_uri=manifest_uri,
    )
    _ensure_inventory_path_matches_expected(
        forcing_dir,
        expected_forcing_dir,
        "forcing_dir",
        model_id=model_id,
        version=version,
    )
    digest = hashlib.sha256()
    total_bytes = 0
    sample_headers: list[str] = []
    time_start: str | None = None
    time_end: str | None = None
    parsed_time_rows = 0
    csv_count = 0
    sampled_file_count = 0
    source_files: list[SourceFile] = []

    for path in _walk_source_files(forcing_dir, source_root):
        if path.suffix.lower() != ".csv":
            continue
        csv_count += 1
        relative_path = _normalize_relative_path(path.relative_to(forcing_dir).as_posix())
        size_bytes, sha256 = _source_file_evidence(
            path,
            source_root,
            model_id=model_id,
            version=version,
            manifest_uri=manifest_uri,
        )
        digest.update(relative_path.encode("utf-8"))
        digest.update(b"\0")
        digest.update(str(size_bytes).encode("ascii"))
        digest.update(b"\0")
        digest.update(sha256.encode("ascii"))
        digest.update(b"\0")
        total_bytes += size_bytes
        if sampled_file_count < FORCING_SAMPLE_FILE_LIMIT:
            header, first_time, last_time, row_count = _csv_time_evidence(
                path,
                source_root,
                model_id=model_id,
                version=version,
                manifest_uri=manifest_uri,
            )
            sampled_file_count += 1
            if header and header not in sample_headers:
                sample_headers.append(header)
            if first_time is not None:
                time_start = first_time if time_start is None else min(time_start, first_time)
            if last_time is not None:
                time_end = last_time if time_end is None else max(time_end, last_time)
            parsed_time_rows += row_count
        if copy_forcing:
            source_files.append(
                SourceFile(
                    source_path=path,
                    source_root=source_root,
                    relative_path=relative_path,
                    object_key=f"{forcing_key}/{relative_path}",
                    object_uri=object_store.uri_for_key(f"{forcing_key}/{relative_path}"),
                    role="forcing",
                )
            )

    forcing_payload_uri = _directory_uri(object_store, forcing_key) if copy_forcing else None
    metadata = {
        "policy": "copied_explicitly" if copy_forcing else "excluded_by_default",
        "forcing_dir": str(forcing_dir),
        "forcing_dir_original_name": model.get("forcing_dir_original_name"),
        "csv_count": csv_count,
        "byte_count": total_bytes,
        "aggregate_checksum": digest.hexdigest() if csv_count else None,
        "sample_headers": sample_headers,
        "sampled_file_count": sampled_file_count,
        "time_coverage": (
            {"start": time_start, "end": time_end} if time_start is not None or time_end is not None else None
        ),
        "parsed_time_rows": parsed_time_rows,
        "sample_file_limit": FORCING_SAMPLE_FILE_LIMIT,
        "sample_byte_limit": FORCING_SAMPLE_BYTE_LIMIT,
        "sample_line_limit": FORCING_SAMPLE_LINE_LIMIT,
        "payload_copied": copy_forcing,
        "forcing_payload_uri": forcing_payload_uri,
        "copied_file_count": csv_count if copy_forcing else 0,
        "copied_byte_count": total_bytes if copy_forcing else 0,
    }
    return metadata, source_files


def _planned_file_entry(
    source_file: SourceFile,
    *,
    model_id: str,
    version: str,
    manifest_uri: str,
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


def _source_file_evidence(
    path: Path,
    source_root: Path,
    *,
    model_id: str | None = None,
    version: str | None = None,
    manifest_uri: str | None = None,
) -> tuple[int, str]:
    return _verified_source_file_evidence(
        path,
        source_root,
        read_error_code="BASINS_PACKAGE_WRITE_FAILED",
        read_error_message="Failed to read Basins package source file",
        model_id=model_id,
        version=version,
        manifest_uri=manifest_uri,
    )


def _migration_source_file_evidence(path: Path, source_root: Path) -> tuple[int, str]:
    return _verified_source_file_evidence(
        path,
        source_root,
        read_error_code="BASINS_MIGRATION_EVIDENCE_READ_FAILED",
        read_error_message="Failed to read Basins migration evidence source file",
    )


def _verified_source_file_evidence(
    path: Path,
    source_root: Path,
    *,
    read_error_code: str,
    read_error_message: str,
    model_id: str | None = None,
    version: str | None = None,
    manifest_uri: str | None = None,
) -> tuple[int, str]:
    try:
        with _open_verified_source_file(
            path,
            source_root,
            model_id=model_id,
            version=version,
            manifest_uri=manifest_uri,
        ) as source:
            stat_result = os.fstat(source.fileno())
            size_bytes = stat_result.st_size
            sha256 = _sha256_handle(source)
    except OSError as error:
        raise BasinsPackageError(
            read_error_code,
            f"{read_error_message}: {path}: {error}",
            model_id=model_id,
            version=version,
            path=str(path),
            manifest_uri=manifest_uri,
        ) from error
    return size_bytes, sha256


def _manifest_file_entry_for_source_file(source_file: SourceFile, *, size_bytes: int, sha256: str) -> dict[str, Any]:
    return {
        "relative_path": source_file.relative_path,
        "object_uri": source_file.object_uri,
        "size_bytes": size_bytes,
        "sha256": sha256,
        "role": source_file.role,
    }


def _manifest_file_entry(
    *,
    object_store: LocalObjectStore,
    manifest_key: str,
    content_bytes: bytes,
    final_size_bytes: int,
) -> dict[str, Any]:
    return {
        "relative_path": "manifest.json",
        "object_uri": object_store.uri_for_key(manifest_key),
        "size_bytes": final_size_bytes,
        "sha256": _sha256_bytes(content_bytes),
        "role": "manifest",
    }


def _manifest_with_manifest_entry(
    manifest_without_self_entry: dict[str, Any],
    included_files: list[dict[str, Any]],
    *,
    object_store: LocalObjectStore,
    manifest_key: str,
) -> tuple[dict[str, Any], bytes]:
    # A manifest cannot contain a normal SHA-256 fixed point of its own final JSON bytes.
    # The package checksum excludes this self-entry; the manifest entry checksum covers
    # the deterministic manifest payload before the self-entry is appended.
    manifest_payload_bytes = _json_bytes(manifest_without_self_entry)
    manifest_entry = _manifest_file_entry(
        object_store=object_store,
        manifest_key=manifest_key,
        content_bytes=manifest_payload_bytes,
        final_size_bytes=0,
    )

    while True:
        manifest = dict(manifest_without_self_entry)
        manifest["included_files"] = sorted(
            [*included_files, manifest_entry],
            key=lambda item: (item["role"], item["relative_path"]),
        )
        manifest_bytes = _json_bytes(manifest)
        if manifest_entry["size_bytes"] == len(manifest_bytes):
            return manifest, manifest_bytes
        manifest_entry = {**manifest_entry, "size_bytes": len(manifest_bytes)}


def _calibration_metadata(model: dict[str, Any], included_files: list[dict[str, Any]]) -> dict[str, Any]:
    calibration_files = [item for item in included_files if item["role"] == "calibration"]
    return {
        "source_count": int(model.get("calibration_count") or 0),
        "included_count": len(calibration_files),
        "included_files": [item["relative_path"] for item in calibration_files],
    }


def _forcing_checksum_material(forcing: dict[str, Any]) -> dict[str, Any]:
    return {
        "policy": forcing.get("policy"),
        "csv_count": forcing.get("csv_count"),
        "byte_count": forcing.get("byte_count"),
        "aggregate_checksum": forcing.get("aggregate_checksum"),
        "payload_copied": forcing.get("payload_copied"),
        "copied_file_count": forcing.get("copied_file_count"),
        "copied_byte_count": forcing.get("copied_byte_count"),
    }


def _forcing_metadata_from_written_entries(
    forcing: dict[str, Any],
    included_files: list[dict[str, Any]],
) -> dict[str, Any]:
    forcing_entries = sorted(
        (item for item in included_files if item["role"] == "forcing"),
        key=lambda item: item["relative_path"],
    )
    if not forcing_entries:
        return forcing

    digest = hashlib.sha256()
    byte_count = 0
    for item in forcing_entries:
        relative_path = str(item["relative_path"])
        size_bytes = int(item["size_bytes"])
        sha256 = str(item["sha256"])
        digest.update(relative_path.encode("utf-8"))
        digest.update(b"\0")
        digest.update(str(size_bytes).encode("ascii"))
        digest.update(b"\0")
        digest.update(sha256.encode("ascii"))
        digest.update(b"\0")
        byte_count += size_bytes

    return {
        **forcing,
        "byte_count": byte_count,
        "aggregate_checksum": digest.hexdigest(),
        "copied_file_count": len(forcing_entries),
        "copied_byte_count": byte_count,
    }


def _read_existing_manifest(
    store: LocalObjectStore,
    manifest_key: str,
    model_id: str,
    version: str,
    manifest_uri: str,
) -> dict[str, Any]:
    try:
        manifest = json.loads(
            _read_object_bytes_no_symlinks(
                store,
                manifest_key,
                model_id=model_id,
                version=version,
                manifest_uri=manifest_uri,
            ).decode("utf-8")
        )
    except (ObjectStoreError, json.JSONDecodeError, UnicodeDecodeError, ValueError) as error:
        raise BasinsPackageError(
            "BASINS_PACKAGE_MANIFEST_INVALID",
            f"Existing Basins package manifest cannot be read: {manifest_uri}",
            model_id=model_id,
            version=version,
            manifest_uri=manifest_uri,
        ) from error
    if not isinstance(manifest, dict):
        raise BasinsPackageError(
            "BASINS_PACKAGE_MANIFEST_INVALID",
            "Existing Basins package manifest JSON must be an object.",
            model_id=model_id,
            version=version,
            manifest_uri=manifest_uri,
        )
    return manifest


def _acquire_publish_lock(
    store: LocalObjectStore,
    lock_key: str,
    model_id: str,
    version: str,
    manifest_uri: str,
) -> None:
    lock_path = _object_path_for_key(store, lock_key)
    try:
        with _object_parent_for_write(
            store,
            lock_key,
            model_id=model_id,
            version=version,
            manifest_uri=manifest_uri,
        ) as target:
            flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
            if hasattr(os, "O_NOFOLLOW"):
                flags |= os.O_NOFOLLOW
            if hasattr(os, "O_CLOEXEC"):
                flags |= os.O_CLOEXEC
            fd = _object_os_open(target.name, flags, 0o666, target)
    except FileExistsError as error:
        raise BasinsPackageError(
            "BASINS_PACKAGE_PUBLISH_IN_PROGRESS",
            "Basins package publication is already in progress for this model/version.",
            model_id=model_id,
            version=version,
            manifest_uri=manifest_uri,
            path=str(lock_path),
        ) from error
    except OSError as error:
        raise BasinsPackageError(
            "BASINS_PACKAGE_WRITE_FAILED",
            f"Failed to acquire Basins package publish lock: {error}",
            model_id=model_id,
            version=version,
            manifest_uri=manifest_uri,
            path=str(lock_path),
        ) from error
    with os.fdopen(fd, "wb") as handle:
        handle.write(
            _json_bytes(
                {
                    "model_id": model_id,
                    "version": version,
                    "manifest_uri": manifest_uri,
                    "created_at": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
                }
            )
        )


def _release_publish_lock(store: LocalObjectStore, lock_key: str) -> None:
    try:
        with _object_parent_for_write(store, lock_key) as target:
            try:
                _object_os_unlink(target.name, target)
            except FileNotFoundError:
                pass
    except (BasinsPackageError, OSError, ValueError):
        pass


def _write_source_file_to_store(
    source_file: SourceFile,
    store: LocalObjectStore,
    *,
    model_id: str,
    version: str,
    manifest_uri: str,
) -> dict[str, Any]:
    size_bytes, sha256 = _write_file_to_store_streaming(
        store,
        source_file.object_key,
        source_file.source_path,
        source_file.source_root,
        model_id=model_id,
        version=version,
        manifest_uri=manifest_uri,
    )
    _verify_object_bytes(
        store,
        source_file.object_key,
        expected_size=size_bytes,
        expected_sha256=sha256,
        model_id=model_id,
        version=version,
        manifest_uri=manifest_uri,
    )
    return _manifest_file_entry_for_source_file(source_file, size_bytes=size_bytes, sha256=sha256)


def _write_file_to_store_streaming(
    store: LocalObjectStore,
    key: str,
    source_path: Path,
    source_root: Path,
    *,
    model_id: str | None = None,
    version: str | None = None,
    manifest_uri: str | None = None,
) -> tuple[int, str]:
    target_path = _object_path_for_key(store, key)
    temp_path = target_path.with_name(f".{target_path.name}.{uuid.uuid4().hex}.part")
    digest = hashlib.sha256()
    size_bytes = 0
    try:
        with _object_parent_for_write(
            store,
            key,
            model_id=model_id,
            version=version,
            manifest_uri=manifest_uri,
        ) as target:
            temp_name = temp_path.name
            temp_fd = _object_os_open(
                temp_name,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL | _object_no_follow_flag() | _object_cloexec_flag(),
                0o666,
                target,
            )
        with (
            _open_verified_source_file(
                source_path,
                source_root,
                model_id=model_id,
                version=version,
                manifest_uri=manifest_uri,
            ) as source,
            os.fdopen(temp_fd, "wb") as target_handle,
        ):
            for chunk in iter(lambda: source.read(1024 * 1024), b""):
                target_handle.write(chunk)
                digest.update(chunk)
                size_bytes += len(chunk)
            target_handle.flush()
            os.fsync(target_handle.fileno())
        with _object_parent_for_existing_write(
            store,
            key,
            model_id=model_id,
            version=version,
            manifest_uri=manifest_uri,
        ) as target:
            _object_os_replace(temp_path.name, target.name, target)
    except OSError as error:
        try:
            _remove_object_temp_path(store, key, temp_path.name)
        except OSError as cleanup_error:
            raise ObjectStoreError(
                f"Failed to write object {key}: {error}; cleanup also failed: {cleanup_error}"
            ) from cleanup_error
        raise ObjectStoreError(f"Failed to write object {key}: {error}") from error
    return size_bytes, digest.hexdigest()


def _write_bytes_to_store_atomic(
    store: LocalObjectStore,
    key: str,
    content: bytes,
    *,
    model_id: str | None = None,
    version: str | None = None,
    manifest_uri: str | None = None,
) -> str:
    target_path = _object_path_for_key(store, key)
    temp_path = target_path.with_name(f".{target_path.name}.{uuid.uuid4().hex}.part")
    try:
        with _object_parent_for_write(
            store,
            key,
            model_id=model_id,
            version=version,
            manifest_uri=manifest_uri,
        ) as target:
            temp_fd = _object_os_open(
                temp_path.name,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL | _object_no_follow_flag() | _object_cloexec_flag(),
                0o666,
                target,
            )
        with os.fdopen(temp_fd, "wb") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        with _object_parent_for_existing_write(
            store,
            key,
            model_id=model_id,
            version=version,
            manifest_uri=manifest_uri,
        ) as target:
            _object_os_replace(temp_path.name, target.name, target)
    except OSError as error:
        try:
            _remove_object_temp_path(store, key, temp_path.name)
        except OSError as cleanup_error:
            raise ObjectStoreError(
                f"Failed to write object {key}: {error}; cleanup also failed: {cleanup_error}"
            ) from cleanup_error
        raise ObjectStoreError(f"Failed to write object {key}: {error}") from error
    return store.uri_for_key(store.normalize_key(key))


def _preflight_object_store_keys(
    store: LocalObjectStore,
    keys: list[str],
    *,
    model_id: str,
    version: str,
    manifest_uri: str,
) -> None:
    for key in keys:
        _object_path_rejecting_symlinks(
            store,
            key,
            model_id=model_id,
            version=version,
            manifest_uri=manifest_uri,
        )


def _object_exists_no_symlinks(
    store: LocalObjectStore,
    key: str,
    *,
    model_id: str,
    version: str,
    manifest_uri: str,
) -> bool:
    try:
        with _object_parent_for_existing_read(
            store,
            key,
            model_id=model_id,
            version=version,
            manifest_uri=manifest_uri,
        ) as target:
            stat_result = _object_os_stat(target.name, target)
            if stat.S_ISLNK(stat_result.st_mode):
                raise _object_path_unsafe_error(
                    target.path,
                    model_id=model_id,
                    version=version,
                    manifest_uri=manifest_uri,
                )
    except FileNotFoundError:
        return False
    except BasinsPackageError:
        raise
    except OSError as error:
        raise ObjectStoreError(f"Failed to check object existence for {key}: {error}") from error
    return True


def _read_object_bytes_no_symlinks(
    store: LocalObjectStore,
    key: str,
    *,
    model_id: str,
    version: str,
    manifest_uri: str,
) -> bytes:
    try:
        with _open_object_file_no_symlinks(
            store,
            key,
            model_id=model_id,
            version=version,
            manifest_uri=manifest_uri,
        ) as handle:
            return handle.read()
    except OSError as error:
        raise ObjectStoreError(f"Failed to read object {key}: {error}") from error


def _object_path_rejecting_symlinks(
    store: LocalObjectStore,
    key_or_uri: str,
    *,
    model_id: str | None = None,
    version: str | None = None,
    manifest_uri: str | None = None,
) -> Path:
    key, parts = _object_key_parts(store, key_or_uri)
    root = Path(store.root)
    candidate = root
    for part in parts:
        candidate = candidate / part
        try:
            mode = candidate.lstat().st_mode
        except FileNotFoundError:
            continue
        except OSError as error:
            raise ObjectStoreError(f"Failed to inspect object path {key}: {error}") from error
        if stat.S_ISLNK(mode):
            raise BasinsPackageError(
                "BASINS_PACKAGE_OBJECT_PATH_UNSAFE",
                "Basins package publication does not follow object-store symlink components.",
                model_id=model_id,
                version=version,
                path=str(candidate),
                manifest_uri=manifest_uri,
            )
    return root.joinpath(*parts)


def _object_key_parts(store: LocalObjectStore, key_or_uri: str) -> tuple[str, tuple[str, ...]]:
    key = store.normalize_key(key_or_uri)
    validation = validate_object_path(key)
    if not validation.valid:
        raise ValueError(validation.error)
    parts = Path(key).parts
    if not parts or ".." in parts:
        raise ValueError(f"Object key must not contain '..': {key_or_uri}")
    return key, parts


def _object_path_for_key(store: LocalObjectStore, key_or_uri: str) -> Path:
    _, parts = _object_key_parts(store, key_or_uri)
    return Path(store.root).joinpath(*parts)


@contextmanager
def _object_parent_for_write(
    store: LocalObjectStore,
    key_or_uri: str,
    *,
    model_id: str | None = None,
    version: str | None = None,
    manifest_uri: str | None = None,
) -> Iterator[ObjectStoreParent]:
    if _OS_OPENAT_OBJECT_STORE_AVAILABLE:
        target = _open_object_parent_at(
            store,
            key_or_uri,
            create=True,
            model_id=model_id,
            version=version,
            manifest_uri=manifest_uri,
        )
        try:
            yield target
        finally:
            if target.parent_fd is not None:
                os.close(target.parent_fd)
        return

    path = _object_path_rejecting_symlinks(
        store,
        key_or_uri,
        model_id=model_id,
        version=version,
        manifest_uri=manifest_uri,
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    path = _object_path_rejecting_symlinks(
        store,
        key_or_uri,
        model_id=model_id,
        version=version,
        manifest_uri=manifest_uri,
    )
    yield ObjectStoreParent(path=path, name=path.name)


@contextmanager
def _object_parent_for_existing_write(
    store: LocalObjectStore,
    key_or_uri: str,
    *,
    model_id: str | None = None,
    version: str | None = None,
    manifest_uri: str | None = None,
) -> Iterator[ObjectStoreParent]:
    if _OS_OPENAT_OBJECT_STORE_AVAILABLE:
        target = _open_object_parent_at(
            store,
            key_or_uri,
            create=False,
            model_id=model_id,
            version=version,
            manifest_uri=manifest_uri,
        )
        try:
            yield target
        finally:
            if target.parent_fd is not None:
                os.close(target.parent_fd)
        return

    path = _object_path_rejecting_symlinks(
        store,
        key_or_uri,
        model_id=model_id,
        version=version,
        manifest_uri=manifest_uri,
    )
    yield ObjectStoreParent(path=path, name=path.name)


@contextmanager
def _object_parent_for_existing_read(
    store: LocalObjectStore,
    key_or_uri: str,
    *,
    model_id: str | None = None,
    version: str | None = None,
    manifest_uri: str | None = None,
) -> Iterator[ObjectStoreParent]:
    with _object_parent_for_existing_write(
        store,
        key_or_uri,
        model_id=model_id,
        version=version,
        manifest_uri=manifest_uri,
    ) as target:
        yield target


def _open_object_parent_at(
    store: LocalObjectStore,
    key_or_uri: str,
    *,
    create: bool,
    model_id: str | None = None,
    version: str | None = None,
    manifest_uri: str | None = None,
) -> ObjectStoreParent:
    _, parts = _object_key_parts(store, key_or_uri)
    root = Path(store.root)
    directory_flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | _object_cloexec_flag()
    current_fd: int | None = None
    try:
        if create:
            root.mkdir(parents=True, exist_ok=True)
        current_fd = os.open(root, directory_flags)
        root_stat = os.fstat(current_fd)
        if not stat.S_ISDIR(root_stat.st_mode):
            raise BasinsPackageError(
                "BASINS_PACKAGE_OBJECT_PATH_UNSAFE",
                "Basins package object-store root is not a directory.",
                model_id=model_id,
                version=version,
                path=str(root),
                manifest_uri=manifest_uri,
            )

        parent_path = root
        for component in parts[:-1]:
            try:
                next_fd = os.open(component, directory_flags, dir_fd=current_fd)
            except FileNotFoundError:
                if not create:
                    raise
                os.mkdir(component, 0o777, dir_fd=current_fd)
                next_fd = os.open(component, directory_flags, dir_fd=current_fd)
            except OSError as error:
                if _object_path_component_is_symlink(parent_path / component):
                    raise _object_path_unsafe_error(
                        parent_path / component,
                        model_id=model_id,
                        version=version,
                        manifest_uri=manifest_uri,
                    ) from error
                raise

            try:
                next_stat = os.fstat(next_fd)
                if not stat.S_ISDIR(next_stat.st_mode):
                    raise _object_path_unsafe_error(
                        parent_path / component,
                        model_id=model_id,
                        version=version,
                        manifest_uri=manifest_uri,
                    )
            except Exception:
                os.close(next_fd)
                raise
            os.close(current_fd)
            current_fd = next_fd
            parent_path = parent_path / component

        target = ObjectStoreParent(path=root.joinpath(*parts), name=parts[-1], parent_fd=current_fd)
        current_fd = None
        return target
    except (BasinsPackageError, FileNotFoundError):
        raise
    except OSError as error:
        raise ObjectStoreError(f"Failed to inspect object-store path {key_or_uri}: {error}") from error
    finally:
        if current_fd is not None:
            try:
                os.close(current_fd)
            except OSError:
                pass


def _object_os_open(name: str, flags: int, mode: int, target: ObjectStoreParent) -> int:
    if target.parent_fd is not None:
        return os.open(name, flags, mode, dir_fd=target.parent_fd)
    return os.open(target.path.with_name(name), flags, mode)


def _object_os_replace(source_name: str, target_name: str, target: ObjectStoreParent) -> None:
    if target.parent_fd is not None:
        os.rename(source_name, target_name, src_dir_fd=target.parent_fd, dst_dir_fd=target.parent_fd)
        return
    os.replace(target.path.with_name(source_name), target.path.with_name(target_name))


def _object_os_unlink(name: str, target: ObjectStoreParent) -> None:
    if target.parent_fd is not None:
        os.unlink(name, dir_fd=target.parent_fd)
        return
    target.path.with_name(name).unlink()


def _object_os_stat(name: str, target: ObjectStoreParent) -> os.stat_result:
    if target.parent_fd is not None and _OS_STAT_SUPPORTS_FOLLOW_SYMLINKS:
        return os.stat(name, dir_fd=target.parent_fd, follow_symlinks=False)
    return target.path.with_name(name).lstat()


def _remove_object_temp_path(store: LocalObjectStore, key_or_uri: str, temp_name: str) -> None:
    with _object_parent_for_existing_write(store, key_or_uri) as target:
        try:
            _object_os_unlink(temp_name, target)
        except FileNotFoundError:
            pass


@contextmanager
def _open_object_file_no_symlinks(
    store: LocalObjectStore,
    key_or_uri: str,
    *,
    model_id: str | None = None,
    version: str | None = None,
    manifest_uri: str | None = None,
) -> Iterator[BinaryIO]:
    with _object_parent_for_existing_read(
        store,
        key_or_uri,
        model_id=model_id,
        version=version,
        manifest_uri=manifest_uri,
    ) as target:
        flags = os.O_RDONLY | _object_no_follow_flag() | _object_cloexec_flag()
        try:
            fd = _object_os_open(target.name, flags, 0o666, target)
        except OSError as error:
            if _object_path_component_is_symlink(target.path):
                raise _object_path_unsafe_error(
                    target.path,
                    model_id=model_id,
                    version=version,
                    manifest_uri=manifest_uri,
                ) from error
            raise
        try:
            stat_result = os.fstat(fd)
            if not stat.S_ISREG(stat_result.st_mode):
                raise _object_path_unsafe_error(
                    target.path,
                    model_id=model_id,
                    version=version,
                    manifest_uri=manifest_uri,
                )
            with os.fdopen(fd, "rb") as handle:
                fd = -1
                yield handle
        finally:
            if fd >= 0:
                os.close(fd)


def _object_path_component_is_symlink(path: Path) -> bool:
    try:
        return stat.S_ISLNK(path.lstat().st_mode)
    except OSError:
        return False


def _object_path_unsafe_error(
    path: Path,
    *,
    model_id: str | None = None,
    version: str | None = None,
    manifest_uri: str | None = None,
) -> BasinsPackageError:
    return BasinsPackageError(
        "BASINS_PACKAGE_OBJECT_PATH_UNSAFE",
        "Basins package publication does not follow object-store symlink components.",
        model_id=model_id,
        version=version,
        path=str(path),
        manifest_uri=manifest_uri,
    )


def _object_no_follow_flag() -> int:
    return os.O_NOFOLLOW if hasattr(os, "O_NOFOLLOW") else 0


def _object_cloexec_flag() -> int:
    return os.O_CLOEXEC if hasattr(os, "O_CLOEXEC") else 0


def _open_verified_source_file(
    path: Path,
    source_root: Path,
    *,
    model_id: str | None = None,
    version: str | None = None,
    manifest_uri: str | None = None,
) -> BinaryIO:
    _reject_source_symlink_path(path, source_root, model_id=model_id, version=version, manifest_uri=manifest_uri)
    resolved = _resolve_package_path(path, model_id=model_id, version=version)
    _ensure_under_source_root(resolved, source_root, model_id=model_id, version=version, manifest_uri=manifest_uri)
    if (
        hasattr(os, "O_NOFOLLOW")
        and hasattr(os, "O_DIRECTORY")
        and _OS_OPEN_SUPPORTS_DIR_FD
    ):
        return _open_verified_source_file_at(
            resolved,
            source_root,
            model_id=model_id,
            version=version,
            manifest_uri=manifest_uri,
        )
    try:
        flags = os.O_RDONLY
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        fd = os.open(path, flags)
        stat_result = os.fstat(fd)
        if not stat.S_ISREG(stat_result.st_mode):
            os.close(fd)
            raise BasinsPackageError(
                "BASINS_PACKAGE_PATH_UNSAFE",
                "Basins package source path is not a regular file.",
                model_id=model_id,
                version=version,
                path=str(path),
                manifest_uri=manifest_uri,
            )
        return os.fdopen(fd, "rb")
    except BasinsPackageError:
        raise
    except OSError as error:
        if path.is_symlink() or resolved.is_symlink():
            raise BasinsPackageError(
                "BASINS_PACKAGE_PATH_UNSAFE",
                "Basins package publication does not follow symlink descendants.",
                model_id=model_id,
                version=version,
                path=str(path),
                manifest_uri=manifest_uri,
            ) from error
        raise


def _open_verified_source_file_at(
    resolved: Path,
    source_root: Path,
    *,
    model_id: str | None = None,
    version: str | None = None,
    manifest_uri: str | None = None,
) -> BinaryIO:
    try:
        relative_parts = resolved.relative_to(source_root).parts
    except ValueError as error:
        raise BasinsPackageError(
            "BASINS_PACKAGE_PATH_UNSAFE",
            "Basins package source path resolves outside the model source directory.",
            model_id=model_id,
            version=version,
            path=str(resolved),
            manifest_uri=manifest_uri,
        ) from error
    if not relative_parts:
        raise BasinsPackageError(
            "BASINS_PACKAGE_PATH_UNSAFE",
            "Basins package source path is not a regular file.",
            model_id=model_id,
            version=version,
            path=str(resolved),
            manifest_uri=manifest_uri,
        )

    directory_flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
    file_flags = os.O_RDONLY | os.O_NOFOLLOW
    if hasattr(os, "O_CLOEXEC"):
        directory_flags |= os.O_CLOEXEC
        file_flags |= os.O_CLOEXEC

    open_dirs: list[int] = []
    file_fd: int | None = None
    try:
        root_fd = os.open(source_root, directory_flags)
        open_dirs.append(root_fd)
        root_stat = os.fstat(root_fd)
        if not stat.S_ISDIR(root_stat.st_mode):
            raise BasinsPackageError(
                "BASINS_PACKAGE_PATH_UNSAFE",
                "Basins package source root is not a directory.",
                model_id=model_id,
                version=version,
                path=str(source_root),
                manifest_uri=manifest_uri,
            )

        current_fd = root_fd
        for component in relative_parts[:-1]:
            next_fd = os.open(component, directory_flags, dir_fd=current_fd)
            open_dirs.append(next_fd)
            next_stat = os.fstat(next_fd)
            if not stat.S_ISDIR(next_stat.st_mode):
                raise BasinsPackageError(
                    "BASINS_PACKAGE_PATH_UNSAFE",
                    "Basins package source ancestor is not a directory.",
                    model_id=model_id,
                    version=version,
                    path=str(resolved),
                    manifest_uri=manifest_uri,
                )
            current_fd = next_fd

        file_fd = os.open(relative_parts[-1], file_flags, dir_fd=current_fd)
        file_stat = os.fstat(file_fd)
        if not stat.S_ISREG(file_stat.st_mode):
            os.close(file_fd)
            file_fd = None
            raise BasinsPackageError(
                "BASINS_PACKAGE_PATH_UNSAFE",
                "Basins package source path is not a regular file.",
                model_id=model_id,
                version=version,
                path=str(resolved),
                manifest_uri=manifest_uri,
            )
        _reject_source_symlink_path(
            resolved,
            source_root,
            model_id=model_id,
            version=version,
            manifest_uri=manifest_uri,
            error_path=resolved,
        )
        fresh_resolved = _resolve_package_path(resolved, model_id=model_id, version=version)
        try:
            fresh_resolved.relative_to(source_root)
        except ValueError as error:
            os.close(file_fd)
            file_fd = None
            raise BasinsPackageError(
                "BASINS_PACKAGE_PATH_UNSAFE",
                "Basins package source path was replaced outside the model source directory.",
                model_id=model_id,
                version=version,
                path=str(resolved),
                manifest_uri=manifest_uri,
            ) from error
        fresh_stat = os.stat(fresh_resolved)
        if (fresh_stat.st_dev, fresh_stat.st_ino) != (file_stat.st_dev, file_stat.st_ino):
            os.close(file_fd)
            file_fd = None
            raise BasinsPackageError(
                "BASINS_PACKAGE_PATH_UNSAFE",
                "Basins package source path was replaced during verified open.",
                model_id=model_id,
                version=version,
                path=str(resolved),
                manifest_uri=manifest_uri,
            )
        handle = os.fdopen(file_fd, "rb")
        file_fd = None
        return handle
    except BasinsPackageError:
        raise
    except FileNotFoundError:
        raise
    except OSError as error:
        raise BasinsPackageError(
            "BASINS_PACKAGE_PATH_UNSAFE",
            "Basins package publication does not follow symlink or replaced source descendants.",
            model_id=model_id,
            version=version,
            path=str(resolved),
            manifest_uri=manifest_uri,
        ) from error
    finally:
        if file_fd is not None:
            try:
                os.close(file_fd)
            except OSError:
                pass
        for directory_fd in reversed(open_dirs):
            try:
                os.close(directory_fd)
            except OSError:
                pass


def _verify_object_bytes(
    store: LocalObjectStore,
    key: str,
    *,
    expected_size: int,
    expected_sha256: str,
    model_id: str | None = None,
    version: str | None = None,
    manifest_uri: str | None = None,
) -> None:
    actual_size, actual_sha256 = _object_size_and_checksum_streaming(
        store,
        key,
        model_id=model_id,
        version=version,
        manifest_uri=manifest_uri,
    )
    if actual_size != expected_size or actual_sha256 != expected_sha256:
        raise ObjectStoreError(
            f"Object verification failed for {key}: expected {expected_size}/{expected_sha256}, "
            f"got {actual_size}/{actual_sha256}"
        )


def _object_size_and_checksum_streaming(
    store: LocalObjectStore,
    key: str,
    *,
    model_id: str | None = None,
    version: str | None = None,
    manifest_uri: str | None = None,
) -> tuple[int, str]:
    digest = hashlib.sha256()
    size_bytes = 0
    try:
        with _open_object_file_no_symlinks(
            store,
            key,
            model_id=model_id,
            version=version,
            manifest_uri=manifest_uri,
        ) as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
                size_bytes += len(chunk)
    except BasinsPackageError:
        raise
    except OSError as error:
        raise ObjectStoreError(f"Failed to verify object {key}: {error}") from error
    return size_bytes, digest.hexdigest()


def _success_payload(status: str, manifest: dict[str, Any]) -> dict[str, Any]:
    return {
        "status": status,
        "model_id": manifest["model_id"],
        "version": manifest["version"],
        "model_package_uri": manifest["model_package_uri"],
        "manifest_uri": manifest["manifest_uri"],
        "package_checksum": manifest["package_checksum"],
    }


def _safe_source_dir(
    value: Any,
    inventory_root: Path,
    inventory_relative_root: Path | None,
    source_root: Path,
    field_name: str,
    *,
    expected_path: Path,
    model_id: str | None = None,
    version: str | None = None,
    manifest_uri: str | None = None,
) -> Path:
    if not isinstance(value, str) or not value:
        raise BasinsPackageError(
            "BASINS_INVENTORY_INVALID",
            f"Basins model record is missing {field_name}.",
            model_id=model_id,
            version=version,
            manifest_uri=manifest_uri,
        )
    path = Path(value).expanduser()
    if not path.is_absolute():
        path = _source_dir_from_relative_inventory_value(
            path,
            inventory_root,
            inventory_relative_root,
            source_root,
            expected_path,
            field_name,
            model_id=model_id,
            version=version,
            manifest_uri=manifest_uri,
        )
    _reject_source_symlink_path(path, source_root, model_id=model_id, version=version, manifest_uri=manifest_uri)
    resolved = _resolve_package_path(path)
    _ensure_under_root(
        resolved,
        inventory_root,
        error_code="BASINS_INVENTORY_PATH_MISMATCH",
        message=f"Basins model {field_name} resolves outside the inventory root.",
        model_id=model_id,
        version=version,
        manifest_uri=manifest_uri,
    )
    _ensure_under_source_root(resolved, source_root, model_id=model_id, version=version, manifest_uri=manifest_uri)
    if not resolved.is_dir():
        raise BasinsPackageError(
            "BASINS_SOURCE_NOT_FOUND",
            f"Basins source directory does not exist: {path}",
            model_id=model_id,
            version=version,
            path=str(path),
            manifest_uri=manifest_uri,
        )
    return resolved


def _source_dir_from_relative_inventory_value(
    path: Path,
    inventory_root: Path,
    inventory_relative_root: Path | None,
    source_root: Path,
    expected_path: Path,
    field_name: str,
    *,
    model_id: str | None = None,
    version: str | None = None,
    manifest_uri: str | None = None,
) -> Path:
    normalized = Path(_normalize_relative_path(path.as_posix()))
    if _relative_inventory_path_matches_expected(
        normalized,
        inventory_root,
        inventory_relative_root,
        source_root,
        expected_path,
    ):
        return expected_path
    candidate = inventory_root / normalized
    raise BasinsPackageError(
        "BASINS_INVENTORY_PATH_MISMATCH",
        f"Basins inventory {field_name} does not match the selected model's canonical source path.",
        model_id=model_id,
        version=version,
        path=str(candidate),
        manifest_uri=manifest_uri,
    )


def _relative_inventory_path_matches_expected(
    relative_path: Path,
    inventory_root: Path,
    inventory_relative_root: Path | None,
    source_root: Path,
    expected_path: Path,
) -> bool:
    expected_relative_paths: set[Path] = set()
    for base in (source_root, inventory_root):
        try:
            expected_relative_paths.add(expected_path.relative_to(base))
        except ValueError:
            continue
    if inventory_relative_root is not None:
        try:
            expected_relative_paths.add(inventory_relative_root / expected_path.relative_to(inventory_root))
        except ValueError:
            pass
    return relative_path in expected_relative_paths


def _safe_source_file(
    path: Path,
    source_root: Path,
    *,
    model_id: str | None = None,
    version: str | None = None,
    manifest_uri: str | None = None,
) -> Path:
    _reject_source_symlink_path(path, source_root, model_id=model_id, version=version, manifest_uri=manifest_uri)
    resolved = _resolve_package_path(path)
    _ensure_under_source_root(resolved, source_root, model_id=model_id, version=version, manifest_uri=manifest_uri)
    if not resolved.is_file():
        raise BasinsPackageError(
            "BASINS_SOURCE_NOT_FOUND",
            f"Basins source file does not exist: {path}",
            model_id=model_id,
            version=version,
            path=str(path),
            manifest_uri=manifest_uri,
        )
    return resolved


def _reject_source_symlink_path(
    path: Path,
    source_root: Path,
    *,
    model_id: str | None = None,
    version: str | None = None,
    manifest_uri: str | None = None,
    error_path: Path | None = None,
) -> None:
    current = path if path.is_absolute() else Path.cwd() / path
    parts: list[Path] = []
    while True:
        parts.append(current)
        if current == current.parent:
            break
        current = current.parent

    for candidate in reversed(parts):
        try:
            resolved_parent = _resolve_package_path(candidate.parent)
        except BasinsPackageError:
            continue
        try:
            resolved_parent.relative_to(source_root)
        except ValueError:
            continue
        if candidate.is_symlink():
            raise BasinsPackageError(
                "BASINS_PACKAGE_PATH_UNSAFE",
                "Basins package publication does not follow symlink descendants.",
                model_id=model_id,
                version=version,
                path=str(error_path or candidate),
                manifest_uri=manifest_uri,
            )


def _ensure_under_source_root(
    path: Path,
    source_root: Path,
    *,
    model_id: str | None = None,
    version: str | None = None,
    manifest_uri: str | None = None,
) -> None:
    _ensure_under_root(
        path,
        source_root,
        error_code="BASINS_PACKAGE_PATH_UNSAFE",
        message="Basins package source path resolves outside the model source directory.",
        model_id=model_id,
        version=version,
        manifest_uri=manifest_uri,
    )


def _ensure_under_root(
    path: Path,
    root: Path,
    *,
    error_code: str,
    message: str,
    model_id: str | None = None,
    version: str | None = None,
    manifest_uri: str | None = None,
) -> None:
    try:
        path.relative_to(root)
    except ValueError as error:
        raise BasinsPackageError(
            error_code,
            message,
            model_id=model_id,
            version=version,
            path=str(path),
            manifest_uri=manifest_uri,
        ) from error


def _resolve_package_path(path: Path, *, model_id: str | None = None, version: str | None = None) -> Path:
    try:
        return path.resolve()
    except (OSError, RuntimeError) as error:
        raise BasinsPackageError(
            "BASINS_PACKAGE_PATH_UNRESOLVABLE",
            "Basins package source path cannot be resolved.",
            model_id=model_id,
            version=version,
            path=str(path),
        ) from error


def _normalize_relative_path(value: str) -> str:
    path = Path(value)
    if path.is_absolute() or ".." in path.parts:
        raise BasinsPackageError("BASINS_PACKAGE_PATH_UNSAFE", f"Unsafe package relative path: {value}", path=value)
    normalized = path.as_posix().strip("/")
    if not normalized:
        raise BasinsPackageError("BASINS_PACKAGE_PATH_UNSAFE", "Package relative path is empty.")
    return normalized


def _validate_object_key_segment(
    value: str,
    field_name: str,
    *,
    model_id: str,
    version: str,
) -> None:
    if value != value.strip():
        raise BasinsPackageError(
            "BASINS_PACKAGE_IDENTIFIER_INVALID",
            f"Basins package {field_name} must not contain leading or trailing whitespace.",
            model_id=model_id,
            version=version,
        )
    if value in {"", ".", ".."}:
        raise BasinsPackageError(
            "BASINS_PACKAGE_IDENTIFIER_INVALID",
            f"Basins package {field_name} must be a non-empty safe object-key segment.",
            model_id=model_id,
            version=version,
        )
    if not all(character.isascii() and (character.isalnum() or character in {"_", "-", "."}) for character in value):
        raise BasinsPackageError(
            "BASINS_PACKAGE_IDENTIFIER_INVALID",
            f"Basins package {field_name} must be a single safe object-key segment.",
            model_id=model_id,
            version=version,
        )


def _walk_source_files(root: Path, source_root: Path) -> Iterator[Path]:
    resolved_root = _resolve_package_path(root)
    _ensure_under_source_root(resolved_root, source_root)
    stack = [resolved_root]
    visited_dirs = {resolved_root}
    while stack:
        directory = stack.pop()
        try:
            children = sorted(directory.iterdir(), key=lambda path: path.name.lower())
        except OSError as error:
            raise BasinsPackageError(
                "BASINS_PACKAGE_PATH_UNRESOLVABLE",
                "Basins package source directory cannot be traversed.",
                path=str(directory),
            ) from error
        for child in children:
            if _is_ignored_source_path(child):
                continue
            if child.is_symlink():
                raise BasinsPackageError(
                    "BASINS_PACKAGE_PATH_UNSAFE",
                    "Basins package publication does not follow symlink descendants.",
                    path=str(child),
                )
            resolved = _resolve_package_path(child)
            _ensure_under_source_root(resolved, source_root)
            if child.is_dir():
                if resolved in visited_dirs:
                    continue
                visited_dirs.add(resolved)
                stack.append(resolved)
            elif child.is_file():
                yield resolved


def _directory_evidence(root: Path) -> tuple[int, int, str]:
    resolved_root = _resolve_package_path(root)
    digest = hashlib.sha256()
    file_count = 0
    byte_count = 0
    for path in _walk_source_files(resolved_root, resolved_root):
        relative_path = path.relative_to(resolved_root).as_posix()
        size_bytes, sha256 = _migration_source_file_evidence(path, resolved_root)
        digest.update(relative_path.encode("utf-8"))
        digest.update(b"\0")
        digest.update(str(size_bytes).encode("ascii"))
        digest.update(b"\0")
        digest.update(sha256.encode("ascii"))
        digest.update(b"\0")
        file_count += 1
        byte_count += size_bytes
    return file_count, byte_count, digest.hexdigest()


def _csv_time_evidence(
    path: Path,
    source_root: Path,
    *,
    model_id: str | None = None,
    version: str | None = None,
    manifest_uri: str | None = None,
) -> tuple[str | None, str | None, str | None, int]:
    try:
        with (
            _open_verified_source_file(
                path,
                source_root,
                model_id=model_id,
                version=version,
                manifest_uri=manifest_uri,
            ) as source,
            open(source.fileno(), "r", encoding="utf-8", errors="replace", newline="", closefd=False) as handle,
        ):
            header = handle.readline(FORCING_SAMPLE_BYTE_LIMIT).strip()
            first_time: str | None = None
            last_time: str | None = None
            row_count = 0
            consumed_bytes = len(header.encode("utf-8", errors="replace"))
            for line in handle:
                consumed_bytes += len(line.encode("utf-8", errors="replace"))
                if consumed_bytes > FORCING_SAMPLE_BYTE_LIMIT or row_count >= FORCING_SAMPLE_LINE_LIMIT:
                    break
                stripped = line.strip()
                if not stripped:
                    continue
                value = stripped.split(",", 1)[0].strip()
                if not value:
                    continue
                first_time = value if first_time is None else first_time
                last_time = value
                row_count += 1
            return header or None, first_time, last_time, row_count
    except OSError as error:
        raise BasinsPackageError(
            "BASINS_PACKAGE_WRITE_FAILED",
            f"Failed to read Basins forcing sample file: {path}: {error}",
            model_id=model_id,
            version=version,
            path=str(path),
            manifest_uri=manifest_uri,
        ) from error


def _is_ignored_source_path(path: Path) -> bool:
    return any(part == ".DS_Store" or part == "@eaDir" or part.endswith("@SynoEAStream") for part in path.parts)


def _directory_uri(object_store: LocalObjectStore, key: str) -> str:
    return object_store.uri_for_key(key).rstrip("/") + "/"


def _write_json_file(
    path: str | Path,
    payload: dict[str, Any],
    *,
    error_code: str,
    model_id: str | None = None,
    version: str | None = None,
    manifest_uri: str | None = None,
) -> None:
    output = Path(path).expanduser()
    try:
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_bytes(_json_bytes(payload))
    except OSError as error:
        raise BasinsPackageError(
            error_code,
            f"Failed to write Basins output JSON: {output}: {error}",
            model_id=model_id,
            version=version,
            path=str(output),
            manifest_uri=manifest_uri,
        ) from error


def _preflight_json_output_path(
    path: str | Path,
    *,
    error_code: str,
    model_id: str | None = None,
    version: str | None = None,
    manifest_uri: str | None = None,
) -> None:
    output = Path(path).expanduser()
    try:
        output.parent.mkdir(parents=True, exist_ok=True)
    except OSError as error:
        raise BasinsPackageError(
            error_code,
            f"Failed to prepare Basins output JSON path: {output}: {error}",
            model_id=model_id,
            version=version,
            path=str(output),
            manifest_uri=manifest_uri,
        ) from error


def _json_bytes(payload: dict[str, Any]) -> bytes:
    return (json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode("utf-8")


def _sha256_json(payload: Any) -> str:
    return _sha256_bytes(json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8"))


def _sha256_bytes(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def _sha256_file(path: Path) -> str:
    with path.open("rb") as handle:
        return _sha256_handle(handle)


def _sha256_handle(handle: BinaryIO) -> str:
    digest = hashlib.sha256()
    for chunk in iter(lambda: handle.read(1024 * 1024), b""):
        digest.update(chunk)
    return digest.hexdigest()
