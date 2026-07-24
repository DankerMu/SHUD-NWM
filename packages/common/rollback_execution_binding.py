from __future__ import annotations

import hashlib
import json
import os
import re
import stat
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from packages.common.python_runtime import (
    validated_target_python_runtime,
    validated_target_python_source_root,
)
from packages.common.safe_fs import (
    SafeFilesystemError,
    atomic_write_bytes_no_follow,
    open_directory_no_follow,
    read_bytes_limited_no_follow,
    stat_no_follow,
)

ROLLBACK_EXECUTION_BINDING_SCHEMA_VERSION = "nhms.scheduler.rollback_execution_binding.v1"
ROLLBACK_EXECUTION_BINDING_NAME = "rollback-execution-binding-v1.json"
ROLLBACK_EXECUTION_BINDING_ARCHIVE_DIRECTORY = "rollback-execution-bindings-v1"
ROLLBACK_EXECUTION_ARTIFACTS_DIRECTORY = ".nhms-rollback-execution-v1"
MAX_ROLLBACK_EXECUTION_BINDING_BYTES = 64 * 1024
MAX_ROLLBACK_SOURCE_TREE_ENTRIES = 200_000
ROLLBACK_EXECUTION_BINDING_STATUSES = frozenset(
    {"prepared", "active", "rolling_forward", "completed"}
)


class RollbackExecutionBindingError(ValueError):
    pass


def rollback_execution_binding_path(workspace_root: str | Path) -> Path:
    workspace = Path(workspace_root).expanduser().resolve()
    return workspace / "scheduler" / ROLLBACK_EXECUTION_BINDING_NAME


def rollback_execution_binding_archive_path(
    workspace_root: str | Path,
    binding_id: str,
) -> Path:
    if not _hex_identity(binding_id, 64):
        raise RollbackExecutionBindingError("rollback execution binding archive identity is invalid")
    workspace = Path(workspace_root).expanduser().resolve()
    return workspace / "scheduler" / ROLLBACK_EXECUTION_BINDING_ARCHIVE_DIRECTORY / f"{binding_id}.json"


def rollback_execution_artifact_root(
    workspace_root: str | Path,
    preparation_receipt_id: str,
    target_writer_generation: str,
) -> Path:
    if not _hex_identity(preparation_receipt_id, 64) or not _hex_identity(
        target_writer_generation,
        (40, 64),
    ):
        raise RollbackExecutionBindingError("rollback execution artifact identity is invalid")
    workspace = Path(workspace_root).expanduser().resolve()
    identity = f"{preparation_receipt_id}-{target_writer_generation}"
    return workspace / ROLLBACK_EXECUTION_ARTIFACTS_DIRECTORY / identity


def binding_identity_payload(binding: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "preparation_receipt_id": binding["preparation_receipt_id"],
        "journal_root_identity": binding["journal_root_identity"],
        "scheduler_lease_identity": binding["scheduler_lease_identity"],
        "workspace_root": binding["workspace_root"],
        "lock_path": binding["lock_path"],
        "target_writer_generation": binding["target_writer_generation"],
        "target_python_runtime": binding["target_python_runtime"],
        "target_python_source_root": binding["target_python_source_root"],
        "writer_repository_root": binding["writer_repository_root"],
        "created_at": binding["created_at"],
    }


def binding_id_for(binding: Mapping[str, Any]) -> str:
    payload = binding_identity_payload(binding)
    return hashlib.sha256(
        json.dumps(payload, separators=(",", ":"), sort_keys=True).encode("utf-8")
    ).hexdigest()


def write_rollback_execution_binding(
    workspace_root: str | Path,
    binding: Mapping[str, Any],
) -> dict[str, Any]:
    workspace = Path(workspace_root).expanduser().resolve()
    validated = validate_rollback_execution_binding(
        binding,
        expected_workspace_root=workspace,
        require_artifacts=(
            binding.get("status") == "active"
            or (
                binding.get("status") == "rolling_forward"
                and binding.get("target_python_runtime") is not None
            )
        ),
    )
    content = (json.dumps(validated, separators=(",", ":"), sort_keys=True) + "\n").encode("utf-8")
    if len(content) > MAX_ROLLBACK_EXECUTION_BINDING_BYTES:
        raise RollbackExecutionBindingError("rollback execution binding exceeds its size limit")
    try:
        atomic_write_bytes_no_follow(
            rollback_execution_binding_path(workspace),
            content,
            containment_root=workspace,
            mode=0o600,
            require_durable_replace=True,
        )
    except (OSError, SafeFilesystemError) as error:
        raise RollbackExecutionBindingError("rollback execution binding write failed") from error
    return validated


