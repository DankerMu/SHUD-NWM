from __future__ import annotations

import hashlib
import os
import stat
from pathlib import Path, PurePosixPath
from typing import Any, Sequence

from packages.common.object_store import LocalObjectStore
from packages.common.safe_fs import (
    SafeFilesystemError,
    atomic_write_bytes_no_follow,
    ensure_directory_no_follow,
    read_bytes_limited_no_follow,
    stat_no_follow,
)
from services.production_closure.object_store_validation_contracts import (
    MAX_DESCENDANT_SYMLINK_SCAN_NODES,
    MAX_RUNTIME_STAGING_DIRECTORY_DEPTH,
    MAX_RUNTIME_STAGING_FILE_COUNT,
    MAX_RUNTIME_STAGING_NODE_COUNT,
    MAX_RUNTIME_STAGING_OBJECT_BYTES,
    MAX_RUNTIME_STAGING_TOTAL_BYTES,
    RUNTIME_DIR_FLAGS,
    RUNTIME_READ_FLAGS,
    SAFE_IDENTIFIER_RE,
    ProductionObjectStoreValidationError,
    RuntimePrefixCollection,
    RuntimeStagedObject,
    RuntimeStagingBudget,
    RuntimeStagingPreparation,
    _sha256_json,
)
from services.production_closure.object_store_validation_path_safety import (
    _refuse_existing_descendant_symlinks,
    _validate_lane_path_contained,
)
from workers.shud_runtime.runtime import SHUDRuntimeError


def _runtime_staging_evidence(
    config: Any,
    store: LocalObjectStore,
    manifest: dict[str, Any],
    stored_verification: dict[str, Any],
    writer: Any,
) -> dict[str, Any]:
    scratch_prefix = f"runs/{config.run_id}/input/scratch/runtime-staging"
    forcing_key = f"{scratch_prefix}/forcing/gfs/2026051600/basin_v1/{manifest['model_id']}/forcing.tsd.forc"
    _write_validation_scratch_object(store, forcing_key, b"forcing\n")
    runtime_manifest = {
        "run_id": f"{config.run_id}_runtime_staging",
        "run_type": "forecast",
        "scenario_id": "production_object_store_validation",
        "source_id": "GFS",
        "cycle_time": "2026-05-16T00:00:00Z",
        "start_time": "2026-05-16T00:00:00Z",
        "end_time": "2026-05-17T00:00:00Z",
        "model": {
            "model_id": manifest["model_id"],
            "basin_version_id": "basin_v1",
            "model_package_uri": manifest["model_package_uri"],
            "project_name": manifest.get("shud_input_name") or manifest["model_id"],
            "segment_count": 2,
        },
        "initial_state": {"state_id": None, "ic_file_uri": None},
        "forcing": {
            "forcing_version_id": "forc_gfs_2026051600",
            "forcing_uri": store.uri_for_key(forcing_key.rsplit("/", maxsplit=1)[0] + "/"),
        },
        "runtime": {"output_interval_minutes": 1440},
        "outputs": {
            "run_manifest_uri": store.uri_for_key(f"runs/{config.run_id}/input/runtime-staging/manifest.json"),
            "output_uri": store.uri_for_key(f"runs/{config.run_id}/output/runtime-staging/"),
            "log_uri": store.uri_for_key(f"runs/{config.run_id}/logs/runtime-staging/"),
        },
    }
    input_dir = config.lane_dir / "runtime-workspace" / "runs" / runtime_manifest["run_id"] / "input"
    output_dir = config.lane_dir / "runtime-workspace" / "runs" / runtime_manifest["run_id"] / "output"
    _validate_lane_path_contained(config, input_dir, path_kind="runtime input directory")
    _validate_lane_path_contained(config, output_dir, path_kind="runtime output directory")
    try:
        ensure_directory_no_follow(input_dir, containment_root=config.lane_dir)
        ensure_directory_no_follow(output_dir, containment_root=config.lane_dir)
    except SafeFilesystemError as error:
        error_code = (
            "PRODUCTION_OBJECT_STORE_EVIDENCE_WRITE_FAILED"
            if error.kind == "io"
            else "PRODUCTION_OBJECT_STORE_EVIDENCE_PATH_UNSAFE"
        )
        raise ProductionObjectStoreValidationError(
            error_code,
            f"Failed to prepare runtime staging workspace directory: {error}",
        ) from error
    _validate_lane_path_contained(config, input_dir, path_kind="runtime input directory")
    _validate_lane_path_contained(config, output_dir, path_kind="runtime output directory")
    try:
        preparation = _prepare_runtime_staging_workspace(
            config,
            store,
            runtime_manifest,
            manifest,
            stored_verification,
            input_dir,
            output_dir,
            allowed_forcing_keys={forcing_key},
        )
    except SHUDRuntimeError as error:
        return {
            "status": "blocked",
            "error_code": error.error_code,
            "message": error.message,
            "runtime_manifest": {
                "model_package_uri": runtime_manifest["model"]["model_package_uri"],
                "manifest_uri": manifest["manifest_uri"],
                "forcing_uri": runtime_manifest["forcing"]["forcing_uri"],
            },
            "validation_object_keys": [forcing_key],
        }
    except ProductionObjectStoreValidationError:
        raise
    evidence = {
        "status": "prepared",
        "execution_status": "not_executed",
        "execution_reason": (
            "fast lane verifies object-URI staging and cfg generation without running a live SHUD solver."
        ),
        "runtime_manifest": {
            "model_package_uri": runtime_manifest["model"]["model_package_uri"],
            "manifest_uri": manifest["manifest_uri"],
            "forcing_uri": runtime_manifest["forcing"]["forcing_uri"],
            "run_manifest_uri": runtime_manifest["outputs"]["run_manifest_uri"],
        },
        "scratch_prefix": scratch_prefix,
        "validation_object_keys": [forcing_key],
        "staged_file_count": len(preparation.staged_files),
        "staged_total_bytes": preparation.budgets["staged_total_bytes"],
        "staged_files": preparation.staged_files,
        "staged_object_receipts": {
            "package": preparation.package_receipts,
            "forcing": preparation.forcing_receipts,
        },
        "forcing_prefix_receipt": preparation.forcing_prefix_receipt,
        "staging_budgets": preparation.budgets,
        "generated_cfg_path": str(preparation.cfg_path),
    }
    writer.write_json(config.lane_dir / "runtime_staging_manifest.json", runtime_manifest)
    return evidence


