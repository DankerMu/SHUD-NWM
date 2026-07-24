from __future__ import annotations

import json
import os
import shutil
import stat
from collections.abc import Mapping, Sequence
from datetime import datetime
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlparse
from uuid import uuid4

from packages.common.provider_atomic import ProviderAtomicError, provider_destination_lock
from packages.common.safe_fs import (
    SafeFilesystemError,
    ensure_directory_no_follow,
    read_bytes_limited_no_follow,
    stat_no_follow,
    unlink_no_follow,
    verify_directory_no_follow,
)
from packages.common.source_identity import normalize_source_id
from packages.common.storage import validate_object_path
from workers.data_adapters.base import cycle_id_for, format_cycle_time, parse_cycle_time

NFS_RAW_MANIFEST_ENABLED_ENV = "NHMS_SCHEDULER_NFS_RAW_MANIFEST_ENABLED"
NFS_RAW_MANIFEST_REQUIRED_ENV = "NHMS_SCHEDULER_REQUIRE_NFS_RAW_MANIFEST"
NFS_RAW_MANIFEST_ROOT_ENV = "NHMS_SCHEDULER_NFS_RAW_MANIFEST_ROOT"
NFS_RAW_MANIFEST_PREFIX_ENV = "NHMS_SCHEDULER_NFS_RAW_MANIFEST_PREFIX"
NFS_RAW_STAGE_ENABLED_ENV = "NHMS_SCHEDULER_STAGE_NFS_RAW_TO_OBJECT_STORE"
NFS_RAW_STAGE_ROOT_ENV = "NHMS_SCHEDULER_NFS_RAW_STAGE_ROOT"
NFS_RAW_STAGE_PREFIX_ENV = "NHMS_SCHEDULER_NFS_RAW_STAGE_PREFIX"
NFS_RAW_MANIFEST_MAX_BYTES = 16 * 1024 * 1024
NFS_RAW_MANIFEST_READY_SOURCE = "node27_nfs_raw_manifest"
# Fixed deployment topology identity, not an environment-configurable policy.
# Node-22 production sees node-27's shared object-store through this NFS path.
NODE22_CANONICAL_NFS_RAW_AUTHORITY_ROOT = Path("/ghdc/data/nwm/object-store")

__all__ = (
    "NFS_RAW_MANIFEST_ENABLED_ENV",
    "NFS_RAW_MANIFEST_PREFIX_ENV",
    "NFS_RAW_MANIFEST_READY_SOURCE",
    "NFS_RAW_MANIFEST_REQUIRED_ENV",
    "NFS_RAW_MANIFEST_ROOT_ENV",
    "NFS_RAW_STAGE_ENABLED_ENV",
    "NFS_RAW_STAGE_PREFIX_ENV",
    "NFS_RAW_STAGE_ROOT_ENV",
    "NODE22_CANONICAL_NFS_RAW_AUTHORITY_ROOT",
    "NfsRawManifestStagingError",
    "forecast_cycle_from_raw_manifest_readiness",
    "nfs_raw_manifest_readiness",
    "nfs_raw_manifest_readiness_from_env",
    "nfs_raw_manifest_source_object_identity_from_env",
    "nfs_raw_manifest_source_policy_from_env",
    "source_object_identity_from_raw_manifest_readiness",
    "source_policy_from_raw_manifest_readiness",
    "stage_nfs_raw_manifest_from_env",
    "stage_nfs_raw_manifest_to_object_store",
)


class NfsRawManifestStagingError(RuntimeError):
    """Raised when node-22 cannot materialize node-27 NFS raw for compute."""


