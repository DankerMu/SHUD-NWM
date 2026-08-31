from __future__ import annotations

import hashlib
import json
import tempfile
from pathlib import Path, PurePosixPath
from typing import Any, Callable

from packages.common.object_store import LocalObjectStore
from packages.common.safe_fs import SafeFilesystemError, atomic_write_bytes_no_follow, unlink_no_follow
from services.production_closure.object_store_validation_contracts import (
    MAX_RAW_INTERMEDIATE_BYTES,
    MAX_STORED_MANIFEST_BYTES,
    PackageChecksumReconstruction,
    ProductionObjectStoreValidationError,
    _deterministic_manifest_bytes,
    _sha256_json,
)
from services.production_closure.object_store_validation_path_safety import _validate_lane_path_contained
from workers.model_registry.basins_package import (
    SUPPORTED_BASINS_PACKAGE_SCHEMA_VERSIONS,
    BasinsPackageError,
    forcing_checksum_material_for_schema_version,
    write_basins_migration_report,
)


def _write_migration_evidence(
    config: Any,
    writer: Any,
    basins_root: Path,
    blockers: list[dict[str, Any]],
) -> dict[str, Any] | None:
    raw_path = config.lane_dir / ".migration_report.raw.json"
    try:
        report, _report_bytes = _write_raw_worker_output(
            config,
            raw_path,
            path_kind="raw migration report file",
            producer=lambda output_path: write_basins_migration_report(
                basins_root=basins_root,
                source_uri=config.source_uri,
                output_path=output_path,
            ),
        )
    except BasinsPackageError as error:
        blocker = error.to_payload()
        blocker["status"] = "blocked"
        blockers.append(blocker)
        writer.write_json(config.lane_dir / "migration_blocker.json", blocker)
        return None
    finally:
        _cleanup_raw_lane_file(config, raw_path, path_kind="raw migration report file")
    writer.write_json(config.lane_dir / "migration_report.json", report)
    return report


def _write_raw_worker_output(
    config: Any,
    raw_path: Path,
    *,
    path_kind: str,
    producer: Callable[[Path], Any],
) -> tuple[Any, bytes]:
    _validate_lane_path_contained(config, raw_path, path_kind=path_kind)
    with tempfile.TemporaryDirectory(prefix="nhms-object-store-validation-", dir=config.lane_dir) as temp_dir_name:
        temp_dir = Path(temp_dir_name)
        temp_path = temp_dir / raw_path.name
        producer_succeeded = False
        try:
            result = producer(temp_path)
            content = _read_raw_worker_output(temp_path, path_kind=path_kind)
            producer_succeeded = True
        finally:
            try:
                unlink_no_follow(temp_path, containment_root=temp_dir, missing_ok=True)
            except (OSError, SafeFilesystemError) as error:
                if producer_succeeded:
                    raise ProductionObjectStoreValidationError(
                        "PRODUCTION_OBJECT_STORE_EVIDENCE_WRITE_FAILED",
                        f"Failed to safely remove temporary {path_kind} {temp_path}: {error}",
                    ) from error
    _write_raw_lane_bytes(config, raw_path, content, path_kind=path_kind)
    return result, content


def _read_raw_worker_output(path: Path, *, path_kind: str) -> bytes:
    try:
        with path.open("rb") as handle:
            content = handle.read(MAX_RAW_INTERMEDIATE_BYTES + 1)
    except OSError as error:
        raise ProductionObjectStoreValidationError(
            "PRODUCTION_OBJECT_STORE_EVIDENCE_WRITE_FAILED",
            f"Failed to read temporary {path_kind} {path}: {error}",
        ) from error
    if len(content) > MAX_RAW_INTERMEDIATE_BYTES:
        raise ProductionObjectStoreValidationError(
            "PRODUCTION_OBJECT_STORE_EVIDENCE_WRITE_FAILED",
            f"Temporary {path_kind} exceeds {MAX_RAW_INTERMEDIATE_BYTES} bytes: {path}",
        )
    return content