def _write_validation_scratch_object(store: LocalObjectStore, key: str, content: bytes) -> str:
    normalized_key = store.normalize_key(key)
    if not normalized_key.startswith("runs/"):
        raise ProductionObjectStoreValidationError(
            "PRODUCTION_OBJECT_STORE_VALIDATION_KEY_UNSAFE",
            "Validation-created runtime scratch objects must stay under runs/<run_id>/.",
        )
    if store.exists(normalized_key):
        raise ProductionObjectStoreValidationError(
            "PRODUCTION_OBJECT_STORE_VALIDATION_OBJECT_EXISTS",
            f"Validation scratch object already exists and will not be overwritten: {normalized_key}",
        )
    return store.write_bytes_atomic(normalized_key, content)


def _prepare_runtime_staging_workspace(
    config: Any,
    store: LocalObjectStore,
    runtime_manifest: dict[str, Any],
    package_manifest: dict[str, Any],
    stored_verification: dict[str, Any],
    input_dir: Path,
    output_dir: Path,
    *,
    allowed_forcing_keys: set[str] | None = None,
) -> RuntimeStagingPreparation:
    _refuse_existing_descendant_symlinks(input_dir, path_kind="runtime input directory")
    _refuse_existing_descendant_symlinks(output_dir, path_kind="runtime output directory")
    budget = RuntimeStagingBudget(
        max_file_count=MAX_RUNTIME_STAGING_FILE_COUNT,
        max_node_count=MAX_RUNTIME_STAGING_NODE_COUNT,
        max_directory_depth=MAX_RUNTIME_STAGING_DIRECTORY_DEPTH,
        max_total_bytes=MAX_RUNTIME_STAGING_TOTAL_BYTES,
        max_object_bytes=MAX_RUNTIME_STAGING_OBJECT_BYTES,
    )
    _assert_runtime_workspace_empty(config, input_dir, path_kind="runtime input directory")
    _assert_runtime_workspace_empty(config, output_dir, path_kind="runtime output directory")
    package_collection = _collect_runtime_package_objects(
        config,
        store,
        package_manifest,
        stored_verification,
        input_dir,
        budget,
    )
    forcing_collection = _collect_runtime_object_or_prefix(
        config,
        store,
        runtime_manifest["forcing"]["forcing_uri"],
        input_dir,
        budget,
        allowed_keys=allowed_forcing_keys,
    )
    staged_objects = [*package_collection.objects, *forcing_collection.objects]
    _assert_runtime_staging_targets_unique(input_dir, staged_objects)
    for staged_object in staged_objects:
        _write_runtime_staging_bytes(config, staged_object.target, staged_object.content)
    staged_receipts = staged_objects
    staged_paths_by_suffix = _runtime_staged_paths_by_suffix(staged_receipts)
    for suffix in (".mesh", ".para", ".calib", ".tsd.forc"):
        if _first_staged_path(staged_paths_by_suffix, suffix) is None:
            raise SHUDRuntimeError("WORKSPACE_INCOMPLETE", f"Missing required staged file: *{suffix}")
    template_path = _first_staged_path(staged_paths_by_suffix, ".cfg.para") or _first_staged_path(
        staged_paths_by_suffix, ".para"
    )
    if template_path is None:
        raise SHUDRuntimeError("CFG_TEMPLATE_MISSING", "No .para template found in staged model package.")
    template_content = _read_runtime_staging_text(config, template_path)
    cfg_path = input_dir / f"{_safe_runtime_project_name(runtime_manifest)}.cfg.para"
    content = "\n".join(line for line in template_content.splitlines() if ".cfg.ic" not in line)
    replacements = {
        "START_TIME": str(runtime_manifest["start_time"]),
        "END_TIME": str(runtime_manifest["end_time"]),
        "OUTPUT_DIR": str(output_dir),
        "MODEL_OUTPUT_INTERVAL": str(runtime_manifest.get("runtime", {}).get("output_interval_minutes", 1440)),
        "INIT_MODE": "1",
        "SEGMENT_COUNT": str(runtime_manifest["model"]["segment_count"]),
    }
    for key, value in replacements.items():
        content = _replace_or_append_runtime_cfg(content, key, value)
    _write_runtime_staging_bytes(config, cfg_path, content.rstrip().encode("utf-8") + b"\n")
    _refuse_existing_descendant_symlinks(input_dir, path_kind="runtime input directory")
    _refuse_existing_descendant_symlinks(output_dir, path_kind="runtime output directory")
    staged_files = _runtime_staged_files_from_receipts(input_dir, staged_receipts, cfg_path)
    return RuntimeStagingPreparation(
        cfg_path=cfg_path,
        package_receipts=[staged.receipt for staged in package_collection.objects],
        forcing_receipts=[staged.receipt for staged in forcing_collection.objects],
        forcing_prefix_receipt=forcing_collection.prefix_receipt,
        staged_files=staged_files,
        budgets=budget.to_payload(),
    )


