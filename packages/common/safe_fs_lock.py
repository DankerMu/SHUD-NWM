"""The shared lock-file opener of the safe-filesystem layer (``safe_fs``), in its own module."""

from __future__ import annotations

import os

_NOFOLLOW = getattr(os, "O_NOFOLLOW", 0)
_CLOEXEC = getattr(os, "O_CLOEXEC", 0)
_CREATE_FLAGS = os.O_RDWR | os.O_CREAT | os.O_EXCL | _NOFOLLOW | _CLOEXEC
_OPEN_FLAGS = os.O_RDWR | _NOFOLLOW | _CLOEXEC
_MAX_ATTEMPTS = 3


def open_lock_file_no_follow(name: str, *, dir_fd: int, mode: int) -> int:
    """Open (creating if needed) the lock file ``name`` under ``dir_fd``, never following a symlink.

    A plain ``O_CREAT`` open that races a concurrent first creator can fail with
    ``ENOENT`` on macOS/APFS instead of opening the winner's file (#2540), across
    threads and across processes alike.  An exclusive create never does that: the
    loser gets a clean ``EEXIST`` and then opens the existing entry without
    ``O_CREAT``.  When that entry is unlinked between the two opens, the whole
    sequence starts over, at most ``_MAX_ATTEMPTS`` times, and the last
    ``FileNotFoundError`` is re-raised.

    A symlink at ``name`` fails the create with ``EEXIST`` and the plain open with
    ``ELOOP``; a directory fails the plain open with ``EISDIR``.  Every such
    ``OSError`` propagates unchanged -- deliberately NOT wrapped in
    ``SafeFilesystemError`` -- because each caller maps errnos itself and keeps
    its own regular-file / inode-identity checks on the returned descriptor.
    """

    attempt = 0
    while True:
        attempt += 1
        try:
            return os.open(name, _CREATE_FLAGS, mode, dir_fd=dir_fd)
        except FileExistsError:
            pass
        try:
            return os.open(name, _OPEN_FLAGS, dir_fd=dir_fd)
        except FileNotFoundError:
            if attempt >= _MAX_ATTEMPTS:
                raise


__all__ = ["open_lock_file_no_follow"]
