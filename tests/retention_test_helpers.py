"""Shared retention-test helpers (non-collectible support module)."""

from __future__ import annotations

import os
import subprocess
import sys
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
from services.orchestrator import retention_copyback_mutex as mutex_module
from services.orchestrator.retention import RetentionConfig

NOW = datetime(2026, 6, 3, 12, 0, tzinfo=UTC)


def _cycle_name(dt: datetime) -> str:
    return dt.strftime("%Y%m%d%H")


def _write(root: Path, rel: str, content: bytes = b"x") -> Path:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)
    return path


def _keys(entries: list[dict]) -> set[str]:
    return {entry["key"] for entry in entries}


def _seed_cycle(
    root: Path,
    cycle_time: datetime,
    *,
    prefixes: tuple[str, ...] = ("raw", "canonical", "forcing"),
    run: bool = False,
) -> dict[str, str]:
    """Seed one cycle's artifacts; returns {kind: object key}."""
    name = _cycle_name(cycle_time)
    keys: dict[str, str] = {}
    for prefix in prefixes:
        _write(root, f"{prefix}/gfs/{name}/payload.nc", b"payload-bytes")
        keys[prefix] = f"{prefix}/gfs/{name}"
    if run:
        _write(root, f"runs/fcst_gfs_{name}_model_a/output/out.nc", b"run-bytes")
        keys["runs"] = f"runs/fcst_gfs_{name}_model_a"
    return keys


def _reasons(entries: list[dict]) -> dict[str, str]:
    return {entry["key"]: entry["reason"] for entry in entries}


EXTRA_CONFIG = RetentionConfig(
    enabled=True,
    dry_run=True,
    retention_days=14,
    extra_roots_enabled=True,
    extra_roots_retention_days=30,
)


def _run_id(cycle_time: datetime) -> str:
    return f"fcst_gfs_{_cycle_name(cycle_time)}_model_a"


def _seed_run_workspace(root: Path, cycle_time: datetime) -> str:
    """Seed a full run workspace under ``<root>/runs``; return its key."""
    run_id = _run_id(cycle_time)
    for rel in (
        "input/manifest.json",
        "output/out.nc",
        "logs/shud.log",
        "state_checkpoint_recovery/checkpoint.json",
    ):
        _write(root, f"runs/{run_id}/{rel}", b"run-bytes")
    return f"runs/{run_id}"


def _entries_for(entries: list[dict], root: Path) -> set[str]:
    resolved = str(root.resolve())
    return {entry["key"] for entry in entries if entry["root"] == resolved}


def _seed_pass_env(monkeypatch) -> None:
    """Enable real additional-root retention for a scheduler pass."""
    monkeypatch.setenv("NHMS_RETENTION_ENABLED", "true")
    monkeypatch.setenv("NHMS_RETENTION_DRY_RUN", "false")
    monkeypatch.setenv("NHMS_RETENTION_EXTRA_ROOTS_ENABLED", "true")
    monkeypatch.delenv("NHMS_RETENTION_DAYS", raising=False)
    monkeypatch.delenv("NHMS_RETENTION_EXTRA_ROOTS_DAYS", raising=False)


def _pass_scheduler(**config_kwargs):
    from services.orchestrator.scheduler import (
        ProductionScheduler,
        ProductionSchedulerConfig,
        _BlockedModelRegistry,
    )

    config = ProductionSchedulerConfig(dry_run=False, **config_kwargs)
    return ProductionScheduler(
        config=config, registry=_BlockedModelRegistry(), adapters={}, active_repository=None
    )


# ===========================================================================
# #2259: the copyback-mutex suite's fixture preamble, moved here verbatim when
# tests/test_retention_copyback_mutex.py (1119 lines) was partitioned into
# tests/test_retention_copyback_mutex_protocol.py and
# tests/test_retention_copyback_mutex_budget.py. Both partitions import every
# name below at module scope, which is what the selector's importer derivation
# reads.
#
# Every root these build is under ``tmp_path.resolve()``: ``safe_fs`` walks with
# ``O_NOFOLLOW`` and macOS's ``tmp_path`` sits under ``/var -> private/var``, so
# a symlinked ancestor would make the removal itself refuse (the
# ``tests/test_copyback_guard.py`` ``_real_root`` idiom).
#
# The patch target is ``services.orchestrator.retention_copyback_mutex``, NOT
# ``services.orchestrator.retention``: #2259 moved ``_delete_entry`` and
# ``_remove_tree_under_copyback_mutex`` -- the only call sites of
# ``acquire_copyback_batch_lock`` / ``release_copyback_batch_lock`` /
# ``remove_tree_allow_symlinks`` -- into that module, and ``retention`` does not
# re-export the names. A patch aimed at the old module therefore raises
# ``AttributeError`` instead of passing vacuously (design D1).
# ===========================================================================

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

    monkeypatch.setattr(mutex_module, "acquire_copyback_batch_lock", forbidden)


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
    real_remove = mutex_module.remove_tree_allow_symlinks

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

    monkeypatch.setattr(mutex_module, "remove_tree_allow_symlinks", observing_remove)
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