def _write_raw_lane_bytes(
    config: Any,
    raw_path: Path,
    content: bytes,
    *,
    path_kind: str,
) -> None:
    _validate_lane_path_contained(config, raw_path, path_kind=path_kind)
    try:
        atomic_write_bytes_no_follow(raw_path, content, containment_root=config.lane_dir)
    except SafeFilesystemError as error:
        error_code = (
            "PRODUCTION_OBJECT_STORE_EVIDENCE_WRITE_FAILED"
            if error.kind == "io"
            else "PRODUCTION_OBJECT_STORE_EVIDENCE_PATH_UNSAFE"
        )
        raise ProductionObjectStoreValidationError(
            error_code,
            f"Failed to safely write {path_kind} {raw_path}: {error}",
        ) from error
    except OSError as error:
        raise ProductionObjectStoreValidationError(
            "PRODUCTION_OBJECT_STORE_EVIDENCE_WRITE_FAILED",
            f"Failed to safely write {path_kind} {raw_path}: {error}",
        ) from error


def _cleanup_raw_lane_file(config: Any, raw_path: Path, *, path_kind: str) -> None:
    try:
        unlink_no_follow(raw_path, containment_root=config.lane_dir, missing_ok=True)
    except SafeFilesystemError as error:
        error_code = (
            "PRODUCTION_OBJECT_STORE_EVIDENCE_WRITE_FAILED"
            if error.kind == "io"
            else "PRODUCTION_OBJECT_STORE_EVIDENCE_PATH_UNSAFE"
        )
        raise ProductionObjectStoreValidationError(
            error_code,
            f"Failed to safely remove {path_kind} {raw_path}: {error}",
        ) from error
    except OSError as error:
        raise ProductionObjectStoreValidationError(
            "PRODUCTION_OBJECT_STORE_EVIDENCE_WRITE_FAILED",
            f"Failed to safely remove {path_kind} {raw_path}: {error}",
        ) from error


def _package_manifest_evidence(publish_result: dict[str, Any], manifest: dict[str, Any]) -> dict[str, Any]:
    return {
        "schema": "nhms.production_closure.object_store.package_manifest.v1",
        "status": publish_result["status"],
        "model_id": manifest["model_id"],
        "version": publish_result["version"],
        "manifest_uri": manifest["manifest_uri"],
        "model_package_uri": manifest["model_package_uri"],
        "package_checksum": manifest["package_checksum"],
        "included_file_count": len(manifest.get("included_files", [])),
        "manifest_included": any(
            isinstance(entry, dict) and entry.get("role") == "manifest" for entry in manifest.get("included_files", [])
        ),
        "source_is_symlink": manifest.get("source_is_symlink"),
    }