def _collect_runtime_package_objects(
    config: Any,
    store: LocalObjectStore,
    package_manifest: dict[str, Any],
    stored_verification: dict[str, Any],
    input_dir: Path,
    budget: RuntimeStagingBudget,
) -> RuntimePrefixCollection:
    verification_by_uri = _runtime_verification_entries_by_uri(stored_verification)
    objects: list[RuntimeStagedObject] = []
    for entry in package_manifest.get("included_files", []):
        if not isinstance(entry, dict) or entry.get("role") == "manifest":
            continue
        _assert_runtime_package_entry_verified(entry, verification_by_uri)
        relative_path = _safe_runtime_relative_path(str(entry.get("relative_path", "")))
        target = input_dir / relative_path
        staged_object = _collect_runtime_object(
            config,
            store,
            str(entry["object_uri"]),
            target,
            receipt_relative_path=relative_path.as_posix(),
            budget=budget,
            expected={
                "object_uri": str(entry["object_uri"]),
                "relative_path": relative_path.as_posix(),
                "role": str(entry["role"]),
                "size_bytes": int(entry["size_bytes"]),
                "sha256": str(entry["sha256"]),
            },
            receipt_source="package_manifest",
        )
        objects.append(staged_object)
    return RuntimePrefixCollection(objects=objects)


def _collect_runtime_object(
    config: Any,
    store: LocalObjectStore,
    uri_or_key: str,
    target: Path,
    *,
    receipt_relative_path: str,
    budget: RuntimeStagingBudget,
    expected: dict[str, Any] | None = None,
    receipt_source: str,
) -> RuntimeStagedObject:
    _validate_lane_path_contained(config, target.parent, path_kind="runtime staging directory")
    content = store.read_bytes_limited(uri_or_key, max_bytes=MAX_RUNTIME_STAGING_OBJECT_BYTES)
    if len(content) > MAX_RUNTIME_STAGING_OBJECT_BYTES:
        raise ProductionObjectStoreValidationError(
            "PRODUCTION_OBJECT_STORE_EVIDENCE_PATH_UNSAFE",
            (
                "Runtime staging object exceeds configured limit of "
                f"{MAX_RUNTIME_STAGING_OBJECT_BYTES} bytes: {uri_or_key}"
            ),
        )
    if not content:
        raise ProductionObjectStoreValidationError(
            "PRODUCTION_OBJECT_STORE_EVIDENCE_PATH_UNSAFE",
            f"Runtime staging object must not be empty: {uri_or_key}",
        )
    size_bytes = len(content)
    sha256 = hashlib.sha256(content).hexdigest()
    if expected is not None:
        _assert_runtime_staged_object_matches_expected(
            expected,
            actual_object_uri=str(uri_or_key),
            actual_relative_path=receipt_relative_path,
            actual_size_bytes=size_bytes,
            actual_sha256=sha256,
        )
    budget.reserve(relative_path=receipt_relative_path, size_bytes=size_bytes)
    receipt = {
        "source": receipt_source,
        "object_uri": store.uri_for_key(store.normalize_key(uri_or_key)),
        "relative_path": receipt_relative_path,
        "target_relative_path": target.relative_to(config.lane_dir).as_posix(),
        "size_bytes": size_bytes,
        "sha256": sha256,
    }
    if expected is not None:
        receipt["role"] = expected["role"]
        receipt["manifest_object_uri"] = expected["object_uri"]
        receipt["manifest_relative_path"] = expected["relative_path"]
        receipt["manifest_size_bytes"] = expected["size_bytes"]
        receipt["manifest_sha256"] = expected["sha256"]
        receipt["verified_manifest_contract"] = True
    return RuntimeStagedObject(target=target, content=content, receipt=receipt)


