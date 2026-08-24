"""Rollback writer rollforward: binding cuts, crash resume, recursive
source sealing, and sequential lifecycle archival.
"""

from __future__ import annotations

import json
import os
from datetime import (
    UTC,
    datetime,
)
from pathlib import Path
from typing import Any

import pytest

from services.orchestrator.reconcile import (
    SacctRecord,
    reconcile_inflight_jobs,
)
from tests.gateway_reconcile_helpers import (
    _bind_current_file_cohort,
    _file_cohort_repository,
)
from tests.gateway_reconcile_writer_helpers import (
    _round14_clean_writer_checkout,
    _round20_write_execution_binding,
)


def test_round20_prepare_retries_same_receipt_after_authority_publication_crash(
    tmp_path: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from packages.common.rollback_execution_binding import (
        RollbackExecutionBindingError,
        read_rollback_execution_binding,
        rollback_execution_binding_path,
    )
    from services.orchestrator import file_orchestration_migration as migration_module
    from services.orchestrator.file_orchestration_journal import (
        FileOrchestrationJournalError,
        FileOrchestrationJournalRepository,
    )
    from services.orchestrator.file_orchestration_migration import prepare_file_journal_rollback

    root = tmp_path / "journal"
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    repository = FileOrchestrationJournalRepository(root)
    assert repository.query_inflight_jobs() == []
    real_write_binding = migration_module.write_rollback_execution_binding

    def crash_before_authority_publication(_workspace_root: Any, _binding: Any) -> dict[str, Any]:
        raise RollbackExecutionBindingError("injected prepared authority publication crash")

    monkeypatch.setattr(
        migration_module,
        "write_rollback_execution_binding",
        crash_before_authority_publication,
    )
    with pytest.raises(
        FileOrchestrationJournalError,
        match="file_journal_rollback_preparation_authority_unavailable",
    ):
        prepare_file_journal_rollback(
            journal_root=root,
            workspace_root=workspace,
            scheduler_state="stopped",
            active_scheduler_processes=0,
            checked_at=datetime.now(UTC),
            checked_by="round20-prepared-authority-crash",
            target_writer_generation="a" * 40,
        )
    fence = root / "reconcile-inventory-rollback-preparation-v2.json"
    persisted_receipt = json.loads(fence.read_text(encoding="utf-8"))
    assert persisted_receipt["status"] == "prepared"
    assert not rollback_execution_binding_path(workspace).exists()

    monkeypatch.setattr(
        migration_module,
        "write_rollback_execution_binding",
        real_write_binding,
    )
    retried = prepare_file_journal_rollback(
        journal_root=root,
        workspace_root=workspace,
        scheduler_state="stopped",
        active_scheduler_processes=0,
        checked_at=datetime.now(UTC),
        checked_by="round20-prepared-authority-retry",
        target_writer_generation="a" * 40,
    )
    assert retried["receipt_id"] == persisted_receipt["receipt_id"]
    authority = read_rollback_execution_binding(workspace, required=True, require_artifacts=False)
    assert authority is not None
    assert authority["status"] == "prepared"
    assert authority["preparation_receipt_id"] == retried["receipt_id"]


def test_round20_rollforward_resumes_after_crash_past_binding_cut(
    tmp_path: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from packages.common.rollback_execution_binding import read_rollback_execution_binding
    from services.orchestrator.file_orchestration_journal import FileOrchestrationJournalRepository
    from services.orchestrator.file_orchestration_migration import (
        complete_file_journal_rollforward,
        prepare_file_journal_rollback,
    )

    root = tmp_path / "journal"
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    repository = FileOrchestrationJournalRepository(root)
    assert repository.query_inflight_jobs() == []
    receipt = prepare_file_journal_rollback(
        journal_root=root,
        workspace_root=workspace,
        scheduler_state="stopped",
        active_scheduler_processes=0,
        checked_at=datetime.now(UTC),
        checked_by="round20-crash-resume-operator",
        target_writer_generation="a" * 40,
    )
    _round20_write_execution_binding(
        workspace=workspace,
        receipt=receipt,
        generation="a" * 40,
    )
    fence = root / "reconcile-inventory-rollback-preparation-v2.json"
    fence_before = fence.read_bytes()
    real_complete = (
        FileOrchestrationJournalRepository._complete_reconcile_inventory_rollforward_under_scheduler_lease
    )

    def crash_after_binding_cut(_self: Any, **_kwargs: Any) -> dict[str, Any]:
        raise RuntimeError("injected crash after rolling_forward binding cut")

    monkeypatch.setattr(
        FileOrchestrationJournalRepository,
        "_complete_reconcile_inventory_rollforward_under_scheduler_lease",
        crash_after_binding_cut,
    )
    with pytest.raises(RuntimeError, match="injected crash"):
        complete_file_journal_rollforward(
            journal_root=root,
            workspace_root=workspace,
            preparation_receipt_id=receipt["receipt_id"],
        )
    cut_binding = read_rollback_execution_binding(workspace, require_artifacts=False)
    assert cut_binding is not None and cut_binding["status"] == "rolling_forward"
    assert fence.read_bytes() == fence_before

    monkeypatch.setattr(
        FileOrchestrationJournalRepository,
        "_complete_reconcile_inventory_rollforward_under_scheduler_lease",
        real_complete,
    )

    def stale_query_must_not_reopen_cut(_self: Any) -> list[Any]:
        raise AssertionError("rolling_forward resume must not repeat the active-state query")

    monkeypatch.setattr(
        FileOrchestrationJournalRepository,
        "query_rollback_unsettled_jobs",
        stale_query_must_not_reopen_cut,
    )
    completed = complete_file_journal_rollforward(
        journal_root=root,
        workspace_root=workspace,
        preparation_receipt_id=receipt["receipt_id"],
    )

    assert completed["rollback_execution_binding_status"] == "completed"
    final_binding = read_rollback_execution_binding(workspace, require_artifacts=False)
    assert final_binding is not None and final_binding["status"] == "completed"
    assert not fence.exists()


def test_round20_rollforward_rejects_partial_terminal_cohort_without_mutation(
    tmp_path: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import shutil

    from packages.common.rollback_execution_binding import read_rollback_execution_binding
    from services.orchestrator.file_orchestration_journal import (
        FileOrchestrationJournalError,
        FileOrchestrationJournalRepository,
    )
    from services.orchestrator.file_orchestration_migration import (
        complete_file_journal_rollforward,
        prepare_file_journal_rollback,
    )

    root = tmp_path / "journal"
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    repository = FileOrchestrationJournalRepository(root)
    assert repository.query_inflight_jobs() == []
    receipt = prepare_file_journal_rollback(
        journal_root=root,
        workspace_root=workspace,
        scheduler_state="stopped",
        active_scheduler_processes=0,
        checked_at=datetime.now(UTC),
        checked_by="round20-partial-terminal-operator",
        target_writer_generation="a" * 40,
    )
    _round20_write_execution_binding(
        workspace=workspace,
        receipt=receipt,
        generation="a" * 40,
    )

    template = _file_cohort_repository(tmp_path / "template")
    key = "cycle_gfs_2026071200_forecast_fixture:forecast"
    job_id = "job_cycle_gfs_2026071200_forecast_fixture_forecast"
    _bind_current_file_cohort(template, key, slurm_job_id="17667")
    tasks = tuple(
        SacctRecord(
            f"17667_{index}",
            "COMPLETED",
            "nhms_forecast",
            comment=f"nhms_idem:{key}",
            array_task_id=index,
        )
        for index in range(17)
    )
    partial = SacctRecord(
        "17667",
        "COMPLETED",
        "nhms_forecast",
        comment=f"nhms_idem:{key}",
        array_member_job_ids=tuple(task.slurm_job_id for task in tasks),
        array_task_records=tasks,
    )
    outcome = reconcile_inflight_jobs(template, sacct_query=lambda _job_id: partial)[0]
    assert outcome.action == "task_accounting_incomplete"
    source_job = template.root / "pipeline-jobs" / f"{job_id}.json"
    destination = root / "pipeline-jobs" / source_job.name
    destination.parent.mkdir(parents=True)
    shutil.copyfile(source_job, destination)
    monkeypatch.setattr(
        repository,
        "_iter_pipeline_job_records",
        lambda *_args, **_kwargs: pytest.fail("rollback quiescence must not replay global history"),
    )
    assert [job.job_id for job in repository.query_rollback_unsettled_jobs()] == [job_id]
    real_read_optional_json = repository._read_optional_json

    def disappearing_direct(path: Any) -> Any:
        payload = real_read_optional_json(path)
        if path == destination and destination.exists():
            destination.unlink()
        return payload

    monkeypatch.setattr(repository, "_read_optional_json", disappearing_direct)
    with pytest.raises(FileOrchestrationJournalError, match="quiescence_authority_changed"):
        repository.query_rollback_unsettled_jobs()
    monkeypatch.setattr(repository, "_read_optional_json", real_read_optional_json)
    shutil.copyfile(source_job, destination)
    fence = root / "reconcile-inventory-rollback-preparation-v2.json"
    fence_before = fence.read_bytes()

    with pytest.raises(
        FileOrchestrationJournalError,
        match="file_journal_rollforward_jobs_unsettled",
    ):
        complete_file_journal_rollforward(
            journal_root=root,
            workspace_root=workspace,
            preparation_receipt_id=receipt["receipt_id"],
        )

    assert fence.read_bytes() == fence_before
    binding = read_rollback_execution_binding(workspace, require_artifacts=False)
    assert binding is not None and binding["status"] == "active"
    assert not (root / "reconcile-inventory").exists()


def test_round20_persisted_source_tree_is_recursively_sealed_and_tamper_fails_closed(
    tmp_path: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import stat
    import subprocess
    from pathlib import Path

    from packages.common.rollback_execution_binding import RollbackExecutionBindingError
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
    checkout, _initial_generation = _round14_clean_writer_checkout(
        tmp_path / "writer",
        content="writer A\n",
    )
    module = checkout / "source_package" / "module.py"
    module.parent.mkdir()
    module.write_text("VALUE = 'A'\n", encoding="utf-8")
    executable = checkout / "bin" / "worker-tool"
    executable.parent.mkdir()
    executable.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    executable.chmod(0o755)
    subprocess.run(("git", "add", "source_package/module.py", "bin/worker-tool"), cwd=checkout, check=True)
    subprocess.run(
        (
            "git",
            "-c",
            "user.name=Round20 Test",
            "-c",
            "user.email=round20@example.invalid",
            "commit",
            "-q",
            "-m",
            "nested source fixture",
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
    receipt = prepare_file_journal_rollback(
        journal_root=root,
        workspace_root=workspace,
        scheduler_state="stopped",
        active_scheduler_processes=0,
        checked_at=datetime.now(UTC),
        checked_by="round20-source-seal-operator",
        target_writer_generation=generation,
    )
    starts: list[Path] = []

    def runner(
        _command: tuple[str, ...],
        *,
        cwd: Any,
        check: bool,
        env: Any,
        pass_fds: Any,
    ) -> Any:
        del check, env, pass_fds
        starts.append(Path(cwd))
        assert (Path(cwd) / "source_package" / "module.py").read_text(encoding="utf-8") == (
            "VALUE = 'A'\n"
        )
        return type("Completed", (), {"returncode": 0})()

    monkeypatch.setattr(migration_module, "_run_rollback_writer", runner)
    real_write_binding = migration_module.write_rollback_execution_binding

    def crash_before_canonical_write(_workspace_root: Any, _binding: Any) -> dict[str, Any]:
        raise RollbackExecutionBindingError("injected crash before canonical binding write")

    monkeypatch.setattr(
        migration_module,
        "write_rollback_execution_binding",
        crash_before_canonical_write,
    )
    with pytest.raises(
        FileOrchestrationJournalError,
        match="file_journal_rollback_execution_binding_unavailable",
    ):
        launch_file_journal_rollback_writer(
            journal_root=root,
            workspace_root=workspace,
            receipt_id=receipt["receipt_id"],
            writer_repository_root=checkout,
            writer_args=("plan-production", "--submit"),
        )
    assert starts == []
    monkeypatch.setattr(
        migration_module,
        "write_rollback_execution_binding",
        real_write_binding,
    )
    launched = launch_file_journal_rollback_writer(
        journal_root=root,
        workspace_root=workspace,
        receipt_id=receipt["receipt_id"],
        writer_repository_root=checkout,
        writer_args=("plan-production", "--submit"),
    )
    source_root = Path(launched["target_python_source_root"])
    for path in (source_root, *source_root.rglob("*")):
        metadata = path.lstat()
        if stat.S_ISDIR(metadata.st_mode) or stat.S_ISREG(metadata.st_mode):
            assert stat.S_IMODE(metadata.st_mode) & 0o222 == 0, path
    sealed_module = source_root / "source_package" / "module.py"
    sealed_executable = source_root / "bin" / "worker-tool"
    assert stat.S_IMODE(sealed_executable.stat().st_mode) & 0o111
    with pytest.raises(PermissionError):
        sealed_module.write_text("VALUE = 'B'\n", encoding="utf-8")

    sealed_module.chmod(0o600)
    with pytest.raises(
        FileOrchestrationJournalError,
        match="file_journal_rollback_execution_binding_invalid",
    ):
        launch_file_journal_rollback_writer(
            journal_root=root,
            workspace_root=workspace,
            receipt_id=receipt["receipt_id"],
            writer_repository_root=checkout,
            writer_args=("plan-production", "--submit"),
        )
    assert len(starts) == 1


def test_round20_two_sequential_rollback_lifecycles_archive_completed_binding(
    tmp_path: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import json
    import subprocess
    from pathlib import Path

    from packages.common.rollback_execution_binding import (
        RollbackExecutionBindingError,
        read_rollback_execution_binding,
        rollback_execution_binding_archive_path,
    )
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
    checkout, generation_a = _round14_clean_writer_checkout(
        tmp_path / "writer",
        content="writer A\n",
    )
    starts: list[str] = []

    def runner(
        _command: tuple[str, ...],
        *,
        cwd: Any,
        check: bool,
        env: Any,
        pass_fds: Any,
    ) -> Any:
        del check, env, pass_fds
        starts.append((Path(cwd) / "writer.txt").read_text(encoding="utf-8"))
        return type("Completed", (), {"returncode": 0})()

    monkeypatch.setattr(migration_module, "_run_rollback_writer", runner)
    receipt_a = prepare_file_journal_rollback(
        journal_root=root,
        workspace_root=workspace,
        scheduler_state="stopped",
        active_scheduler_processes=0,
        checked_at=datetime.now(UTC),
        checked_by="round20-lifecycle-a",
        target_writer_generation=generation_a,
    )
    launched_a = launch_file_journal_rollback_writer(
        journal_root=root,
        workspace_root=workspace,
        receipt_id=receipt_a["receipt_id"],
        writer_repository_root=checkout,
        writer_args=("plan-production", "--submit"),
    )
    completed_a = complete_file_journal_rollforward(
        journal_root=root,
        workspace_root=workspace,
        preparation_receipt_id=receipt_a["receipt_id"],
    )
    assert completed_a["rollback_execution_binding_status"] == "completed"
    assert complete_file_journal_rollforward(
        journal_root=root,
        workspace_root=workspace,
        preparation_receipt_id=receipt_a["receipt_id"],
    ) == completed_a

    (checkout / "writer.txt").write_text("writer B\n", encoding="utf-8")
    subprocess.run(("git", "add", "writer.txt"), cwd=checkout, check=True)
    subprocess.run(
        (
            "git",
            "-c",
            "user.name=Round20 Test",
            "-c",
            "user.email=round20@example.invalid",
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
    assert generation_b != generation_a
    real_archive = migration_module.archive_completed_rollback_execution_binding
    archive_cut_injected = False

    def archive_then_crash(workspace_root: Any, binding: Any) -> dict[str, Any]:
        nonlocal archive_cut_injected
        archived = real_archive(workspace_root, binding)
        if not archive_cut_injected:
            archive_cut_injected = True
            raise RollbackExecutionBindingError("injected crash after archive durability cut")
        return archived

    monkeypatch.setattr(
        migration_module,
        "archive_completed_rollback_execution_binding",
        archive_then_crash,
    )
    with pytest.raises(
        FileOrchestrationJournalError,
        match="file_journal_rollback_preparation_authority_unavailable",
    ):
        prepare_file_journal_rollback(
            journal_root=root,
            workspace_root=workspace,
            scheduler_state="stopped",
            active_scheduler_processes=0,
            checked_at=datetime.now(UTC),
            checked_by="round20-lifecycle-b-archive-cut",
            target_writer_generation=generation_b,
        )
    fence = root / "reconcile-inventory-rollback-preparation-v2.json"
    receipt_b_after_archive_cut = json.loads(fence.read_text(encoding="utf-8"))
    canonical_after_archive_cut = read_rollback_execution_binding(
        workspace,
        required=True,
        require_artifacts=False,
    )
    assert canonical_after_archive_cut is not None
    assert canonical_after_archive_cut["binding_id"] == launched_a["rollback_execution_binding_id"]
    assert canonical_after_archive_cut["status"] == "completed"
    assert starts == ["writer A\n"]
    monkeypatch.setattr(
        migration_module,
        "archive_completed_rollback_execution_binding",
        real_archive,
    )
    receipt_b = prepare_file_journal_rollback(
        journal_root=root,
        workspace_root=workspace,
        scheduler_state="stopped",
        active_scheduler_processes=0,
        checked_at=datetime.now(UTC),
        checked_by="round20-lifecycle-b-retry",
        target_writer_generation=generation_b,
    )
    assert receipt_b["receipt_id"] == receipt_b_after_archive_cut["receipt_id"]
    prepared_b = read_rollback_execution_binding(
        workspace,
        required=True,
        require_artifacts=False,
    )
    assert prepared_b is not None and prepared_b["status"] == "prepared"

    real_write_binding = migration_module.write_rollback_execution_binding
    canonical_cut_injected = False

    def write_canonical_then_crash(workspace_root: Any, binding: Any) -> dict[str, Any]:
        nonlocal canonical_cut_injected
        written = real_write_binding(workspace_root, binding)
        if (
            not canonical_cut_injected
            and binding.get("status") == "active"
            and binding.get("preparation_receipt_id") == receipt_b["receipt_id"]
        ):
            canonical_cut_injected = True
            raise RollbackExecutionBindingError("injected crash after canonical durability cut")
        return written

    monkeypatch.setattr(
        migration_module,
        "write_rollback_execution_binding",
        write_canonical_then_crash,
    )
    with pytest.raises(
        FileOrchestrationJournalError,
        match="file_journal_rollback_execution_binding_unavailable",
    ):
        launch_file_journal_rollback_writer(
            journal_root=root,
            workspace_root=workspace,
            receipt_id=receipt_b["receipt_id"],
            writer_repository_root=checkout,
            writer_args=("plan-production", "--submit"),
        )
    active_after_canonical_cut = read_rollback_execution_binding(workspace, required=True)
    assert active_after_canonical_cut is not None
    assert active_after_canonical_cut["preparation_receipt_id"] == receipt_b["receipt_id"]
    assert active_after_canonical_cut["status"] == "active"
    assert starts == ["writer A\n"]
    monkeypatch.setattr(
        migration_module,
        "write_rollback_execution_binding",
        real_write_binding,
    )

    launched_b = launch_file_journal_rollback_writer(
        journal_root=root,
        workspace_root=workspace,
        receipt_id=receipt_b["receipt_id"],
        writer_repository_root=checkout,
        writer_args=("plan-production", "--submit"),
    )
    assert launched_b["rollback_execution_binding_id"] != launched_a["rollback_execution_binding_id"]
    assert starts == ["writer A\n", "writer B\n"]
    archived_a_path = rollback_execution_binding_archive_path(
        workspace,
        launched_a["rollback_execution_binding_id"],
    )
    archived_a = json.loads(archived_a_path.read_text(encoding="utf-8"))
    assert archived_a["binding_id"] == launched_a["rollback_execution_binding_id"]
    assert archived_a["status"] == "completed"
    active_b = read_rollback_execution_binding(workspace, required=True)
    assert active_b is not None
    assert active_b["binding_id"] == launched_b["rollback_execution_binding_id"]
    assert active_b["status"] == "active"

    completed_b = complete_file_journal_rollforward(
        journal_root=root,
        workspace_root=workspace,
        preparation_receipt_id=receipt_b["receipt_id"],
    )
    assert completed_b["rollback_execution_binding_status"] == "completed"
    final_binding = read_rollback_execution_binding(workspace, required=True, require_artifacts=False)
    assert final_binding is not None
    assert final_binding["binding_id"] == launched_b["rollback_execution_binding_id"]
    assert final_binding["status"] == "completed"


@pytest.mark.parametrize("generation_length", [40, 64])
def test_round16_prepare_normalizes_full_git_generation_and_receipt_marks_submit(
    tmp_path: Any,
    generation_length: int,
) -> None:
    from services.orchestrator.file_orchestration_journal import FileOrchestrationJournalRepository
    from services.orchestrator.file_orchestration_migration import (
        prepare_file_journal_rollback,
        require_file_journal_rollback_prepared,
    )

    root = tmp_path / str(generation_length) / "journal"
    workspace = tmp_path / str(generation_length) / "workspace"
    workspace.mkdir(parents=True)
    repository = FileOrchestrationJournalRepository(root)
    assert repository.query_inflight_jobs() == []
    uppercase_generation = "ABCDEF0123" * (generation_length // 10) + "ABCD"[: generation_length % 10]
    receipt = prepare_file_journal_rollback(
        journal_root=root,
        workspace_root=workspace,
        scheduler_state="stopped",
        active_scheduler_processes=0,
        checked_at=datetime.now(UTC),
        checked_by="round16-operator",
        target_writer_generation=uppercase_generation,
    )
    assert receipt["preflight"]["target_writer_generation"] == uppercase_generation.lower()
    assert receipt["preflight"]["dry_run"] is False
    required = require_file_journal_rollback_prepared(
        journal_root=root,
        workspace_root=workspace,
        receipt_id=receipt["receipt_id"],
        actual_writer_generation=uppercase_generation,
    )
    assert required["preflight"]["target_writer_generation"] == uppercase_generation.lower()
    assert required["preflight"]["dry_run"] is False


def test_round16_launcher_requires_target_checkout_executable_runtime(
    tmp_path: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import sys

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
    checkout, generation = _round14_clean_writer_checkout(
        tmp_path / "writer",
        content="target-only sentinel\n",
    )
    receipt = prepare_file_journal_rollback(
        journal_root=root,
        workspace_root=workspace,
        scheduler_state="stopped",
        active_scheduler_processes=0,
        checked_at=datetime.now(UTC),
        checked_by="round16-operator",
        target_writer_generation=generation,
    )
    runtime = checkout / ".venv" / "bin" / "python"
    assert str(runtime.absolute()) != sys.executable
    assert os.access(sys.executable, os.X_OK)
    runtime.unlink()
    starts: list[tuple[str, ...]] = []

    def runner(command: tuple[str, ...], **_kwargs: Any) -> Any:
        starts.append(command)
        return type("Completed", (), {"returncode": 0})()

    monkeypatch.setattr(migration_module, "_run_rollback_writer", runner)
    for create_non_executable in (False, True):
        if create_non_executable:
            runtime.write_text("not executable\n", encoding="utf-8")
            runtime.chmod(0o644)
        with pytest.raises(
            FileOrchestrationJournalError,
            match="file_journal_rollback_writer_runtime_unavailable",
        ):
            launch_file_journal_rollback_writer(
                journal_root=root,
                workspace_root=workspace,
                receipt_id=receipt["receipt_id"],
                writer_repository_root=checkout,
                writer_args=("plan-production", "--submit"),
            )
        assert starts == []
