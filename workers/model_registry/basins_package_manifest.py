from __future__ import annotations

import hashlib
from collections.abc import Callable, Iterator, Mapping, Sequence
from pathlib import Path
from typing import Any

from packages.common.object_store import LocalObjectStore, ObjectStoreError

from .basins_package_contracts import (
    FORCING_SAMPLE_BYTE_LIMIT,
    FORCING_SAMPLE_LINE_LIMIT,
    SUPPORTED_BASINS_PACKAGE_SCHEMA_VERSIONS,
    BasinsPackageError,
    SourceFile,
    _json_bytes,
    _sha256_bytes,
    _sha256_json,
    forcing_checksum_material_for_schema_version,
)
from .basins_package_inventory import _ensure_inventory_path_matches_expected, _expected_forcing_dir
from .basins_package_object_store import _directory_uri
from .basins_package_source_io import _normalize_relative_path, _safe_source_dir, _source_file_evidence


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

def _calibration_metadata(
    model: dict[str, Any],
    included_files: list[dict[str, Any]],
    calibration_overrides: Sequence[Mapping[str, Any]] | None = None,
) -> dict[str, Any]:
    calibration_files = [item for item in included_files if item["role"] == "calibration"]
    metadata: dict[str, Any] = {
        "source_count": int(model.get("calibration_count") or 0),
        "included_count": len(calibration_files),
        "included_files": [item["relative_path"] for item in calibration_files],
    }
    # #1832: absence is meaningful.  A package with no declared override carries
    # NO `overrides` key at all -- an empty list would be indistinguishable from
    # "this publisher does not record overrides", which is the ambiguity #1816
    # was written against.
    if calibration_overrides:
        metadata["overrides"] = [dict(item) for item in calibration_overrides]
    return metadata

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

def _manifest_payload_without_self_entry(manifest: dict[str, Any]) -> dict[str, Any]:
    payload = dict(manifest)
    payload["included_files"] = [
        entry
        for entry in manifest.get("included_files", [])
        if isinstance(entry, dict) and entry.get("role") != "manifest"
    ]
    return payload