def nfs_raw_manifest_readiness_from_env(source_id: str, cycle_time: datetime) -> dict[str, Any] | None:
    enabled = _env_flag(NFS_RAW_MANIFEST_ENABLED_ENV)
    required = _env_flag(NFS_RAW_MANIFEST_REQUIRED_ENV)
    if not enabled and not required:
        return None
    root = os.getenv(NFS_RAW_MANIFEST_ROOT_ENV) or os.getenv("OBJECT_STORE_ROOT")
    prefix = os.getenv(NFS_RAW_MANIFEST_PREFIX_ENV) or os.getenv("OBJECT_STORE_PREFIX") or "s3://nhms"
    if root in (None, ""):
        return {
            "status": "missing",
            "required": required,
            "reason": "object_store_root_missing",
            "source": NFS_RAW_MANIFEST_READY_SOURCE,
            "source_id": normalize_source_id(source_id),
            "cycle_id": cycle_id_for(source_id, cycle_time),
            "cycle_time": _format_time(cycle_time),
            "env": NFS_RAW_MANIFEST_ROOT_ENV,
        }
    return nfs_raw_manifest_readiness(
        source_id=source_id,
        cycle_time=cycle_time,
        object_store_root=root,
        object_store_prefix=prefix,
        required=required,
    )


def nfs_raw_manifest_readiness(
    *,
    source_id: str,
    cycle_time: datetime,
    object_store_root: str | Path,
    object_store_prefix: str = "s3://nhms",
    required: bool = False,
    max_manifest_bytes: int = NFS_RAW_MANIFEST_MAX_BYTES,
) -> dict[str, Any]:
    source_id = normalize_source_id(source_id)
    cycle_time = parse_cycle_time(cycle_time)
    compact_cycle = format_cycle_time(cycle_time)
    root = _absolute_path(object_store_root)
    root_evidence = {
        "required": required,
        "source": NFS_RAW_MANIFEST_READY_SOURCE,
        "source_id": source_id,
        "cycle_id": cycle_id_for(source_id, cycle_time),
        "cycle_time": _format_time(cycle_time),
        "object_store_root": str(root),
    }
    try:
        verified_root = verify_directory_no_follow(root)
    except FileNotFoundError:
        return {**root_evidence, "status": "missing", "reason": "object_store_root_missing"}
    except (OSError, SafeFilesystemError) as error:
        return {
            **root_evidence,
            "status": "invalid",
            "reason": "object_store_root_unreadable",
            "error": str(error),
        }

    manifest_keys = _candidate_manifest_keys(source_id, compact_cycle)
    manifest_path: Path | None = None
    manifest_key: str | None = None
    for key in manifest_keys:
        path = verified_root / key
        try:
            stat_no_follow(path, containment_root=verified_root)
        except FileNotFoundError:
            continue
        except (OSError, SafeFilesystemError) as error:
            return {
                **root_evidence,
                "status": "invalid",
                "reason": "manifest_unreadable",
                "manifest_key": key,
                "manifest_path": str(path),
                "error": str(error),
            }
        manifest_path = path
        manifest_key = key
        break
    if manifest_path is None or manifest_key is None:
        return {
            **root_evidence,
            "status": "missing",
            "reason": "manifest_not_found",
            "manifest_keys": manifest_keys,
        }

    payload, payload_error = _read_manifest_payload(
        manifest_path,
        root=verified_root,
        max_manifest_bytes=max_manifest_bytes,
    )
    if payload_error is not None:
        return {
            **root_evidence,
            **payload_error,
            "status": "invalid",
            "manifest_key": manifest_key,
            "manifest_path": str(manifest_path),
        }

    validation_error = _validate_manifest_identity(payload, source_id=source_id, cycle_time=cycle_time)
    if validation_error is not None:
        return {
            **root_evidence,
            **validation_error,
            "status": "invalid",
            "manifest_key": manifest_key,
            "manifest_path": str(manifest_path),
        }

    entries = payload.get("entries")
    if not isinstance(entries, Sequence) or isinstance(entries, str | bytes | bytearray) or not entries:
        return {
            **root_evidence,
            "status": "invalid",
            "reason": "manifest_entries_missing",
            "manifest_key": manifest_key,
            "manifest_path": str(manifest_path),
        }
    local_keys, entry_error = _entry_local_keys(entries)
    if entry_error is not None:
        return {
            **root_evidence,
            **entry_error,
            "status": "invalid",
            "manifest_key": manifest_key,
            "manifest_path": str(manifest_path),
            "entry_count": len(entries),
        }
    file_evidence, file_error = _verify_entry_files(verified_root, local_keys)
    if file_error is not None:
        return {
            **root_evidence,
            **file_evidence,
            **file_error,
            "status": "invalid",
            "manifest_key": manifest_key,
            "manifest_path": str(manifest_path),
            "entry_count": len(entries),
        }

    manifest_uri = str(payload.get("manifest_uri") or "").strip()
    if not manifest_uri:
        manifest_uri = _manifest_uri_for_key(manifest_key, object_store_prefix)
    return {
        **root_evidence,
        **file_evidence,
        "status": "ready",
        "reason": None,
        "manifest_uri": manifest_uri,
        "manifest_key": manifest_key,
        "manifest_path": str(manifest_path),
        "entry_count": len(entries),
        "metadata": _bounded_manifest_metadata(payload.get("metadata")),
    }


