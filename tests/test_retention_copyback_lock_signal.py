"""Requirement-driven tests for the orchestrator retention lock-failure signal (#2262).

Contract, from the "Destructive removal of a copyback directory tree holds the
same mutex" requirement in `harden-copyback-mutex-residuals`:

* a lock failure is that entry's `failed[]` record naming its shape --
  `lock_timeout`, `lock_unsafe` or `lock_budget_exhausted` -- with the error
  text and class name (EF-14);
* each pass receipt carries `copyback_lock_failures` with all three counts,
  zeros included, and receipt compaction keeps that block when it drops the
  per-entry detail (EF-14);
* the tolerated plan-to-delete window stays tolerated: a writer that commits
  into a planned tree before the pass acquires does not change what the pass
  removes (EF-15, pins existing behaviour).

This lane acquires with the default `flock`, which is per open file description,
so a holder or writer in this same process contends with the pass exactly as a
separate host would. Roots live under `tmp_path.resolve()` because `safe_fs`
walks with `O_NOFOLLOW` and macOS `tmp_path` sits under `/var`.
"""

from __future__ import annotations

import os
import shutil
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from packages.common.copyback_guard import (
    COPYBACK_BATCH_LOCK_NAME,
    acquire_copyback_batch_lock,
    copyback_batch_lock,
    release_copyback_batch_lock,
)

# #2259: both seams this file patches -- the acquisition and the removal --
# are called from `_delete_entry` / `_remove_tree_under_copyback_mutex`, which
# moved to this module; `retention` no longer binds either name.
from services.orchestrator import retention_copyback_mutex as mutex_module

# `scheduler_evidence_payload` is loaded through `scheduler_evidence` (the two
# import each other), exactly as production loads it.
from services.orchestrator import scheduler_evidence  # noqa: F401
from services.orchestrator import scheduler_evidence_payload as payload_module
from services.orchestrator.retention import RetentionConfig, run_retention

NOW = datetime(2026, 6, 3, 12, 0, tzinfo=UTC)
DELETING_CONFIG = RetentionConfig(
    enabled=True,
    dry_run=False,
    retention_days=14,
    extra_roots_enabled=True,
    extra_roots_retention_days=30,
)
DRY_RUN_CONFIG = RetentionConfig(
    enabled=True,
    dry_run=True,
    retention_days=14,
    extra_roots_enabled=True,
    extra_roots_retention_days=30,
)
ZERO_LOCK_FAILURES = {"lock_timeout": 0, "lock_unsafe": 0, "lock_budget_exhausted": 0}


def _real_dir(tmp_path: Path, name: str) -> Path:
    path = tmp_path.resolve() / name
    path.mkdir(parents=True, exist_ok=True)
    return path


def _seed_run(root: Path, days_old: int) -> str:
    run_id = f"fcst_gfs_{(NOW - timedelta(days=days_old)).strftime('%Y%m%d%H')}_model_a"
    for rel in ("input/manifest.json", "output/out.nc"):
        path = root / "runs" / run_id / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"run-bytes")
    return f"runs/{run_id}"


def _pass(store: Path, copyback: Path, *, budget: float, config: RetentionConfig = DELETING_CONFIG) -> Any:
    return run_retention(
        object_store_root=store,
        now=NOW,
        config=config,
        runs_only_roots=(copyback,),
        copyback_root=copyback,
        copyback_lock_wait_budget_seconds=budget,
    )


def _shapes(result: Any) -> dict[str, tuple[str, str]]:
    return {entry["key"]: (entry["lock_failure"], entry["error_type"]) for entry in result.failed}


# --- EF-14: typed lock failures, counts, and compaction ------------------------


def test_ef14_a_stalled_holder_yields_one_timeout_and_budget_exhausted_for_the_rest(tmp_path: Path) -> None:
    store = _real_dir(tmp_path, "object-store")
    copyback = _real_dir(tmp_path, "copyback")
    keys = sorted(_seed_run(copyback, days) for days in (40, 45, 50))

    holder = acquire_copyback_batch_lock(copyback, timeout_seconds=10)
    try:
        result = _pass(store, copyback, budget=0.5)
    finally:
        release_copyback_batch_lock(holder)

    shapes = _shapes(result)
    assert set(shapes) == set(keys)
    assert sorted(shapes.values()) == [
        ("lock_budget_exhausted", "CopybackLockBudgetExhausted"),
        ("lock_budget_exhausted", "CopybackLockBudgetExhausted"),
        ("lock_timeout", "CopybackLockTimeout"),
    ]
    assert all(entry["error"] for entry in result.failed)
    payload = result.to_dict()
    assert payload["copyback_lock_failures"] == {"lock_timeout": 1, "lock_unsafe": 0, "lock_budget_exhausted": 2}
    for key in keys:
        assert (copyback / key / "output/out.nc").exists()


