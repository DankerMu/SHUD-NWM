from __future__ import annotations

import os
import stat
from pathlib import Path
from typing import Any

from packages.common.safe_fs import SafeFilesystemError, open_directory_no_follow

MAX_TARGET_PYTHON_RUNTIME_PATH_LENGTH = 4096


def validated_target_python_runtime(value: Any, *, required: bool = False) -> str | None:
    """Return a canonical executable regular-file runtime path.

    The submission contract deliberately rejects symlinks.  Rollback launchers
    materialize an fd-bound private executable snapshot before constructing this
    field, closing the final-check-to-exec symlink race for both the controller
    and downstream Slurm workers.
    """

    if value in (None, ""):
        if required:
            raise ValueError("target_python_runtime is required")
        return None
    if not isinstance(value, str) or len(value) > MAX_TARGET_PYTHON_RUNTIME_PATH_LENGTH or "\x00" in value:
        raise ValueError("target_python_runtime must be a bounded path string")
    candidate = Path(value).expanduser()
    if not candidate.is_absolute():
        raise ValueError("target_python_runtime must be absolute")
    try:
        configured = candidate.absolute()
        opened = configured.stat(follow_symlinks=False)
    except OSError as error:
        raise ValueError("target_python_runtime must exist") from error
    if not stat.S_ISREG(opened.st_mode):
        raise ValueError("target_python_runtime must be an executable regular file")
    if not os.access(configured, os.X_OK):
        raise ValueError("target_python_runtime must be an executable regular file")
    return str(configured)


def validated_target_python_source_root(value: Any, *, required: bool = False) -> str | None:
    if value in (None, ""):
        if required:
            raise ValueError("target_python_source_root is required")
        return None
    if not isinstance(value, str) or len(value) > MAX_TARGET_PYTHON_RUNTIME_PATH_LENGTH or "\x00" in value:
        raise ValueError("target_python_source_root must be a bounded path string")
    candidate = Path(value).expanduser()
    if not candidate.is_absolute():
        raise ValueError("target_python_source_root must be absolute")
    configured = candidate.absolute()
    try:
        directory_fd = open_directory_no_follow(configured)
    except (FileNotFoundError, OSError, SafeFilesystemError) as error:
        raise ValueError("target_python_source_root must be a no-follow directory") from error
    else:
        os.close(directory_fd)
    return str(configured)


__all__ = (
    "MAX_TARGET_PYTHON_RUNTIME_PATH_LENGTH",
    "validated_target_python_runtime",
    "validated_target_python_source_root",
)
