"""The retention deleter's copyback-mutex lane (#2238, extracted by #2259).

Three symbols, moved verbatim out of :mod:`services.orchestrator.retention`
when that module crossed the 1,000-line guard: the pass-level lock budget, the
per-entry removal that records failures instead of raising, and the one-tree
hold that brackets a removal with the copyback batch mutex. They moved
**together** because every call site of ``acquire_copyback_batch_lock`` /
``release_copyback_batch_lock`` / ``remove_tree_allow_symlinks`` lives inside
them, and a monkeypatch seam is only honest when the patched module is the one
that calls: ``retention.py`` therefore does not re-export those three names, so
a patch aimed at the old target raises ``AttributeError`` instead of passing
vacuously (design D1 of ``split-oversized-surfaces-batch``).

``_resolve_copyback_lock_root`` deliberately stayed behind: its only caller is
``run_retention`` and it reaches back into ``retention``'s root-admission
helpers, so moving it would close a top-level import cycle. The dependency runs
one way only -- ``retention`` imports this module at runtime, this module needs
``retention`` for the ``RetentionResult`` annotation alone and takes it under
``TYPE_CHECKING``.
"""

from __future__ import annotations

import shutil
import time
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

from packages.common.copyback_guard import (
    CopybackLockBudgetExhausted,
    CopybackLockError,
    acquire_copyback_batch_lock,
    copyback_lock_failure_kind,
    release_copyback_batch_lock,
)
from packages.common.safe_fs import SafeFilesystemError, remove_tree_allow_symlinks

if TYPE_CHECKING:
    from services.orchestrator.retention import RetentionResult


@dataclass
class _CopybackLockBudget:
    """The copyback root under the mutex, plus one pass's remaining wait budget.

    One instance per pass, shared by every copyback-root removal and charged by
    each acquisition attempt one of them makes: the budget is a *pass*-level
    bound, so those attempts have to draw on the same counter (design D9). Once
    it is spent the remaining entries are refused before acquiring, so they
    leave the counter untouched rather than driving it further negative.
    ``root`` travels with it because the removal and the acquisition must name
    the same resolved root.
    """

    root: Path
    budget_seconds: float
    remaining_seconds: float


def _delete_entry(
    entry: dict[str, Any],
    result: RetentionResult,
    *,
    containment_root: Path | None = None,
    copyback_lock: _CopybackLockBudget | None = None,
) -> None:
    """Remove one planned entry, recording failure instead of raising.

    ``containment_root`` is set for additional roots (design D6 / issue #1615):
    removal goes through ``remove_tree_allow_symlinks`` on the run's parent, so
    the walk cannot follow a symlink out of the root that is being swept, and a
    descendant symlink inside a selected, aged run workspace is unlinked as a
    link instead of refusing the whole tree. The object-store root keeps the
    historical ``shutil.rmtree``; changing it is out of this change's scope.

    ``copyback_lock`` is set only for entries on the **shared** copyback root
    (#2238): that removal, and only that removal, is taken under the copyback
    batch mutex, so it cannot land inside another process's
    rename-to-backup-then-promote window and destroy that writer's rollback
    material. It is nested inside the ``containment_root`` branch by
    construction, so the primary root's ``shutil.rmtree`` can never be locked.

    ``SafeFilesystemError`` and ``CopybackLockError`` (hence its
    ``CopybackLockTimeout`` subclass) are ``RuntimeError``s, **not**
    ``OSError``s, so both must be named explicitly here: letting either escape
    would collapse the pass receipt to ``{"status": "error"}``
    (scheduler_runtime) and abort the ``cleanup`` CLI mid-sweep (cli.py wraps
    nothing), both violating this module's "failures never abort the pass"
    contract. An unavailable mutex is therefore one ``failed`` entry carrying
    the error text, its class name and its typed ``lock_failure`` shape --
    never a removal, and never an interrupted sweep.
    """
    path = Path(entry["path"])
    try:
        if containment_root is not None:
            if copyback_lock is not None:
                _remove_tree_under_copyback_mutex(
                    path,
                    containment_root=containment_root,
                    copyback_lock=copyback_lock,
                )
            else:
                remove_tree_allow_symlinks(
                    path.parent,
                    path.name,
                    containment_root=containment_root,
                    missing_ok=False,
                )
        else:
            shutil.rmtree(path)
    except CopybackLockError as error:
        result.failed.append(
            {
                **entry,
                "error": str(error),
                "error_type": type(error).__name__,
                "lock_failure": copyback_lock_failure_kind(error),
            }
        )
        return
    except (OSError, SafeFilesystemError) as error:
        result.failed.append({**entry, "error": str(error), "error_type": type(error).__name__})
        return
    result.deleted.append(entry)
    result.freed_bytes += int(entry.get("size_bytes", 0))


def _remove_tree_under_copyback_mutex(
    path: Path,
    *,
    containment_root: Path,
    copyback_lock: _CopybackLockBudget,
) -> None:
    """Hold the copyback batch mutex for exactly one tree removal (design D2/D9).

    Acquire immediately before the removal and release immediately after, so the
    held window is one ``remove_tree_allow_symlinks`` and never the pass's
    planning walk -- which sizes every candidate over NFS and would starve the
    promoting writers whose acquisition budget this mutex is sized for.

    ``acquire``/``release`` rather than the ``copyback_batch_lock`` context
    manager, because the *acquisition* has to be measured separately from the
    hold to be charged against the pass budget, and the context manager exposes
    no seam between the two. The charged span is the whole
    ``acquire_copyback_batch_lock`` call -- the guard's own identity syscalls as
    well as the blocking poll -- but it closes before the removal begins, so
    only the acquisition is charged, never the removal, and a large uncontended
    tree cannot consume the budget. An exhausted budget refuses **before**
    acquiring: the remaining entries on this root must not each add another
    deadline's wait.
    """
    if copyback_lock.remaining_seconds <= 0:
        raise CopybackLockBudgetExhausted(
            f"copyback batch lock wait budget of {copyback_lock.budget_seconds}s "
            f"is exhausted for this retention pass; {path} was not removed"
        )
    started = time.monotonic()
    try:
        fd = acquire_copyback_batch_lock(
            copyback_lock.root,
            timeout_seconds=copyback_lock.remaining_seconds,
        )
    finally:
        copyback_lock.remaining_seconds -= time.monotonic() - started
    try:
        remove_tree_allow_symlinks(
            path.parent,
            path.name,
            containment_root=containment_root,
            missing_ok=False,
        )
    finally:
        release_copyback_batch_lock(fd)