def forecast_cycle_from_raw_manifest_readiness(
    readiness: Mapping[str, Any],
    *,
    source_id: str,
    cycle_time: datetime,
) -> dict[str, Any]:
    source_id = normalize_source_id(source_id)
    parsed_cycle_time = parse_cycle_time(cycle_time)
    return {
        "cycle_id": cycle_id_for(source_id, parsed_cycle_time),
        "source_id": source_id,
        "cycle_time": parsed_cycle_time,
        "issue_time": parsed_cycle_time,
        "status": "raw_complete",
        "manifest_uri": str(readiness["manifest_uri"]),
        "retry_count": 0,
        "error_code": None,
        "error_message": None,
        "created_at": None,
        "source_cycle_truth": NFS_RAW_MANIFEST_READY_SOURCE,
    }


def nfs_raw_manifest_source_object_identity_from_env(source_id: str, cycle_time: datetime) -> dict[str, Any] | None:
    readiness = nfs_raw_manifest_readiness_from_env(source_id, cycle_time)
    if not isinstance(readiness, Mapping):
        return None
    return source_object_identity_from_raw_manifest_readiness(readiness)


def nfs_raw_manifest_source_policy_from_env(source_id: str, cycle_time: datetime) -> dict[str, Any] | None:
    readiness = nfs_raw_manifest_readiness_from_env(source_id, cycle_time)
    if not isinstance(readiness, Mapping):
        return None
    return source_policy_from_raw_manifest_readiness(readiness)


def source_object_identity_from_raw_manifest_readiness(readiness: Mapping[str, Any]) -> dict[str, Any] | None:
    metadata = _metadata_from_raw_manifest_readiness(readiness)
    if metadata is None:
        return None
    source_object_identity = metadata.get("source_object_identity")
    if not isinstance(source_object_identity, Mapping) or not source_object_identity:
        return None
    return dict(source_object_identity)


def source_policy_from_raw_manifest_readiness(readiness: Mapping[str, Any]) -> dict[str, Any] | None:
    metadata = _metadata_from_raw_manifest_readiness(readiness)
    if metadata is None:
        return None
    source_policy = metadata.get("source_policy")
    if not isinstance(source_policy, Mapping) or not source_policy:
        return None
    return dict(source_policy)


def _metadata_from_raw_manifest_readiness(readiness: Mapping[str, Any]) -> dict[str, Any] | None:
    if readiness.get("status") != "ready":
        return None
    manifest_path = readiness.get("manifest_path")
    object_store_root = readiness.get("object_store_root")
    if manifest_path in (None, "") or object_store_root in (None, ""):
        return None
    try:
        root = verify_directory_no_follow(_absolute_path(object_store_root))
        payload, payload_error = _read_manifest_payload(
            _absolute_path(str(manifest_path)),
            root=root,
            max_manifest_bytes=NFS_RAW_MANIFEST_MAX_BYTES,
        )
    except (OSError, SafeFilesystemError):
        return None
    if payload_error is not None:
        return None
    metadata = payload.get("metadata")
    if not isinstance(metadata, Mapping):
        return None
    return dict(metadata)


