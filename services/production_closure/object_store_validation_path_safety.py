from __future__ import annotations

import os
import stat
from pathlib import Path, PurePosixPath
from typing import Any
from urllib.parse import unquote, urlsplit, urlunsplit

from packages.common.safe_fs import SafeFilesystemError, stat_no_follow
from services.production_closure.object_store_validation_contracts import (
    ENCODED_SEPARATOR_RE,
    MAX_DESCENDANT_SYMLINK_SCAN_NODES,
    MAX_PERCENT_DECODE_ROUNDS,
    RUNTIME_DIR_FLAGS,
    SAFE_RUN_ID_RE,
    SENSITIVE_PREFIX_ASSIGNMENT_RE,
    SENSITIVE_PREFIX_SEPARATOR_RE,
    ProductionObjectStoreValidationError,
)


def _validate_config(config: Any) -> None:
    if config.target not in {"s3", "minio", "local-production-like"}:
        raise ProductionObjectStoreValidationError(
            "PRODUCTION_OBJECT_STORE_TARGET_INVALID",
            "Object-store target must be one of: s3, minio, local-production-like.",
        )
    if config.cleanup_policy not in {"delete", "quarantine", "retain"}:
        raise ProductionObjectStoreValidationError(
            "PRODUCTION_OBJECT_STORE_CLEANUP_POLICY_INVALID",
            "Cleanup policy must be one of: delete, quarantine, retain.",
        )
    if not config.version:
        raise ProductionObjectStoreValidationError(
            "PRODUCTION_OBJECT_STORE_VERSION_MISSING",
            "Basins package version must not be empty.",
        )
    if config.object_store_prefix and "://" not in config.object_store_prefix:
        raise ProductionObjectStoreValidationError(
            "PRODUCTION_OBJECT_STORE_PREFIX_INVALID",
            "Object-store prefix must be an object URI prefix such as s3://bucket/prefix.",
        )
    _validate_object_store_prefix_safe(config.configured_object_store_prefix)
    if config.object_store_prefix != config.configured_object_store_prefix:
        _validate_object_store_prefix_safe(config.object_store_prefix)


def _validate_internal_lane_paths(config: Any) -> None:
    for path, path_kind in (
        (config.lane_dir / "synthetic-basins", "synthetic basins fixture"),
        (config.lane_dir / ".inventory.raw.json", "raw inventory file"),
        (config.lane_dir / ".package_manifest.raw.json", "raw package manifest file"),
        (config.lane_dir / ".migration_report.raw.json", "raw migration report file"),
        (config.lane_dir / "runtime-workspace", "runtime workspace"),
    ):
        _validate_lane_path_contained(config, path, path_kind=path_kind)
    _validate_local_object_store_root(config)


def _validate_local_object_store_root(config: Any) -> None:
    if config.object_store_root.is_symlink():
        raise ProductionObjectStoreValidationError(
            "PRODUCTION_OBJECT_STORE_EVIDENCE_SYMLINK",
            f"local object store root must not be a symlink: {config.object_store_root}",
        )
    _refuse_symlink_components(config.object_store_root)
    resolved_lane = config.lane_dir.resolve(strict=False)
    try:
        config.object_store_root.expanduser().resolve(strict=False).relative_to(resolved_lane)
    except ValueError:
        pass
    else:
        _validate_lane_path_contained(config, config.object_store_root, path_kind="local object store root")
        _refuse_existing_descendant_symlinks(config.object_store_root, path_kind="local object store root")
        return
    try:
        configured_root = config.object_store_root.expanduser().resolve(strict=False)
        default_root = (config.lane_dir / "local-object-store").resolve(strict=False)
        configured_root.relative_to(default_root)
    except ValueError:
        _refuse_run_scoped_local_object_store_symlinks(config)
        return
    _validate_lane_path_contained(config, config.object_store_root, path_kind="local object store root")
    _refuse_existing_descendant_symlinks(config.object_store_root, path_kind="local object store root")


def _validate_lane_path_contained(
    config: Any,
    path: Path,
    *,
    path_kind: str,
) -> None:
    _refuse_symlink_components(path)
    if path.is_symlink():
        raise ProductionObjectStoreValidationError(
            "PRODUCTION_OBJECT_STORE_EVIDENCE_SYMLINK",
            f"{path_kind} must not be a symlink: {path}",
        )
    resolved_path = path.resolve(strict=False)
    resolved_lane = config.lane_dir.resolve(strict=False)
    try:
        resolved_path.relative_to(config.evidence_root)
        resolved_path.relative_to(resolved_lane)
    except ValueError as error:
        raise ProductionObjectStoreValidationError(
            "PRODUCTION_OBJECT_STORE_EVIDENCE_PATH_UNSAFE",
            f"{path_kind} must stay under the current object-store evidence lane.",
        ) from error


def _refuse_run_scoped_local_object_store_symlinks(config: Any) -> None:
    root = config.object_store_root.expanduser()
    for prefix in _run_scoped_local_object_store_prefixes(config):
        _refuse_existing_descendant_symlinks(root / prefix, path_kind="local object store run prefix")


