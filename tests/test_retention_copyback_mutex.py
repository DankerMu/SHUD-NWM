"""Requirement-driven tests for retention's copyback mutex lane (#2238).

Contract, from the ``object-store-copyback-mutual-exclusion`` delta this change
adds: a process that removes a directory tree under the **shared** object-store
copyback root holds that root's batch mutex for the whole of the removal, once
per removed tree; the run-workspace root and the primary object store are not
locked; an unavailable mutex is one recorded ``failed`` entry and never an
aborted pass; a removal that RAISES inside the held mutex still frees it; one
pass-level wait budget bounds a stalled sweep -- charged by every acquisition,
including one that succeeds after waiting; a pass that removes nothing acquires
nothing; and the mutex changes timing, never selection.

Its own file rather than an extension of ``tests/test_retention_extra_roots.py``
(747 lines, the #1405/#1318/#1617 root-admission contract): this is the deleter
side of a cross-process protocol, not another root-admission rule.

Every root here is built under ``tmp_path.resolve()``. ``safe_fs`` walks with
``O_NOFOLLOW`` and macOS's ``tmp_path`` sits under ``/var -> private/var``, so a
symlinked ancestor would make the removal itself refuse (the
``tests/test_copyback_guard.py`` ``_real_root`` idiom). The cases that must
TIME OUT drive the acquisition deadline sub-second through the pass budget, and
the probes that ask "is it held right now?" carry a 0.2 s deadline of their own,
so the suite stays fast; the contended cases that must SUCCEED keep a roomy
budget and wait for a real release instead. ``flock`` is per open file
description, so a holder in this same process contends with the pass exactly as
a separate host would.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
import time
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from packages.common.copyback_guard import (
    COPYBACK_BATCH_LOCK_NAME,
    CopybackLockTimeout,
    acquire_copyback_batch_lock,
    release_copyback_batch_lock,
)
from packages.common.safe_fs import SafeFilesystemError
from services.orchestrator import cli
from services.orchestrator import retention as retention_module
from services.orchestrator.retention import (
    EXTRA_ROOT_NOT_ABSOLUTE_REASON,
    PIPELINE_FRONTIER_EXEMPT_REASON,
    ROOT_OVERLAP_REASON,
    run_retention,
)
from tests.retention_test_helpers import (
    EXTRA_CONFIG,
    NOW,
    _cycle_name,
    _entries_for,
    _pass_scheduler,
    _run_id,
    _seed_cycle,
    _seed_pass_env,
    _seed_run_workspace,
    _write,
)

REPO_ROOT = Path(__file__).resolve().parents[1]

# Enabled, executing, additional-root gate open: the node-22 db-free shape that
# made this change non-latent.
DELETING_CONFIG = replace(EXTRA_CONFIG, dry_run=False)

AGED = NOW - timedelta(days=40)  # past both the 14 d and the 30 d window


def _real_dir(tmp_path: Path, name: str) -> Path:
    """A retention root with no symlinked ancestor."""

    path = tmp_path.resolve() / name
    path.mkdir(parents=True, exist_ok=True)
    return path


def _lock_file(root: Path) -> Path:
    return root / COPYBACK_BATCH_LOCK_NAME


def _plant_lock_file(root: Path, *, mode: int = 0o600) -> Path:
    """Pre-create the fixed-name lock file under ``root``."""

    lock_file = _lock_file(root)
    lock_file.write_bytes(b"")
    os.chmod(lock_file, mode)
    return lock_file


def _forbid_acquisitions(monkeypatch: pytest.MonkeyPatch) -> None:
    """Make any acquisition in this pass a loud failure.

    ``AssertionError`` is neither ``OSError`` nor ``CopybackLockError``, so it
    escapes ``_delete_entry``'s ``except`` tuple instead of being absorbed into
    ``failed[]`` where a test could miss it.
    """

    def forbidden(*args: object, **kwargs: object) -> int:
        raise AssertionError("this pass must not acquire the copyback batch mutex")

    monkeypatch.setattr(retention_module, "acquire_copyback_batch_lock", forbidden)


def _observe_removals(
    monkeypatch: pytest.MonkeyPatch,
    locked_root: Path,
) -> list[tuple[str, bool]]:
    """Record ``(removed path, was ``locked_root``'s mutex held)`` per removal.

    The probe is a real competing acquisition from this process against the same
    root: ``flock`` is per open file description, so it blocks exactly as a
    second host would and times out within 0.2 s when the pass holds the lock.
    This is what distinguishes "acquires around the removal" from "acquires
    after it", which a one-directional wait test cannot see.
    """

    observations: list[tuple[str, bool]] = []
    real_remove = retention_module.remove_tree_allow_symlinks

    def observing_remove(parent: Path, name: str, **kwargs: Any) -> Any:
        try:
            probe = acquire_copyback_batch_lock(locked_root, timeout_seconds=0.2)
        except CopybackLockTimeout:
            held = True
        else:
            held = False
            release_copyback_batch_lock(probe)
        observations.append((str(Path(parent) / name), held))
        return real_remove(parent, name, **kwargs)

    monkeypatch.setattr(retention_module, "remove_tree_allow_symlinks", observing_remove)
    return observations


def _holder_subprocess(root: Path) -> subprocess.Popen[str]:
    """A real second process holding ``root``'s batch mutex (guard-test idiom)."""

    program = (
        "import sys, time\n"
        f"sys.path.insert(0, {str(REPO_ROOT)!r})\n"
        "from packages.common.copyback_guard import acquire_copyback_batch_lock\n"
        f"acquire_copyback_batch_lock({str(root)!r})\n"
        "print('held', flush=True)\n"
        "time.sleep(120)\n"
    )
    holder = subprocess.Popen(
        [sys.executable, "-c", program],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    assert holder.stdout is not None
    assert holder.stdout.readline().strip() == "held"
    return holder


def _errors_for(entries: list[dict[str, Any]], root: Path) -> dict[str, str]:
    return {entry["key"]: entry["error"] for entry in entries if entry["root"] == str(root)}


# ---------------------------------------------------------------------------
# EF-1 -- the copyback lane locks: a removal waits out a competing holder.
# ---------------------------------------------------------------------------
def test_ef1_a_copyback_removal_waits_for_a_competing_holder(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = _real_dir(tmp_path, "object-store")
    copyback = _real_dir(tmp_path, "copyback")
    key = _seed_run_workspace(copyback, AGED)

    acquiring = threading.Event()
    real_acquire = retention_module.acquire_copyback_batch_lock

    def announcing_acquire(root: Path, **kwargs: Any) -> int:
        acquiring.set()
        return real_acquire(root, **kwargs)

    monkeypatch.setattr(retention_module, "acquire_copyback_batch_lock", announcing_acquire)

    box: dict[str, Any] = {}

    def run_pass() -> None:
        try:
            box["result"] = run_retention(
                object_store_root=store,
                now=NOW,
                config=DELETING_CONFIG,
                runs_only_roots=(copyback,),
                copyback_root=copyback,
                copyback_lock_wait_budget_seconds=30.0,
            )
        except BaseException as error:  # pragma: no cover - asserted below
            box["error"] = error

    holder_fd = acquire_copyback_batch_lock(copyback, timeout_seconds=10)
    thread = threading.Thread(target=run_pass)
    thread.start()
    try:
        assert acquiring.wait(timeout=10)
        time.sleep(0.3)
        # Blocked, not running: the tree is untouched while the mutex is held.
        assert thread.is_alive()
        assert (copyback / key / "output/out.nc").exists()
    finally:
        release_copyback_batch_lock(holder_fd)

    thread.join(timeout=30)
    assert not thread.is_alive()
    assert "error" not in box
    result = box["result"]
    # ... and it completes once the holder releases.
    assert _entries_for(result.deleted, copyback) == {key}
    assert result.failed == []
    assert not (copyback / key).exists()


# ---------------------------------------------------------------------------
# EF-2 -- the reverse direction: a second party cannot acquire *during* the
# removal. Asserted independently, because EF-1 alone passes against an
# implementation that acquires after the removal instead of before it.
# ---------------------------------------------------------------------------
def test_ef2_a_second_party_cannot_acquire_while_a_removal_runs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = _real_dir(tmp_path, "object-store")
    copyback = _real_dir(tmp_path, "copyback")
    key = _seed_run_workspace(copyback, AGED)
    observations = _observe_removals(monkeypatch, copyback)

    result = run_retention(
        object_store_root=store,
        now=NOW,
        config=DELETING_CONFIG,
        runs_only_roots=(copyback,),
        copyback_root=copyback,
        copyback_lock_wait_budget_seconds=30.0,
    )

    assert observations == [(str(copyback / key), True)]
    assert _entries_for(result.deleted, copyback) == {key}
    assert not (copyback / key).exists()
    # Held only until the removal returned: the next acquisition is immediate.
    released = acquire_copyback_batch_lock(copyback, timeout_seconds=1.0)
    release_copyback_batch_lock(released)


# ---------------------------------------------------------------------------
# EF-3 -- the other two lanes do not lock, asserted as a behavioural difference:
# they complete in the same pass in which the copyback lane is locked out.
# ---------------------------------------------------------------------------
def test_ef3_the_workspace_and_primary_lanes_delete_while_the_copyback_mutex_is_held(
    tmp_path: Path,
) -> None:
    store = _real_dir(tmp_path, "object-store")
    workspace = _real_dir(tmp_path, "workspace")
    copyback = _real_dir(tmp_path, "copyback")
    store_key = _seed_run_workspace(store, AGED)
    workspace_key = _seed_run_workspace(workspace, AGED)
    copyback_key = _seed_run_workspace(copyback, AGED)

    holder_fd = acquire_copyback_batch_lock(copyback, timeout_seconds=10)
    try:
        result = run_retention(
            object_store_root=store,
            now=NOW,
            config=DELETING_CONFIG,
            # The copyback root first, so the workspace entry is processed
            # *after* the locked-out one: a failure never aborts the sweep.
            runs_only_roots=(copyback, workspace),
            copyback_root=copyback,
            copyback_lock_wait_budget_seconds=0.4,
        )
    finally:
        release_copyback_batch_lock(holder_fd)

    assert _entries_for(result.deleted, store) == {store_key}
    assert _entries_for(result.deleted, workspace) == {workspace_key}
    assert _entries_for(result.failed, copyback) == {copyback_key}
    assert not (store / store_key).exists()
    assert not (workspace / workspace_key).exists()
    assert (copyback / copyback_key / "output/out.nc").exists()
    # The behavioural difference, on disk: no mutex was created for either lane.
    assert not _lock_file(store).exists()
    assert not _lock_file(workspace).exists()
    assert _lock_file(copyback).exists()


# ---------------------------------------------------------------------------
# EF-4 -- WORKSPACE_ROOT and the copyback root resolving to one directory (the
# #1318 silent dedup): the single admitted root IS the shared one, so it locks.
# ---------------------------------------------------------------------------
def test_ef4_the_deduplicated_root_is_locked(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = _real_dir(tmp_path, "object-store")
    shared = _real_dir(tmp_path, "shared")
    key = _seed_run_workspace(shared, AGED)
    observations = _observe_removals(monkeypatch, shared)

    result = run_retention(
        object_store_root=store,
        now=NOW,
        config=DELETING_CONFIG,
        # Both call sites' shape: WORKSPACE_ROOT then the copyback root.
        runs_only_roots=(str(shared), str(shared)),
        copyback_root=str(shared),
        copyback_lock_wait_budget_seconds=30.0,
    )

    assert result.extra_roots == [str(shared)]
    assert observations == [(str(shared / key), True)]
    assert _entries_for(result.deleted, shared) == {key}
    assert not (shared / key).exists()


# ---------------------------------------------------------------------------
# EF-5 -- the copyback root resolving onto the primary object store: still
# swept (through the primary arm), deliberately not locked, because in that
# configuration every copyback writer refuses before acquiring -- four lanes
# return a `copyback_root_matches_object_store_root` skip and two raise
# (`tile_publisher.forcing_copyback_backfill`,
# `scripts/canonical_precip_copyback_backfill.py`) -- so no second party to
# this mutex exists.
# ---------------------------------------------------------------------------
def test_ef5_a_copyback_root_that_is_the_primary_object_store_is_swept_but_not_locked(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = _real_dir(tmp_path, "object-store")
    key = _seed_run_workspace(store, AGED)
    _forbid_acquisitions(monkeypatch)

    result = run_retention(
        object_store_root=store,
        now=NOW,
        config=DELETING_CONFIG,
        runs_only_roots=(store,),
        copyback_root=store,
        copyback_lock_wait_budget_seconds=30.0,
    )

    # Dropped from the additional roots by the resolved-path dedup against the
    # primary, yet still swept.
    assert result.extra_roots == []
    assert _entries_for(result.deleted, store) == {key}
    assert not (store / key).exists()
    # Both halves: the removal happened, and no lock file was created.
    assert not _lock_file(store).exists()


# ---------------------------------------------------------------------------
# EF-6 -- a root the resolver dropped is neither swept nor locked.
# ---------------------------------------------------------------------------
def test_ef6_a_relative_copyback_root_is_neither_swept_nor_locked(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = _real_dir(tmp_path, "object-store")
    cwd = _real_dir(tmp_path, "cwd")
    nested = cwd / "relative" / "copyback"
    key = _seed_run_workspace(nested, AGED)
    monkeypatch.chdir(cwd)
    _forbid_acquisitions(monkeypatch)

    result = run_retention(
        object_store_root=store,
        now=NOW,
        config=DELETING_CONFIG,
        runs_only_roots=("relative/copyback",),
        copyback_root="relative/copyback",
        copyback_lock_wait_budget_seconds=30.0,
    )

    assert result.extra_roots == []
    assert result.deleted == []
    assert (nested / key / "output/out.nc").exists()
    assert not _lock_file(nested).exists()
    # Recorded exactly once: naming the root for the mutex must not duplicate
    # the skip entry the root resolver already produced for the same value.
    assert [
        entry["root"]
        for entry in result.skipped
        if entry["reason"] == EXTRA_ROOT_NOT_ABSOLUTE_REASON
    ] == ["relative/copyback"]


def test_ef6_an_overlap_rejected_copyback_root_is_neither_swept_nor_locked(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = _real_dir(tmp_path, "object-store")
    workspace = _real_dir(tmp_path, "workspace")
    # Inside the workspace root's own ``runs/<canonical_run_id>`` target lane,
    # so #1617 rejects it in favour of the already-admitted workspace root.
    copyback = workspace / "runs" / _run_id(NOW - timedelta(days=1))
    key = _seed_run_workspace(copyback, AGED)
    _forbid_acquisitions(monkeypatch)

    result = run_retention(
        object_store_root=store,
        now=NOW,
        config=DELETING_CONFIG,
        runs_only_roots=(workspace, copyback),
        copyback_root=copyback,
        copyback_lock_wait_budget_seconds=30.0,
    )

    assert result.extra_roots == [str(workspace)]
    assert [
        (entry["root"], entry["conflicting_root"])
        for entry in result.skipped
        if entry["reason"] == ROOT_OVERLAP_REASON
    ] == [(str(copyback), str(workspace))]
    assert result.deleted == []
    assert (copyback / key / "output/out.nc").exists()
    assert not _lock_file(copyback).exists()


# ---------------------------------------------------------------------------
# EF-7 -- a blank or unset copyback root locks nothing, and the pass otherwise
# behaves exactly as it does today (the whole non-db-free deployment).
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("value", [None, "", "   "], ids=["unset", "blank", "whitespace"])
def test_ef7_a_blank_or_unset_copyback_root_locks_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, value: str | None
) -> None:
    store = _real_dir(tmp_path, "object-store")
    copyback = _real_dir(tmp_path, "copyback")
    key = _seed_run_workspace(copyback, AGED)
    _forbid_acquisitions(monkeypatch)

    result = run_retention(
        object_store_root=store,
        now=NOW,
        config=DELETING_CONFIG,
        runs_only_roots=(copyback,),
        copyback_root=value,
        copyback_lock_wait_budget_seconds=30.0,
    )

    assert _entries_for(result.deleted, copyback) == {key}
    assert result.failed == []
    assert not (copyback / key).exists()
    assert not _lock_file(copyback).exists()


# ---------------------------------------------------------------------------
# EF-8 -- per tree: N acquisitions, N releases, and the mutex free between them.
# ---------------------------------------------------------------------------
def test_ef8_each_copyback_tree_gets_its_own_acquire_and_release(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = _real_dir(tmp_path, "object-store")
    copyback = _real_dir(tmp_path, "copyback")
    keys = {_seed_run_workspace(copyback, NOW - timedelta(days=days)) for days in (40, 50, 60)}
    assert len(keys) == 3

    events: list[str] = []
    real_acquire = retention_module.acquire_copyback_batch_lock
    real_release = retention_module.release_copyback_batch_lock

    def recording_acquire(root: Path, **kwargs: Any) -> int:
        if events:
            # Between two removals the mutex must be free: this probe times out
            # if the previous removal's lock is still held (a whole-pass hold).
            probe = acquire_copyback_batch_lock(root, timeout_seconds=0.2)
            release_copyback_batch_lock(probe)
        events.append("acquire")
        return real_acquire(root, **kwargs)

    def recording_release(fd: int) -> None:
        events.append("release")
        real_release(fd)

    monkeypatch.setattr(retention_module, "acquire_copyback_batch_lock", recording_acquire)
    monkeypatch.setattr(retention_module, "release_copyback_batch_lock", recording_release)

    result = run_retention(
        object_store_root=store,
        now=NOW,
        config=DELETING_CONFIG,
        runs_only_roots=(copyback,),
        copyback_root=copyback,
        copyback_lock_wait_budget_seconds=30.0,
    )

    assert result.failed == []
    assert _entries_for(result.deleted, copyback) == keys
    assert events == ["acquire", "release"] * 3


# ---------------------------------------------------------------------------
# EF-9 -- a timeout against a real second process is a recorded failure, the
# tree survives, and the pass keeps going.
# ---------------------------------------------------------------------------
def test_ef9_a_timeout_is_a_recorded_failure_and_the_tree_survives(tmp_path: Path) -> None:
    store = _real_dir(tmp_path, "object-store")
    workspace = _real_dir(tmp_path, "workspace")
    copyback = _real_dir(tmp_path, "copyback")
    copyback_key = _seed_run_workspace(copyback, AGED)
    workspace_key = _seed_run_workspace(workspace, NOW - timedelta(days=50))

    holder = _holder_subprocess(copyback)
    try:
        result = run_retention(
            object_store_root=store,
            now=NOW,
            config=DELETING_CONFIG,
            runs_only_roots=(copyback, workspace),
            copyback_root=copyback,
            copyback_lock_wait_budget_seconds=0.5,
        )
    finally:
        holder.kill()
        holder.wait(timeout=10)

    # `CopybackLockTimeout` did not escape `_delete_entry`: the pass returned.
    errors = _errors_for(result.failed, copyback)
    assert set(errors) == {copyback_key}
    assert "deadline" in errors[copyback_key]
    assert _entries_for(result.deleted, copyback) == set()
    assert (copyback / copyback_key / "output/out.nc").exists()
    # Nothing of the blocked tree's planned size reached the freed total.
    blocked_size = next(
        entry["size_bytes"] for entry in result.planned if entry["key"] == copyback_key
    )
    assert blocked_size > 0
    assert result.freed_bytes == sum(int(entry["size_bytes"]) for entry in result.deleted)
    # The entries after it are still processed.
    assert _entries_for(result.deleted, workspace) == {workspace_key}
    assert not (workspace / workspace_key).exists()


# ---------------------------------------------------------------------------
# EF-10 -- an unsafe lock file is a recorded failure too, so the `except` tuple
# is proven to catch the base class and not just the timeout subclass.
# ---------------------------------------------------------------------------
def test_ef10_an_unsafe_lock_file_is_a_recorded_failure_and_the_trees_survive(
    tmp_path: Path,
) -> None:
    store = _real_dir(tmp_path, "object-store")
    workspace = _real_dir(tmp_path, "workspace")
    copyback = _real_dir(tmp_path, "copyback")
    copyback_keys = {
        _seed_run_workspace(copyback, NOW - timedelta(days=days)) for days in (40, 50)
    }
    workspace_key = _seed_run_workspace(workspace, NOW - timedelta(days=60))
    _plant_lock_file(copyback, mode=0o644)

    result = run_retention(
        object_store_root=store,
        now=NOW,
        config=DELETING_CONFIG,
        runs_only_roots=(copyback, workspace),
        copyback_root=copyback,
        copyback_lock_wait_budget_seconds=30.0,
    )

    errors = _errors_for(result.failed, copyback)
    assert set(errors) == copyback_keys
    # A non-timeout `CopybackLockError`: it fails the identity assertion before
    # any blocking poll, so it charges the budget only its own near-zero
    # elapsed and never a deadline's wait. Every entry on the root was
    # therefore attempted, and refused for the same reason.
    assert all("0600" in error for error in errors.values())
    assert _entries_for(result.deleted, copyback) == set()
    for key in copyback_keys:
        assert (copyback / key / "output/out.nc").exists()
    assert result.freed_bytes == sum(int(entry["size_bytes"]) for entry in result.deleted)
    assert _entries_for(result.deleted, workspace) == {workspace_key}


# ---------------------------------------------------------------------------
# T3 -- the release is in a ``finally``: a removal that RAISES inside the held
# mutex still frees it. Not a hypothetical: `remove_tree_allow_symlinks` is
# called with `missing_ok=False`, and this change's own Known limits keep the
# plan-to-delete window open and name a second, unlocked deleter on the same
# tree -- so `safe_fs` re-raising `FileNotFoundError` (or wrapping any other
# `OSError` into `SafeFilesystemError`) inside the held window is reachable.
# Without the `finally` the fd leaks and this process holds the cross-process
# mutex until exit, blocking every #2035 writer -- the exact failure this change
# exists to prevent, and silent, because `failed[]` drives no metric.
#
# Asserted structurally on all three halves: the failure is recorded, the NEXT
# tree still gets removed (which a leaked fd makes impossible, since `flock` is
# per open file description and the pass would contend with itself), and a
# competing acquisition succeeds immediately once the pass returns.
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    "make_error",
    [
        lambda target: SafeFilesystemError(f"synthetic removal refusal for {target}", kind="io"),
        lambda target: OSError(f"synthetic removal refusal for {target}"),
    ],
    ids=["safe-filesystem-error", "os-error"],
)
def test_a_removal_that_raises_inside_the_mutex_still_releases_it(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    make_error: Any,
) -> None:
    store = _real_dir(tmp_path, "object-store")
    copyback = _real_dir(tmp_path, "copyback")
    keys = {_seed_run_workspace(copyback, NOW - timedelta(days=days)) for days in (40, 50)}
    assert len(keys) == 2

    removals: list[str] = []
    real_remove = retention_module.remove_tree_allow_symlinks

    def failing_first_remove(parent: Path, name: str, **kwargs: Any) -> Any:
        target = Path(parent) / name
        removals.append(str(target))
        if len(removals) == 1:
            raise make_error(target)
        return real_remove(parent, name, **kwargs)

    monkeypatch.setattr(retention_module, "remove_tree_allow_symlinks", failing_first_remove)

    result = run_retention(
        object_store_root=store,
        now=NOW,
        config=DELETING_CONFIG,
        runs_only_roots=(copyback,),
        copyback_root=copyback,
        # Small on purpose: a build whose release is not in a `finally` makes
        # the second tree wait out this budget against the leaked fd instead of
        # acquiring, so the case stays fast when it reds.
        copyback_lock_wait_budget_seconds=2.0,
    )

    # Plan order is not this test's business: derive both keys from what ran.
    assert len(removals) == 2
    first_key = str(Path(removals[0]).relative_to(copyback))
    second_key = str(Path(removals[1]).relative_to(copyback))
    assert {first_key, second_key} == keys

    # (a) the raising tree is one recorded failure carrying its error ...
    errors = _errors_for(result.failed, copyback)
    assert set(errors) == {first_key}
    assert "synthetic removal refusal" in errors[first_key]
    assert (copyback / first_key / "output/out.nc").exists()
    # (b) ... and the tree after it is still removed, which a leaked fd -- held
    # by this same process -- would turn into a self-inflicted lock timeout.
    assert _entries_for(result.deleted, copyback) == {second_key}
    assert not (copyback / second_key).exists()
    assert result.freed_bytes == sum(int(entry["size_bytes"]) for entry in result.deleted)
    # (c) the mutex is free the moment the pass returns. No `try`: a raise here
    # IS the failure, and it is structural -- no message text is read.
    freed = acquire_copyback_batch_lock(copyback, timeout_seconds=0.2)
    release_copyback_batch_lock(freed)


# ---------------------------------------------------------------------------
# EF-11 -- the pass-level wait budget bounds a stalled sweep: per-tree deadlines
# alone multiply by the tree count, and a sweep that outlasts its own 12-hourly
# cadence is an outage of its own.
# ---------------------------------------------------------------------------
def test_ef11_the_pass_level_wait_budget_bounds_a_stalled_sweep(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = _real_dir(tmp_path, "object-store")
    workspace = _real_dir(tmp_path, "workspace")
    copyback = _real_dir(tmp_path, "copyback")
    copyback_keys = {
        _seed_run_workspace(copyback, NOW - timedelta(days=days))
        for days in (40, 45, 50, 55, 60, 65)
    }
    assert len(copyback_keys) == 6
    workspace_key = _seed_run_workspace(workspace, NOW - timedelta(days=70))

    # The refusal, pinned structurally: six planned trees, exactly ONE
    # acquisition. Deleting the `remaining_seconds <= 0` guard leaves the five
    # unattempted entries each calling `acquire_copyback_batch_lock` (with a
    # spent, negative deadline), which the message assertions below catch only
    # through the word "budget" and `elapsed` does not catch at all.
    acquisitions: list[float] = []
    real_acquire = retention_module.acquire_copyback_batch_lock

    def counting_acquire(root: Path, **kwargs: Any) -> int:
        acquisitions.append(float(kwargs["timeout_seconds"]))
        return real_acquire(root, **kwargs)

    monkeypatch.setattr(retention_module, "acquire_copyback_batch_lock", counting_acquire)

    holder_fd = acquire_copyback_batch_lock(copyback, timeout_seconds=10)
    started = time.monotonic()
    try:
        result = run_retention(
            object_store_root=store,
            now=NOW,
            config=DELETING_CONFIG,
            runs_only_roots=(copyback, workspace),
            copyback_root=copyback,
            copyback_lock_wait_budget_seconds=0.5,
        )
    finally:
        elapsed = time.monotonic() - started
        release_copyback_batch_lock(holder_fd)

    # Six per-tree deadlines would be >= 3.0 s; one pass budget is 0.5 s. This
    # is the clause's own wall-clock phrase read directly; the acquisition count
    # below is what makes the refusal itself non-optional.
    assert elapsed < 2.0
    assert len(acquisitions) == 1
    errors = _errors_for(result.failed, copyback)
    assert set(errors) == copyback_keys
    # The first entry spent the budget waiting; the other five were never
    # attempted, and say so.
    budget_exhausted = [key for key, error in errors.items() if "budget" in error]
    assert len(budget_exhausted) == 5
    waited = [key for key, error in errors.items() if "deadline" in error]
    assert len(waited) == 1
    for key in copyback_keys:
        assert (copyback / key / "output/out.nc").exists()
    # The pass still returns normally and the other roots are unaffected.
    assert _entries_for(result.deleted, workspace) == {workspace_key}
    assert _entries_for(result.deleted, store) == set()


# ---------------------------------------------------------------------------
# EF-11, second half -- "regardless of how many copyback entries were planned".
# The case above has exactly ONE acquisition, so it cannot see how the budget is
# spent; two mutants survive it. Passing `budget_seconds` instead of
# `remaining_seconds` to every acquire bounds the pass at N x budget, and
# charging the budget only when the acquisition RAISED charges a
# successful-after-waiting acquisition zero, so N trees can each wait a full
# budget -- unbounded in the tree count, which is exactly the clause's text.
#
# What discriminates is a wait that SUCCEEDS after consuming part of the budget:
# a holder released mid-wait, then a second acquisition whose deadline must be
# `budget - consumed`. Asserted on the `timeout_seconds` the pass actually
# passes -- the decision is made before the call, so this reads the arithmetic
# and not a race.
# ---------------------------------------------------------------------------
def test_ef11_a_wait_that_succeeds_is_charged_against_the_pass_budget(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = _real_dir(tmp_path, "object-store")
    copyback = _real_dir(tmp_path, "copyback")
    keys = {_seed_run_workspace(copyback, NOW - timedelta(days=days)) for days in (40, 50)}
    assert len(keys) == 2
    budget = 5.0
    hold_seconds = 0.3

    deadlines: list[float] = []
    waits: list[float] = []
    acquiring = threading.Event()
    real_acquire = retention_module.acquire_copyback_batch_lock

    def measuring_acquire(root: Path, **kwargs: Any) -> int:
        deadlines.append(float(kwargs["timeout_seconds"]))
        started = time.monotonic()
        acquiring.set()  # after `started`, so the measured wait >= the hold
        try:
            return real_acquire(root, **kwargs)
        finally:
            waits.append(time.monotonic() - started)

    monkeypatch.setattr(retention_module, "acquire_copyback_batch_lock", measuring_acquire)

    holder_fd = acquire_copyback_batch_lock(copyback, timeout_seconds=10)
    released = threading.Event()

    def release_mid_wait() -> None:
        if acquiring.wait(timeout=10):
            time.sleep(hold_seconds)
        release_copyback_batch_lock(holder_fd)
        released.set()

    releaser = threading.Thread(target=release_mid_wait)
    releaser.start()
    try:
        result = run_retention(
            object_store_root=store,
            now=NOW,
            config=DELETING_CONFIG,
            runs_only_roots=(copyback,),
            copyback_root=copyback,
            copyback_lock_wait_budget_seconds=budget,
        )
    finally:
        releaser.join(timeout=30)
    assert released.is_set()

    # The fixture is only worth anything if the first acquisition really waited
    # and both trees really went: a release that never fired would otherwise
    # leave this green on two refusals.
    assert result.failed == []
    assert _entries_for(result.deleted, copyback) == keys
    assert len(deadlines) == 2
    assert waits[0] >= hold_seconds
    # First: the whole budget. Second: the budget minus what the first spent --
    # strictly smaller by at least the hold, and equal to the measured wait.
    assert deadlines[0] == budget
    assert deadlines[1] <= budget - hold_seconds
    assert deadlines[1] == pytest.approx(budget - waits[0], abs=0.1)


# ---------------------------------------------------------------------------
# EF-11, third case -- the other side of the same arithmetic: the budget charges
# the ACQUISITION, never the hold (design D9, and the
# `_remove_tree_under_copyback_mutex` docstring: "a large uncontended tree
# cannot consume the budget"). Both cases above measure only contended waits, so
# a build that wraps acquire AND removal in one `try`/`finally` and charges the
# whole span once at the end survives them: on the timeout path it still charges
# the wait, so EF-11's refusal count holds.
#
# What discriminates is a removal whose hold OUTLASTS the whole budget while
# nothing contends the mutex. Charging the hold would then spend the budget on
# the first tree and refuse the second as exhausted; charging the acquisition
# only leaves the second tree the full budget and removes it. The sleep is on
# the first `remove_tree_allow_symlinks` call by counter, so plan order is not
# this test's business.
# ---------------------------------------------------------------------------
def test_ef11_a_long_uncontended_hold_is_not_charged_against_the_pass_budget(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = _real_dir(tmp_path, "object-store")
    copyback = _real_dir(tmp_path, "copyback")
    keys = {_seed_run_workspace(copyback, NOW - timedelta(days=days)) for days in (40, 50)}
    assert len(keys) == 2
    budget = 0.4
    hold_seconds = 0.6  # deliberately > budget: charging it exhausts the pass

    deadlines: list[float] = []
    removals: list[str] = []
    real_acquire = retention_module.acquire_copyback_batch_lock
    real_remove = retention_module.remove_tree_allow_symlinks

    def measuring_acquire(root: Path, **kwargs: Any) -> int:
        deadlines.append(float(kwargs["timeout_seconds"]))
        return real_acquire(root, **kwargs)

    def slow_first_remove(parent: Path, name: str, **kwargs: Any) -> Any:
        removals.append(str(Path(parent) / name))
        if len(removals) == 1:
            time.sleep(hold_seconds)
        return real_remove(parent, name, **kwargs)

    monkeypatch.setattr(retention_module, "acquire_copyback_batch_lock", measuring_acquire)
    monkeypatch.setattr(retention_module, "remove_tree_allow_symlinks", slow_first_remove)

    result = run_retention(
        object_store_root=store,
        now=NOW,
        config=DELETING_CONFIG,
        runs_only_roots=(copyback,),
        copyback_root=copyback,
        copyback_lock_wait_budget_seconds=budget,
    )

    # (a) fixture integrity: the slow removal really ran, on a copyback tree.
    assert removals
    assert str(Path(removals[0]).relative_to(copyback)) in keys
    # (b) the second tree was still attempted -- charging the hold spends the
    # whole budget on the first tree, so there is no second deadline at all ...
    assert len(deadlines) == 2
    assert len(removals) == 2
    assert deadlines[0] == budget
    # ... and it was handed the budget back essentially whole. The tolerance is
    # far tighter than the hold, so only an uncharged hold can satisfy it; it
    # rests on an uncontended acquisition costing near zero, which is the same
    # assumption the charged-wait case above already asserts against (`abs=0.1`
    # on a deadline it derives from the measured wait alone).
    assert deadlines[1] == pytest.approx(budget, abs=0.15)
    # (c) and the tree is deleted rather than recorded as budget-exhausted.
    assert result.failed == []
    assert _entries_for(result.deleted, copyback) == keys
    for key in keys:
        assert not (copyback / key).exists()


# ---------------------------------------------------------------------------
# EF-12 -- a pass that provably removes nothing acquires nothing: creating the
# lock file is itself a write on the shared root.
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    "overrides",
    [
        {"dry_run": True},
        {"enabled": False},
        {"extra_roots_enabled": False},
    ],
    ids=["dry-run", "disabled", "extra-roots-gate-closed"],
)
def test_ef12_a_zero_write_pass_acquires_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, overrides: dict[str, bool]
) -> None:
    store = _real_dir(tmp_path, "object-store")
    copyback = _real_dir(tmp_path, "copyback")
    key = _seed_run_workspace(copyback, AGED)
    _forbid_acquisitions(monkeypatch)

    result = run_retention(
        object_store_root=store,
        now=NOW,
        config=replace(DELETING_CONFIG, **overrides),
        runs_only_roots=(copyback,),
        copyback_root=copyback,
        copyback_lock_wait_budget_seconds=30.0,
    )

    assert result.deleted == []
    assert result.failed == []
    assert (copyback / key / "output/out.nc").exists()
    assert not _lock_file(copyback).exists()


# ---------------------------------------------------------------------------
# EF-13 -- the predicate is unchanged: same fixture, both arms, compared
# directly. The mutex changes timing, never selection.
# ---------------------------------------------------------------------------
def _seed_predicate_fixture(tmp_path: Path, arm: str) -> dict[str, Path]:
    """One fixture spanning the adjudication branches the two arms can differ on.

    Aged-out, frontier-exempt, within-window and unparseable, on all three
    roots. The ``tiles/`` file is a published-surface canary rather than a
    branch: retention never enumerates it, so it only has to still be there.
    """

    store = _real_dir(tmp_path, f"{arm}-object-store")
    workspace = _real_dir(tmp_path, f"{arm}-workspace")
    copyback = _real_dir(tmp_path, f"{arm}-copyback")
    # Primary: an aged cycle cohort (deleted) and one inside the frontier bound
    # (exempt), plus their run workspaces.
    _seed_cycle(store, NOW - timedelta(days=40), run=True)
    _seed_cycle(store, NOW - timedelta(days=20), run=True)
    _write(store, "tiles/2026/tile.png")
    for root in (workspace, copyback):
        _seed_run_workspace(root, NOW - timedelta(days=40))  # aged out
        _seed_run_workspace(root, NOW - timedelta(days=32))  # frontier exempt
        _seed_run_workspace(root, NOW - timedelta(days=10))  # within window
        _write(root, f"runs/manual_salvage_{_cycle_name(NOW)}_keep/notes.txt")
    return {"store": store, "workspace": workspace, "copyback": copyback}


def _shape(entries: list[dict[str, Any]], roots: dict[str, Path]) -> set[tuple[str, str, str]]:
    """Root-label + key + reason, with the arm's path prefix factored out."""

    labels = {str(path): label for label, path in roots.items()}
    return {
        (labels[entry["root"]], entry["key"], str(entry.get("reason")))
        for entry in entries
    }


def test_ef13_the_mutex_changes_timing_never_selection(tmp_path: Path) -> None:
    bound = NOW - timedelta(days=35)
    arms: dict[str, dict[str, Any]] = {}
    for arm, copyback_root in (("locked", "copyback"), ("unlocked", None)):
        roots = _seed_predicate_fixture(tmp_path, arm)
        result = run_retention(
            object_store_root=roots["store"],
            now=NOW,
            config=DELETING_CONFIG,
            active_lower_bound=bound,
            active_lower_bound_source="test",
            runs_only_roots=(roots["workspace"], roots["copyback"]),
            copyback_root=roots["copyback"] if copyback_root else None,
            copyback_lock_wait_budget_seconds=30.0,
        )
        arms[arm] = {"roots": roots, "result": result}

    locked, unlocked = arms["locked"], arms["unlocked"]
    for bucket in ("planned", "deleted", "skipped", "failed"):
        assert _shape(getattr(locked["result"], bucket), locked["roots"]) == _shape(
            getattr(unlocked["result"], bucket), unlocked["roots"]
        ), bucket
    # The comparison is only worth anything if the fixture actually exercises
    # deletion, protection and the frontier on the locked arm.
    assert locked["result"].failed == []
    assert _entries_for(locked["result"].deleted, locked["roots"]["copyback"])
    assert locked["result"].freed_bytes == unlocked["result"].freed_bytes
    reasons = {entry["reason"] for entry in locked["result"].skipped}
    assert {
        PIPELINE_FRONTIER_EXEMPT_REASON,
        "within_retention_window",
        "unparseable_run_cycle",
    } <= reasons
    # ... and only the locked arm took the mutex.
    assert _lock_file(locked["roots"]["copyback"]).exists()
    assert not _lock_file(unlocked["roots"]["copyback"]).exists()
    for arm in arms.values():
        assert (arm["roots"]["store"] / "tiles/2026/tile.png").exists()


# ---------------------------------------------------------------------------
# EF-14 -- the lock file is never itself a removal candidate. Unlinking it
# under a live holder splits the mutex: that holder keeps its flock on the
# now-detached inode while the next acquirer creates a fresh file and locks
# that, so the two run unserialised for the rest of that hold. A party that had
# already opened the old inode is refused loudly rather than admitted
# (`_require_lock_identity` runs after the flock and sees the path/fd drift),
# and the root converges on the new file once the stale holder exits -- so the
# damage is a window of no mutual exclusion the length of one hold, not a
# permanent poisoning. A window is enough: that is precisely the interval this
# whole change exists to serialise.
# ---------------------------------------------------------------------------
def test_ef14_the_lock_file_is_never_a_removal_candidate(tmp_path: Path) -> None:
    store = _real_dir(tmp_path, "object-store")
    workspace = _real_dir(tmp_path, "workspace")
    copyback = _real_dir(tmp_path, "copyback")
    store_keys = _seed_cycle(store, AGED, run=True)
    workspace_key = _seed_run_workspace(workspace, AGED)
    copyback_key = _seed_run_workspace(copyback, AGED)
    lock_files = [_plant_lock_file(root) for root in (store, workspace, copyback)]

    result = run_retention(
        object_store_root=store,
        now=NOW,
        config=DELETING_CONFIG,
        runs_only_roots=(workspace, copyback),
        copyback_root=copyback,
        copyback_lock_wait_budget_seconds=30.0,
    )

    for bucket in (result.planned, result.deleted, result.skipped, result.failed):
        assert not any(COPYBACK_BATCH_LOCK_NAME in entry["key"] for entry in bucket)
    for lock_file in lock_files:
        assert lock_file.is_file()
    # The full deletion path still ran on every root, lock file present.
    assert _entries_for(result.deleted, store) == set(store_keys.values())
    assert _entries_for(result.deleted, workspace) == {workspace_key}
    assert _entries_for(result.deleted, copyback) == {copyback_key}


# ---------------------------------------------------------------------------
# EF-15 -- both call sites name the copyback root. Asserted at the call site
# rather than inferred: the wiring is the only place a deployed pass can lose
# the mutex entirely without any test on the retention module itself noticing.
# ---------------------------------------------------------------------------
def test_the_scheduler_pass_names_the_copyback_root_for_the_mutex(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Deleting ``copyback_root=`` from ``scheduler_runtime._run_retention``
    must make this fail: the workspace lane would look identical."""

    _seed_pass_env(monkeypatch)
    store = _real_dir(tmp_path, "object-store")
    workspace = _real_dir(tmp_path, "workspace")
    copyback = _real_dir(tmp_path, "copyback")
    workspace_key = _seed_run_workspace(workspace, AGED)
    copyback_key = _seed_run_workspace(copyback, NOW - timedelta(days=50))
    monkeypatch.setenv("WORKSPACE_ROOT", str(workspace))
    monkeypatch.setenv("NHMS_OBJECT_STORE_COPYBACK_ROOT", str(copyback))
    observations = _observe_removals(monkeypatch, copyback)

    scheduler = _pass_scheduler(workspace_root=str(workspace), object_store_root=str(store))
    payload = scheduler._run_retention(NOW)

    assert payload["status"] == "completed"
    assert payload["counts"]["failed"] == 0
    assert observations == [
        (str(workspace / workspace_key), False),
        (str(copyback / copyback_key), True),
    ]
    assert not _lock_file(workspace).exists()


def test_the_cleanup_cli_names_the_copyback_root_for_the_mutex(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Same for ``cli._run_cleanup``, which reads the root from the environment."""

    now = datetime.now(UTC)
    store = _real_dir(tmp_path, "object-store")
    workspace = _real_dir(tmp_path, "workspace")
    copyback = _real_dir(tmp_path, "copyback")
    evidence_dir = workspace / "scheduler" / "evidence"
    evidence_dir.mkdir(parents=True, exist_ok=True)
    workspace_key = _seed_run_workspace(workspace, now - timedelta(days=60))
    copyback_key = _seed_run_workspace(copyback, now - timedelta(days=70))
    monkeypatch.setenv("WORKSPACE_ROOT", str(workspace))
    monkeypatch.setenv("NHMS_SCHEDULER_EVIDENCE_ROOT", str(evidence_dir))
    monkeypatch.setenv("OBJECT_STORE_ROOT", str(store))
    monkeypatch.setenv("NHMS_OBJECT_STORE_COPYBACK_ROOT", str(copyback))
    monkeypatch.setenv("NHMS_RETENTION_EXTRA_ROOTS_ENABLED", "true")
    monkeypatch.delenv("NHMS_RETENTION_EXTRA_ROOTS_DAYS", raising=False)
    monkeypatch.delenv("NHMS_PUBLISHED_ARTIFACT_ROOT", raising=False)
    monkeypatch.delenv("NHMS_RETENTION_FRONTIER_MAX_AGE_HOURS", raising=False)
    # The out-of-pass entrypoint fails closed into dry-run without a frontier,
    # and a dry run deletes nothing at all.
    (evidence_dir / "pass-1.json").write_text(
        json.dumps(
            {
                "schema_version": "nhms.production_scheduler.pass_evidence.v1",
                "pass_id": "pass-1",
                "started_at": now.astimezone(UTC).isoformat().replace("+00:00", "Z"),
                "retention": {
                    "status": "completed",
                    "frontier": {
                        "active_lower_bound": (now - timedelta(days=1)).astimezone(UTC).isoformat(),
                        "source": "scheduler_pass",
                        "protected_count": 0,
                    },
                },
            }
        ),
        encoding="utf-8",
    )
    observations = _observe_removals(monkeypatch, copyback)

    payload = cli._run_cleanup(retention_days=14, dry_run=False)

    assert payload["dry_run"] is False
    assert payload["counts"]["failed"] == 0
    assert observations == [
        (str(workspace / workspace_key), False),
        (str(copyback / copyback_key), True),
    ]
    assert not _lock_file(workspace).exists()
