"""Writer/barrier utilities shared by the split gateway-reconcile suites (#1809).

Not a collectible test module; owns the rollback-writer checkout/binding
fixtures used by the five writer partitions and the transactional
thread-launch barrier helpers used by the idempotency barrier partition.
"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable
from typing import Any


def _join_all_deadline(threads: list[threading.Thread], *, deadline: float) -> None:
    """Join every thread under one absolute deadline, never a full timeout each.

    A per-thread ``join(timeout)`` multiplies the parent's wait by the peer
    count, so a stranding peer could leave the parent waiting many times longer
    than the barrier bound (task 5.2). The normal path uses the same single
    deadline as the partial-launch cleanup path.
    """

    for thread in threads:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break
        thread.join(timeout=remaining)


def _start_attempt_threads(
    worker: Callable[[int], None],
    parties: int,
    barrier: threading.Barrier,
    *,
    join_timeout: float,
) -> list[threading.Thread]:
    """Start ``parties`` explicit peers transactionally and join them under one deadline.

    The 8-party gateway harness's transactional launch seam (task 5.2): only
    successfully started threads are tracked. If a later ``Thread.start()``
    raises after a subset is running, the Barrier is aborted so the running
    peers leave ``Barrier.wait()``, every tracked peer is joined against one
    absolute cleanup deadline, and the ORIGINAL launch cause is re-raised.
    Returns the successfully started threads (already joined on the normal
    path), so the caller's liveness assertion stays on the real peer set.
    """

    started: list[threading.Thread] = []
    try:
        for index in range(parties):
            thread = threading.Thread(target=worker, args=(index,))
            thread.start()
            started.append(thread)
    except BaseException as launch_error:  # pragma: no cover - asserted below
        barrier.abort()
        _join_all_deadline(started, deadline=time.monotonic() + join_timeout)
        raise launch_error

    _join_all_deadline(started, deadline=time.monotonic() + join_timeout)
    return started


def _round14_clean_writer_checkout(root: Any, *, content: str) -> tuple[Any, str]:
    import shutil
    import subprocess
    import sys
    from pathlib import Path

    checkout = Path(root)
    checkout.mkdir(parents=True)
    (checkout / ".gitignore").write_text(".venv/\n.target-python/\n", encoding="utf-8")
    (checkout / "writer.txt").write_text(content, encoding="utf-8")
    private_runtime = checkout / ".target-python" / "bin" / "python"
    private_runtime.parent.mkdir(parents=True)
    shutil.copy2(sys._base_executable, private_runtime)
    base_lib = Path(sys._base_executable).parent.parent / "lib"
    if base_lib.is_dir():
        (checkout / ".target-python" / "lib").symlink_to(base_lib)
    runtime = checkout / ".venv" / "bin" / "python"
    runtime.parent.mkdir(parents=True)
    runtime.symlink_to(private_runtime)
    (checkout / ".venv" / "pyvenv.cfg").write_text(
        f"home = {private_runtime.parent}\n"
        "include-system-site-packages = false\n"
        f"version = {sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}\n"
        f"executable = {private_runtime}\n",
        encoding="utf-8",
    )
    subprocess.run(("git", "init", "-q"), cwd=checkout, check=True)
    subprocess.run(("git", "add", ".gitignore", "writer.txt"), cwd=checkout, check=True)
    subprocess.run(
        (
            "git",
            "-c",
            "user.name=Round14 Test",
            "-c",
            "user.email=round14@example.invalid",
            "commit",
            "-q",
            "-m",
            "writer fixture",
        ),
        cwd=checkout,
        check=True,
    )
    generation = subprocess.run(
        ("git", "rev-parse", "HEAD"),
        cwd=checkout,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    return checkout, generation


def _round20_write_execution_binding(
    *,
    workspace: Any,
    receipt: dict[str, Any],
    generation: str,
    lock_path: Any | None = None,
) -> dict[str, Any]:
    from pathlib import Path

    from packages.common.rollback_execution_binding import (
        ROLLBACK_EXECUTION_BINDING_SCHEMA_VERSION,
        binding_id_for,
        rollback_execution_artifact_root,
        write_rollback_execution_binding,
    )

    workspace_root = Path(workspace).resolve()
    assets = rollback_execution_artifact_root(
        workspace_root,
        receipt["receipt_id"],
        generation,
    )
    assets.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    assets.parent.chmod(0o700)
    source_root = assets / "source"
    source_root.mkdir(parents=True)
    source_root.chmod(0o500)
    runtime = assets / "runtime" / "bin" / "python"
    runtime.parent.mkdir(parents=True)
    runtime.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    runtime.chmod(0o500)
    runtime.parent.chmod(0o500)
    runtime.parent.parent.chmod(0o500)
    assets.chmod(0o500)
    configured_lock = Path(lock_path or workspace_root / "scheduler" / "production-scheduler.lock").resolve()
    binding: dict[str, Any] = {
        "schema_version": ROLLBACK_EXECUTION_BINDING_SCHEMA_VERSION,
        "binding_id": "",
        "status": "active",
        "preparation_receipt_id": receipt["receipt_id"],
        "journal_root_identity": dict(receipt["journal_root_identity"]),
        "scheduler_lease_identity": dict(receipt["scheduler_lease_identity"]),
        "workspace_root": str(workspace_root),
        "lock_path": str(configured_lock),
        "target_writer_generation": generation,
        "target_python_runtime": str(runtime),
        "target_python_source_root": str(source_root),
        "writer_repository_root": str(assets),
        "created_at": receipt["prepared_at"],
        "updated_at": receipt["prepared_at"],
    }
    binding["binding_id"] = binding_id_for(binding)
    return write_rollback_execution_binding(workspace_root, binding)