def stage_nfs_raw_manifest_from_env(state_evidence: Mapping[str, Any]) -> dict[str, Any] | None:
    if not _env_flag(NFS_RAW_STAGE_ENABLED_ENV):
        return None
    nfs_raw_manifest = state_evidence.get("nfs_raw_manifest")
    if not isinstance(nfs_raw_manifest, Mapping) or nfs_raw_manifest.get("status") != "ready":
        return None
    target_root = os.getenv(NFS_RAW_STAGE_ROOT_ENV) or os.getenv("OBJECT_STORE_ROOT")
    if target_root in (None, ""):
        raise NfsRawManifestStagingError(
            f"{NFS_RAW_STAGE_ROOT_ENV} or OBJECT_STORE_ROOT is required to stage NFS raw inputs."
        )
    source_root = os.getenv(NFS_RAW_MANIFEST_ROOT_ENV) or os.getenv("OBJECT_STORE_ROOT")
    if source_root not in (None, "") and nfs_raw_manifest.get("status") == "ready":
        manifest_key = str(nfs_raw_manifest.get("manifest_key") or "").strip()
        if manifest_key:
            nfs_raw_manifest = {
                **dict(nfs_raw_manifest),
                "object_store_root": source_root,
                "manifest_path": str(_absolute_path(str(source_root)) / manifest_key),
            }
    return stage_nfs_raw_manifest_to_object_store(
        nfs_raw_manifest,
        target_object_store_root=target_root,
        target_object_store_prefix=os.getenv(NFS_RAW_STAGE_PREFIX_ENV)
        or os.getenv("OBJECT_STORE_PREFIX")
        or os.getenv(NFS_RAW_MANIFEST_PREFIX_ENV)
        or "s3://nhms",
    )


def stage_nfs_raw_manifest_to_object_store(
    readiness: Mapping[str, Any],
    *,
    target_object_store_root: str | Path,
    target_object_store_prefix: str = "s3://nhms",
) -> dict[str, Any]:
    if readiness.get("status") != "ready":
        raise NfsRawManifestStagingError("NFS raw manifest must be ready before staging.")
    source_root_value = readiness.get("object_store_root")
    manifest_key = str(readiness.get("manifest_key") or "").strip()
    manifest_path_value = readiness.get("manifest_path")
    if source_root_value in (None, "") or manifest_path_value in (None, "") or not manifest_key:
        raise NfsRawManifestStagingError("NFS raw manifest evidence is missing source root or manifest path.")

    source_root = verify_directory_no_follow(_absolute_path(str(source_root_value)))
    target_root = ensure_directory_no_follow(_absolute_path(target_object_store_root))
    try:
        if source_root.samefile(target_root):
            return {
                "status": "skipped",
                "reason": "source_target_same",
                "source": NFS_RAW_MANIFEST_READY_SOURCE,
                "source_object_store_root": "[local-path]",
                "target_object_store_root": "[local-path]",
                "manifest_uri": _object_uri_evidence(str(readiness.get("manifest_uri") or "")),
                "manifest_key": manifest_key,
            }
    except OSError:
        pass

    manifest_path = _absolute_path(str(manifest_path_value))
    target_manifest_path = target_root / manifest_key
    ensure_directory_no_follow(target_manifest_path.parent, containment_root=target_root)
    try:
        with provider_destination_lock(target_manifest_path, containment_root=target_root):
            payload, payload_error = _read_manifest_payload(
                manifest_path,
                root=source_root,
                max_manifest_bytes=NFS_RAW_MANIFEST_MAX_BYTES,
            )
            if payload_error is not None:
                raise NfsRawManifestStagingError(str(payload_error.get("reason") or "manifest_unreadable"))
            source_id = str(readiness.get("source_id") or "")
            cycle_time = parse_cycle_time(str(readiness.get("cycle_time") or ""))
            validation_error = _validate_manifest_identity(payload, source_id=source_id, cycle_time=cycle_time)
            if validation_error is not None:
                raise NfsRawManifestStagingError(
                    str(validation_error.get("reason") or "manifest_identity_invalid")
                )
            entries = payload.get("entries")
            if not isinstance(entries, Sequence) or isinstance(entries, str | bytes | bytearray) or not entries:
                raise NfsRawManifestStagingError("manifest_entries_missing")
            local_keys, entry_error = _entry_local_keys(entries)
            if entry_error is not None:
                raise NfsRawManifestStagingError(str(entry_error.get("reason") or "manifest_entry_invalid"))
            _file_evidence, file_error = _verify_entry_files(source_root, local_keys)
            if file_error is not None:
                raise NfsRawManifestStagingError(str(file_error.get("reason") or "raw_files_invalid"))

            source_manifest_bytes = read_bytes_limited_no_follow(
                manifest_path,
                max_bytes=NFS_RAW_MANIFEST_MAX_BYTES,
                containment_root=source_root,
            )
            if _staged_target_matches_source(
                target_root=target_root,
                target_manifest_path=target_manifest_path,
                source_manifest_bytes=source_manifest_bytes,
                local_keys=local_keys,
            ):
                return _nfs_stage_result(
                    readiness=readiness,
                    source_id=source_id,
                    cycle_time=cycle_time,
                    manifest_key=manifest_key,
                    target_object_store_prefix=target_object_store_prefix,
                    status="skipped",
                    reason="already_staged",
                )

            # The manifest is the completion marker. Remove a stale generation
            # before replacing raw files so readers cannot observe it as ready
            # while this generation is only partially staged.
            unlink_no_follow(target_manifest_path, containment_root=target_root, missing_ok=True)
            copied_files = 0
            copied_bytes = 0
            for local_key in sorted(set(local_keys)):
                copied_bytes += _copy_object_file(source_root, target_root, local_key)
                copied_files += 1
            current_source_manifest = read_bytes_limited_no_follow(
                manifest_path,
                max_bytes=NFS_RAW_MANIFEST_MAX_BYTES,
                containment_root=source_root,
            )
            if current_source_manifest != source_manifest_bytes:
                raise NfsRawManifestStagingError("source_manifest_changed_during_staging")
            manifest_bytes = _copy_object_file(source_root, target_root, manifest_key)
            return _nfs_stage_result(
                readiness=readiness,
                source_id=source_id,
                cycle_time=cycle_time,
                manifest_key=manifest_key,
                target_object_store_prefix=target_object_store_prefix,
                status="staged",
                staged_file_count=copied_files,
                staged_manifest_bytes=manifest_bytes,
                staged_raw_bytes=copied_bytes,
            )
    except ProviderAtomicError as error:
        raise NfsRawManifestStagingError(f"raw_stage_lock_failed:{error.reason}") from error