def _run_scoped_local_object_store_prefixes(config: Any) -> tuple[Path, ...]:
    prefix_path = PurePosixPath(unquote(urlsplit(config.object_store_prefix).path).strip("/"))
    prefixes = {
        Path("runs") / config.run_id,
        Path(*prefix_path.parts) if prefix_path.parts else Path(),
        Path(config.version).parent,
    }
    return tuple(sorted((prefix for prefix in prefixes if str(prefix) != "."), key=str))


def _refuse_existing_descendant_symlinks(root: Path, *, path_kind: str) -> None:
    if root.is_symlink():
        raise ProductionObjectStoreValidationError(
            "PRODUCTION_OBJECT_STORE_EVIDENCE_SYMLINK",
            f"{path_kind} must not contain symlinks: {root}",
        )
    if not root.exists():
        return
    try:
        root_stat = stat_no_follow(root)
    except SafeFilesystemError as error:
        raise ProductionObjectStoreValidationError(
            "PRODUCTION_OBJECT_STORE_EVIDENCE_SYMLINK",
            f"{path_kind} must not contain symlinks: {root}",
        ) from error
    if not stat.S_ISDIR(root_stat.st_mode):
        return
    root_fd = _open_existing_directory_fd(root, root_stat, path_kind=path_kind)
    try:
        node_count = 0
        _refuse_descendant_symlinks_fd(
            root_fd,
            root,
            path_kind=path_kind,
            node_count=node_count,
        )
    finally:
        os.close(root_fd)


def _refuse_descendant_symlinks_fd(
    dir_fd: int,
    path_label: Path,
    *,
    path_kind: str,
    node_count: int,
) -> int:
    with os.scandir(dir_fd) as scanner:
        for entry in scanner:
            node_count += 1
            entry_path = path_label / entry.name
            if node_count > MAX_DESCENDANT_SYMLINK_SCAN_NODES:
                raise ProductionObjectStoreValidationError(
                    "PRODUCTION_OBJECT_STORE_EVIDENCE_PATH_UNSAFE",
                    (
                        f"{path_kind} exceeds configured symlink preflight traversal "
                        f"limit of {MAX_DESCENDANT_SYMLINK_SCAN_NODES}: {path_label}"
                    ),
                )
            try:
                entry_stat = entry.stat(follow_symlinks=False)
            except OSError as error:
                raise ProductionObjectStoreValidationError(
                    "PRODUCTION_OBJECT_STORE_EVIDENCE_PATH_UNSAFE",
                    f"Failed to stat {path_kind} entry {entry_path}: {error}",
                ) from error
            if stat.S_ISLNK(entry_stat.st_mode):
                raise ProductionObjectStoreValidationError(
                    "PRODUCTION_OBJECT_STORE_EVIDENCE_SYMLINK",
                    f"{path_kind} must not contain symlinks: {entry_path}",
                )
            if stat.S_ISDIR(entry_stat.st_mode):
                child_fd = _open_runtime_prefix_child_dir(dir_fd, entry.name, entry_path, entry_stat)
                try:
                    node_count = _refuse_descendant_symlinks_fd(
                        child_fd,
                        entry_path,
                        path_kind=path_kind,
                        node_count=node_count,
                    )
                finally:
                    os.close(child_fd)
    return node_count


def _open_existing_directory_fd(path: Path, expected_stat: os.stat_result, *, path_kind: str) -> int:
    try:
        fd = os.open(path, RUNTIME_DIR_FLAGS)
        opened = os.fstat(fd)
    except OSError as error:
        raise ProductionObjectStoreValidationError(
            "PRODUCTION_OBJECT_STORE_EVIDENCE_PATH_UNSAFE",
            f"Failed to open {path_kind} directory {path}: {error}",
        ) from error
    if expected_stat.st_dev != opened.st_dev or expected_stat.st_ino != opened.st_ino:
        os.close(fd)
        raise ProductionObjectStoreValidationError(
            "PRODUCTION_OBJECT_STORE_EVIDENCE_PATH_UNSAFE",
            f"{path_kind} directory changed while being opened: {path}",
        )
    return fd