def _verify_stored_objects(store: LocalObjectStore, manifest: dict[str, Any]) -> dict[str, Any]:
    stored_manifest_bytes = store.read_bytes_limited(str(manifest["manifest_uri"]), max_bytes=MAX_STORED_MANIFEST_BYTES)
    stored_manifest = json.loads(stored_manifest_bytes.decode("utf-8"))
    stored_manifest_sha256 = hashlib.sha256(stored_manifest_bytes).hexdigest()
    package_checksum_reconstruction = _package_checksum_from_stored_manifest(stored_manifest)
    package_checksum_verified = (
        package_checksum_reconstruction.checksum
        == stored_manifest.get("package_checksum")
        == manifest.get("package_checksum")
    )
    package_checksum_matches_manifest = stored_manifest.get("package_checksum") == manifest.get("package_checksum")
    entries = []
    all_verified = package_checksum_verified
    for entry in stored_manifest.get("included_files", []):
        if not isinstance(entry, dict):
            continue
        object_uri = str(entry["object_uri"])
        actual_size, actual_sha256 = store.size_and_checksum(object_uri)
        expected_sha256 = entry["sha256"]
        manifest_payload_sha256 = None
        final_manifest_sha256 = None
        if entry.get("role") == "manifest":
            manifest_payload = _stored_manifest_payload_without_self_entry(stored_manifest)
            manifest_payload_sha256 = hashlib.sha256(_deterministic_manifest_bytes(manifest_payload)).hexdigest()
            final_manifest_sha256 = actual_sha256
            actual_sha256 = manifest_payload_sha256
        verified = actual_sha256 == expected_sha256 and actual_size == entry["size_bytes"]
        all_verified = all_verified and verified
        entries.append(
            {
                "relative_path": entry["relative_path"],
                "role": entry["role"],
                "object_uri": object_uri,
                "expected_size_bytes": entry["size_bytes"],
                "actual_size_bytes": actual_size,
                "expected_sha256": expected_sha256,
                "manifest_recorded_sha256": entry["sha256"] if entry.get("role") == "manifest" else None,
                "actual_sha256": actual_sha256,
                "manifest_payload_sha256": manifest_payload_sha256,
                "final_manifest_sha256": final_manifest_sha256,
                "verified": verified,
            }
        )
    return {
        "schema": "nhms.production_closure.object_store.stored_object_verification.v1",
        "status": "verified" if all_verified else "blocked",
        "manifest_uri": manifest["manifest_uri"],
        "model_package_uri": manifest["model_package_uri"],
        "package_checksum": manifest["package_checksum"],
        "package_checksum_confirmed_from_stored_manifest": package_checksum_verified,
        "package_checksum_matches_manifest": package_checksum_matches_manifest,
        "package_checksum_reconstruction_status": package_checksum_reconstruction.status,
        "package_checksum_source_model_identity_basis": package_checksum_reconstruction.identity_basis,
        "package_checksum_reconstruction_limitation": package_checksum_reconstruction.limitation,
        "stored_manifest_package_checksum": stored_manifest.get("package_checksum"),
        "recomputed_package_checksum": package_checksum_reconstruction.checksum,
        "stored_manifest_sha256": stored_manifest_sha256,
        "entry_count": len(entries),
        "entries": entries,
    }


def _stored_manifest_payload_without_self_entry(stored_manifest: dict[str, Any]) -> dict[str, Any]:
    payload = dict(stored_manifest)
    payload["included_files"] = [
        entry
        for entry in stored_manifest.get("included_files", [])
        if isinstance(entry, dict) and entry.get("role") != "manifest"
    ]
    return payload


def _package_checksum_from_stored_manifest(stored_manifest: dict[str, Any]) -> PackageChecksumReconstruction:
    source_model_identity = _source_model_identity_for_package_checksum(stored_manifest)
    if source_model_identity["identity"]["root_relative_resolved_path"] is None:
        return PackageChecksumReconstruction(
            checksum=None,
            status="limited",
            identity_basis=str(source_model_identity["basis"]),
            limitation="stored_manifest_does_not_prove_root_relative_resolved_path",
        )
    # #1813: the checksum material shape is a property of the generation the
    # manifest was published under, not of the code reading it.  An unknown
    # generation is a recorded reconstruction limitation, never a verification
    # failure -- this validator must not accuse an immutable package of drift
    # because it predates or postdates the packager it runs beside.
    stored_schema_version = stored_manifest.get("schema_version")
    if stored_schema_version not in SUPPORTED_BASINS_PACKAGE_SCHEMA_VERSIONS:
        return PackageChecksumReconstruction(
            checksum=None,
            status="limited",
            identity_basis=str(source_model_identity["basis"]),
            limitation="stored_manifest_package_schema_version_unsupported",
        )
    included_files = [
        {
            "relative_path": entry["relative_path"],
            "role": entry["role"],
            "size_bytes": entry["size_bytes"],
            "sha256": entry["sha256"],
        }
        for entry in stored_manifest.get("included_files", [])
        if isinstance(entry, dict) and entry.get("role") != "manifest"
    ]
    checksum_material = {
        "schema_version": stored_schema_version,
        "model_id": stored_manifest.get("model_id"),
        "version": stored_manifest.get("version"),
        "included_files": sorted(included_files, key=lambda item: (item["role"], item["relative_path"])),
        "forcing": _forcing_checksum_material(stored_manifest.get("forcing"), str(stored_schema_version)),
        "copy_forcing": bool(stored_manifest.get("forcing", {}).get("payload_copied", False))
        if isinstance(stored_manifest.get("forcing"), dict)
        else False,
        "source_model_identity": source_model_identity["identity"],
    }
    return PackageChecksumReconstruction(
        checksum=_sha256_json(checksum_material),
        status="confirmed",
        identity_basis=str(source_model_identity["basis"]),
    )


