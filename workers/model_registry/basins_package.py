from __future__ import annotations

import hashlib as hashlib
import json
import os
import stat as stat
import uuid as uuid
from collections.abc import Iterator, Mapping, Sequence
from contextlib import contextmanager as contextmanager
from dataclasses import dataclass as dataclass
from datetime import UTC, datetime
from errno import ENOENT as ENOENT
from fnmatch import fnmatchcase as fnmatchcase
from pathlib import Path
from typing import Any, Callable
from typing import BinaryIO as BinaryIO

from packages.common.object_store import MAX_OBJECT_MANIFEST_BYTES, LocalObjectStore, ObjectStoreError
from packages.common.storage import validate_object_path as validate_object_path

from .basins_discovery import (
    GIS_REQUIRED_FILES as GIS_REQUIRED_FILES,
)
from .basins_discovery import (
    SHUD_REQUIRED_PATTERNS as SHUD_REQUIRED_PATTERNS,
)
from .basins_discovery import (
    BasinsDiscoveryError as BasinsDiscoveryError,
)
from .basins_discovery import (
    _classify_basins_root_metadata as _classify_basins_root_metadata,
)
from .basins_discovery import _slug_id as _basins_slug_id  # noqa: F401
from .basins_discovery import (
    discover_basins_inventory as discover_basins_inventory,
)
from .basins_package_contracts import (
    BASINS_MIGRATION_REPORT_SCHEMA_VERSION as BASINS_MIGRATION_REPORT_SCHEMA_VERSION,
)
from .basins_package_contracts import (
    BASINS_PACKAGE_SCHEMA_VERSION as BASINS_PACKAGE_SCHEMA_VERSION,
)
from .basins_package_contracts import (
    BASINS_PACKAGE_SCHEMA_VERSION_V1 as BASINS_PACKAGE_SCHEMA_VERSION_V1,
)
from .basins_package_contracts import (
    BASINS_PACKAGE_SOURCE_IDENTITY_SCHEMA_VERSION as BASINS_PACKAGE_SOURCE_IDENTITY_SCHEMA_VERSION,
)
from .basins_package_contracts import (
    FORCING_SAMPLE_BYTE_LIMIT as FORCING_SAMPLE_BYTE_LIMIT,
)
from .basins_package_contracts import (
    FORCING_SAMPLE_LINE_LIMIT as FORCING_SAMPLE_LINE_LIMIT,
)
from .basins_package_contracts import (
    SUPPORTED_BASINS_PACKAGE_SCHEMA_VERSIONS as SUPPORTED_BASINS_PACKAGE_SCHEMA_VERSIONS,
)
from .basins_package_contracts import (
    BasinsPackageError as BasinsPackageError,
)
from .basins_package_contracts import (
    ObjectStoreParent as ObjectStoreParent,
)
from .basins_package_contracts import (
    SourceFile as SourceFile,
)
from .basins_package_contracts import (
    _json_bytes as _json_bytes,
)
from .basins_package_contracts import (
    _sha256_bytes as _sha256_bytes,
)
from .basins_package_contracts import (
    _sha256_file as _sha256_file,
)
from .basins_package_contracts import (
    _sha256_handle as _sha256_handle,
)
from .basins_package_contracts import (
    _sha256_json as _sha256_json,
)
from .basins_package_contracts import (
    forcing_checksum_material_for_schema_version as forcing_checksum_material_for_schema_version,
)
from .basins_package_inventory import (
    _canonical_basin_slug_from_source_path as _canonical_basin_slug_from_source_path,
)
from .basins_package_inventory import (
    _canonical_shud_required_file_name as _canonical_shud_required_file_name,
)
from .basins_package_inventory import (
    _ensure_inventory_path_matches_expected as _ensure_inventory_path_matches_expected,
)
from .basins_package_inventory import (
    _expected_forcing_dir as _expected_forcing_dir,
)
from .basins_package_inventory import (
    _expected_input_dir as _expected_input_dir,
)
from .basins_package_inventory import (
    _find_publishable_model as _find_publishable_model,
)
from .basins_package_inventory import (
    _optional_shud_runtime_files as _optional_shud_runtime_files,
)
from .basins_package_inventory import _package_source_files as _leaf_package_source_files
from .basins_package_inventory import (
    _planned_file_entry as _planned_file_entry,
)
from .basins_package_inventory import (
    _read_inventory as _read_inventory,
)
from .basins_package_inventory import (
    _recorded_relative_inventory_root as _recorded_relative_inventory_root,
)
from .basins_package_inventory import (
    _resolved_inventory_root as _resolved_inventory_root,
)
from .basins_package_inventory import (
    _resolved_source_root as _resolved_source_root,
)
from .basins_package_inventory import (
    _source_file_for_package as _source_file_for_package,
)
from .basins_package_inventory import (
    _source_identity_from_plan as _source_identity_from_plan,
)
from .basins_package_inventory import (
    _validated_canonical_required_source_files as _validated_canonical_required_source_files,
)
from .basins_package_inventory import (
    _verify_expected_source_identity as _verify_expected_source_identity,
)
from .basins_package_inventory import (
    _verify_model_id_matches_canonical_identity as _verify_model_id_matches_canonical_identity,
)
from .basins_package_manifest import (
    _calibration_metadata as _calibration_metadata,
)
from .basins_package_manifest import _forcing_metadata as _leaf_forcing_metadata
from .basins_package_manifest import (
    _forcing_metadata_from_written_entries as _forcing_metadata_from_written_entries,
)
from .basins_package_manifest import (
    _manifest_file_entry as _manifest_file_entry,
)
from .basins_package_manifest import (
    _manifest_payload_without_self_entry as _manifest_payload_without_self_entry,
)
from .basins_package_manifest import (
    _manifest_with_manifest_entry as _manifest_with_manifest_entry,
)
from .basins_package_manifest import (
    _success_payload as _success_payload,
)
from .basins_package_manifest import _verify_existing_manifest_consistency as _leaf_verify_existing_manifest_consistency
from .basins_package_object_store import (
    _acquire_publish_lock as _acquire_publish_lock,
)
from .basins_package_object_store import (
    _directory_uri as _directory_uri,
)
from .basins_package_object_store import (
    _manifest_file_entry_for_source_file as _manifest_file_entry_for_source_file,
)
from .basins_package_object_store import (
    _object_cloexec_flag as _object_cloexec_flag,
)
from .basins_package_object_store import (
    _object_exists_no_symlinks as _object_exists_no_symlinks,
)
from .basins_package_object_store import (
    _object_key_parts as _object_key_parts,
)
from .basins_package_object_store import (
    _object_no_follow_flag as _object_no_follow_flag,
)
from .basins_package_object_store import (
    _object_os_open as _object_os_open,
)
from .basins_package_object_store import (
    _object_os_replace as _object_os_replace,
)
from .basins_package_object_store import (
    _object_os_stat as _object_os_stat,
)
from .basins_package_object_store import (
    _object_os_unlink as _object_os_unlink,
)
from .basins_package_object_store import (
    _object_parent_for_existing_read as _object_parent_for_existing_read,
)
from .basins_package_object_store import (
    _object_parent_for_existing_write as _object_parent_for_existing_write,
)
from .basins_package_object_store import (
    _object_parent_for_write as _object_parent_for_write,
)
from .basins_package_object_store import (
    _object_path_component_is_symlink as _object_path_component_is_symlink,
)
from .basins_package_object_store import (
    _object_path_for_key as _object_path_for_key,
)
from .basins_package_object_store import (
    _object_path_rejecting_symlinks as _object_path_rejecting_symlinks,
)
from .basins_package_object_store import (
    _object_path_unsafe_error as _object_path_unsafe_error,
)
from .basins_package_object_store import _object_size_and_checksum_streaming as _leaf_object_size_and_checksum_streaming
from .basins_package_object_store import (
    _object_store_from_env as _object_store_from_env,
)
from .basins_package_object_store import (
    _open_object_file_no_symlinks as _open_object_file_no_symlinks,
)
from .basins_package_object_store import (
    _open_object_parent_at as _open_object_parent_at,
)
from .basins_package_object_store import (
    _preflight_object_store_keys as _preflight_object_store_keys,
)
from .basins_package_object_store import (
    _read_object_bytes_no_symlinks as _read_object_bytes_no_symlinks,
)
from .basins_package_object_store import (
    _release_publish_lock as _release_publish_lock,
)
from .basins_package_object_store import (
    _remove_object_temp_path as _remove_object_temp_path,
)
from .basins_package_object_store import (
    _validate_object_key_segment as _validate_object_key_segment,
)
from .basins_package_object_store import _verify_object_bytes as _leaf_verify_object_bytes
from .basins_package_object_store import (
    _write_bytes_to_store_atomic as _write_bytes_to_store_atomic,
)
from .basins_package_object_store import _write_file_to_store_streaming as _leaf_write_file_to_store_streaming
from .basins_package_object_store import _write_source_file_to_store as _leaf_write_source_file_to_store
from .basins_package_source_io import _csv_time_evidence as _leaf_csv_time_evidence
from .basins_package_source_io import _directory_evidence as _leaf_directory_evidence
from .basins_package_source_io import (
    _ensure_under_root as _ensure_under_root,
)
from .basins_package_source_io import (
    _ensure_under_source_root as _ensure_under_source_root,
)
from .basins_package_source_io import (
    _is_ignored_source_path as _is_ignored_source_path,
)
from .basins_package_source_io import _migration_source_file_evidence as _leaf_migration_source_file_evidence
from .basins_package_source_io import (
    _normalize_relative_path as _normalize_relative_path,
)
from .basins_package_source_io import (
    _open_verified_source_file as _open_verified_source_file,
)
from .basins_package_source_io import (
    _open_verified_source_file_at as _open_verified_source_file_at,
)
from .basins_package_source_io import (
    _preflight_json_output_path as _preflight_json_output_path,
)
from .basins_package_source_io import (
    _reject_source_symlink_path as _reject_source_symlink_path,
)
from .basins_package_source_io import (
    _relative_inventory_path_matches_expected as _relative_inventory_path_matches_expected,
)
from .basins_package_source_io import (
    _resolve_package_path as _resolve_package_path,
)
from .basins_package_source_io import (
    _safe_source_dir as _safe_source_dir,
)
from .basins_package_source_io import (
    _safe_source_file as _safe_source_file,
)
from .basins_package_source_io import (
    _source_dir_from_relative_inventory_value as _source_dir_from_relative_inventory_value,
)
from .basins_package_source_io import (
    _source_file_evidence as _source_file_evidence,
)
from .basins_package_source_io import _source_file_size as _leaf_source_file_size
from .basins_package_source_io import (
    _verified_source_file_evidence as _verified_source_file_evidence,
)
from .basins_package_source_io import _walk_source_files as _leaf_walk_source_files
from .basins_package_source_io import (
    _write_json_file as _write_json_file,
)