def _staged_target_matches_source(
    *,
    target_root: Path,
    target_manifest_path: Path,
    source_manifest_bytes: bytes,
    local_keys: Sequence[str],
) -> bool:
    try:
        target_manifest_bytes = read_bytes_limited_no_follow(
            target_manifest_path,
            max_bytes=NFS_RAW_MANIFEST_MAX_BYTES,
            containment_root=target_root,
        )
    except FileNotFoundError:
        return False
    except (OSError, SafeFilesystemError):
        return False
    if target_manifest_bytes != source_manifest_bytes:
        return False
    _evidence, error = _verify_entry_files(target_root, local_keys)
    return error is None


def _nfs_stage_result(
    *,
    readiness: Mapping[str, Any],
    source_id: str,
    cycle_time: datetime,
    manifest_key: str,
    target_object_store_prefix: str,
    status: str,
    reason: str | None = None,
    staged_file_count: int = 0,
    staged_manifest_bytes: int = 0,
    staged_raw_bytes: int = 0,
) -> dict[str, Any]:
    manifest_uri = str(readiness.get("manifest_uri") or "").strip() or _manifest_uri_for_key(
        manifest_key,
        target_object_store_prefix,
    )
    result = {
        "status": status,
        "source": NFS_RAW_MANIFEST_READY_SOURCE,
        "source_id": normalize_source_id(source_id),
        "cycle_id": str(readiness.get("cycle_id") or cycle_id_for(source_id, cycle_time)),
        "cycle_time": _format_time(cycle_time),
        "manifest_uri": _object_uri_evidence(manifest_uri),
        "manifest_key": manifest_key,
        "source_object_store_root": "[local-path]",
        "target_object_store_root": "[local-path]",
        "staged_file_count": staged_file_count,
        "staged_manifest_bytes": staged_manifest_bytes,
        "staged_raw_bytes": staged_raw_bytes,
    }
    if reason is not None:
        result["reason"] = reason
    return result