def _collect_runtime_object_or_prefix(
    config: Any,
    store: LocalObjectStore,
    uri_or_key: str,
    input_dir: Path,
    budget: RuntimeStagingBudget,
    *,
    allowed_keys: set[str] | None = None,
) -> RuntimePrefixCollection:
    normalized_key = store.normalize_key(uri_or_key)
    source_path = store.resolve_path(normalized_key)
    try:
        source_stat = stat_no_follow(source_path, containment_root=store.root)
    except FileNotFoundError as error:
        raise SHUDRuntimeError("ARTIFACT_NOT_FOUND", f"Object storage artifact not found: {uri_or_key}") from error
    except SafeFilesystemError as error:
        raise ProductionObjectStoreValidationError(
            "PRODUCTION_OBJECT_STORE_EVIDENCE_PATH_UNSAFE",
            f"Runtime staging source path is unsafe: {source_path}: {error}",
        ) from error
    if stat.S_ISREG(source_stat.st_mode):
        if allowed_keys is not None and normalized_key not in allowed_keys:
            raise ProductionObjectStoreValidationError(
                "PRODUCTION_OBJECT_STORE_EVIDENCE_PATH_UNSAFE",
                f"Runtime staging object is not validation-owned: {normalized_key}",
            )
        target = input_dir / source_path.name
        return RuntimePrefixCollection(
            objects=[
                _collect_runtime_object(
                    config,
                    store,
                    normalized_key,
                    target,
                    receipt_relative_path=source_path.name,
                    budget=budget,
                    receipt_source="forcing_object",
                )
            ],
            prefix_receipt=None,
        )
    if stat.S_ISDIR(source_stat.st_mode):
        return _collect_runtime_prefix_objects(
            config,
            store,
            normalized_key,
            source_path,
            source_stat,
            input_dir,
            budget,
            allowed_keys=allowed_keys,
        )
    raise SHUDRuntimeError(
        "ARTIFACT_NOT_FOUND",
        f"Object storage artifact is not a regular file or directory: {uri_or_key}",
    )


def _runtime_verification_entries_by_uri(stored_verification: dict[str, Any]) -> dict[str, dict[str, Any]]:
    entries: dict[str, dict[str, Any]] = {}
    for entry in stored_verification.get("entries", []):
        if not isinstance(entry, dict) or entry.get("role") == "manifest":
            continue
        object_uri = str(entry.get("object_uri", ""))
        if object_uri:
            entries[object_uri] = entry
    return entries


def _assert_runtime_package_entry_verified(
    manifest_entry: dict[str, Any],
    verification_by_uri: dict[str, dict[str, Any]],
) -> None:
    object_uri = str(manifest_entry.get("object_uri", ""))
    verification = verification_by_uri.get(object_uri)
    if verification is None or verification.get("verified") is not True:
        raise ProductionObjectStoreValidationError(
            "PRODUCTION_OBJECT_STORE_EVIDENCE_PATH_UNSAFE",
            f"Runtime package staging is missing a verified stored-object receipt for {object_uri}.",
        )
    expected_pairs = (
        ("relative_path", str(manifest_entry.get("relative_path", "")), str(verification.get("relative_path", ""))),
        ("role", str(manifest_entry.get("role", "")), str(verification.get("role", ""))),
        (
            "size_bytes",
            int(manifest_entry.get("size_bytes", -1)),
            int(verification.get("expected_size_bytes", -2)),
        ),
        ("sha256", str(manifest_entry.get("sha256", "")), str(verification.get("expected_sha256", ""))),
    )
    for field_name, expected, actual in expected_pairs:
        if expected != actual:
            raise ProductionObjectStoreValidationError(
                "PRODUCTION_OBJECT_STORE_EVIDENCE_PATH_UNSAFE",
                f"Runtime package staging verification receipt mismatch for {object_uri}: {field_name}.",
            )


def _assert_runtime_staged_object_matches_expected(
    expected: dict[str, Any],
    *,
    actual_object_uri: str,
    actual_relative_path: str,
    actual_size_bytes: int,
    actual_sha256: str,
) -> None:
    if str(expected["object_uri"]) != actual_object_uri:
        raise ProductionObjectStoreValidationError(
            "PRODUCTION_OBJECT_STORE_EVIDENCE_PATH_UNSAFE",
            "Runtime package staging object URI changed before staging.",
        )
    if str(expected["relative_path"]) != actual_relative_path:
        raise ProductionObjectStoreValidationError(
            "PRODUCTION_OBJECT_STORE_EVIDENCE_PATH_UNSAFE",
            f"Runtime package staging relative path changed before staging: {actual_relative_path}",
        )
    if int(expected["size_bytes"]) != actual_size_bytes or str(expected["sha256"]) != actual_sha256:
        raise ProductionObjectStoreValidationError(
            "PRODUCTION_OBJECT_STORE_EVIDENCE_PATH_UNSAFE",
            f"Runtime package staging bytes differ from verified manifest contract: {actual_relative_path}",
        )