def test_ef14_an_unsafe_lock_file_is_lock_unsafe_for_every_entry(tmp_path: Path) -> None:
    store = _real_dir(tmp_path, "object-store")
    copyback = _real_dir(tmp_path, "copyback")
    keys = {_seed_run(copyback, days) for days in (40, 50)}
    lock_file = copyback / COPYBACK_BATCH_LOCK_NAME
    lock_file.write_bytes(b"")
    os.chmod(lock_file, 0o644)

    result = _pass(store, copyback, budget=30.0)

    assert _shapes(result) == dict.fromkeys(keys, ("lock_unsafe", "CopybackLockError"))
    assert result.to_dict()["copyback_lock_failures"] == {
        "lock_timeout": 0,
        "lock_unsafe": 2,
        "lock_budget_exhausted": 0,
    }


def test_ef14_a_removal_error_carries_no_lock_failure_and_counts_stay_zero(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = _real_dir(tmp_path, "object-store")
    copyback = _real_dir(tmp_path, "copyback")
    key = _seed_run(copyback, 40)

    def refusing_remove(parent: Path, name: str, **kwargs: Any) -> None:
        raise OSError(f"synthetic removal refusal for {name}")

    monkeypatch.setattr(mutex_module, "remove_tree_allow_symlinks", refusing_remove)

    result = _pass(store, copyback, budget=30.0)

    assert [entry["key"] for entry in result.failed] == [key]
    assert "lock_failure" not in result.failed[0]
    assert "synthetic removal refusal" in result.failed[0]["error"]
    assert result.failed[0]["error_type"] == "OSError"
    assert result.to_dict()["copyback_lock_failures"] == ZERO_LOCK_FAILURES


def test_ef14_every_receipt_shape_carries_the_block_and_compaction_keeps_it(tmp_path: Path) -> None:
    store = _real_dir(tmp_path, "object-store")
    copyback = _real_dir(tmp_path, "copyback")
    _seed_run(copyback, 40)
    _seed_run(copyback, 50)

    dry_run = _pass(store, copyback, budget=30.0, config=DRY_RUN_CONFIG)
    assert dry_run.to_dict()["copyback_lock_failures"] == ZERO_LOCK_FAILURES
    assert not (copyback / COPYBACK_BATCH_LOCK_NAME).exists()

    holder = acquire_copyback_batch_lock(copyback, timeout_seconds=10)
    try:
        blocked = _pass(store, copyback, budget=0.3)
    finally:
        release_copyback_batch_lock(holder)
    retention_payload = {"status": "completed", **blocked.to_dict()}
    expected = {"lock_timeout": 1, "lock_unsafe": 0, "lock_budget_exhausted": 1}

    compacted = payload_module._compact_retention(retention_payload)

    # Per-entry detail is gone; the typed signal is not.
    assert "failed" not in compacted
    assert compacted["failed_count"] == 2
    assert compacted["copyback_lock_failures"] == expected

    # The same through the receipt writer's size-pressure path, which is what
    # actually triggers compaction on a large sweep.
    padded = {
        **retention_payload,
        "failed": [
            *retention_payload["failed"],
            *({"key": f"runs/pad-{index}", "error": "x" * 400} for index in range(40)),
        ],
    }
    receipt, serialized = payload_module._serialized_evidence_within_limit(
        type("Config", (), {"max_evidence_bytes": 8_000})(),
        {"status": "submitted", "retention": padded},
        artifact_path=Path("scheduler_evidence.json"),
    )
    assert len(serialized.encode("utf-8")) <= 8_000
    assert "failed" not in receipt["retention"]
    assert receipt["retention"]["copyback_lock_failures"] == expected


# --- EF-15: the plan-to-delete window is tolerated, not re-adjudicated ----------


def test_ef15_a_writer_committing_into_a_planned_tree_before_acquisition_does_not_change_the_removal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = _real_dir(tmp_path, "object-store")
    copyback = _real_dir(tmp_path, "copyback")
    key = _seed_run(copyback, 40)
    planned_only = _pass(store, copyback, budget=30.0, config=DRY_RUN_CONFIG)
    planned_keys = sorted(entry["key"] for entry in planned_only.planned)
    assert planned_keys == [key]

    committed: list[str] = []
    real_acquire = mutex_module.acquire_copyback_batch_lock

    def acquire_after_a_writer_commits(root: Path, **kwargs: Any) -> int:
        if not committed:
            with copyback_batch_lock(copyback, timeout_seconds=10):
                tree = copyback / key
                shutil.rmtree(tree)
                (tree / "output").mkdir(parents=True)
                (tree / "output" / "rewritten.nc").write_bytes(b"new")
            committed.append(key)
        return real_acquire(root, **kwargs)

    monkeypatch.setattr(mutex_module, "acquire_copyback_batch_lock", acquire_after_a_writer_commits)

    result = _pass(store, copyback, budget=30.0)

    assert committed == [key]
    # Serialised, not re-adjudicated: the freshly committed tree is removed and
    # the planned and deleted sets are those of the plan made before the write.
    assert sorted(entry["key"] for entry in result.planned) == planned_keys
    assert sorted(entry["key"] for entry in result.deleted) == planned_keys
    assert result.failed == []
    assert not (copyback / key).exists()
