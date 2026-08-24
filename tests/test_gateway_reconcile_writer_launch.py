"""Rollback writer launch: ambient-environment sanitization, cross-
filesystem runtime snapshots, generation binding, and fail-closed gates.
"""

from __future__ import annotations

import json
import os
import threading
from datetime import (
    UTC,
    datetime,
)
from pathlib import Path
from typing import Any

import pytest

from tests.gateway_reconcile_helpers import _file_cohort_repository
from tests.gateway_reconcile_writer_helpers import (
    _round14_clean_writer_checkout,
    _round20_write_execution_binding,
)


def test_round18_launcher_binds_receipt_roots_lock_and_sanitizes_ambient_environment(
    tmp_path: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from services.orchestrator import file_orchestration_migration as migration_module
    from services.orchestrator.file_orchestration_journal import (
        FileOrchestrationJournalError,
        FileOrchestrationJournalRepository,
    )
    from services.orchestrator.file_orchestration_migration import (
        launch_file_journal_rollback_writer,
        prepare_file_journal_rollback,
    )

    root_a = tmp_path / "authority-a" / "journal"
    workspace_a = tmp_path / "authority-a" / "workspace"
    lock_a = workspace_a / "locks" / "rollback-production.lock"
    workspace_a.mkdir(parents=True)
    repository = FileOrchestrationJournalRepository(root_a)
    assert repository.query_inflight_jobs() == []
    checkout, generation = _round14_clean_writer_checkout(
        tmp_path / "writer-a",
        content="writer A\n",
    )
    receipt = prepare_file_journal_rollback(
        journal_root=root_a,
        workspace_root=workspace_a,
        lock_path=lock_a,
        scheduler_state="stopped",
        active_scheduler_processes=0,
        checked_at=datetime.now(UTC),
        checked_by="round18-operator",
        target_writer_generation=generation,
    )
    ambient_b = tmp_path / "ambient-b"
    monkeypatch.setenv("WORKSPACE_ROOT", str(ambient_b / "workspace"))
    monkeypatch.setenv("NHMS_SCHEDULER_JOURNAL_ROOT", str(ambient_b / "journal"))
    monkeypatch.setenv("NHMS_SCHEDULER_LOCK_BACKEND", "postgres")
    monkeypatch.setenv("NHMS_SCHEDULER_LOCK_ROOT", str(ambient_b / "locks"))
    monkeypatch.setenv("VIRTUAL_ENV", str(ambient_b / ".venv"))
    monkeypatch.setenv("PYTHONPATH", str(ambient_b / "pythonpath"))
    starts: list[dict[str, Any]] = []

    def runner(
        command: tuple[str, ...],
        *,
        cwd: Any,
        check: bool,
        env: Any,
        pass_fds: Any,
    ) -> Any:
        starts.append(
            {
                "command": command,
                "cwd": cwd,
                "check": check,
                "env": dict(env),
                "pass_fds": tuple(pass_fds),
            }
        )
        return type("Completed", (), {"returncode": 0})()

    monkeypatch.setattr(migration_module, "_run_rollback_writer", runner)
    for override in (
        ("--workspace-root", str(ambient_b / "workspace")),
        (f"--workspace-root={ambient_b / 'workspace'}",),
        ("--lock-path", str(ambient_b / "lock")),
        (f"--lock-path={ambient_b / 'lock'}",),
    ):
        with pytest.raises(
            FileOrchestrationJournalError,
            match="file_journal_rollback_writer_command_invalid",
        ):
            launch_file_journal_rollback_writer(
                journal_root=root_a,
                workspace_root=workspace_a,
                lock_path=lock_a,
                receipt_id=receipt["receipt_id"],
                writer_repository_root=checkout,
                writer_args=("plan-production", "--submit", *override),
            )
        assert starts == []

    launched = launch_file_journal_rollback_writer(
        journal_root=root_a,
        workspace_root=workspace_a,
        lock_path=lock_a,
        receipt_id=receipt["receipt_id"],
        writer_repository_root=checkout,
        writer_args=("plan-production", "--submit"),
    )
    assert launched["writer_exit_code"] == 0
    assert len(starts) == 1
    started = starts[0]
    child_env = started["env"]
    assert child_env["WORKSPACE_ROOT"] == str(workspace_a.resolve())
    assert child_env["NHMS_SCHEDULER_JOURNAL_ROOT"] == str(root_a.resolve())
    assert child_env["NHMS_SCHEDULER_LOCK_BACKEND"] == "file"
    assert child_env["NHMS_SCHEDULER_LOCK_ROOT"] == str(lock_a.parent.resolve())
    assert child_env["NHMS_SCHEDULER_DB_FREE_REQUIRED"] == "true"
    assert "VIRTUAL_ENV" not in child_env
    assert "PYTHONPATH" not in child_env
    assert str(ambient_b) not in json.dumps(started, default=str)
    assert started["command"][-4:] == (
        "--workspace-root",
        str(workspace_a.resolve()),
        "--lock-path",
        str(lock_a.resolve()),
    )
    assert len(started["pass_fds"]) == 1

    with pytest.raises(FileOrchestrationJournalError, match="file_journal_rollback_not_prepared"):
        launch_file_journal_rollback_writer(
            journal_root=root_a,
            workspace_root=ambient_b / "workspace",
            lock_path=ambient_b / "lock",
            receipt_id=receipt["receipt_id"],
            writer_repository_root=checkout,
            writer_args=("plan-production", "--submit"),
        )
    assert len(starts) == 1


def test_round18_open_runtime_snapshot_is_cross_filesystem_safe_after_path_replacement(
    tmp_path: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import hashlib
    import subprocess

    from services.orchestrator import file_orchestration_migration as migration_module

    source_runtime = tmp_path / "source" / "python"
    source_runtime.parent.mkdir()
    source_runtime.write_text('#!/bin/sh\nprintf A > "$1"\n', encoding="utf-8")
    source_runtime.chmod(0o700)
    original_digest = hashlib.sha256(source_runtime.read_bytes()).hexdigest()
    source_fd = os.open(
        source_runtime,
        os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0),
    )
    source_stat = os.fstat(source_fd)
    original_identity = source_stat.st_dev, source_stat.st_ino

    replacement_runtime = tmp_path / "replacement-python"
    replacement_runtime.write_text('#!/bin/sh\nprintf B > "$1"\n', encoding="utf-8")
    replacement_runtime.chmod(0o700)
    os.replace(replacement_runtime, source_runtime)
    bound_runtime = tmp_path / "other-filesystem-capable-bundle" / "bin" / "python"
    bound_runtime.parent.mkdir(parents=True)

    def hardlink_is_not_a_runtime_snapshot(*_args: Any, **_kwargs: Any) -> None:
        raise AssertionError("runtime snapshot must not depend on same-filesystem hardlinks")

    monkeypatch.setattr(migration_module.os, "link", hardlink_is_not_a_runtime_snapshot)
    try:
        migration_module._copy_open_runtime_snapshot(
            source_fd=source_fd,
            bound_path=bound_runtime,
            runtime_identity=original_identity,
        )
    finally:
        os.close(source_fd)

    assert hashlib.sha256(bound_runtime.read_bytes()).hexdigest() == original_digest
    assert hashlib.sha256(source_runtime.read_bytes()).hexdigest() != original_digest
    assert (bound_runtime.stat().st_dev, bound_runtime.stat().st_ino) != original_identity
    execution_marker = tmp_path / "snapshot-executed"
    subprocess.run((str(bound_runtime), str(execution_marker)), check=True)
    assert execution_marker.read_text(encoding="utf-8") == "A"


def test_round16_commit_snapshot_closes_post_check_worktree_switch(
    tmp_path: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import hashlib
    import subprocess
    from pathlib import Path

    from services.orchestrator import file_orchestration_migration as migration_module
    from services.orchestrator.file_orchestration_journal import FileOrchestrationJournalRepository
    from services.orchestrator.file_orchestration_migration import (
        launch_file_journal_rollback_writer,
        prepare_file_journal_rollback,
    )

    checkout, _initial_generation = _round14_clean_writer_checkout(
        tmp_path / "writer",
        content="A\n",
    )
    cli_path = checkout / "services" / "orchestrator" / "cli.py"
    cli_path.parent.mkdir(parents=True)
    (checkout / "services" / "__init__.py").write_text("", encoding="utf-8")
    (cli_path.parent / "__init__.py").write_text("", encoding="utf-8")
    cli_path.write_text(
        "from pathlib import Path\n"
        "import sys\n"
        "arguments = sys.argv[1:]\n"
        "assert arguments[0] == 'plan-production'\n"
        "assert '--submit' in arguments\n"
        "evidence = Path(arguments[arguments.index('--evidence-dir') + 1])\n"
        "evidence.mkdir(parents=True, exist_ok=True)\n"
        "(evidence / 'writer-generation.txt').write_text("
        "(Path.cwd() / 'writer.txt').read_text(encoding='utf-8'), encoding='utf-8')\n",
        encoding="utf-8",
    )
    subprocess.run(("git", "add", "services"), cwd=checkout, check=True)
    subprocess.run(
        (
            "git",
            "-c",
            "user.name=Round16 Test",
            "-c",
            "user.email=round16@example.invalid",
            "commit",
            "-q",
            "-m",
            "writer A executable",
        ),
        cwd=checkout,
        check=True,
    )
    generation_a = subprocess.run(
        ("git", "rev-parse", "HEAD"),
        cwd=checkout,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    (checkout / "writer.txt").write_text("B\n", encoding="utf-8")
    subprocess.run(("git", "add", "writer.txt"), cwd=checkout, check=True)
    subprocess.run(
        (
            "git",
            "-c",
            "user.name=Round16 Test",
            "-c",
            "user.email=round16@example.invalid",
            "commit",
            "-q",
            "-m",
            "writer B",
        ),
        cwd=checkout,
        check=True,
    )
    generation_b = subprocess.run(
        ("git", "rev-parse", "HEAD"),
        cwd=checkout,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    subprocess.run(("git", "checkout", "--detach", "--quiet", generation_a), cwd=checkout, check=True)

    root = tmp_path / "journal"
    workspace = tmp_path / "workspace"
    evidence = tmp_path / "evidence"
    workspace.mkdir()
    repository = FileOrchestrationJournalRepository(root)
    assert repository.query_inflight_jobs() == []
    receipt = prepare_file_journal_rollback(
        journal_root=root,
        workspace_root=workspace,
        scheduler_state="stopped",
        active_scheduler_processes=0,
        checked_at=datetime.now(UTC),
        checked_by="round16-operator",
        target_writer_generation=generation_a,
    )

    real_runner = migration_module._run_rollback_writer
    at_child_boundary = threading.Event()
    continue_child = threading.Event()
    snapshot_paths: list[Any] = []

    def barrier_runner(
        command: tuple[str, ...],
        *,
        cwd: Any,
        check: bool,
        env: Any,
        pass_fds: Any,
    ) -> Any:
        snapshot_paths.append(cwd)
        at_child_boundary.set()
        assert continue_child.wait(timeout=5)
        return real_runner(
            command,
            cwd=cwd,
            check=check,
            env=env,
            pass_fds=pass_fds,
        )

    monkeypatch.setattr(migration_module, "_run_rollback_writer", barrier_runner)

    def hardlink_is_not_a_runtime_snapshot(*_args: Any, **_kwargs: Any) -> None:
        raise AssertionError("runtime snapshot must not depend on same-filesystem hardlinks")

    monkeypatch.setattr(migration_module.os, "link", hardlink_is_not_a_runtime_snapshot)
    outcome: dict[str, Any] = {}

    def launch() -> None:
        outcome.update(
            launch_file_journal_rollback_writer(
                journal_root=root,
                workspace_root=workspace,
                receipt_id=receipt["receipt_id"],
                writer_repository_root=checkout,
                writer_args=(
                    "plan-production",
                    "--submit",
                    "--evidence-dir",
                    str(evidence),
                ),
            )
        )

    thread = threading.Thread(target=launch)
    target_runtime = checkout / ".venv" / "bin" / "python"
    resolved_runtime = target_runtime.resolve(strict=True)
    original_runtime_identity = resolved_runtime.stat().st_dev, resolved_runtime.stat().st_ino
    original_runtime_digest = hashlib.sha256(resolved_runtime.read_bytes()).hexdigest()
    thread.start()
    assert at_child_boundary.wait(timeout=10)
    replacement_runtime = tmp_path / "replacement-runtime-b"
    replacement_marker = tmp_path / "replacement-runtime-was-executed"
    replacement_runtime.write_text(
        f"#!/bin/sh\necho B > {replacement_marker}\nexit 91\n",
        encoding="utf-8",
    )
    replacement_runtime.chmod(0o700)
    os.replace(replacement_runtime, resolved_runtime)
    subprocess.run(("git", "checkout", "--detach", "--quiet", generation_b), cwd=checkout, check=True)
    continue_child.set()
    thread.join(timeout=10)
    assert not thread.is_alive()
    assert outcome["writer_exit_code"] == 0
    assert outcome["actual_writer_generation"] == generation_a
    assert (evidence / "writer-generation.txt").read_text(encoding="utf-8") == "A\n"
    assert (checkout / "writer.txt").read_text(encoding="utf-8") == "B\n"
    assert not replacement_marker.exists()
    assert snapshot_paths == [Path(outcome["target_python_source_root"])]
    assert all(path.is_dir() for path in snapshot_paths)
    retained_runtime = Path(outcome["target_python_runtime"])
    assert retained_runtime.exists()
    assert (retained_runtime.stat().st_dev, retained_runtime.stat().st_ino) != original_runtime_identity
    assert hashlib.sha256(retained_runtime.read_bytes()).hexdigest() == original_runtime_digest
    assert hashlib.sha256(resolved_runtime.read_bytes()).hexdigest() != original_runtime_digest
    assert (resolved_runtime.stat().st_dev, resolved_runtime.stat().st_ino) != original_runtime_identity
    assert outcome["target_python_runtime_retention"] == ("retained_fail_closed_until_operator_cleanup")


def test_round18_rollforward_is_excluded_while_old_writer_execution_is_active(
    tmp_path: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import shutil

    from packages.common.rollback_execution_binding import read_rollback_execution_binding
    from services.orchestrator import file_orchestration_migration as migration_module
    from services.orchestrator.file_orchestration_journal import (
        FileOrchestrationJournalError,
        FileOrchestrationJournalRepository,
    )
    from services.orchestrator.file_orchestration_migration import (
        complete_file_journal_rollforward,
        launch_file_journal_rollback_writer,
        prepare_file_journal_rollback,
    )

    root = tmp_path / "journal"
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    repository = FileOrchestrationJournalRepository(root)
    assert repository.query_inflight_jobs() == []
    checkout, generation = _round14_clean_writer_checkout(
        tmp_path / "writer",
        content="writer A\n",
    )
    receipt = prepare_file_journal_rollback(
        journal_root=root,
        workspace_root=workspace,
        scheduler_state="stopped",
        active_scheduler_processes=0,
        checked_at=datetime.now(UTC),
        checked_by="round18-operator",
        target_writer_generation=generation,
    )
    prepared_authority = read_rollback_execution_binding(
        workspace,
        required=True,
        require_artifacts=False,
    )
    assert prepared_authority is not None
    assert prepared_authority["status"] == "prepared"
    template = _file_cohort_repository(
        tmp_path / "template",
        member_count=1,
        with_runtime_rows=False,
    )
    job_id = "job_cycle_gfs_2026071200_forecast_fixture_forecast"
    source_job = template.root / "pipeline-jobs" / f"{job_id}.json"
    entered = threading.Event()
    continue_writer = threading.Event()

    def blocked_writer(
        _command: tuple[str, ...],
        *,
        cwd: Any,
        check: bool,
        env: Any,
        pass_fds: Any,
    ) -> Any:
        del cwd, check, env
        assert len(pass_fds) == 1
        entered.set()
        assert continue_writer.wait(timeout=5)
        destination = root / "pipeline-jobs" / source_job.name
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source_job, destination)
        return type("Completed", (), {"returncode": 0})()

    monkeypatch.setattr(migration_module, "_run_rollback_writer", blocked_writer)
    outcome: dict[str, Any] = {}

    def launch() -> None:
        outcome.update(
            launch_file_journal_rollback_writer(
                journal_root=root,
                workspace_root=workspace,
                receipt_id=receipt["receipt_id"],
                writer_repository_root=checkout,
                writer_args=("plan-production", "--submit"),
            )
        )

    thread = threading.Thread(target=launch)
    thread.start()
    assert entered.wait(timeout=10)
    active_authority = read_rollback_execution_binding(workspace, required=True)
    assert active_authority is not None
    assert active_authority["status"] == "active"
    assert active_authority["binding_id"] != prepared_authority["binding_id"]
    with pytest.raises(
        FileOrchestrationJournalError,
        match="file_journal_rollback_execution_active",
    ):
        complete_file_journal_rollforward(
            journal_root=root,
            workspace_root=workspace,
            preparation_receipt_id=receipt["receipt_id"],
        )
    assert not (root / "reconcile-inventory" / f"{job_id}.json").exists()
    continue_writer.set()
    thread.join(timeout=10)
    assert not thread.is_alive()
    assert outcome["writer_exit_code"] == 0

    with pytest.raises(
        FileOrchestrationJournalError,
        match="file_journal_rollforward_jobs_unsettled",
    ):
        complete_file_journal_rollforward(
            journal_root=root,
            workspace_root=workspace,
            preparation_receipt_id=receipt["receipt_id"],
        )
    assert (root / "pipeline-jobs" / source_job.name).is_file()
    (root / "pipeline-jobs" / source_job.name).unlink()

    completed = complete_file_journal_rollforward(
        journal_root=root,
        workspace_root=workspace,
        preparation_receipt_id=receipt["receipt_id"],
    )
    assert completed["preparation_receipt_id"] == receipt["receipt_id"]
    assert completed["rollback_execution_binding_status"] == "completed"
    assert not (root / "reconcile-inventory" / f"{job_id}.json").exists()
    reopened = FileOrchestrationJournalRepository(root)
    assert reopened.query_reserved_unbound_jobs() == []


def test_round14_prepare_resumes_after_receipt_write_before_marker_unlink(
    tmp_path: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:

    from services.orchestrator import file_orchestration_journal as journal_module
    from services.orchestrator.file_orchestration_journal import (
        FileOrchestrationJournalError,
        FileOrchestrationJournalRepository,
    )
    from services.orchestrator.file_orchestration_migration import (
        complete_file_journal_rollforward,
        prepare_file_journal_rollback,
        require_file_journal_rollback_prepared,
    )

    root = tmp_path / "journal"
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    repository = FileOrchestrationJournalRepository(root)
    assert repository.query_inflight_jobs() == []
    marker = root / "reconcile-inventory-migration-v1.json"
    receipt_path = root / "reconcile-inventory-rollback-preparation-v2.json"
    real_unlink = journal_module.unlink_no_follow
    crashed = False

    def crash_before_marker_unlink(path: Any, **kwargs: Any) -> None:
        nonlocal crashed
        if Path(path) == marker and not crashed:
            crashed = True
            raise OSError("injected crash after preparing receipt")
        real_unlink(path, **kwargs)

    monkeypatch.setattr(journal_module, "unlink_no_follow", crash_before_marker_unlink)
    with pytest.raises(
        FileOrchestrationJournalError,
        match="file_journal_rollback_preparation_unavailable",
    ):
        prepare_file_journal_rollback(
            journal_root=root,
            workspace_root=workspace,
            scheduler_state="stopped",
            active_scheduler_processes=0,
            checked_at=datetime.now(UTC),
            checked_by="round14-first-operator",
            target_writer_generation="a" * 40,
        )
    preparing = json.loads(receipt_path.read_text(encoding="utf-8"))
    assert preparing["status"] == "preparing"
    assert marker.is_file()

    monkeypatch.setattr(journal_module, "unlink_no_follow", real_unlink)
    with pytest.raises(
        FileOrchestrationJournalError,
        match="file_journal_rollback_fence_conflict",
    ):
        prepare_file_journal_rollback(
            journal_root=root,
            workspace_root=workspace,
            scheduler_state="stopped",
            active_scheduler_processes=0,
            checked_at=datetime.now(UTC),
            checked_by="round14-wrong-generation",
            target_writer_generation="b" * 40,
        )
    assert marker.is_file()
    assert json.loads(receipt_path.read_text(encoding="utf-8")) == preparing
    prepared = prepare_file_journal_rollback(
        journal_root=root,
        workspace_root=workspace,
        scheduler_state="stopped",
        active_scheduler_processes=0,
        checked_at=datetime.now(UTC),
        checked_by="round14-retry-operator",
        target_writer_generation="a" * 40,
    )
    assert prepared["status"] == "prepared"
    assert prepared["receipt_id"] == preparing["receipt_id"]
    assert not marker.exists()
    assert prepare_file_journal_rollback(
        journal_root=root,
        workspace_root=workspace,
        scheduler_state="stopped",
        active_scheduler_processes=0,
        checked_at=datetime.now(UTC),
        checked_by="round14-idempotent-retry",
        target_writer_generation="a" * 40,
    ) == prepared
    assert require_file_journal_rollback_prepared(
        journal_root=root,
        workspace_root=workspace,
        receipt_id=prepared["receipt_id"],
        actual_writer_generation="a" * 40,
    )["status"] == "prepared"
    _round20_write_execution_binding(
        workspace=workspace,
        receipt=prepared,
        generation="a" * 40,
    )

    rollforward = complete_file_journal_rollforward(
        journal_root=root,
        workspace_root=workspace,
        preparation_receipt_id=prepared["receipt_id"],
    )
    assert rollforward["preparation_receipt_id"] == prepared["receipt_id"]
    assert marker.is_file()
    assert not receipt_path.exists()


def test_round14_writer_launch_is_strictly_generation_bound_and_fail_closed(
    tmp_path: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import shutil
    import subprocess
    import sys

    from packages.common.python_runtime import validated_target_python_runtime
    from packages.common.rollback_execution_binding import (
        read_rollback_execution_binding,
        write_rollback_execution_binding,
    )
    from services.orchestrator import file_orchestration_migration as migration_module
    from services.orchestrator.file_orchestration_journal import (
        FileOrchestrationJournalError,
        FileOrchestrationJournalRepository,
    )
    from services.orchestrator.file_orchestration_migration import (
        launch_file_journal_rollback_writer,
        prepare_file_journal_rollback,
    )

    root = tmp_path / "journal"
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    repository = FileOrchestrationJournalRepository(root)
    assert repository.query_inflight_jobs() == []
    checkout_a, generation_a = _round14_clean_writer_checkout(
        tmp_path / "writer-a",
        content="writer A\n",
    )
    checkout_b, _generation_b = _round14_clean_writer_checkout(
        tmp_path / "writer-b",
        content="writer B\n",
    )
    venv_python_root = (
        checkout_a
        / ".venv"
        / "lib"
        / f"python{sys.version_info.major}.{sys.version_info.minor}"
    )
    site_packages = venv_python_root / "site-packages"
    site_packages.mkdir(parents=True)
    site_module = site_packages / "rollback_site_sentinel.py"
    site_module.write_text("VALUE = 'retained-site-package'\n", encoding="utf-8")
    receipt = prepare_file_journal_rollback(
        journal_root=root,
        workspace_root=workspace,
        scheduler_state="stopped",
        active_scheduler_processes=0,
        checked_at=datetime.now(UTC),
        checked_by="round14-operator",
        target_writer_generation=generation_a,
    )
    starts: list[tuple[tuple[str, ...], Any, str, Any]] = []

    def runner(argv: tuple[str, ...], *, cwd: Any, check: bool, env: Any, pass_fds: Any) -> Any:
        assert check is False
        assert len(pass_fds) == 1
        starts.append((argv, cwd, (cwd / "writer.txt").read_text(encoding="utf-8"), env))
        return type("Completed", (), {"returncode": 0})()

    monkeypatch.setattr(migration_module, "_run_rollback_writer", runner)

    launched = launch_file_journal_rollback_writer(
        journal_root=root,
        workspace_root=workspace,
        receipt_id=receipt["receipt_id"],
        writer_repository_root=checkout_a,
        writer_args=("plan-production", "--submit"),
    )
    assert launched["writer_exit_code"] == 0
    assert launched["actual_writer_generation"] == generation_a
    assert launched["writer_repository_root"] == str(checkout_a.resolve())
    assert len(starts) == 1
    command, cwd, snapshot_content, child_env = starts[0]
    assert Path(command[0]).name == "python"
    assert Path(command[0]).parent.name == "bin"
    retained_generation = (
        workspace
        / ".nhms-rollback-execution-v1"
        / f"{receipt['receipt_id']}-{generation_a}"
    )
    assert Path(command[0]).parent.parent == retained_generation / "runtime"
    assert command[1:] == (
        "-m",
        "services.orchestrator.cli",
        "plan-production",
        "--submit",
        "--workspace-root",
        str(workspace.resolve()),
        "--lock-path",
        str((workspace / "scheduler" / "production-scheduler.lock").resolve()),
    )
    assert child_env["NHMS_TARGET_PYTHON_RUNTIME"] == command[0]
    assert launched["target_python_runtime"] == command[0]
    assert Path(launched["target_python_runtime"]).is_file()
    assert launched["target_python_runtime_retention"] == ("retained_fail_closed_until_operator_cleanup")
    assert validated_target_python_runtime(
        launched["target_python_runtime"],
        required=True,
    ) == launched["target_python_runtime"]
    source_runtime = (checkout_a / ".venv" / "bin" / "python").resolve(strict=True)
    assert Path(command[0]).stat().st_ino != source_runtime.stat().st_ino
    assert Path(command[0]).read_bytes() == source_runtime.read_bytes()
    assert cwd != checkout_a.resolve()
    assert snapshot_content == "writer A\n"
    assert cwd == Path(launched["target_python_source_root"])
    assert cwd.is_dir()
    retained_root = retained_generation / "runtime"
    assert not any(path.is_symlink() for path in retained_root.rglob("*"))
    retained_config = (retained_root / "pyvenv.cfg").read_text(encoding="utf-8")
    assert str(checkout_a / ".target-python") not in retained_config
    assert f"home = {retained_root / 'bin'}" in retained_config
    (checkout_a / ".venv" / "pyvenv.cfg").unlink()
    (checkout_a / ".target-python" / "lib").unlink()
    site_module.unlink()
    site_packages.rmdir()
    venv_python_root.rmdir()
    (checkout_a / ".venv" / "lib").rmdir()
    runtime_probe = subprocess.run(
        (
            str(Path(command[0])),
            "-c",
            "import _json, json, ssl, sys, rollback_site_sentinel; "
            "print(json.dumps({'site': rollback_site_sentinel.VALUE, "
            "'prefix': sys.prefix, 'base_prefix': sys.base_prefix, "
            "'extension': _json.__name__, 'ssl': ssl.__name__}))",
        ),
        check=True,
        capture_output=True,
        text=True,
    )
    runtime_identity = json.loads(runtime_probe.stdout)
    assert runtime_identity == {
        "site": "retained-site-package",
        "prefix": str(retained_root),
        "base_prefix": str(retained_root),
        "extension": "_json",
        "ssl": "ssl",
    }

    real_resolver = migration_module._resolve_clean_writer_generation
    resolution_calls = 0

    def generation_changes_after_gate(repository_root: Any) -> tuple[Any, str]:
        nonlocal resolution_calls
        resolution_calls += 1
        resolved_root, resolved_generation = real_resolver(repository_root)
        if resolution_calls == 2:
            return resolved_root, "f" * len(resolved_generation)
        return resolved_root, resolved_generation

    monkeypatch.setattr(
        migration_module,
        "_resolve_clean_writer_generation",
        generation_changes_after_gate,
    )
    relaunched = launch_file_journal_rollback_writer(
        journal_root=root,
        workspace_root=workspace,
        receipt_id=receipt["receipt_id"],
        writer_repository_root=checkout_a,
        writer_args=("plan-production", "--submit"),
    )
    assert relaunched["rollback_execution_binding_id"] == launched["rollback_execution_binding_id"]
    assert relaunched["target_python_source_root"] == launched["target_python_source_root"]
    assert len(starts) == 2
    monkeypatch.setattr(
        migration_module,
        "_resolve_clean_writer_generation",
        real_resolver,
    )

    with pytest.raises(FileOrchestrationJournalError, match="file_journal_rollback_not_prepared"):
        launch_file_journal_rollback_writer(
            journal_root=root,
            workspace_root=workspace,
            receipt_id=receipt["receipt_id"],
            writer_repository_root=checkout_b,
            writer_args=("plan-production", "--submit"),
        )
    assert len(starts) == 2
    (checkout_a / "writer.txt").write_text("writer A dirty\n", encoding="utf-8")
    with pytest.raises(
        FileOrchestrationJournalError,
        match="file_journal_rollback_writer_generation_dirty",
    ):
        launch_file_journal_rollback_writer(
            journal_root=root,
            workspace_root=workspace,
            receipt_id=receipt["receipt_id"],
            writer_repository_root=checkout_a,
            writer_args=("plan-production", "--submit"),
        )
    (checkout_a / "writer.txt").write_text("writer A\n", encoding="utf-8")
    (checkout_a / "untracked.txt").write_text("untracked\n", encoding="utf-8")
    with pytest.raises(
        FileOrchestrationJournalError,
        match="file_journal_rollback_writer_generation_dirty",
    ):
        launch_file_journal_rollback_writer(
            journal_root=root,
            workspace_root=workspace,
            receipt_id=receipt["receipt_id"],
            writer_repository_root=checkout_a,
            writer_args=("plan-production", "--submit"),
        )
    unresolvable = tmp_path / "not-a-repository"
    unresolvable.mkdir()
    for repository_root in (unresolvable, tmp_path / "missing-repository"):
        with pytest.raises(FileOrchestrationJournalError):
            launch_file_journal_rollback_writer(
                journal_root=root,
                workspace_root=workspace,
                receipt_id=receipt["receipt_id"],
                writer_repository_root=repository_root,
                writer_args=("plan-production", "--submit"),
            )
    assert len(starts) == 2
    (checkout_a / "untracked.txt").unlink()

    rolling_forward = read_rollback_execution_binding(workspace, required=True)
    assert rolling_forward is not None
    write_rollback_execution_binding(
        workspace,
        {**rolling_forward, "status": "rolling_forward"},
    )
    with pytest.raises(
        FileOrchestrationJournalError,
        match="file_journal_rollback_execution_binding_conflict",
    ):
        launch_file_journal_rollback_writer(
            journal_root=root,
            workspace_root=workspace,
            receipt_id=receipt["receipt_id"],
            writer_repository_root=checkout_a,
            writer_args=("plan-production", "--submit"),
        )
    assert len(starts) == 2
    shutil.rmtree(checkout_a)
    assert not checkout_a.exists()
    retained_binding = read_rollback_execution_binding(workspace, required=True)
    assert retained_binding is not None
    assert Path(retained_binding["target_python_source_root"]) == retained_generation / "source"
    assert Path(retained_binding["target_python_runtime"]) == retained_generation / "runtime/bin/python"
    post_checkout_removal = subprocess.run(
        (
            retained_binding["target_python_runtime"],
            "-c",
            "from pathlib import Path; print(Path('writer.txt').read_text().strip())",
        ),
        cwd=retained_binding["target_python_source_root"],
        check=True,
        capture_output=True,
        text=True,
    )
    assert post_checkout_removal.stdout.strip() == "writer A"