def _collect_runtime_prefix_objects(
    config: Any,
    store: LocalObjectStore,
    normalized_prefix_key: str,
    source_path: Path,
    source_stat: os.stat_result,
    input_dir: Path,
    budget: RuntimeStagingBudget,
    *,
    allowed_keys: set[str] | None = None,
) -> RuntimePrefixCollection:
    root_fd = _open_runtime_prefix_dir(source_path, store.root)
    objects: list[RuntimeStagedObject] = []
    receipts: list[dict[str, Any]] = []
    prefix_digest = hashlib.sha256()
    try:
        _assert_runtime_prefix_identity(source_path, store.root, source_stat, root_fd)
        _collect_runtime_prefix_dir_fd(
            config,
            store,
            root_fd,
            normalized_prefix_key,
            source_path,
            PurePosixPath(),
            input_dir,
            budget,
            objects,
            receipts,
            prefix_digest,
            allowed_keys=allowed_keys,
        )
        _assert_runtime_prefix_identity(source_path, store.root, source_stat, root_fd)
    finally:
        os.close(root_fd)
    if not receipts:
        raise ProductionObjectStoreValidationError(
            "PRODUCTION_OBJECT_STORE_EVIDENCE_PATH_UNSAFE",
            f"Runtime staging prefix must contain at least one non-empty regular file: {source_path}",
        )
    prefix_receipt = {
        "source": "forcing_prefix",
        "object_uri": store.uri_for_key(normalized_prefix_key.rstrip("/") + "/"),
        "root_path": str(source_path),
        "root_device": source_stat.st_dev,
        "root_inode": source_stat.st_ino,
        "file_count": len(receipts),
        "total_bytes": sum(int(receipt["size_bytes"]) for receipt in receipts),
        "aggregate_sha256": prefix_digest.hexdigest(),
        "objects": receipts,
    }
    return RuntimePrefixCollection(objects=objects, prefix_receipt=prefix_receipt)


def _collect_runtime_prefix_dir_fd(
    config: Any,
    store: LocalObjectStore,
    dir_fd: int,
    normalized_prefix_key: str,
    path_label: Path,
    relative_dir: PurePosixPath,
    input_dir: Path,
    budget: RuntimeStagingBudget,
    objects: list[RuntimeStagedObject],
    receipts: list[dict[str, Any]],
    prefix_digest: Any,
    *,
    allowed_keys: set[str] | None = None,
) -> None:
    entries: list[tuple[str, os.stat_result, Path, PurePosixPath]] = []
    with os.scandir(dir_fd) as scanner:
        for entry in scanner:
            entry_relative = PurePosixPath(relative_dir, entry.name)
            entry_path = path_label / entry.name
            budget.reserve_node(relative_path=entry_relative.as_posix())
            try:
                entry_stat = entry.stat(follow_symlinks=False)
            except OSError as error:
                raise ProductionObjectStoreValidationError(
                    "PRODUCTION_OBJECT_STORE_EVIDENCE_PATH_UNSAFE",
                    f"Failed to stat runtime staging prefix entry {entry_path}: {error}",
                ) from error
            entry_key = PurePosixPath(normalized_prefix_key, entry_relative.as_posix()).as_posix()
            if allowed_keys is not None and not _runtime_prefix_entry_allowed(
                entry_key,
                entry_stat,
                allowed_keys,
            ):
                raise ProductionObjectStoreValidationError(
                    "PRODUCTION_OBJECT_STORE_EVIDENCE_PATH_UNSAFE",
                    f"Runtime staging prefix contains non-validation object: {entry_key}",
                )
            entries.append((entry.name, entry_stat, entry_path, entry_relative))
    entries.sort(key=lambda item: item[0])
    for entry_name, entry_stat, entry_path, entry_relative in entries:
        if stat.S_ISLNK(entry_stat.st_mode):
            raise ProductionObjectStoreValidationError(
                "PRODUCTION_OBJECT_STORE_EVIDENCE_PATH_UNSAFE",
                f"Runtime staging prefix must not contain symlinks: {entry_path}",
            )
        if stat.S_ISDIR(entry_stat.st_mode):
            relative_depth = len(entry_relative.parts)
            if relative_depth > budget.max_directory_depth:
                raise ProductionObjectStoreValidationError(
                    "PRODUCTION_OBJECT_STORE_EVIDENCE_PATH_UNSAFE",
                    (
                        "Runtime staging prefix exceeds configured directory depth "
                        f"limit of {budget.max_directory_depth}: {entry_relative.as_posix()}"
                    ),
                )
            child_fd = _open_runtime_prefix_child_dir(dir_fd, entry_name, entry_path, entry_stat)
            try:
                _collect_runtime_prefix_dir_fd(
                    config,
                    store,
                    child_fd,
                    normalized_prefix_key,
                    entry_path,
                    entry_relative,
                    input_dir,
                    budget,
                    objects,
                    receipts,
                    prefix_digest,
                    allowed_keys=allowed_keys,
                )
            finally:
                os.close(child_fd)
            continue
        if not stat.S_ISREG(entry_stat.st_mode):
            raise ProductionObjectStoreValidationError(
                "PRODUCTION_OBJECT_STORE_EVIDENCE_PATH_UNSAFE",
                f"Runtime staging prefix entries must be regular files or directories: {entry_path}",
            )
        if entry_stat.st_size <= 0:
            raise ProductionObjectStoreValidationError(
                "PRODUCTION_OBJECT_STORE_EVIDENCE_PATH_UNSAFE",
                f"Runtime staging prefix files must not be empty: {entry_path}",
            )
        relative_path = _safe_runtime_relative_path(entry_relative.as_posix())
        relative_key = PurePosixPath(normalized_prefix_key, entry_relative.as_posix()).as_posix()
        budget.reserve(relative_path=relative_path.as_posix(), size_bytes=entry_stat.st_size)
        content = _read_runtime_prefix_file(dir_fd, entry_name, entry_path, entry_stat)
        sha256 = hashlib.sha256(content).hexdigest()
        target = input_dir / relative_path
        receipt = {
            "source": "forcing_prefix",
            "object_uri": store.uri_for_key(relative_key),
            "relative_path": entry_relative.as_posix(),
            "target_relative_path": target.relative_to(config.lane_dir).as_posix(),
            "size_bytes": len(content),
            "sha256": sha256,
        }
        receipt_material = {
            "object_uri": receipt["object_uri"],
            "relative_path": receipt["relative_path"],
            "size_bytes": receipt["size_bytes"],
            "sha256": receipt["sha256"],
        }
        prefix_digest.update(_sha256_json(receipt_material).encode("ascii"))
        receipts.append(receipt)
        objects.append(RuntimeStagedObject(target=target, content=content, receipt=receipt))