def _success_payload(status: str, manifest: dict[str, Any]) -> dict[str, Any]:
    return {
        "status": status,
        "model_id": manifest["model_id"],
        "version": manifest["version"],
        "model_package_uri": manifest["model_package_uri"],
        "manifest_uri": manifest["manifest_uri"],
        "package_checksum": manifest["package_checksum"],
    }

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
    walk_source_files: Callable[[Path, Path], Iterator[Path]],
    source_file_size: Callable[..., int],
    csv_time_evidence: Callable[..., tuple[str | None, str | None, str | None, int]],
    forcing_sample_file_limit: int
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

    for path in walk_source_files(forcing_dir, source_root):
        if path.suffix.lower() != ".csv":
            continue
        csv_count += 1
        relative_path = _normalize_relative_path(path.relative_to(forcing_dir).as_posix())
        if copy_forcing:
            # Only the copied case still needs per-file payload digests: those
            # payloads become `included_files` entries and are covered there too.
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
        else:
            # #1813: excluded forcing is not identity material, so the aggregate
            # payload digest has no consumer.  Count and bytes survive on stat
            # alone -- publication cost stops scaling with historical volume.
            size_bytes = source_file_size(
                path,
                source_root,
                model_id=model_id,
                version=version,
                manifest_uri=manifest_uri,
            )
        total_bytes += size_bytes
        if sampled_file_count < forcing_sample_file_limit:
            header, first_time, last_time, row_count = csv_time_evidence(
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
            if object_store is None:
                raise BasinsPackageError(
                    "BASINS_INVENTORY_INVALID",
                    "Forcing payload planning requires an object store.",
                    model_id=model_id,
                    version=version,
                    manifest_uri=manifest_uri,
                )
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

    forcing_payload_uri = _directory_uri(object_store, forcing_key) if copy_forcing and object_store else None
    metadata = {
        "policy": "copied_explicitly" if copy_forcing else "excluded_by_default",
        "forcing_dir": str(forcing_dir),
        "forcing_dir_original_name": model.get("forcing_dir_original_name"),
        "csv_count": csv_count,
        "byte_count": total_bytes,
        "aggregate_checksum": digest.hexdigest() if (copy_forcing and csv_count) else None,
        "sample_headers": sample_headers,
        "sampled_file_count": sampled_file_count,
        "time_coverage": (
            {"start": time_start, "end": time_end} if time_start is not None or time_end is not None else None
        ),
        "parsed_time_rows": parsed_time_rows,
        "sample_file_limit": forcing_sample_file_limit,
        "sample_byte_limit": FORCING_SAMPLE_BYTE_LIMIT,
        "sample_line_limit": FORCING_SAMPLE_LINE_LIMIT,
        "payload_copied": copy_forcing,
        "forcing_payload_uri": forcing_payload_uri,
        "copied_file_count": csv_count if copy_forcing else 0,
        "copied_byte_count": total_bytes if copy_forcing else 0,
    }
    return metadata, source_files

def _verify_existing_manifest_consistency(
    store: LocalObjectStore,
    manifest: dict[str, Any],
    *,
    checksum_material: dict[str, Any],
    model_id: str,
    version: str,
    manifest_uri: str,
    verify_object_bytes: Callable[..., None]
) -> None:
    manifest_entries = [
        entry
        for entry in manifest.get("included_files", [])
        if isinstance(entry, dict) and entry.get("role") == "manifest"
    ]
    if len(manifest_entries) != 1:
        raise BasinsPackageError(
            "BASINS_PACKAGE_MANIFEST_INVALID",
            "Existing Basins package manifest must include exactly one manifest self-entry.",
            model_id=model_id,
            version=version,
            manifest_uri=manifest_uri,
        )

    for entry in manifest.get("included_files", []):
        if not isinstance(entry, dict):
            raise BasinsPackageError(
                "BASINS_PACKAGE_MANIFEST_INVALID",
                "Existing Basins package manifest included_files entries must be objects.",
                model_id=model_id,
                version=version,
                manifest_uri=manifest_uri,
            )
        try:
            object_uri = str(entry["object_uri"])
            expected_size = int(entry["size_bytes"])
            expected_sha256 = str(entry["sha256"])
        except (KeyError, TypeError, ValueError) as error:
            raise BasinsPackageError(
                "BASINS_PACKAGE_MANIFEST_INVALID",
                "Existing Basins package manifest has an invalid included_files entry.",
                model_id=model_id,
                version=version,
                manifest_uri=manifest_uri,
            ) from error

        if entry.get("role") == "manifest":
            if object_uri != manifest_uri:
                raise BasinsPackageError(
                    "BASINS_PACKAGE_MANIFEST_INVALID",
                    "Existing Basins package manifest self-entry URI must match the manifest URI.",
                    model_id=model_id,
                    version=version,
                    manifest_uri=manifest_uri,
                )
            payload_sha256 = _sha256_bytes(_json_bytes(_manifest_payload_without_self_entry(manifest)))
            if expected_sha256 != payload_sha256:
                raise BasinsPackageError(
                    "BASINS_PACKAGE_MANIFEST_INVALID",
                    "Existing Basins package manifest self-entry checksum does not match stored manifest bytes.",
                    model_id=model_id,
                    version=version,
                    manifest_uri=manifest_uri,
                )
        object_expected_sha256 = expected_sha256
        if entry.get("role") == "manifest":
            object_expected_sha256 = _sha256_bytes(_json_bytes(manifest))
        try:
            verify_object_bytes(
                store,
                object_uri,
                expected_size=expected_size,
                expected_sha256=object_expected_sha256,
                model_id=model_id,
                version=version,
                manifest_uri=manifest_uri,
            )
        except (ObjectStoreError, ValueError) as error:
            raise BasinsPackageError(
                "BASINS_PACKAGE_MANIFEST_INVALID",
                f"Existing Basins package object does not match manifest entry: {object_uri}",
                model_id=model_id,
                version=version,
                manifest_uri=manifest_uri,
            ) from error

    non_manifest_entries = [
        {
            "relative_path": entry["relative_path"],
            "role": entry["role"],
            "size_bytes": entry["size_bytes"],
            "sha256": entry["sha256"],
        }
        for entry in manifest.get("included_files", [])
        if isinstance(entry, dict) and entry.get("role") != "manifest"
    ]
    stored_schema_version = manifest.get("schema_version")
    if stored_schema_version not in SUPPORTED_BASINS_PACKAGE_SCHEMA_VERSIONS:
        raise BasinsPackageError(
            "BASINS_PACKAGE_MANIFEST_INVALID",
            "Existing Basins package manifest declares an unsupported package schema version.",
            model_id=model_id,
            version=version,
            manifest_uri=manifest_uri,
        )
    stored_forcing = manifest.get("forcing")
    reconstructed_checksum_material = {
        "schema_version": stored_schema_version,
        "model_id": manifest.get("model_id"),
        "version": manifest.get("version"),
        "included_files": sorted(non_manifest_entries, key=lambda item: (item["role"], item["relative_path"])),
        # The stored manifest's own generation decides the material shape;
        # pre-migration manifests keep reconstructing with the old seven fields.
        "forcing": forcing_checksum_material_for_schema_version(
            stored_forcing if isinstance(stored_forcing, Mapping) else {},
            str(stored_schema_version),
        ),
        "copy_forcing": bool(manifest.get("forcing", {}).get("payload_copied", False))
        if isinstance(manifest.get("forcing"), dict)
        else False,
        "source_model_identity": checksum_material["source_model_identity"],
    }
    if _sha256_json(reconstructed_checksum_material) != manifest.get("package_checksum"):
        raise BasinsPackageError(
            "BASINS_PACKAGE_MANIFEST_INVALID",
            "Existing Basins package manifest package checksum does not match recorded entries.",
            model_id=model_id,
            version=version,
            manifest_uri=manifest_uri,
        )
