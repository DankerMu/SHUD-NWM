"""The copyback mutex's budget and selection invariants (#2238, split by #2259).

EF-11 through EF-15: one pass-level wait budget bounds a stalled sweep and is
charged by every acquisition (including one that succeeds after waiting) but
never by the hold itself; a pass that provably removes nothing acquires nothing;
the mutex changes timing, never selection; the lock file is never itself a
removal candidate; and both production call sites -- the scheduler pass and the
``cleanup`` CLI -- name the copyback root for the mutex.

Its sibling ``tests/test_retention_copyback_mutex_protocol.py`` owns EF-1
through EF-10 and the exception-release case.

The shared fixture preamble is ``tests/retention_test_helpers.py``; the
monkeypatch target it exposes, ``mutex_module``, is
``services.orchestrator.retention_copyback_mutex`` -- the module that owns the
call sites (design D1).

The cases that must TIME OUT drive the acquisition deadline sub-second through
the pass budget, so the suite stays fast; ``flock`` is per open file
description, so a holder in this same process contends with the pass exactly as
a separate host would.
"""

from __future__ import annotations

import json
import threading
import time
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from packages.common.copyback_guard import (
    COPYBACK_BATCH_LOCK_NAME,
    acquire_copyback_batch_lock,
    release_copyback_batch_lock,
)
from services.orchestrator import cli
from services.orchestrator import retention_copyback_mutex as mutex_module
from services.orchestrator.retention import (
    PIPELINE_FRONTIER_EXEMPT_REASON,
    run_retention,
)
from tests.retention_test_helpers import (
    AGED,
    DELETING_CONFIG,
    NOW,
    _cycle_name,
    _entries_for,
    _errors_for,
    _forbid_acquisitions,
    _lock_file,
    _observe_removals,
    _pass_scheduler,
    _plant_lock_file,
    _real_dir,
    _seed_cycle,
    _seed_pass_env,
    _seed_run_workspace,
    _write,
)


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
    real_acquire = mutex_module.acquire_copyback_batch_lock

    def counting_acquire(root: Path, **kwargs: Any) -> int:
        acquisitions.append(float(kwargs["timeout_seconds"]))
        return real_acquire(root, **kwargs)

    monkeypatch.setattr(mutex_module, "acquire_copyback_batch_lock", counting_acquire)

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
    real_acquire = mutex_module.acquire_copyback_batch_lock

    def measuring_acquire(root: Path, **kwargs: Any) -> int:
        deadlines.append(float(kwargs["timeout_seconds"]))
        started = time.monotonic()
        acquiring.set()  # after `started`, so the measured wait >= the hold
        try:
            return real_acquire(root, **kwargs)
        finally:
            waits.append(time.monotonic() - started)

    monkeypatch.setattr(mutex_module, "acquire_copyback_batch_lock", measuring_acquire)

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
    real_acquire = mutex_module.acquire_copyback_batch_lock
    real_remove = mutex_module.remove_tree_allow_symlinks

    def measuring_acquire(root: Path, **kwargs: Any) -> int:
        deadlines.append(float(kwargs["timeout_seconds"]))
        return real_acquire(root, **kwargs)

    def slow_first_remove(parent: Path, name: str, **kwargs: Any) -> Any:
        removals.append(str(Path(parent) / name))
        if len(removals) == 1:
            time.sleep(hold_seconds)
        return real_remove(parent, name, **kwargs)

    monkeypatch.setattr(mutex_module, "acquire_copyback_batch_lock", measuring_acquire)
    monkeypatch.setattr(mutex_module, "remove_tree_allow_symlinks", slow_first_remove)

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