def _validate_object_store_prefix_safe(prefix: str) -> None:
    if not prefix:
        return
    try:
        parsed = urlsplit(prefix)
    except ValueError as error:
        raise ProductionObjectStoreValidationError(
            "PRODUCTION_OBJECT_STORE_PREFIX_UNSAFE",
            "Object-store prefix must not contain credential material.",
        ) from error
    if parsed.username or parsed.password:
        raise ProductionObjectStoreValidationError(
            "PRODUCTION_OBJECT_STORE_PREFIX_UNSAFE",
            "Object-store prefix must not contain userinfo credentials.",
        )
    if parsed.query:
        raise ProductionObjectStoreValidationError(
            "PRODUCTION_OBJECT_STORE_PREFIX_UNSAFE",
            "Object-store prefix must not contain query parameters.",
        )
    if parsed.fragment:
        raise ProductionObjectStoreValidationError(
            "PRODUCTION_OBJECT_STORE_PREFIX_UNSAFE",
            "Object-store prefix must not contain fragments.",
        )
    for decoded in _canonical_decode_steps(prefix):
        if ENCODED_SEPARATOR_RE.search(decoded):
            raise ProductionObjectStoreValidationError(
                "PRODUCTION_OBJECT_STORE_PREFIX_UNSAFE",
                "Object-store prefix path must not contain encoded separators.",
            )
        if SENSITIVE_PREFIX_ASSIGNMENT_RE.search(decoded):
            raise ProductionObjectStoreValidationError(
                "PRODUCTION_OBJECT_STORE_PREFIX_UNSAFE",
                "Object-store prefix must not contain credential assignments.",
            )
        decoded_parts = SENSITIVE_PREFIX_SEPARATOR_RE.split(decoded)
        if any(SENSITIVE_PREFIX_ASSIGNMENT_RE.search(part) for part in decoded_parts):
            raise ProductionObjectStoreValidationError(
                "PRODUCTION_OBJECT_STORE_PREFIX_UNSAFE",
                "Object-store prefix must not contain credential assignments.",
            )
        decoded_parsed = urlsplit(decoded)
        if decoded_parsed.username or decoded_parsed.password:
            raise ProductionObjectStoreValidationError(
                "PRODUCTION_OBJECT_STORE_PREFIX_UNSAFE",
                "Object-store prefix must not contain userinfo credentials.",
            )
        _guard_url_authority(decoded_parsed.netloc)
        for segment in decoded_parsed.path.split("/"):
            if segment in {".", ".."} or "\\" in segment:
                raise ProductionObjectStoreValidationError(
                    "PRODUCTION_OBJECT_STORE_PREFIX_UNSAFE",
                    "Object-store prefix path must not contain traversal.",
                )


def _guard_url_authority(netloc: str) -> None:
    if not netloc:
        return
    if "/" in netloc or "\\" in netloc:
        raise ProductionObjectStoreValidationError(
            "PRODUCTION_OBJECT_STORE_PREFIX_UNSAFE",
            "Object-store prefix URL authority must not contain separators.",
        )
    host = netloc.rsplit("@", maxsplit=1)[-1].split(":", maxsplit=1)[0]
    if host in {".", ".."} or any(segment in {"", ".", ".."} for segment in host.split(".")):
        raise ProductionObjectStoreValidationError(
            "PRODUCTION_OBJECT_STORE_PREFIX_UNSAFE",
            "Object-store prefix URL authority must not contain traversal.",
        )


def _canonical_decode_steps(value: str) -> tuple[str, ...]:
    steps = [value]
    current = value
    for _ in range(MAX_PERCENT_DECODE_ROUNDS):
        decoded = unquote(current)
        if decoded == current:
            break
        steps.append(decoded)
        current = decoded
    if unquote(current) != current:
        raise ProductionObjectStoreValidationError(
            "PRODUCTION_OBJECT_STORE_PREFIX_UNSAFE",
            "Object-store prefix contains over-encoded percent escapes.",
        )
    return tuple(steps)


def _operational_prefix(value: str) -> str:
    try:
        parsed = urlsplit(value)
    except ValueError:
        return value
    if not parsed.scheme or not parsed.netloc:
        return value
    hostname = parsed.hostname or ""
    netloc = hostname
    if parsed.port is not None:
        netloc = f"{netloc}:{parsed.port}"
    return urlunsplit((parsed.scheme, netloc, parsed.path.rstrip("/"), "", ""))


def _safe_run_id(run_id: str) -> str:
    if SAFE_RUN_ID_RE.fullmatch(run_id):
        return run_id
    raise ProductionObjectStoreValidationError(
        "PRODUCTION_OBJECT_STORE_RUN_ID_UNSAFE",
        "run_id may contain only alphanumeric characters, underscores, and hyphens.",
    )


def _safe_resolved_evidence_root(evidence_root: Path) -> Path:
    root = evidence_root.expanduser()
    _refuse_symlink_components_to_deepest_existing(root)
    return root.resolve(strict=False)


def _refuse_symlink_components(path: Path) -> None:
    current = Path(path.anchor) if path.is_absolute() else Path()
    for part in path.parts:
        if part == path.anchor or part == "":
            continue
        current = current / part
        if current.is_symlink():
            raise ProductionObjectStoreValidationError(
                "PRODUCTION_OBJECT_STORE_EVIDENCE_SYMLINK",
                f"Evidence path component must not be a symlink: {current}",
            )


def _refuse_symlink_components_to_deepest_existing(path: Path) -> None:
    current = Path(path.anchor) if path.is_absolute() else Path()
    for part in path.parts:
        if part == path.anchor or part == "":
            continue
        current = current / part
        if current.is_symlink():
            raise ProductionObjectStoreValidationError(
                "PRODUCTION_OBJECT_STORE_EVIDENCE_SYMLINK",
                f"Evidence path component must not be a symlink: {current}",
            )
        if not current.exists():
            break


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
