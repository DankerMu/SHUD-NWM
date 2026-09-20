"""Refresh configuration, workspace budget and environment resolution.

Split out of ``scripts/scheduler_file_provider_refresh.py`` by #1099; the
historical module remains the executable entrypoint and attribute facade.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import stat
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Callable

from packages.common.libpq_env import LIBPQ_CONNECTION_ENV_KEYS
from packages.common.provider_atomic import ProviderPreimage
from packages.common.safe_fs import (
    SafeFilesystemError,
    ensure_directory_no_follow,
    read_bytes_limited_no_follow,
    verify_directory_no_follow,
)
from scripts.publish_scheduler_file_registry import CALIBRATION_OVERRIDE_PATH_ENV_NAME
from scripts.scheduler_refresh.constants import (
    MAX_WORKSPACE_BYTES,
    MAX_WORKSPACE_DEPTH,
    MAX_WORKSPACE_ENTRIES,
    RefreshError,
)
from workers.model_registry.basins_calibration_overrides import DEFAULT_CALIBRATION_OVERRIDES_PATH


class _WorkspaceBudget:
    """Streaming, pre-side-effect accounting for one private run workspace."""

    def __init__(self, root: Path, *, max_bytes: int, max_entries: int, max_depth: int) -> None:
        self.root = root.expanduser().absolute()
        self.max_bytes = max_bytes
        self.max_entries = max_entries
        self.max_depth = max_depth
        self.total_bytes = 0
        self.total_entries = 0
        self._file_sizes: dict[Path, int] = {}
        self.rescan()

    def rescan(self) -> None:
        self.total_bytes = 0
        self.total_entries = 0
        self._file_sizes = {}
        try:
            metadata = os.lstat(self.root)
        except OSError as error:
            raise RefreshError("workspace_limit_exceeded") from error
        if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISDIR(metadata.st_mode):
            raise RefreshError("workspace_limit_exceeded")
        self._scan_directory(self.root, depth=0)

    def _scan_directory(self, directory: Path, *, depth: int) -> None:
        if depth > self.max_depth:
            raise RefreshError("workspace_limit_exceeded")
        try:
            with os.scandir(directory) as entries:
                for entry in entries:
                    metadata = entry.stat(follow_symlinks=False)
                    if stat.S_ISLNK(metadata.st_mode):
                        raise RefreshError("workspace_limit_exceeded")
                    self.total_entries += 1
                    if self.total_entries > self.max_entries:
                        raise RefreshError("workspace_limit_exceeded")
                    path = Path(entry.path).absolute()
                    if stat.S_ISDIR(metadata.st_mode):
                        self._scan_directory(path, depth=depth + 1)
                    elif stat.S_ISREG(metadata.st_mode):
                        self.total_bytes += metadata.st_size
                        if self.total_bytes > self.max_bytes:
                            raise RefreshError("workspace_limit_exceeded")
                    else:
                        raise RefreshError("workspace_limit_exceeded")
        except OSError as error:
            raise RefreshError("workspace_limit_exceeded") from error

    def _relative(self, path: Path) -> Path:
        candidate = path.expanduser().absolute()
        try:
            relative = candidate.relative_to(self.root)
        except ValueError as error:
            raise RefreshError("workspace_limit_exceeded") from error
        if ".." in relative.parts:
            raise RefreshError("workspace_limit_exceeded")
        return relative

    def ensure_directory(self, path: Path) -> None:
        relative = self._relative(path)
        current = self.root
        for depth, part in enumerate(relative.parts, start=1):
            current = current / part
            try:
                metadata = os.lstat(current)
            except FileNotFoundError:
                self._reserve(entries=1, byte_delta=0, parent_depth=depth)
                try:
                    os.mkdir(current, 0o700)
                except OSError as error:
                    self.rescan()
                    raise RefreshError("workspace_limit_exceeded") from error
            else:
                if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISDIR(metadata.st_mode):
                    raise RefreshError("workspace_limit_exceeded")

    def write_json(self, path: Path, payload: Mapping[str, Any]) -> None:
        content = json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True).encode("utf-8") + b"\n"
        self.write_bytes(path, content)

    def write_bytes(self, path: Path, content: bytes) -> None:
        target = path.expanduser().absolute()
        self.ensure_directory(target.parent)
        self._reserve_file(target, len(content))
        try:
            target.write_bytes(content)
        except OSError as error:
            self.rescan()
            raise RefreshError("workspace_limit_exceeded") from error
        self.verify_external_write(target)

    def reserve_external_write(self, path: Path, size: int) -> None:
        target = path.expanduser().absolute()
        self.ensure_directory(target.parent)
        self._reserve_file(target, size)

    def finalize_external_write(self, path: Path, size: int) -> None:
        target = path.expanduser().absolute()
        reserved = self._file_sizes.get(target)
        if reserved is None:
            self.reserve_external_write(target, size)
            return
        if size < 0 or size > reserved:
            raise RefreshError("workspace_limit_exceeded")
        self._reserve(entries=0, byte_delta=size - reserved, parent_depth=0)
        self._file_sizes[target] = size

    def verify_external_write(self, path: Path) -> None:
        target = path.expanduser().absolute()
        try:
            metadata = os.lstat(target)
        except OSError as error:
            self.rescan()
            raise RefreshError("workspace_limit_exceeded") from error
        expected = self._file_sizes.get(target)
        if (
            expected is None
            or stat.S_ISLNK(metadata.st_mode)
            or not stat.S_ISREG(metadata.st_mode)
            or metadata.st_size != expected
        ):
            self.rescan()
            raise RefreshError("workspace_limit_exceeded")

    def _reserve_file(self, target: Path, size: int) -> None:
        if size < 0:
            raise RefreshError("workspace_limit_exceeded")
        relative = self._relative(target)
        try:
            metadata = os.lstat(target)
        except FileNotFoundError:
            old_size = 0
            entry_delta = 1
        else:
            if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISREG(metadata.st_mode):
                raise RefreshError("workspace_limit_exceeded")
            old_size = self._file_sizes.get(target, metadata.st_size)
            entry_delta = 0
        self._reserve(
            entries=entry_delta,
            byte_delta=size - old_size,
            parent_depth=max(len(relative.parts) - 1, 0),
        )
        self._file_sizes[target] = size

    def _reserve(self, *, entries: int, byte_delta: int, parent_depth: int) -> None:
        next_entries = self.total_entries + entries
        next_bytes = self.total_bytes + byte_delta
        if (
            parent_depth > self.max_depth
            or next_entries > self.max_entries
            or next_bytes > self.max_bytes
            or next_entries < 0
            or next_bytes < 0
        ):
            raise RefreshError("workspace_limit_exceeded")
        self.total_entries = next_entries
        self.total_bytes = next_bytes

    def copy_tree(self, source: Path, target: Path) -> None:
        source_metadata = os.lstat(source)
        if stat.S_ISLNK(source_metadata.st_mode) or not stat.S_ISDIR(source_metadata.st_mode):
            raise RefreshError("workspace_limit_exceeded")
        if target.exists() or target.is_symlink():
            raise RefreshError("workspace_limit_exceeded")
        self.ensure_directory(target)
        self._copy_directory_contents(source, target)

    def copy_file(self, source: Path, target: Path) -> None:
        metadata = os.lstat(source)
        if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISREG(metadata.st_mode):
            raise RefreshError("workspace_limit_exceeded")
        if target.exists() or target.is_symlink():
            raise RefreshError("workspace_limit_exceeded")
        self.ensure_directory(target.parent)
        self._copy_regular_file(source, target, metadata)

    def _copy_directory_contents(self, source: Path, target: Path) -> None:
        try:
            with os.scandir(source) as entries:
                for entry in entries:
                    source_path = Path(entry.path)
                    target_path = target / entry.name
                    metadata = entry.stat(follow_symlinks=False)
                    if stat.S_ISLNK(metadata.st_mode):
                        raise RefreshError("workspace_limit_exceeded")
                    if stat.S_ISDIR(metadata.st_mode):
                        self.ensure_directory(target_path)
                        self._copy_directory_contents(source_path, target_path)
                    elif stat.S_ISREG(metadata.st_mode):
                        self._copy_regular_file(source_path, target_path, metadata)
                    else:
                        raise RefreshError("workspace_limit_exceeded")
        except OSError as error:
            self.rescan()
            raise RefreshError("workspace_limit_exceeded") from error

    def _copy_regular_file(self, source: Path, target: Path, metadata: os.stat_result) -> None:
        self._reserve_file(target, metadata.st_size)
        try:
            with source.open("rb") as source_handle:
                opened = os.fstat(source_handle.fileno())
                if (opened.st_dev, opened.st_ino, opened.st_size) != (
                    metadata.st_dev,
                    metadata.st_ino,
                    metadata.st_size,
                ):
                    raise OSError("workspace source changed before copy")
                with target.open("xb") as target_handle:
                    remaining = metadata.st_size
                    while remaining:
                        chunk = source_handle.read(min(1024 * 1024, remaining))
                        if not chunk:
                            raise OSError("workspace source shortened during copy")
                        target_handle.write(chunk)
                        remaining -= len(chunk)
                    if source_handle.read(1):
                        raise OSError("workspace source grew during copy")
                shutil.copystat(source, target, follow_symlinks=False)
        except OSError as error:
            try:
                target.unlink()
            except OSError:
                pass
            self.rescan()
            raise RefreshError("workspace_limit_exceeded") from error
        self.verify_external_write(target)

@dataclass(frozen=True)
class RefreshConfig:
    basins_root: Path
    registry_uri: str
    readiness_uri: str
    state_uri: str
    object_store_root: Path
    provider_store_root: Path
    object_store_prefix: str
    workspace_root: Path
    receipt_root: Path
    emergency_root: Path
    refresh_lock: Path
    worker_registry_uri: str | None = None
    # #1832: the checked-in calibration-override declaration.  Wired explicitly
    # rather than left to the publisher's default so this lane's use of it is
    # visible here: if THIS lane republished a declared basin from the source
    # value it would re-derive the ORIGINAL `model_id` and silently revert the
    # registry to an identity whose per-model forcing and warm state have since
    # been rebuilt under the overridden one.
    # `None` is the documented escape hatch the publisher already honours (a
    # rehearsal / fixture run that must load no declaration at all); production
    # never sets it, which is the point of the default.
    calibration_overrides_path: Path | None = DEFAULT_CALIBRATION_OVERRIDES_PATH

    @classmethod
    def from_env(cls) -> RefreshConfig:
        if any(os.getenv(name) not in (None, "") for name in LIBPQ_CONNECTION_ENV_KEYS):
            raise RefreshError("configuration_invalid")
        return cls(
            basins_root=_absolute_env_path("NHMS_BASINS_ROOT"),
            registry_uri=_required_env("NHMS_SCHEDULER_REGISTRY_MANIFEST"),
            readiness_uri=_required_env("NHMS_SCHEDULER_CANONICAL_READINESS_INDEX"),
            state_uri=_required_env("NHMS_SCHEDULER_STATE_INDEX"),
            object_store_root=_absolute_env_path("OBJECT_STORE_ROOT"),
            provider_store_root=_absolute_env_path("NHMS_SCHEDULER_PROVIDER_STORE_ROOT"),
            object_store_prefix=_required_env("OBJECT_STORE_PREFIX"),
            workspace_root=_absolute_env_path("NHMS_SCHEDULER_PROVIDER_REFRESH_WORK_ROOT"),
            receipt_root=_absolute_env_path("NHMS_SCHEDULER_PROVIDER_REFRESH_RECEIPT_ROOT"),
            emergency_root=_absolute_env_path("NHMS_SCHEDULER_PROVIDER_REFRESH_EMERGENCY_ROOT"),
            refresh_lock=_absolute_env_path("NHMS_SCHEDULER_PROVIDER_REFRESH_LOCK"),
            worker_registry_uri=_required_env("NHMS_SLURM_SCHEDULER_REGISTRY_MANIFEST"),
            calibration_overrides_path=_optional_absolute_env_path(
                CALIBRATION_OVERRIDE_PATH_ENV_NAME,
                DEFAULT_CALIBRATION_OVERRIDES_PATH,
            ),
        )

@dataclass
class EmergencySlot:
    path: Path
    parent_fd: int
    file_fd: int
    device: int
    inode: int

@dataclass(frozen=True)
class _ProviderRollbackRecord:
    name: str
    path: Path
    containment_root: Path
    max_bytes: int
    previous: bytes | None
    committed: ProviderPreimage

def _provider_failure_reason(error: Exception) -> str | None:
    reason = getattr(error, "reason", None) or getattr(error, "error_code", None)
    details = getattr(error, "details", {})
    if isinstance(details, Mapping) and details.get("provider_reason"):
        reason = details["provider_reason"]
    return str(reason) if reason not in (None, "") else None

def _preflight_config(config: RefreshConfig) -> None:
    for directory in (config.basins_root, config.object_store_root, config.provider_store_root):
        verify_directory_no_follow(directory)
    _ensure_private_directory(config.refresh_lock.parent)
    lock_parent = os.lstat(config.refresh_lock.parent)
    if lock_parent.st_uid != os.geteuid() or stat.S_IMODE(lock_parent.st_mode) & 0o077:
        raise RefreshError("configuration_invalid")
    for directory in (
        config.workspace_root,
        config.receipt_root,
        config.emergency_root,
    ):
        _ensure_private_directory(directory)
        metadata = os.lstat(directory)
        if metadata.st_uid != os.geteuid() or stat.S_IMODE(metadata.st_mode) & 0o077:
            raise RefreshError("configuration_invalid")
    for uri in (config.registry_uri, config.readiness_uri, config.state_uri):
        provider_path = Path(uri).expanduser()
        if not provider_path.is_absolute():
            raise RefreshError("configuration_invalid")
        try:
            provider_path.relative_to(config.provider_store_root)
        except ValueError as error:
            raise RefreshError("configuration_invalid") from error
    if config.worker_registry_uri is not None:
        worker_registry_path = Path(config.worker_registry_uri).expanduser()
        if not worker_registry_path.is_absolute() or worker_registry_path == Path(config.registry_uri):
            raise RefreshError("configuration_invalid")
        try:
            worker_registry_path.relative_to(config.object_store_root)
        except ValueError as error:
            raise RefreshError("configuration_invalid") from error
    if not config.object_store_prefix.startswith("s3://"):
        raise RefreshError("configuration_invalid")

def _ensure_private_directory(path: Path) -> None:
    try:
        metadata = os.lstat(path)
    except FileNotFoundError:
        ensure_directory_no_follow(path)
        os.chmod(path, 0o700, follow_symlinks=False)
    else:
        if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISDIR(metadata.st_mode):
            raise RefreshError("configuration_invalid")
    verify_directory_no_follow(path)

def _optional_absolute_env_path(name: str, default: Path) -> Path:
    value = os.getenv(name, "").strip()
    if not value:
        return default
    path = Path(value).expanduser()
    if not path.is_absolute():
        raise RefreshError("configuration_invalid")
    return path

def _absolute_env_path(name: str) -> Path:
    value = Path(_required_env(name)).expanduser()
    if not value.is_absolute():
        raise RefreshError("configuration_invalid")
    return value

def _required_env(name: str) -> str:
    value = os.getenv(name, "").strip()
    if not value:
        raise RefreshError("configuration_invalid")
    return value

def _env_flag(name: str) -> bool:
    return os.getenv(name, "").strip().lower() in {"1", "true", "yes", "on"}

def _enforce_workspace_bounds(root: Path) -> None:
    _WorkspaceBudget(
        root,
        max_bytes=MAX_WORKSPACE_BYTES,
        max_entries=MAX_WORKSPACE_ENTRIES,
        max_depth=MAX_WORKSPACE_DEPTH,
    )

def _iso_utc(value: datetime) -> str:
    if value.tzinfo is None:
        raise RefreshError("provider_invalid")
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")

def _cleanup_run_workspace(path: Path, identity: os.stat_result, *, containment_root: Path) -> None:
    path.relative_to(containment_root)
    current = os.lstat(path)
    if stat.S_ISLNK(current.st_mode) or (current.st_dev, current.st_ino) != (identity.st_dev, identity.st_ino):
        raise RefreshError("workspace_limit_exceeded")
    _enforce_workspace_bounds(path)
    shutil.rmtree(path)

def _apply_environment_file(path: Path) -> Callable[[], None]:
    try:
        content = read_bytes_limited_no_follow(path, max_bytes=64 * 1024, containment_root=path.parent)
        text_content = content.decode("utf-8")
    except (OSError, SafeFilesystemError, UnicodeDecodeError) as error:
        raise RefreshError("configuration_invalid") from error
    parsed: dict[str, str] = {}
    for raw_line in text_content.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if "=" not in line:
            raise RefreshError("configuration_invalid")
        name, value = line.split("=", 1)
        if (
            re.fullmatch(r"[A-Z][A-Z0-9_]*", name) is None
            or "\x00" in value
            or name in parsed
        ):
            raise RefreshError("configuration_invalid")
        parsed[name] = value
    previous = {name: os.environ.get(name) for name in parsed}
    os.environ.update(parsed)

    def restore() -> None:
        for name, value in previous.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value

    return restore