def archive_completed_rollback_execution_binding(
    workspace_root: str | Path,
    binding: Mapping[str, Any],
) -> dict[str, Any]:
    workspace = Path(workspace_root).expanduser().resolve()
    validated = validate_rollback_execution_binding(
        binding,
        expected_workspace_root=workspace,
        require_artifacts=False,
    )
    if validated["status"] != "completed":
        raise RollbackExecutionBindingError("only completed rollback bindings may be archived")
    archive_path = rollback_execution_binding_archive_path(workspace, validated["binding_id"])
    existing = _read_rollback_execution_binding_path(
        archive_path,
        workspace=workspace,
        required=False,
        require_artifacts=False,
    )
    if existing is not None:
        if existing != validated:
            raise RollbackExecutionBindingError("rollback execution binding archive conflicts")
        return existing
    content = (json.dumps(validated, separators=(",", ":"), sort_keys=True) + "\n").encode("utf-8")
    if len(content) > MAX_ROLLBACK_EXECUTION_BINDING_BYTES:
        raise RollbackExecutionBindingError("rollback execution binding exceeds its size limit")
    try:
        atomic_write_bytes_no_follow(
            archive_path,
            content,
            containment_root=workspace,
            mode=0o600,
            require_durable_replace=True,
        )
    except (OSError, SafeFilesystemError) as error:
        raise RollbackExecutionBindingError("rollback execution binding archive write failed") from error
    return validated


def read_rollback_execution_binding(
    workspace_root: str | Path,
    *,
    required: bool = False,
    require_artifacts: bool = True,
) -> dict[str, Any] | None:
    workspace = Path(workspace_root).expanduser().resolve()
    return _read_rollback_execution_binding_path(
        rollback_execution_binding_path(workspace),
        workspace=workspace,
        required=required,
        require_artifacts=require_artifacts,
    )