# #1813: forcing CSV payload evidence left package identity here.  The bump is
# itself the named identity migration -- BASINS_PACKAGE_SCHEMA_VERSION is inside
# the content material, so a republished package re-mints its identity under a
# declared packaging schema change rather than under a silent content change.
FORCING_SAMPLE_FILE_LIMIT = 5
MAX_EXISTING_MANIFEST_BYTES = MAX_OBJECT_MANIFEST_BYTES
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


def publish_basins_package(
    *,
    inventory_path: str | Path,
    model_id: str,
    version: str,
    output_path: str | Path,
    copy_forcing: bool = False,
    object_store: LocalObjectStore | None = None,
    output_capacity_guard: Callable[[Path, int], None] | None = None,
    output_write_guard: Callable[[Path, int], None] | None = None,
    expected_source_identity: dict[str, Any] | None = None,
    calibration_overrides: Sequence[Mapping[str, Any]] | None = None,
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
    source_identity, planned_included_files = _source_identity_from_plan(
        model=model,
        package_files=source_files,
        forcing=forcing,
        copy_forcing=copy_forcing,
        model_id=model_id,
        version=version,
        manifest_uri=manifest_uri,
    )
    _verify_expected_source_identity(
        expected_source_identity,
        source_identity,
        model_id=model_id,
        version=version,
        manifest_uri=manifest_uri,
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
        _verify_existing_manifest_consistency(
            store,
            existing_manifest,
            checksum_material=checksum_material,
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
            before_write=output_write_guard,
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
            _verify_existing_manifest_consistency(
                store,
                existing_manifest,
                checksum_material=checksum_material,
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
                before_write=output_write_guard,
            )
            return _success_payload("already_done", existing_manifest)

        if output_capacity_guard is not None:
            output_capacity_guard(Path(output_path).expanduser(), MAX_EXISTING_MANIFEST_BYTES)
        elif output_write_guard is None:
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
            "calibration": _calibration_metadata(model, included_files, calibration_overrides),
            "created_at": created_at,
        }
        manifest, manifest_bytes = _manifest_with_manifest_entry(
            manifest_without_self_entry,
            included_files,
            object_store=store,
            manifest_key=manifest_key,
        )
        if len(manifest_bytes) > MAX_EXISTING_MANIFEST_BYTES:
            raise BasinsPackageError(
                "BASINS_PACKAGE_MANIFEST_TOO_LARGE",
                "Generated Basins package manifest exceeds the bounded manifest size.",
                model_id=model_id,
                version=version,
                manifest_uri=manifest_uri,
            )
        if output_write_guard is not None:
            output_write_guard(Path(output_path).expanduser(), len(manifest_bytes))
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

def basins_package_source_identity(
    *,
    inventory_path: str | Path,
    model_id: str,
) -> dict[str, Any]:
    """Plan a root-independent identity for every source affecting a package."""

    version = "source-identity"
    inventory, _inventory_bytes = _read_inventory(inventory_path)
    model = _find_publishable_model(inventory, model_id, version)
    inventory_root = _resolved_inventory_root(inventory, model_id, version)
    inventory_relative_root = _recorded_relative_inventory_root(inventory)
    source_root = _resolved_source_root(model, inventory_root, model_id, version)
    package_files = _package_source_files(
        model,
        inventory_root,
        inventory_relative_root,
        source_root,
        None,
        "",
        model_id=model_id,
        version=version,
        manifest_uri=None,
    )
    forcing, forcing_files = _forcing_metadata(
        model=model,
        inventory_root=inventory_root,
        inventory_relative_root=inventory_relative_root,
        source_root=source_root,
        object_store=None,
        forcing_key="",
        copy_forcing=False,
        model_id=model_id,
        version=version,
        manifest_uri=None,
    )
    source_files = [*package_files, *forcing_files]
    identity, _planned_entries = _source_identity_from_plan(
        model=model,
        package_files=source_files,
        forcing=forcing,
        copy_forcing=False,
        model_id=model_id,
        version=version,
        manifest_uri=None,
    )
    return identity

def write_basins_migration_report(
    *,
    basins_root: str | Path,
    source_uri: str,
    output_path: str | Path,
) -> dict[str, Any]:
    root = Path(basins_root).expanduser()
    try:
        root_is_symlink = _classify_basins_root_metadata(root)
    except BasinsDiscoveryError as error:
        raise BasinsPackageError(error.error_code, str(error), path=error.path or str(root)) from error
    if root_is_symlink:
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


def _forcing_checksum_material(forcing: Mapping[str, Any]) -> dict[str, Any]:
    return forcing_checksum_material_for_schema_version(forcing, BASINS_PACKAGE_SCHEMA_VERSION)


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
                max_bytes=MAX_EXISTING_MANIFEST_BYTES,
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


# Eight patchable callable seams: facade wrappers forwarding the facade's current
# runtime binding into the leaf implementation function, so a monkeypatch on the
# historical module object is observed by the moved call path.
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
) -> list[SourceFile]:
    return _leaf_package_source_files(
        model,
        inventory_root,
        inventory_relative_root,
        source_root,
        object_store,
        package_key,
        model_id=model_id,
        version=version,
        manifest_uri=manifest_uri,
        walk_source_files=_walk_source_files,
    )


def _walk_source_files(root: Path, source_root: Path) -> Iterator[Path]:
    return _leaf_walk_source_files(root, source_root)


def _csv_time_evidence(
    path: Path,
    source_root: Path,
    *,
    model_id: str | None = None,
    version: str | None = None,
    manifest_uri: str | None = None,
) -> tuple[str | None, str | None, str | None, int]:
    return _leaf_csv_time_evidence(
        path,
        source_root,
        model_id=model_id,
        version=version,
        manifest_uri=manifest_uri,
    )


def _migration_source_file_evidence(path: Path, source_root: Path) -> tuple[int, str]:
    return _leaf_migration_source_file_evidence(path, source_root)


def _source_file_size(
    path: Path,
    source_root: Path,
    *,
    model_id: str | None = None,
    version: str | None = None,
    manifest_uri: str | None = None,
) -> int:
    return _leaf_source_file_size(
        path,
        source_root,
        model_id=model_id,
        version=version,
        manifest_uri=manifest_uri,
    )


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
    return _leaf_write_file_to_store_streaming(
        store,
        key,
        source_path,
        source_root,
        model_id=model_id,
        version=version,
        manifest_uri=manifest_uri,
    )


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
    _leaf_verify_object_bytes(
        store,
        key,
        expected_size=expected_size,
        expected_sha256=expected_sha256,
        model_id=model_id,
        version=version,
        manifest_uri=manifest_uri,
        object_size_and_checksum_streaming=_object_size_and_checksum_streaming,
    )


def _object_size_and_checksum_streaming(
    store: LocalObjectStore,
    key: str,
    *,
    model_id: str | None = None,
    version: str | None = None,
    manifest_uri: str | None = None,
) -> tuple[int, str]:
    return _leaf_object_size_and_checksum_streaming(
        store,
        key,
        model_id=model_id,
        version=version,
        manifest_uri=manifest_uri,
    )


# Four coordinator helpers that forward the same facade runtime bindings.  They
# are not among the eight governed callable seams, but each calls at least one:
# a test patching the facade helper must still alter the real call path.
def _forcing_metadata(
    *,
    model: dict[str, Any],
    inventory_root: Path,
    inventory_relative_root: Path | None,
    source_root: Path,
    object_store: LocalObjectStore | None,
    forcing_key: str,
    copy_forcing: bool,
    model_id: str,
    version: str,
    manifest_uri: str | None = None,
) -> tuple[dict[str, Any], list[SourceFile]]:
    return _leaf_forcing_metadata(
        model=model,
        inventory_root=inventory_root,
        inventory_relative_root=inventory_relative_root,
        source_root=source_root,
        object_store=object_store,
        forcing_key=forcing_key,
        copy_forcing=copy_forcing,
        model_id=model_id,
        version=version,
        manifest_uri=manifest_uri,
        walk_source_files=_walk_source_files,
        source_file_size=_source_file_size,
        csv_time_evidence=_csv_time_evidence,
        forcing_sample_file_limit=FORCING_SAMPLE_FILE_LIMIT,
    )


def _verify_existing_manifest_consistency(
    store: LocalObjectStore,
    manifest: dict[str, Any],
    *,
    checksum_material: dict[str, Any],
    model_id: str,
    version: str,
    manifest_uri: str,
) -> None:
    _leaf_verify_existing_manifest_consistency(
        store,
        manifest,
        checksum_material=checksum_material,
        model_id=model_id,
        version=version,
        manifest_uri=manifest_uri,
        verify_object_bytes=_verify_object_bytes,
    )


def _write_source_file_to_store(
    source_file: SourceFile,
    store: LocalObjectStore,
    *,
    model_id: str,
    version: str,
    manifest_uri: str,
) -> dict[str, Any]:
    return _leaf_write_source_file_to_store(
        source_file,
        store,
        model_id=model_id,
        version=version,
        manifest_uri=manifest_uri,
        write_file_to_store_streaming=_write_file_to_store_streaming,
        verify_object_bytes=_verify_object_bytes,
    )


def _directory_evidence(root: Path) -> tuple[int, int, str]:
    return _leaf_directory_evidence(
        root,
        walk_source_files=_walk_source_files,
        migration_source_file_evidence=_migration_source_file_evidence,
    )