def _runtime_prefix_entry_allowed(entry_key: str, entry_stat: os.stat_result, allowed_keys: set[str]) -> bool:
    if stat.S_ISDIR(entry_stat.st_mode):
        return any(key.startswith(f"{entry_key}/") for key in allowed_keys)
    return entry_key in allowed_keys


def _open_runtime_prefix_dir(path: Path, containment_root: Path) -> int:
    try:
        fd = os.open(path, RUNTIME_DIR_FLAGS)
        opened = os.fstat(fd)
        stat_no_follow(path, containment_root=containment_root)
        if not stat.S_ISDIR(opened.st_mode):
            raise ProductionObjectStoreValidationError(
                "PRODUCTION_OBJECT_STORE_EVIDENCE_PATH_UNSAFE",
                f"Runtime staging prefix is not a directory: {path}",
            )
        return fd
    except ProductionObjectStoreValidationError:
        raise
    except (OSError, SafeFilesystemError) as error:
        raise ProductionObjectStoreValidationError(
            "PRODUCTION_OBJECT_STORE_EVIDENCE_PATH_UNSAFE",
            f"Failed to open runtime staging prefix directory {path}: {error}",
        ) from error


def _open_runtime_prefix_child_dir(
    parent_fd: int,
    name: str,
    path_label: Path,
    expected_stat: os.stat_result,
) -> int:
    try:
        child_fd = os.open(name, RUNTIME_DIR_FLAGS, dir_fd=parent_fd)
        opened = os.fstat(child_fd)
        if expected_stat.st_dev != opened.st_dev or expected_stat.st_ino != opened.st_ino:
            raise ProductionObjectStoreValidationError(
                "PRODUCTION_OBJECT_STORE_EVIDENCE_PATH_UNSAFE",
                f"Runtime staging prefix directory changed during traversal: {path_label}",
            )
        return child_fd
    except ProductionObjectStoreValidationError:
        raise
    except OSError as error:
        raise ProductionObjectStoreValidationError(
            "PRODUCTION_OBJECT_STORE_EVIDENCE_PATH_UNSAFE",
            f"Failed to open runtime staging prefix directory {path_label}: {error}",
        ) from error