def _source_model_identity_for_package_checksum(stored_manifest: dict[str, Any]) -> dict[str, Any]:
    basin_slug = stored_manifest.get("basin_slug")
    shud_input_name = stored_manifest.get("shud_input_name")
    root_relative = stored_manifest.get("root_relative_resolved_path")
    if isinstance(root_relative, str) and root_relative:
        return {
            "basis": "stored_manifest.root_relative_resolved_path",
            "identity": {
                "basin_slug": basin_slug,
                "shud_input_name": shud_input_name,
                "root_relative_resolved_path": root_relative,
            },
        }

    inferred_root_relative = _infer_copied_root_relative_resolved_path(stored_manifest)
    if inferred_root_relative is not None:
        return {
            "basis": "documented_148_copied_root_non_symlink_source_suffix",
            "identity": {
                "basin_slug": basin_slug,
                "shud_input_name": shud_input_name,
                "root_relative_resolved_path": inferred_root_relative,
            },
        }

    return {
        "basis": "unavailable",
        "identity": {
            "basin_slug": basin_slug,
            "shud_input_name": shud_input_name,
            "root_relative_resolved_path": None,
        },
    }


def _infer_copied_root_relative_resolved_path(stored_manifest: dict[str, Any]) -> str | None:
    """Infer only the documented #148 copied-root case.

    Basins discovery sets root_relative_resolved_path equal to the basin slug when
    a non-symlink copied root is scanned and the source/resolved model paths both
    end with the basin slug. Without those manifest facts, the package checksum is
    intentionally left unconfirmed instead of treating basin_slug as that field.
    """
    if stored_manifest.get("source_is_symlink") is not False:
        return None
    basin_slug = stored_manifest.get("basin_slug")
    source_path = stored_manifest.get("source_path")
    resolved_source_path = stored_manifest.get("resolved_source_path")
    if not all(isinstance(value, str) and value for value in (basin_slug, source_path, resolved_source_path)):
        return None
    basin_parts = PurePosixPath(str(basin_slug)).parts
    if not basin_parts or any(part in {"", ".", ".."} for part in basin_parts):
        return None
    if PurePosixPath(str(basin_slug)).is_absolute():
        return None
    source_parts = Path(str(source_path)).parts
    resolved_parts = Path(str(resolved_source_path)).parts
    if tuple(source_parts[-len(basin_parts) :]) != basin_parts:
        return None
    if tuple(resolved_parts[-len(basin_parts) :]) != basin_parts:
        return None
    return str(basin_slug)


def _forcing_checksum_material(forcing: Any, schema_version: str) -> Any:
    """Mirror of the packager material, keyed on the stored manifest's own generation.

    Delegates to the packager so the two implementations cannot drift; the
    non-dict passthrough is kept because a malformed stored manifest must
    reconstruct to a mismatching checksum rather than raise here.
    """

    if not isinstance(forcing, dict):
        return forcing
    return forcing_checksum_material_for_schema_version(forcing, schema_version)