def _read_manifest_payload(
    path: Path,
    *,
    root: Path,
    max_manifest_bytes: int,
) -> tuple[dict[str, Any], dict[str, Any] | None]:
    try:
        content = read_bytes_limited_no_follow(path, max_bytes=max_manifest_bytes, containment_root=root)
    except (OSError, SafeFilesystemError) as error:
        return {}, {"reason": "manifest_unreadable", "error": str(error)}
    if len(content) > max_manifest_bytes:
        return {}, {"reason": "manifest_too_large", "max_bytes": max_manifest_bytes}
    try:
        payload = json.loads(content.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        return {}, {"reason": "manifest_invalid_json", "error": str(error)}
    if not isinstance(payload, dict):
        return {}, {"reason": "manifest_not_object"}
    return payload, None


def _copy_object_file(source_root: Path, target_root: Path, key: str) -> int:
    validation = validate_object_path(key)
    if not validation.valid:
        raise NfsRawManifestStagingError(f"Invalid staged object key {key!r}: {validation.error}")
    source_path = source_root / key
    target_path = target_root / key
    try:
        source_stat = stat_no_follow(source_path, containment_root=source_root)
    except (OSError, SafeFilesystemError) as error:
        raise NfsRawManifestStagingError(f"Source object {key} is not readable: {error}") from error
    if not stat.S_ISREG(source_stat.st_mode):
        raise NfsRawManifestStagingError(f"Source object {key} is not a regular file.")
    ensure_directory_no_follow(target_path.parent, containment_root=target_root)
    temp_path = target_path.with_name(f".{target_path.name}.{uuid4().hex}.stage")
    try:
        shutil.copyfile(source_path, temp_path, follow_symlinks=False)
        os.replace(temp_path, target_path)
    except OSError as error:
        raise NfsRawManifestStagingError(f"Failed to stage object {key}: {error}") from error
    finally:
        try:
            unlink_no_follow(temp_path, containment_root=target_root, missing_ok=True)
        except (OSError, SafeFilesystemError):
            pass
    return int(source_stat.st_size)


def _validate_manifest_identity(
    payload: Mapping[str, Any],
    *,
    source_id: str,
    cycle_time: datetime,
) -> dict[str, Any] | None:
    try:
        manifest_source = normalize_source_id(str(payload.get("source_id") or ""))
    except ValueError as error:
        return {"reason": "manifest_source_invalid", "error": str(error)}
    if manifest_source.lower() != source_id.lower():
        return {
            "reason": "manifest_source_mismatch",
            "manifest_source_id": manifest_source,
            "expected_source_id": source_id,
        }
    try:
        manifest_cycle_time = parse_cycle_time(str(payload.get("cycle_time") or ""))
    except (TypeError, ValueError) as error:
        return {"reason": "manifest_cycle_time_invalid", "error": str(error)}
    if format_cycle_time(manifest_cycle_time) != format_cycle_time(cycle_time):
        return {
            "reason": "manifest_cycle_time_mismatch",
            "manifest_cycle_time": _format_time(manifest_cycle_time),
            "expected_cycle_time": _format_time(cycle_time),
        }
    manifest_uri = str(payload.get("manifest_uri") or "").strip()
    if manifest_uri and not _manifest_uri_matches_source_cycle(
        manifest_uri,
        source_id=source_id,
        cycle_time=cycle_time,
    ):
        return {"reason": "manifest_uri_mismatch", "manifest_uri": manifest_uri}
    return None


def _entry_local_keys(entries: Sequence[Any]) -> tuple[list[str], dict[str, Any] | None]:
    local_keys: list[str] = []
    for index, entry in enumerate(entries):
        if not isinstance(entry, Mapping):
            return [], {"reason": "manifest_entry_not_object", "entry_index": index}
        local_key = str(entry.get("local_key") or "").strip()
        if not local_key:
            return [], {"reason": "manifest_entry_local_key_missing", "entry_index": index}
        validation = validate_object_path(local_key)
        if not validation.valid:
            return [], {
                "reason": "manifest_entry_local_key_invalid",
                "entry_index": index,
                "local_key": local_key,
                "error": validation.error,
            }
        local_keys.append(local_key)
    return local_keys, None


def _verify_entry_files(root: Path, local_keys: Sequence[str]) -> tuple[dict[str, Any], dict[str, Any] | None]:
    unique_keys = sorted(set(local_keys))
    missing: list[str] = []
    empty: list[str] = []
    total_bytes = 0
    for local_key in unique_keys:
        path = root / local_key
        try:
            stat_result = stat_no_follow(path, containment_root=root)
        except FileNotFoundError:
            missing.append(local_key)
            continue
        except (OSError, SafeFilesystemError) as error:
            return (
                {"physical_file_count": len(unique_keys), "checked_file_count": len(unique_keys)},
                {"reason": "raw_file_unreadable", "local_key": local_key, "error": str(error)},
            )
        if not stat.S_ISREG(stat_result.st_mode):
            return (
                {"physical_file_count": len(unique_keys), "checked_file_count": len(unique_keys)},
                {"reason": "raw_file_not_regular", "local_key": local_key},
            )
        if stat_result.st_size <= 0:
            empty.append(local_key)
        total_bytes += int(stat_result.st_size)
    evidence = {
        "physical_file_count": len(unique_keys),
        "checked_file_count": len(unique_keys),
        "total_bytes": total_bytes,
    }
    if missing:
        return (
            {**evidence, "missing_file_count": len(missing), "missing_file_samples": missing[:5]},
            {"reason": "raw_files_missing"},
        )
    if empty:
        return (
            {**evidence, "empty_file_count": len(empty), "empty_file_samples": empty[:5]},
            {"reason": "raw_files_empty"},
        )
    return evidence, None


def _candidate_manifest_keys(source_id: str, compact_cycle: str) -> list[str]:
    source_names: list[str] = []
    for value in (source_id, source_id.lower(), source_id.upper()):
        if value not in source_names:
            source_names.append(value)
    return [f"raw/{source}/{compact_cycle}/manifest.json" for source in source_names]


def _manifest_uri_for_key(key: str, object_store_prefix: str) -> str:
    prefix = object_store_prefix.strip().rstrip("/")
    if not prefix:
        return key
    return f"{prefix}/{key}"


def _manifest_uri_matches_source_cycle(manifest_uri: str, *, source_id: str, cycle_time: datetime) -> bool:
    value = manifest_uri.strip()
    if not value:
        return False
    parsed = urlparse(value)
    if parsed.scheme:
        if parsed.scheme != "s3" or not parsed.netloc or parsed.params or parsed.query or parsed.fragment:
            return False
        key = unquote(parsed.path).strip("/")
        return _manifest_key_suffix_matches_source_cycle(key, source_id=source_id, cycle_time=cycle_time)
    if parsed.netloc:
        return False
    key = unquote(value).strip("/")
    parts = key.split("/")
    return len(parts) == 4 and _manifest_key_suffix_matches_source_cycle(
        key,
        source_id=source_id,
        cycle_time=cycle_time,
    )


def _manifest_key_suffix_matches_source_cycle(key: str, *, source_id: str, cycle_time: datetime) -> bool:
    parts = key.split("/")
    if len(parts) < 4 or any(part in {"", ".", ".."} for part in parts):
        return False
    raw, source, cycle, filename = parts[-4:]
    return (
        raw == "raw"
        and source.lower() == normalize_source_id(source_id).lower()
        and cycle == format_cycle_time(cycle_time)
        and filename == "manifest.json"
    )


def _bounded_manifest_metadata(value: Any) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        return {}
    allowed_keys = (
        "first_forecast_hour",
        "last_forecast_hour",
        "max_lead_hours",
        "physical_file_count",
        "physical_file_layout",
        "total_file_count",
        "variable_count",
    )
    return {key: value[key] for key in allowed_keys if key in value}


def _env_flag(name: str) -> bool:
    value = os.getenv(name)
    if value is None:
        return False
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _absolute_path(path: str | Path) -> Path:
    root = Path(path).expanduser()
    return root if root.is_absolute() else Path.cwd() / root


def _object_uri_evidence(value: str) -> str:
    parsed = urlparse(str(value or ""))
    if parsed.scheme in {"s3", "published"}:
        return "[object-uri]"
    if parsed.scheme:
        return "[uri]"
    return str(value or "")


def _format_time(value: datetime) -> str:
    return parse_cycle_time(value).isoformat().replace("+00:00", "Z")
