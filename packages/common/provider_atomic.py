from __future__ import annotations

import fcntl
import hashlib
import os
import stat
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Iterator, Mapping

from packages.common.safe_fs import (
    SafeFilesystemError,
    atomic_write_bytes_no_follow,
    ensure_directory_no_follow,
    open_directory_no_follow,
    read_bytes_limited_no_follow,
    stat_no_follow,
)


class ProviderAtomicError(RuntimeError):
    def __init__(self, reason: str, *, phase: str) -> None:
        super().__init__(reason)
        self.reason = reason
        self.phase = phase


@dataclass(frozen=True)
class ProviderPreimage:
    exists: bool
    sha256: str | None = None
    device: int | None = None
    inode: int | None = None
    mode: int | None = None
    uid: int | None = None
    gid: int | None = None
    size: int | None = None
    mtime_ns: int | None = None

    def to_dict(self) -> dict[str, int | str | bool | None]:
        return asdict(self)

    @classmethod
    def from_value(cls, value: ProviderPreimage | Mapping[str, object]) -> ProviderPreimage:
        if isinstance(value, cls):
            return value
        return cls(
            exists=bool(value.get("exists")),
            sha256=str(value["sha256"]) if value.get("sha256") is not None else None,
            device=int(value["device"]) if value.get("device") is not None else None,
            inode=int(value["inode"]) if value.get("inode") is not None else None,
            mode=int(value["mode"]) if value.get("mode") is not None else None,
            uid=int(value["uid"]) if value.get("uid") is not None else None,
            gid=int(value["gid"]) if value.get("gid") is not None else None,
            size=int(value["size"]) if value.get("size") is not None else None,
            mtime_ns=int(value["mtime_ns"]) if value.get("mtime_ns") is not None else None,
        )


def capture_provider_preimage(
    path: Path,
    *,
    containment_root: Path | None = None,
    max_bytes: int,
) -> ProviderPreimage:
    try:
        metadata = stat_no_follow(path, containment_root=containment_root)
    except FileNotFoundError:
        return ProviderPreimage(exists=False)
    if not stat.S_ISREG(metadata.st_mode):
        raise ProviderAtomicError("provider_destination_not_regular", phase="precommit")
    content = read_bytes_limited_no_follow(path, max_bytes=max_bytes, containment_root=containment_root)
    if len(content) > max_bytes:
        raise ProviderAtomicError("provider_destination_size_limit_exceeded", phase="precommit")
    return ProviderPreimage(
        exists=True,
        sha256=hashlib.sha256(content).hexdigest(),
        device=metadata.st_dev,
        inode=metadata.st_ino,
        mode=stat.S_IMODE(metadata.st_mode),
        uid=metadata.st_uid,
        gid=metadata.st_gid,
        size=metadata.st_size,
        mtime_ns=metadata.st_mtime_ns,
    )


def provider_lock_path(path: Path) -> Path:
    return path.with_name(f".{path.name}.lock")


@contextmanager
def provider_destination_lock(
    path: Path,
    *,
    containment_root: Path | None = None,
    blocking: bool = True,
) -> Iterator[None]:
    lock_path = provider_lock_path(path)
    lock_fd: int | None = None
    parent_fd: int | None = None
    flags = os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0)
    try:
        ensure_directory_no_follow(lock_path.parent, containment_root=containment_root)
        parent_fd = open_directory_no_follow(lock_path.parent, containment_root=containment_root)
        lock_fd = os.open(lock_path.name, flags, 0o600, dir_fd=parent_fd)
        os.fchmod(lock_fd, 0o600)
        opened = os.fstat(lock_fd)
        if not stat.S_ISREG(opened.st_mode):
            raise ProviderAtomicError("provider_lock_not_regular", phase="precommit")
        current = os.stat(lock_path.name, dir_fd=parent_fd, follow_symlinks=False)
        if (opened.st_dev, opened.st_ino) != (current.st_dev, current.st_ino):
            raise ProviderAtomicError("provider_lock_changed", phase="precommit")
        lock_flags = fcntl.LOCK_EX | (0 if blocking else fcntl.LOCK_NB)
        try:
            fcntl.flock(lock_fd, lock_flags)
        except BlockingIOError as error:
            raise ProviderAtomicError("provider_already_running", phase="precommit") from error
        yield
    except ProviderAtomicError:
        raise
    except (OSError, SafeFilesystemError) as error:
        raise ProviderAtomicError("provider_lock_unavailable", phase="precommit") from error
    finally:
        if lock_fd is not None:
            try:
                fcntl.flock(lock_fd, fcntl.LOCK_UN)
            finally:
                os.close(lock_fd)
        if parent_fd is not None:
            os.close(parent_fd)


def atomic_replace_provider_bytes(
    path: Path,
    content: bytes,
    *,
    containment_root: Path | None = None,
    max_bytes: int,
    expected_preimage: ProviderPreimage | Mapping[str, object] | None = None,
    lock_held: bool = False,
    blocking_lock: bool = False,
) -> ProviderPreimage:
    @contextmanager
    def maybe_lock() -> Iterator[None]:
        if lock_held:
            yield
        else:
            with provider_destination_lock(path, containment_root=containment_root, blocking=blocking_lock):
                yield

    with maybe_lock():
        before = capture_provider_preimage(path, containment_root=containment_root, max_bytes=max_bytes)
        previous_content = (
            read_bytes_limited_no_follow(path, max_bytes=max_bytes, containment_root=containment_root)
            if before.exists
            else None
        )
        if previous_content is not None and hashlib.sha256(previous_content).hexdigest() != before.sha256:
            raise ProviderAtomicError("provider_preimage_changed", phase="precommit")
        if expected_preimage is not None and before != ProviderPreimage.from_value(expected_preimage):
            raise ProviderAtomicError("provider_preimage_changed", phase="precommit")
        try:
            atomic_write_bytes_no_follow(
                path,
                content,
                containment_root=containment_root,
                temp_suffix="provider",
                require_durable_replace=True,
            )
        except SafeFilesystemError as error:
            phase = "replace_uncertain" if error.kind == "indeterminate" else "precommit"
            reason = "provider_replace_uncertain" if phase == "replace_uncertain" else "provider_replace_failed"
            raise ProviderAtomicError(reason, phase=phase) from error
        after = capture_provider_preimage(path, containment_root=containment_root, max_bytes=max_bytes)
        if not after.exists or after.sha256 != hashlib.sha256(content).hexdigest():
            if previous_content is None:
                raise ProviderAtomicError("provider_postread_failed", phase="replace_uncertain")
            try:
                atomic_write_bytes_no_follow(
                    path,
                    previous_content,
                    containment_root=containment_root,
                    temp_suffix="provider-restore",
                    mode=before.mode,
                    require_durable_replace=True,
                )
                restored = capture_provider_preimage(
                    path,
                    containment_root=containment_root,
                    max_bytes=max_bytes,
                )
            except (SafeFilesystemError, ProviderAtomicError) as error:
                raise ProviderAtomicError("provider_postread_failed", phase="replace_uncertain") from error
            if restored.sha256 != before.sha256:
                raise ProviderAtomicError("provider_postread_failed", phase="replace_uncertain")
            raise ProviderAtomicError("provider_restored_previous", phase="postcommit")
        return after