def _read_rollback_execution_binding_path(
    path: Path,
    *,
    workspace: Path,
    required: bool,
    require_artifacts: bool,
) -> dict[str, Any] | None:
    try:
        metadata = stat_no_follow(path, containment_root=workspace)
        if (
            not stat.S_ISREG(metadata.st_mode)
            or metadata.st_uid != os.geteuid()
            or stat.S_IMODE(metadata.st_mode) & 0o077
        ):
            raise RollbackExecutionBindingError("rollback execution binding has unsafe ownership or mode")
        content = read_bytes_limited_no_follow(
            path,
            max_bytes=MAX_ROLLBACK_EXECUTION_BINDING_BYTES,
            containment_root=workspace,
        )
    except FileNotFoundError:
        if required:
            raise RollbackExecutionBindingError("rollback execution binding is missing")
        return None
    except (OSError, SafeFilesystemError) as error:
        raise RollbackExecutionBindingError("rollback execution binding read failed") from error
    if len(content) > MAX_ROLLBACK_EXECUTION_BINDING_BYTES:
        raise RollbackExecutionBindingError("rollback execution binding exceeds its size limit")
    try:
        decoded = json.loads(content.decode("utf-8", errors="strict"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise RollbackExecutionBindingError("rollback execution binding is invalid JSON") from error
    return validate_rollback_execution_binding(
        decoded,
        expected_workspace_root=workspace,
        require_artifacts=require_artifacts,
    )


def seal_rollback_python_source_tree(source_root: str | Path) -> Path:
    source = Path(source_root).expanduser().absolute()
    _inspect_rollback_python_source_tree(source, seal=True)
    return source


def validate_rollback_execution_binding(
    value: Any,
    *,
    expected_workspace_root: str | Path,
    require_artifacts: bool,
) -> dict[str, Any]:
    required_fields = {
        "schema_version",
        "binding_id",
        "status",
        "preparation_receipt_id",
        "journal_root_identity",
        "scheduler_lease_identity",
        "workspace_root",
        "lock_path",
        "target_writer_generation",
        "target_python_runtime",
        "target_python_source_root",
        "writer_repository_root",
        "created_at",
        "updated_at",
    }
    if not isinstance(value, Mapping) or set(value) != required_fields:
        raise RollbackExecutionBindingError("rollback execution binding fields are invalid")
    binding = dict(value)
    workspace = Path(expected_workspace_root).expanduser().resolve()
    if (
        binding.get("schema_version") != ROLLBACK_EXECUTION_BINDING_SCHEMA_VERSION
        or binding.get("status") not in ROLLBACK_EXECUTION_BINDING_STATUSES
        or not _hex_identity(binding.get("binding_id"), 64)
        or not _hex_identity(binding.get("preparation_receipt_id"), 64)
        or not _hex_identity(binding.get("target_writer_generation"), (40, 64))
        or not isinstance(binding.get("created_at"), str)
        or not isinstance(binding.get("updated_at"), str)
    ):
        raise RollbackExecutionBindingError("rollback execution binding identity is invalid")
    if binding["binding_id"] != binding_id_for(binding):
        raise RollbackExecutionBindingError("rollback execution binding digest is invalid")
    configured_workspace = _absolute_path(binding.get("workspace_root"), "workspace_root")
    if configured_workspace != workspace:
        raise RollbackExecutionBindingError("rollback execution binding belongs to another workspace")
    _absolute_path(binding.get("lock_path"), "lock_path")
    execution_artifacts = (
        binding.get("target_python_runtime"),
        binding.get("target_python_source_root"),
        binding.get("writer_repository_root"),
    )
    if binding["status"] == "prepared":
        if execution_artifacts != (None, None, None):
            raise RollbackExecutionBindingError(
                "prepared rollback execution binding cannot contain execution artifacts"
            )
    elif binding["status"] == "active" and any(value is None for value in execution_artifacts):
        raise RollbackExecutionBindingError("rollback execution binding artifacts are incomplete")
    elif any(value is None for value in execution_artifacts) and execution_artifacts != (None, None, None):
        raise RollbackExecutionBindingError("rollback execution binding artifacts are incomplete")
    elif execution_artifacts != (None, None, None):
        _absolute_path(binding.get("writer_repository_root"), "writer_repository_root")
    _validated_journal_identity(binding.get("journal_root_identity"))
    _validated_lease_identity(binding.get("scheduler_lease_identity"))
    if require_artifacts:
        if binding["status"] not in {"active", "rolling_forward"}:
            raise RollbackExecutionBindingError(
                "rollback execution binding status has no executable artifacts"
            )
        try:
            runtime = Path(validated_target_python_runtime(binding["target_python_runtime"], required=True) or "")
            source = Path(
                validated_target_python_source_root(binding["target_python_source_root"], required=True) or ""
            )
        except ValueError as error:
            raise RollbackExecutionBindingError("rollback execution binding artifact is unavailable") from error
        artifact_root = rollback_execution_artifact_root(
            workspace,
            binding["preparation_receipt_id"],
            binding["target_writer_generation"],
        )
        if runtime != artifact_root / "runtime" / "bin" / "python" or source != artifact_root / "source":
            raise RollbackExecutionBindingError(
                "rollback execution binding artifacts are outside the retained generation"
            )
        retention_parent = artifact_root.parent
        _require_owned_private_directory(retention_parent)
        _require_owned_immutable(artifact_root, regular=False)
        _require_owned_immutable(runtime, regular=True)
        _require_owned_immutable(source, regular=False)
        _inspect_rollback_python_source_tree(source, seal=False)
    return binding


def _absolute_path(value: Any, field: str) -> Path:
    if not isinstance(value, str) or not value or "\x00" in value or len(value) > 4096:
        raise RollbackExecutionBindingError(f"rollback execution binding {field} is invalid")
    path = Path(value).expanduser()
    if not path.is_absolute():
        raise RollbackExecutionBindingError(f"rollback execution binding {field} must be absolute")
    return path.resolve()


def _hex_identity(value: Any, lengths: int | tuple[int, ...]) -> bool:
    allowed = (lengths,) if isinstance(lengths, int) else lengths
    return isinstance(value, str) and len(value) in allowed and re.fullmatch(r"[0-9a-f]+", value) is not None


def _validated_journal_identity(value: Any) -> None:
    if (
        not isinstance(value, Mapping)
        or set(value) != {"path_digest", "device", "inode"}
        or not _hex_identity(value.get("path_digest"), 64)
        or type(value.get("device")) is not int
        or type(value.get("inode")) is not int
    ):
        raise RollbackExecutionBindingError("rollback execution binding journal identity is invalid")


def _validated_lease_identity(value: Any) -> None:
    if (
        not isinstance(value, Mapping)
        or set(value) != {"backend", "lock_path_digest", "workspace_root_digest"}
        or value.get("backend") != "file"
        or not _hex_identity(value.get("lock_path_digest"), 64)
        or not _hex_identity(value.get("workspace_root_digest"), 64)
    ):
        raise RollbackExecutionBindingError("rollback execution binding lease identity is invalid")


def _require_owned_immutable(path: Path, *, regular: bool) -> None:
    try:
        metadata = path.stat(follow_symlinks=False)
    except OSError as error:
        raise RollbackExecutionBindingError("rollback execution binding artifact is unavailable") from error
    expected_type = stat.S_ISREG(metadata.st_mode) if regular else stat.S_ISDIR(metadata.st_mode)
    if (
        not expected_type
        or metadata.st_uid != os.geteuid()
        or stat.S_IMODE(metadata.st_mode) & 0o222
    ):
        raise RollbackExecutionBindingError("rollback execution binding artifact has unsafe ownership or mode")


def _require_owned_private_directory(path: Path) -> None:
    try:
        metadata = path.stat(follow_symlinks=False)
    except OSError as error:
        raise RollbackExecutionBindingError("rollback execution retention root is unavailable") from error
    if (
        not stat.S_ISDIR(metadata.st_mode)
        or metadata.st_uid != os.geteuid()
        or stat.S_IMODE(metadata.st_mode) & 0o077
    ):
        raise RollbackExecutionBindingError(
            "rollback execution retention root has unsafe ownership or mode"
        )


def _inspect_rollback_python_source_tree(source_root: Path, *, seal: bool) -> None:
    try:
        root_fd = open_directory_no_follow(source_root)
        count = [0]
        try:
            _inspect_rollback_python_source_directory(root_fd, seal=seal, count=count)
            root_metadata = os.fstat(root_fd)
            _require_source_entry(root_metadata, directory=True, seal=seal, entry_fd=root_fd)
        finally:
            os.close(root_fd)
    except RollbackExecutionBindingError:
        raise
    except (FileNotFoundError, OSError, SafeFilesystemError) as error:
        raise RollbackExecutionBindingError("rollback Python source tree is unsafe") from error


def _inspect_rollback_python_source_directory(
    directory_fd: int,
    *,
    seal: bool,
    count: list[int],
) -> None:
    with os.scandir(directory_fd) as entries:
        names = [entry.name for entry in entries]
    for name in names:
        count[0] += 1
        if count[0] > MAX_ROLLBACK_SOURCE_TREE_ENTRIES:
            raise RollbackExecutionBindingError("rollback Python source tree exceeds its entry limit")
        metadata = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
        if stat.S_ISDIR(metadata.st_mode):
            child_fd = os.open(
                name,
                os.O_RDONLY
                | os.O_DIRECTORY
                | getattr(os, "O_NOFOLLOW", 0)
                | getattr(os, "O_CLOEXEC", 0),
                dir_fd=directory_fd,
            )
            try:
                opened = os.fstat(child_fd)
                if (opened.st_dev, opened.st_ino) != (metadata.st_dev, metadata.st_ino):
                    raise RollbackExecutionBindingError("rollback Python source directory changed")
                _inspect_rollback_python_source_directory(child_fd, seal=seal, count=count)
                _require_source_entry(opened, directory=True, seal=seal, entry_fd=child_fd)
            finally:
                os.close(child_fd)
            continue
        if stat.S_ISREG(metadata.st_mode):
            file_fd = os.open(
                name,
                os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0),
                dir_fd=directory_fd,
            )
            try:
                opened = os.fstat(file_fd)
                if (opened.st_dev, opened.st_ino) != (metadata.st_dev, metadata.st_ino):
                    raise RollbackExecutionBindingError("rollback Python source file changed")
                _require_source_entry(opened, directory=False, seal=seal, entry_fd=file_fd)
            finally:
                os.close(file_fd)
            continue
        raise RollbackExecutionBindingError("rollback Python source tree contains a non-regular entry")


def _require_source_entry(
    metadata: os.stat_result,
    *,
    directory: bool,
    seal: bool,
    entry_fd: int,
) -> None:
    expected_type = stat.S_ISDIR(metadata.st_mode) if directory else stat.S_ISREG(metadata.st_mode)
    if not expected_type or metadata.st_uid != os.geteuid():
        raise RollbackExecutionBindingError("rollback Python source tree has unsafe ownership or type")
    mode = stat.S_IMODE(metadata.st_mode)
    if seal:
        os.fchmod(entry_fd, mode & ~0o222)
    elif mode & 0o222:
        raise RollbackExecutionBindingError("rollback Python source tree is writable")


__all__ = (
    "ROLLBACK_EXECUTION_BINDING_NAME",
    "ROLLBACK_EXECUTION_BINDING_ARCHIVE_DIRECTORY",
    "ROLLBACK_EXECUTION_ARTIFACTS_DIRECTORY",
    "ROLLBACK_EXECUTION_BINDING_SCHEMA_VERSION",
    "RollbackExecutionBindingError",
    "archive_completed_rollback_execution_binding",
    "binding_id_for",
    "read_rollback_execution_binding",
    "rollback_execution_artifact_root",
    "rollback_execution_binding_path",
    "rollback_execution_binding_archive_path",
    "seal_rollback_python_source_tree",
    "validate_rollback_execution_binding",
    "write_rollback_execution_binding",
)
