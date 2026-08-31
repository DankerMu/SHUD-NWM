"""Process mutex for node-27 timeseries lifecycle mutation (#1893).

One fixed flock serializes recurring compression, cold residency, retention,
and manual decompression/replay. Autopipe stays outside this mutex: it cannot
write an eligible compressed group and is fenced by transactional revalidation.
"""

from __future__ import annotations

import fcntl
import os
import stat
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from pathlib import Path

LIFECYCLE_LOCK_PATH = Path("/tmp/nhms-node27-timeseries-lifecycle.lock")
_LOCK_MODE = 0o600
_ENV_KEY = "NODE27_TIMESERIES_LIFECYCLE_LOCK_PATH"


class LifecycleLockError(RuntimeError):
    """The lifecycle mutex is unsafe or cannot be opened."""


class LifecycleLockContended(RuntimeError):
    """Another lifecycle lane already holds the mutex."""


def refuse_lifecycle_lock_env_override(env: Mapping[str, str] | None = None) -> None:
    """Env may repeat the fixed path; any other value is a config refusal."""

    mapping = {} if env is None else env
    raw = mapping.get(_ENV_KEY)
    if raw is None or raw == "":
        return
    if str(raw) != "/tmp/nhms-node27-timeseries-lifecycle.lock":
        raise LifecycleLockError(
            "NODE27_TIMESERIES_LIFECYCLE_LOCK_PATH cannot override "
            "/tmp/nhms-node27-timeseries-lifecycle.lock"
        )


def _open_flags() -> int:
    return os.O_RDWR | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0)


def _require_lock_identity(path: Path, fd: int) -> None:
    try:
        info = os.fstat(fd)
        named = os.lstat(path)
    except OSError as error:
        raise LifecycleLockError(f"lifecycle lock identity is unavailable: {error}") from error
    if not stat.S_ISREG(info.st_mode) or not stat.S_ISREG(named.st_mode):
        raise LifecycleLockError("lifecycle lock must be a regular file")
    if stat.S_ISLNK(named.st_mode):
        raise LifecycleLockError("lifecycle lock must not be a symlink")
    if stat.S_IMODE(info.st_mode) != _LOCK_MODE or stat.S_IMODE(named.st_mode) != _LOCK_MODE:
        raise LifecycleLockError("lifecycle lock must have mode 0600")
    if info.st_uid != os.geteuid() or named.st_uid != os.geteuid():
        raise LifecycleLockError("lifecycle lock must be owned by the effective user")
    if info.st_nlink != 1 or named.st_nlink != 1:
        raise LifecycleLockError("lifecycle lock must have exactly one hard link")
    if (info.st_dev, info.st_ino) != (named.st_dev, named.st_ino):
        raise LifecycleLockError("lifecycle lock path/fd identity drifted")


def acquire_timeseries_lifecycle_lock(path: Path | None = None) -> int | None:
    """Open the fixed mutex no-follow and take a nonblocking exclusive flock.

    Returns a held fd, or ``None`` on contention. Never unlinks the lock file.
    """

    if path is None:
        path = LIFECYCLE_LOCK_PATH
    if not path.is_absolute():
        raise LifecycleLockError("lifecycle lock path must be absolute")
    flags = _open_flags()
    fd: int | None = None
    try:
        try:
            named = os.lstat(path)
        except FileNotFoundError:
            named = None
        if named is not None and stat.S_ISLNK(named.st_mode):
            raise LifecycleLockError("lifecycle lock must not be a symlink")
        try:
            fd = os.open(path, flags | os.O_CREAT | os.O_EXCL, _LOCK_MODE)
        except FileExistsError:
            fd = os.open(path, flags)
        _require_lock_identity(path, fd)
        os.fchmod(fd, _LOCK_MODE)
        _require_lock_identity(path, fd)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            os.close(fd)
            return None
        _require_lock_identity(path, fd)
        held = fd
        fd = None
        return held
    except LifecycleLockError:
        if fd is not None:
            os.close(fd)
        raise
    except OSError as error:
        if fd is not None:
            os.close(fd)
        errno = getattr(error, "errno", None)
        if errno in {getattr(os, "ELOOP", 62), 62}:
            raise LifecycleLockError("lifecycle lock must not be a symlink") from error
        raise LifecycleLockError(f"cannot acquire lifecycle lock: {error}") from error


def release_timeseries_lifecycle_lock(fd: int) -> None:
    """Drop the flock and close the fd. The lock file is never unlinked."""

    try:
        fcntl.flock(fd, fcntl.LOCK_UN)
    finally:
        os.close(fd)


@contextmanager
def timeseries_lifecycle_lock(path: Path | None = None) -> Iterator[int]:
    """Hold the mutex or raise ``LifecycleLockContended`` without mutating data."""

    fd = acquire_timeseries_lifecycle_lock(path)
    if fd is None:
        raise LifecycleLockContended("timeseries lifecycle lock is held")
    try:
        yield fd
    finally:
        release_timeseries_lifecycle_lock(fd)
