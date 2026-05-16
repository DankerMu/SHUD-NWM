from __future__ import annotations

import hashlib
import json
import os
import uuid
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from packages.common.object_store import LocalObjectStore, ObjectStoreError

from .basins_discovery import (
    GIS_REQUIRED_FILES,
    SHUD_REQUIRED_PATTERNS,
    BasinsDiscoveryError,
    discover_basins_inventory,
)

BASINS_PACKAGE_SCHEMA_VERSION = "basins.package.v1"
BASINS_MIGRATION_REPORT_SCHEMA_VERSION = "basins.migration.v1"
FORCING_SAMPLE_FILE_LIMIT = 5
FORCING_SAMPLE_BYTE_LIMIT = 64 * 1024
FORCING_SAMPLE_LINE_LIMIT = 1000


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
    relative_path: str
    object_key: str
    object_uri: str
    role: str


def publish_basins_package(
    *,
    inventory_path: str | Path,
    model_id: str,
    version: str,
    output_path: str | Path,
    copy_forcing: bool = False,
    object_store: LocalObjectStore | None = None,
) -> dict[str, Any]:
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
    source_root = _resolved_source_root(model, inventory_root, model_id, version)

    package_files = _package_source_files(
        model,
        inventory_root,
        source_root,
        store,
        package_key,
        model_id=model_id,
        version=version,
    )
    forcing, forcing_files = _forcing_metadata(
        model=model,
        inventory_root=inventory_root,
        source_root=source_root,
        object_store=store,
        forcing_key=forcing_key,
        copy_forcing=copy_forcing,
    )
    source_files = [*package_files, *(forcing_files if copy_forcing else [])]
    planned_included_files = sorted(
        [_planned_file_entry(source_file) for source_file in source_files],
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

    if store.exists(manifest_key):
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
        if store.exists(manifest_key):
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
            included_files.append(_write_source_file_to_store(source_file, store))
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
        store.write_bytes_atomic(manifest_key, manifest_bytes)
        _verify_object_bytes(
            store,
            manifest_key,
            expected_size=len(manifest_bytes),
            expected_sha256=_sha256_bytes(manifest_bytes),
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
    except json.JSONDecodeError as error:
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
    for model in models:
        if isinstance(model, dict) and model.get("model_id") == model_id:
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
    source_root: Path,
    object_store: LocalObjectStore,
    package_key: str,
    *,
    model_id: str,
    version: str,
) -> list[SourceFile]:
    input_dir = _safe_source_dir(model.get("input_dir"), inventory_root, source_root, "input_dir")
    required_files = model.get("required_files")
    if not isinstance(required_files, dict):
        raise BasinsPackageError("BASINS_INVENTORY_INVALID", "Basins model record is missing required_files.")
    _validate_canonical_required_files(
        required_files,
        input_dir,
        source_root,
        model_id=model_id,
        version=version,
    )

    files: list[SourceFile] = []
    for role, relative_names in required_files.items():
        if not isinstance(relative_names, list):
            continue
        entry_role = "gis" if str(role).startswith("gis_") else "runtime_input"
        for relative_name in relative_names:
            relative_path = _normalize_relative_path(str(relative_name))
            source_path = _safe_source_file(input_dir / relative_path, source_root)
            files.append(
                SourceFile(
                    source_path=source_path,
                    relative_path=relative_path,
                    object_key=f"{package_key}/{relative_path}",
                    object_uri=object_store.uri_for_key(f"{package_key}/{relative_path}"),
                    role=entry_role,
                )
            )

    calib_path = source_root / "CALIB"
    _reject_source_symlink_path(calib_path, source_root)
    if calib_path.is_dir():
        calib_dir = _resolve_package_path(calib_path)
        _ensure_under_source_root(calib_dir, source_root)
        for path in _walk_source_files(calib_path, source_root):
            relative_path = _normalize_relative_path(Path("CALIB", path.relative_to(calib_dir)).as_posix())
            files.append(
                SourceFile(
                    source_path=path,
                    relative_path=relative_path,
                    object_key=f"{package_key}/{relative_path}",
                    object_uri=object_store.uri_for_key(f"{package_key}/{relative_path}"),
                    role="calibration",
                )
            )
    return sorted(files, key=lambda item: (item.role, item.relative_path))


def _validate_canonical_required_files(
    required_files: dict[str, Any],
    input_dir: Path,
    source_root: Path,
    *,
    model_id: str,
    version: str,
) -> None:
    missing: list[str] = []
    for role, pattern in SHUD_REQUIRED_PATTERNS:
        relative_names = required_files.get(role)
        if not isinstance(relative_names, list) or not relative_names:
            missing.append(role)
            continue
        matching_paths = []
        for relative_name in relative_names:
            relative_path = _normalize_relative_path(str(relative_name))
            if Path(relative_path).match(pattern):
                matching_paths.append(relative_path)
        if not matching_paths:
            missing.append(role)
            continue
        for relative_path in matching_paths:
            _safe_source_file(input_dir / relative_path, source_root)

    for role, file_name in GIS_REQUIRED_FILES:
        expected_path = _normalize_relative_path(f"gis/{file_name}")
        relative_names = required_files.get(role)
        if not isinstance(relative_names, list) or expected_path not in [str(name) for name in relative_names]:
            missing.append(role)
            continue
        _safe_source_file(input_dir / expected_path, source_root)

    if missing:
        roles = ", ".join(sorted(missing))
        raise BasinsPackageError(
            "BASINS_REQUIRED_FILES_MISSING",
            f"Basins inventory is missing canonical required file roles or paths: {roles}",
            model_id=model_id or None,
            version=version,
            path=str(input_dir),
        )


def _forcing_metadata(
    *,
    model: dict[str, Any],
    inventory_root: Path,
    source_root: Path,
    object_store: LocalObjectStore,
    forcing_key: str,
    copy_forcing: bool,
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

    forcing_dir = _safe_source_dir(forcing_dir_value, inventory_root, source_root, "forcing_dir")
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
        size_bytes = path.stat().st_size
        sha256 = _sha256_file(path)
        digest.update(relative_path.encode("utf-8"))
        digest.update(b"\0")
        digest.update(str(size_bytes).encode("ascii"))
        digest.update(b"\0")
        digest.update(sha256.encode("ascii"))
        digest.update(b"\0")
        total_bytes += size_bytes
        if sampled_file_count < FORCING_SAMPLE_FILE_LIMIT:
            header, first_time, last_time, row_count = _csv_time_evidence(path)
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


def _planned_file_entry(source_file: SourceFile) -> dict[str, Any]:
    return {
        "relative_path": source_file.relative_path,
        "role": source_file.role,
        "size_bytes": source_file.source_path.stat().st_size,
        "sha256": _sha256_file(source_file.source_path),
    }


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
        manifest = json.loads(store.read_bytes(manifest_key).decode("utf-8"))
    except (ObjectStoreError, json.JSONDecodeError, UnicodeDecodeError) as error:
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
    lock_path = store.resolve_path(lock_key)
    try:
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(lock_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL)
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
        store.resolve_path(lock_key).unlink(missing_ok=True)
    except (OSError, ValueError):
        pass


def _write_source_file_to_store(source_file: SourceFile, store: LocalObjectStore) -> dict[str, Any]:
    size_bytes, sha256 = _write_file_to_store_streaming(store, source_file.object_key, source_file.source_path)
    _verify_object_bytes(store, source_file.object_key, expected_size=size_bytes, expected_sha256=sha256)
    return _manifest_file_entry_for_source_file(source_file, size_bytes=size_bytes, sha256=sha256)


def _write_file_to_store_streaming(store: LocalObjectStore, key: str, source_path: Path) -> tuple[int, str]:
    target_path = store.resolve_path(key)
    temp_path = target_path.with_name(f".{target_path.name}.{uuid.uuid4().hex}.part")
    digest = hashlib.sha256()
    size_bytes = 0
    try:
        target_path.parent.mkdir(parents=True, exist_ok=True)
        with source_path.open("rb") as source, temp_path.open("wb") as target:
            for chunk in iter(lambda: source.read(1024 * 1024), b""):
                target.write(chunk)
                digest.update(chunk)
                size_bytes += len(chunk)
        os.replace(temp_path, target_path)
    except OSError as error:
        try:
            temp_path.unlink(missing_ok=True)
        except OSError as cleanup_error:
            raise ObjectStoreError(
                f"Failed to write object {key}: {error}; cleanup also failed: {cleanup_error}"
            ) from cleanup_error
        raise ObjectStoreError(f"Failed to write object {key}: {error}") from error
    return size_bytes, digest.hexdigest()


def _verify_object_bytes(
    store: LocalObjectStore,
    key: str,
    *,
    expected_size: int,
    expected_sha256: str,
) -> None:
    actual_size, actual_sha256 = _object_size_and_checksum_streaming(store, key)
    if actual_size != expected_size or actual_sha256 != expected_sha256:
        raise ObjectStoreError(
            f"Object verification failed for {key}: expected {expected_size}/{expected_sha256}, "
            f"got {actual_size}/{actual_sha256}"
        )


def _object_size_and_checksum_streaming(store: LocalObjectStore, key: str) -> tuple[int, str]:
    path = store.resolve_path(key)
    digest = hashlib.sha256()
    size_bytes = 0
    try:
        with path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
                size_bytes += len(chunk)
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


def _safe_source_dir(value: Any, inventory_root: Path, source_root: Path, field_name: str) -> Path:
    if not isinstance(value, str) or not value:
        raise BasinsPackageError("BASINS_INVENTORY_INVALID", f"Basins model record is missing {field_name}.")
    path = Path(value).expanduser()
    _reject_source_symlink_path(path, source_root)
    resolved = _resolve_package_path(path)
    _ensure_under_root(
        resolved,
        inventory_root,
        error_code="BASINS_INVENTORY_PATH_MISMATCH",
        message=f"Basins model {field_name} resolves outside the inventory root.",
    )
    _ensure_under_source_root(resolved, source_root)
    if not resolved.is_dir():
        raise BasinsPackageError(
            "BASINS_SOURCE_NOT_FOUND",
            f"Basins source directory does not exist: {path}",
            path=str(path),
        )
    return resolved


def _safe_source_file(path: Path, source_root: Path) -> Path:
    _reject_source_symlink_path(path, source_root)
    resolved = _resolve_package_path(path)
    _ensure_under_source_root(resolved, source_root)
    if not resolved.is_file():
        raise BasinsPackageError(
            "BASINS_SOURCE_NOT_FOUND",
            f"Basins source file does not exist: {path}",
            path=str(path),
        )
    return resolved


def _reject_source_symlink_path(path: Path, source_root: Path) -> None:
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
                path=str(candidate),
            )


def _ensure_under_source_root(path: Path, source_root: Path) -> None:
    _ensure_under_root(
        path,
        source_root,
        error_code="BASINS_PACKAGE_PATH_UNSAFE",
        message="Basins package source path resolves outside the model source directory.",
    )


def _ensure_under_root(
    path: Path,
    root: Path,
    *,
    error_code: str,
    message: str,
    model_id: str | None = None,
    version: str | None = None,
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
        size_bytes = path.stat().st_size
        sha256 = _sha256_file(path)
        digest.update(relative_path.encode("utf-8"))
        digest.update(b"\0")
        digest.update(str(size_bytes).encode("ascii"))
        digest.update(b"\0")
        digest.update(sha256.encode("ascii"))
        digest.update(b"\0")
        file_count += 1
        byte_count += size_bytes
    return file_count, byte_count, digest.hexdigest()


def _csv_time_evidence(path: Path) -> tuple[str | None, str | None, str | None, int]:
    try:
        with path.open("r", encoding="utf-8", errors="replace", newline="") as handle:
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
    except OSError:
        return None, None, None, 0


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
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()