def _read_runtime_prefix_file(
    parent_fd: int,
    name: str,
    path_label: Path,
    expected_stat: os.stat_result,
) -> bytes:
    if not stat.S_ISREG(expected_stat.st_mode):
        raise ProductionObjectStoreValidationError(
            "PRODUCTION_OBJECT_STORE_EVIDENCE_PATH_UNSAFE",
            f"Runtime staging prefix file must be a regular file: {path_label}",
        )
    if expected_stat.st_size > MAX_RUNTIME_STAGING_OBJECT_BYTES:
        raise ProductionObjectStoreValidationError(
            "PRODUCTION_OBJECT_STORE_EVIDENCE_PATH_UNSAFE",
            (
                "Runtime staging object exceeds configured per-object byte "
                f"limit of {MAX_RUNTIME_STAGING_OBJECT_BYTES}: {path_label}"
            ),
        )
    file_fd: int | None = None
    try:
        file_fd = os.open(name, RUNTIME_READ_FLAGS, dir_fd=parent_fd)
        opened = os.fstat(file_fd)
        if not stat.S_ISREG(opened.st_mode):
            raise ProductionObjectStoreValidationError(
                "PRODUCTION_OBJECT_STORE_EVIDENCE_PATH_UNSAFE",
                f"Runtime staging prefix file must be a regular file: {path_label}",
            )
        if expected_stat.st_dev != opened.st_dev or expected_stat.st_ino != opened.st_ino:
            raise ProductionObjectStoreValidationError(
                "PRODUCTION_OBJECT_STORE_EVIDENCE_PATH_UNSAFE",
                f"Runtime staging prefix file changed while being opened: {path_label}",
            )
        content = os.read(file_fd, MAX_RUNTIME_STAGING_OBJECT_BYTES + 1)
    except ProductionObjectStoreValidationError:
        raise
    except OSError as error:
        raise ProductionObjectStoreValidationError(
            "PRODUCTION_OBJECT_STORE_EVIDENCE_PATH_UNSAFE",
            f"Failed to read runtime staging prefix file {path_label}: {error}",
        ) from error
    finally:
        if file_fd is not None:
            os.close(file_fd)
    if len(content) > MAX_RUNTIME_STAGING_OBJECT_BYTES:
        raise ProductionObjectStoreValidationError(
            "PRODUCTION_OBJECT_STORE_EVIDENCE_PATH_UNSAFE",
            (
                "Runtime staging object exceeds configured per-object byte "
                f"limit of {MAX_RUNTIME_STAGING_OBJECT_BYTES}: {path_label}"
            ),
        )
    if len(content) != expected_stat.st_size:
        raise ProductionObjectStoreValidationError(
            "PRODUCTION_OBJECT_STORE_EVIDENCE_PATH_UNSAFE",
            f"Runtime staging prefix file changed while being read: {path_label}",
        )
    return content


def _assert_runtime_staging_targets_unique(input_dir: Path, staged_objects: Sequence[RuntimeStagedObject]) -> None:
    targets_by_relative_path: dict[str, str] = {}
    for staged_object in staged_objects:
        try:
            relative_path = staged_object.target.relative_to(input_dir).as_posix()
        except ValueError as error:
            raise ProductionObjectStoreValidationError(
                "PRODUCTION_OBJECT_STORE_EVIDENCE_PATH_UNSAFE",
                f"Runtime staging target escapes input directory: {staged_object.target}",
            ) from error
        object_uri = str(staged_object.receipt.get("object_uri", ""))
        previous_uri = targets_by_relative_path.get(relative_path)
        if previous_uri is not None:
            raise ProductionObjectStoreValidationError(
                "PRODUCTION_OBJECT_STORE_EVIDENCE_PATH_UNSAFE",
                (
                    "Runtime staging target path collision before write: "
                    f"{relative_path} from {previous_uri} and {object_uri}"
                ),
            )
        targets_by_relative_path[relative_path] = object_uri


def _assert_runtime_prefix_identity(
    source_path: Path,
    containment_root: Path,
    expected_stat: os.stat_result,
    fd: int,
) -> None:
    try:
        current_stat = stat_no_follow(source_path, containment_root=containment_root)
    except (OSError, SafeFilesystemError) as error:
        raise ProductionObjectStoreValidationError(
            "PRODUCTION_OBJECT_STORE_EVIDENCE_PATH_UNSAFE",
            f"Runtime staging prefix identity changed during traversal: {source_path}: {error}",
        ) from error
    opened = os.fstat(fd)
    if (
        expected_stat.st_dev != current_stat.st_dev
        or expected_stat.st_ino != current_stat.st_ino
        or expected_stat.st_dev != opened.st_dev
        or expected_stat.st_ino != opened.st_ino
    ):
        raise ProductionObjectStoreValidationError(
            "PRODUCTION_OBJECT_STORE_EVIDENCE_PATH_UNSAFE",
            f"Runtime staging prefix identity changed during traversal: {source_path}",
        )


def _write_runtime_staging_bytes(config: Any, target: Path, content: bytes) -> None:
    _validate_lane_path_contained(config, target.parent, path_kind="runtime staging directory")
    try:
        atomic_write_bytes_no_follow(target, content, containment_root=config.lane_dir)
    except SafeFilesystemError as error:
        error_code = (
            "PRODUCTION_OBJECT_STORE_EVIDENCE_WRITE_FAILED"
            if error.kind == "io"
            else "PRODUCTION_OBJECT_STORE_EVIDENCE_PATH_UNSAFE"
        )
        raise ProductionObjectStoreValidationError(
            error_code,
            f"Failed to write runtime staging file {target}: {error}",
        ) from error


