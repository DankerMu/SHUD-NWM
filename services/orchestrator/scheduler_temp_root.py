"""Pass-scoped creation of the disposable scheduler temp_root (#3061).

The runtime-root preflight (``scheduler_runtime_roots``) admits a missing
``temp_root`` on its ``allow_create`` arm but stays a pure check, because
``ProductionScheduler.from_env`` runs it too. A pass calls
``_scheduler_create_admitted_temp_root`` right after its own runtime-root
preflight, before the ``blocked`` branch, so a creation failure leaves through
the existing preflight-blocked evidence.
"""

from __future__ import annotations

import os
import stat
from pathlib import Path

from packages.common.safe_fs import SafeFilesystemError, open_directory_no_follow
from services.orchestrator import scheduler as _scheduler

_TEMP_ROOT_MODE = 0o700


def _scheduler_create_admitted_temp_root(root_preflight: dict[str, object], temp_root: Path | None) -> None:
    """Create the missing temp_root a ready runtime-root preflight admitted.

    Only a ready verdict whose temp_root check says ``allow_create`` and
    ``exists=False`` is acted on -- that verdict already proved the parent an
    existing, writable, non-symlink directory and the path inside the approved
    roots. The parent is pinned without following symlinks and only the final
    component is created (mode 0700, never the parent). The outcome lands on the
    check as ``created``; a failure flips the verdict to ``blocked`` with a
    ``SCHEDULER_ROOT_TEMP_ROOT_<REASON>`` blocker.
    """

    if root_preflight.get("status") != "ready" or temp_root is None:
        return
    checks = root_preflight.get("checks")
    check = checks.get("temp_root") if isinstance(checks, dict) else None
    if not isinstance(check, dict) or check.get("allow_create") is not True or check.get("exists") is not False:
        return
    created, failure_reason = _create_directory_final_component_no_follow(Path(temp_root), _TEMP_ROOT_MODE)
    check["created"] = created
    if failure_reason is None:
        return
    path = check.get("path")
    blocker = _scheduler._scheduler_root_blocker("temp_root", failure_reason, path if isinstance(path, str) else None)
    blockers = root_preflight.get("blockers")
    root_preflight["blockers"] = [*blockers, blocker] if isinstance(blockers, list) else [blocker]
    root_preflight["status"] = "blocked"


def _create_directory_final_component_no_follow(path: Path, mode: int) -> tuple[bool, str | None]:
    """mkdir ``path``'s final component under its no-follow-pinned parent.

    Returns ``(created, failure_reason)``. A concurrent creator is benign: an
    existing real directory is ``(False, None)``. Reasons mirror the preflight's
    vocabulary for the same geometry: a vanished parent is ``NOT_WRITABLE`` (the
    allow_create arm's verdict for a missing parent), a symlink anywhere on the
    parent walk is ``UNSAFE_PATH``, and a final entry that turned into a symlink
    or a non-directory is ``SYMLINK`` / ``NOT_DIRECTORY``.
    """

    try:
        parent_fd = open_directory_no_follow(path.parent)
    except FileNotFoundError:
        return False, "NOT_WRITABLE"
    except SafeFilesystemError as error:
        return False, _safe_fs_failure_reason(error)
    except OSError as error:
        return False, _scheduler._scheduler_root_os_error_reason(error)
    created = False
    try:
        try:
            os.mkdir(path.name, mode, dir_fd=parent_fd)
            created = True
        except FileExistsError:
            pass
        entry = os.stat(path.name, dir_fd=parent_fd, follow_symlinks=False)
    except FileNotFoundError:
        return created, "NOT_WRITABLE"
    except OSError as error:
        return created, _scheduler._scheduler_root_os_error_reason(error)
    finally:
        os.close(parent_fd)
    if stat.S_ISLNK(entry.st_mode):
        return False, "SYMLINK"
    if not stat.S_ISDIR(entry.st_mode):
        return False, "NOT_DIRECTORY"
    return created, None


def _safe_fs_failure_reason(error: SafeFilesystemError) -> str:
    cause = error.__cause__
    if error.kind == "io" and isinstance(cause, OSError):
        if isinstance(cause, FileNotFoundError):
            return "NOT_WRITABLE"
        return _scheduler._scheduler_root_os_error_reason(cause)
    return "UNSAFE_PATH"
