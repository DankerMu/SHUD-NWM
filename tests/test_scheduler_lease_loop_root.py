"""A looping workspace root is refused with the structured lock-parent reason (#2582).

``_open_lock_parent_directory`` used to let the loop escape unclassified: the
3.11 pin's non-strict ``resolve()`` raises an errno-less ``RuntimeError``, and on
3.13+ the folded loop reaches ``mkdir(exist_ok=True)``, which raises a bare
``FileExistsError``.  Both now end at
``UnsafeSchedulerLockError("unsafe_lock_parent_directory")``, so the lease
reports ``acquired=False`` and evidence preparation raises its typed
``unsafe_evidence_directory``.  This file is also run once under
``uv run --python 3.14`` (tasks.md 2.2).

Every ``services.*`` import is function-local, so this file joins no frozen
top-level importer set (``tests/test_select_ci_tests.py``).
"""

from __future__ import annotations

import os
from datetime import UTC, datetime
from pathlib import Path

import pytest

_STARTED_AT = datetime(2026, 9, 27, 12, 0, tzinfo=UTC)


def _real(tmp_path: Path) -> Path:
    # macOS ``tmp_path`` sits under ``/var -> private/var``; anchor every root at
    # the real path so only the loop under test is a symlink.
    return Path(os.path.realpath(tmp_path))


def _two_link_loop(tmp_path: Path) -> Path:
    base = _real(tmp_path)
    (base / "loop_a").symlink_to(base / "loop_b")
    (base / "loop_b").symlink_to(base / "loop_a")
    return base / "loop_a"


def _self_loop(tmp_path: Path) -> Path:
    base = _real(tmp_path)
    (base / "loop_self").symlink_to(base / "loop_self")
    return base / "loop_self"


_LOOPS = pytest.mark.parametrize("make_loop", [_two_link_loop, _self_loop], ids=["two_link", "self"])


@_LOOPS
def test_the_lock_parent_open_raises_the_typed_refusal(tmp_path: Path, make_loop) -> None:
    from services.orchestrator.scheduler_lease import UnsafeSchedulerLockError, _open_lock_parent_directory

    workspace_root = make_loop(tmp_path)

    with pytest.raises(UnsafeSchedulerLockError) as excinfo:
        _open_lock_parent_directory(workspace_root / "scheduler", workspace_root)

    assert excinfo.value.reason == "unsafe_lock_parent_directory"


@_LOOPS
def test_the_lease_reports_the_structured_reason_instead_of_raising(tmp_path: Path, make_loop) -> None:
    from services.orchestrator.scheduler_lease import FileSchedulerLease

    workspace_root = make_loop(tmp_path)
    lease = FileSchedulerLease(
        workspace_root / "scheduler" / "production_scheduler.lock",
        ttl_seconds=60,
        workspace_root=workspace_root,
    )

    result = lease.acquire(pass_id="scheduler_2026092712_loop", started_at=_STARTED_AT)

    assert result["acquired"] is False
    assert result["reason"] == "unsafe_lock_parent_directory"
    assert lease.acquired is False


@_LOOPS
def test_evidence_directory_preparation_raises_its_typed_error(tmp_path: Path, make_loop) -> None:
    from services.orchestrator.scheduler_evidence import SchedulerEvidenceWriteError, open_evidence_directory

    workspace_root = make_loop(tmp_path)

    with pytest.raises(SchedulerEvidenceWriteError) as excinfo:
        open_evidence_directory(workspace_root / "scheduler" / "evidence", workspace_root)

    assert str(excinfo.value) == "unsafe_evidence_directory"


def test_a_regular_file_workspace_root_is_refused_with_the_same_reason(tmp_path: Path) -> None:
    """The ``EEXIST`` arm on its own: ``mkdir(exist_ok=True)`` over a regular file."""

    from services.orchestrator.scheduler_lease import UnsafeSchedulerLockError, _open_lock_parent_directory

    workspace_root = _real(tmp_path) / "not_a_directory"
    workspace_root.write_bytes(b"")

    with pytest.raises(UnsafeSchedulerLockError) as excinfo:
        _open_lock_parent_directory(workspace_root / "scheduler", workspace_root)

    assert excinfo.value.reason == "unsafe_lock_parent_directory"


def test_a_workspace_root_symlinked_to_a_real_directory_is_still_accepted(tmp_path: Path) -> None:
    """Pin (2.3): the root resolves to a real directory and the lease is acquired as before."""

    from services.orchestrator.scheduler_lease import FileSchedulerLease

    real_root = _real(tmp_path) / "workspace"
    real_root.mkdir()
    link = _real(tmp_path) / "workspace_link"
    link.symlink_to(real_root, target_is_directory=True)
    lock_path = real_root / "scheduler" / "production_scheduler.lock"
    lease = FileSchedulerLease(lock_path, ttl_seconds=60, workspace_root=link)

    result = lease.acquire(pass_id="scheduler_2026092712_link", started_at=_STARTED_AT)
    try:
        assert result["acquired"] is True, result
        assert lock_path.is_file()
    finally:
        lease.release(pass_id="scheduler_2026092712_link")
