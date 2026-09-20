"""The copyback mutex's acquire/release protocol (#2238, partitioned by #2259).

EF-1 through EF-10 plus the exception-release case: WHICH lane locks, that the
hold brackets the removal in both directions, one acquire/release per removed
tree, that a root the resolver dropped is neither swept nor locked, that an
unavailable mutex is one recorded ``failed`` entry rather than an aborted pass,
and that a removal raising inside the held mutex still frees it.

Its sibling ``tests/test_retention_copyback_mutex_budget.py`` owns the
pass-level wait budget (EF-11), the zero-write pass (EF-12), selection
invariance (EF-13), the lock file's exemption from removal (EF-14) and the two
call sites that name the copyback root (EF-15).

The shared fixture preamble is ``tests/retention_test_helpers.py``; the
monkeypatch target it exposes, ``mutex_module``, is
``services.orchestrator.retention_copyback_mutex`` -- the module that owns the
call sites (design D1).

The cases that must TIME OUT drive the acquisition deadline sub-second through
the pass budget, and the probes that ask "is it held right now?" carry a 0.2 s
deadline of their own, so the suite stays fast; the contended cases that must
SUCCEED keep a roomy budget and wait for a real release instead. ``flock`` is
per open file description, so a holder in this same process contends with the
pass exactly as a separate host would.
"""

from __future__ import annotations

import threading
import time
from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest

from packages.common.copyback_guard import (
    acquire_copyback_batch_lock,
    release_copyback_batch_lock,
)
from packages.common.safe_fs import SafeFilesystemError
from services.orchestrator import retention_copyback_mutex as mutex_module
from services.orchestrator.retention import (
    EXTRA_ROOT_NOT_ABSOLUTE_REASON,
    ROOT_OVERLAP_REASON,
    run_retention,
)
from tests.retention_test_helpers import (
    AGED,
    DELETING_CONFIG,
    NOW,
    _entries_for,
    _errors_for,
    _forbid_acquisitions,
    _holder_subprocess,
    _lock_file,
    _observe_removals,
    _plant_lock_file,
    _real_dir,
    _run_id,
    _seed_run_workspace,
)


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
    real_acquire = mutex_module.acquire_copyback_batch_lock

    def announcing_acquire(root: Path, **kwargs: Any) -> int:
        acquiring.set()
        return real_acquire(root, **kwargs)

    monkeypatch.setattr(mutex_module, "acquire_copyback_batch_lock", announcing_acquire)

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
# swept (through the primary arm), deliberately not locked. The rule is
# membership: in that configuration the resolved-path dedup against the
# primary drops the root from `result.extra_roots`, and a root that is not a
# member is not locked. Whether any writer could nonetheless hold this mutex
# on such a root is a property of the writers, and it is not settled here.
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
    real_acquire = mutex_module.acquire_copyback_batch_lock
    real_release = mutex_module.release_copyback_batch_lock

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

    monkeypatch.setattr(mutex_module, "acquire_copyback_batch_lock", recording_acquire)
    monkeypatch.setattr(mutex_module, "release_copyback_batch_lock", recording_release)

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
    real_remove = mutex_module.remove_tree_allow_symlinks

    def failing_first_remove(parent: Path, name: str, **kwargs: Any) -> Any:
        target = Path(parent) / name
        removals.append(str(target))
        if len(removals) == 1:
            raise make_error(target)
        return real_remove(parent, name, **kwargs)

    monkeypatch.setattr(mutex_module, "remove_tree_allow_symlinks", failing_first_remove)

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