def _read_runtime_staging_text(config: Any, path: Path) -> str:
    try:
        content = read_bytes_limited_no_follow(
            path,
            max_bytes=MAX_RUNTIME_STAGING_OBJECT_BYTES,
            containment_root=config.lane_dir,
        )
    except (OSError, SafeFilesystemError) as error:
        raise ProductionObjectStoreValidationError(
            "PRODUCTION_OBJECT_STORE_EVIDENCE_PATH_UNSAFE",
            f"Failed to read runtime staging file {path}: {error}",
        ) from error
    if len(content) > MAX_RUNTIME_STAGING_OBJECT_BYTES:
        raise ProductionObjectStoreValidationError(
            "PRODUCTION_OBJECT_STORE_EVIDENCE_PATH_UNSAFE",
            f"Runtime staging file exceeds configured limit of {MAX_RUNTIME_STAGING_OBJECT_BYTES} bytes: {path}",
        )
    return content.decode("utf-8")


def _assert_runtime_workspace_empty(config: Any, root: Path, *, path_kind: str) -> None:
    _validate_lane_path_contained(config, root, path_kind=path_kind)
    if not root.exists():
        return
    try:
        root_stat = stat_no_follow(root, containment_root=config.lane_dir)
    except (OSError, SafeFilesystemError) as error:
        raise ProductionObjectStoreValidationError(
            "PRODUCTION_OBJECT_STORE_EVIDENCE_PATH_UNSAFE",
            f"Failed to inspect {path_kind} before runtime staging: {error}",
        ) from error
    if not stat.S_ISDIR(root_stat.st_mode):
        raise ProductionObjectStoreValidationError(
            "PRODUCTION_OBJECT_STORE_EVIDENCE_PATH_UNSAFE",
            f"{path_kind} must be a directory: {root}",
        )
    root_fd = _open_runtime_prefix_dir(root, config.lane_dir)
    try:
        _assert_directory_empty_fd(root_fd, root, path_kind=path_kind)
    finally:
        os.close(root_fd)


def _assert_directory_empty_fd(root_fd: int, path_label: Path, *, path_kind: str) -> None:
    count = 0
    with os.scandir(root_fd) as scanner:
        for entry in scanner:
            count += 1
            if count > MAX_DESCENDANT_SYMLINK_SCAN_NODES:
                raise ProductionObjectStoreValidationError(
                    "PRODUCTION_OBJECT_STORE_EVIDENCE_PATH_UNSAFE",
                    (
                        f"{path_kind} exceeds configured preflight traversal "
                        f"limit of {MAX_DESCENDANT_SYMLINK_SCAN_NODES}: {path_label}"
                    ),
                )
            raise ProductionObjectStoreValidationError(
                "PRODUCTION_OBJECT_STORE_EVIDENCE_PATH_UNSAFE",
                f"{path_kind} must be empty before runtime staging: {path_label / entry.name}",
            )


def _runtime_staged_paths_by_suffix(staged_objects: Sequence[RuntimeStagedObject]) -> dict[str, list[Path]]:
    paths_by_suffix: dict[str, list[Path]] = {}
    for staged in staged_objects:
        paths_by_suffix.setdefault(staged.target.name, []).append(staged.target)
        for suffix in staged.target.suffixes:
            paths_by_suffix.setdefault(suffix, []).append(staged.target)
    for paths in paths_by_suffix.values():
        paths.sort()
    return paths_by_suffix


def _first_staged_path(paths_by_suffix: dict[str, list[Path]], suffix: str) -> Path | None:
    matches = [
        path
        for paths_suffix, paths in paths_by_suffix.items()
        if paths_suffix.endswith(suffix)
        for path in paths
        if path.name.endswith(suffix)
    ]
    return sorted(matches)[0] if matches else None


def _runtime_staged_files_from_receipts(
    input_dir: Path,
    staged_objects: Sequence[RuntimeStagedObject],
    cfg_path: Path,
) -> list[str]:
    staged_files = {staged.target.relative_to(input_dir).as_posix() for staged in staged_objects}
    staged_files.add(cfg_path.relative_to(input_dir).as_posix())
    return sorted(staged_files)


def _safe_runtime_relative_path(value: str) -> Path:
    relative = PurePosixPath(value)
    if relative.is_absolute() or any(part in {"", ".", ".."} for part in relative.parts):
        raise ProductionObjectStoreValidationError(
            "PRODUCTION_OBJECT_STORE_EVIDENCE_PATH_UNSAFE",
            f"Runtime staging object path is unsafe: {value}",
        )
    return Path(*relative.parts)


def _safe_runtime_project_name(runtime_manifest: dict[str, Any]) -> str:
    name = str(runtime_manifest.get("model", {}).get("project_name") or runtime_manifest["model"]["model_id"])
    if SAFE_IDENTIFIER_RE.fullmatch(name):
        return name
    raise ProductionObjectStoreValidationError(
        "PRODUCTION_OBJECT_STORE_EVIDENCE_PATH_UNSAFE",
        f"Runtime staging project name is unsafe: {name}",
    )


def _replace_or_append_runtime_cfg(content: str, key: str, value: str) -> str:
    lines = []
    replaced = False
    for line in content.splitlines():
        stripped = line.strip()
        if stripped.startswith(f"{key} ") or stripped.startswith(f"{key}="):
            lines.append(f"{key} = {value}")
            replaced = True
        else:
            lines.append(line)
    if not replaced:
        lines.append(f"{key} = {value}")
    return "\n".join(lines)
