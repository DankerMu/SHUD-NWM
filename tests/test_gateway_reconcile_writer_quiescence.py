"""Rollback quiescence: strict terminal allowlists, bounded historical
scans, and authority-change zero-mutation guards.
"""

from __future__ import annotations

import json
import os
from datetime import (
    UTC,
    datetime,
)
from typing import Any

import pytest

from tests.gateway_reconcile_helpers import (
    _bind_current_file_cohort,
    _file_cohort_repository,
)
from tests.gateway_reconcile_writer_helpers import (
    _round14_clean_writer_checkout,
    _round20_write_execution_binding,
)


def test_round21_launcher_rejects_missing_prepared_binding_without_start(
    tmp_path: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from packages.common.rollback_execution_binding import rollback_execution_binding_path
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
    assert FileOrchestrationJournalRepository(root).query_inflight_jobs() == []
    checkout, generation = _round14_clean_writer_checkout(tmp_path / "writer", content="A\n")
    receipt = prepare_file_journal_rollback(
        journal_root=root,
        workspace_root=workspace,
        scheduler_state="stopped",
        active_scheduler_processes=0,
        checked_at=datetime.now(UTC),
        checked_by="round21-missing-binding",
        target_writer_generation=generation,
    )
    rollback_execution_binding_path(workspace).unlink()
    starts: list[tuple[str, ...]] = []
    monkeypatch.setattr(
        migration_module,
        "_run_rollback_writer",
        lambda command, **_kwargs: starts.append(command),
    )

    with pytest.raises(
        FileOrchestrationJournalError,
        match="file_journal_rollback_execution_binding_conflict",
    ):
        launch_file_journal_rollback_writer(
            journal_root=root,
            workspace_root=workspace,
            receipt_id=receipt["receipt_id"],
            writer_repository_root=checkout,
            writer_args=("plan-production", "--submit"),
        )
    assert starts == []


def test_round21_launcher_cannot_reopen_completed_binding(
    tmp_path: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
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
    assert FileOrchestrationJournalRepository(root).query_inflight_jobs() == []
    checkout, generation = _round14_clean_writer_checkout(tmp_path / "writer", content="A\n")
    receipt = prepare_file_journal_rollback(
        journal_root=root,
        workspace_root=workspace,
        scheduler_state="stopped",
        active_scheduler_processes=0,
        checked_at=datetime.now(UTC),
        checked_by="round21-completed-no-launch",
        target_writer_generation=generation,
    )
    complete_file_journal_rollforward(
        journal_root=root,
        workspace_root=workspace,
        preparation_receipt_id=receipt["receipt_id"],
    )
    starts: list[tuple[str, ...]] = []
    monkeypatch.setattr(
        migration_module,
        "_run_rollback_writer",
        lambda command, **_kwargs: starts.append(command),
    )

    with pytest.raises(FileOrchestrationJournalError, match="file_journal_rollback_not_prepared"):
        launch_file_journal_rollback_writer(
            journal_root=root,
            workspace_root=workspace,
            receipt_id=receipt["receipt_id"],
            writer_repository_root=checkout,
            writer_args=("plan-production", "--submit"),
        )
    assert starts == []


@pytest.mark.parametrize(
    ("status", "slurm_job_id", "blocks"),
    [
        *((status, "123", False) for status in (
            "succeeded",
            "partially_failed",
            "failed",
            "cancelled",
            "submission_failed",
            "reservation_lost",
            "permanently_failed",
        )),
        ("running", "local", True),
        ("pending", None, True),
        ("queued", "", True),
        ("reconciling", "123", True),
        ("future_unknown_status", "123", True),
        ("", None, True),
    ],
)
def test_round21_rollback_quiescence_is_strict_terminal_allowlist(
    status: str,
    slurm_job_id: str | None,
    blocks: bool,
) -> None:
    from services.orchestrator.file_orchestration_journal import _job_blocks_rollback_quiescence

    assert _job_blocks_rollback_quiescence(
        {"status": status, "slurm_job_id": slurm_job_id}
    ) is blocks


def test_round21_rollback_query_skips_100001_historical_terminal_records(
    tmp_path: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import shutil

    from services.orchestrator.file_orchestration_journal import FileOrchestrationJournalRepository
    from services.orchestrator.file_orchestration_migration import prepare_file_journal_rollback

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
        checked_by="round21-100k-history",
        target_writer_generation="a" * 40,
    )
    history_template = _file_cohort_repository(
        tmp_path / "history-source", member_count=1, with_runtime_rows=False
    )
    key = "cycle_gfs_2026071200_forecast_fixture:forecast"
    _bind_current_file_cohort(history_template, key, slurm_job_id="17667")
    job_id = "job_cycle_gfs_2026071200_forecast_fixture_forecast"
    terminal_record = json.loads(
        (history_template.root / "pipeline-jobs" / f"{job_id}.json").read_text(
            encoding="utf-8"
        )
    )
    terminal_payload = terminal_record.get("payload", terminal_record)
    terminal_payload["status"] = "submission_failed"
    terminal_payload["submit_outcome"] = "rejected"
    terminal_line = (json.dumps(terminal_record, separators=(",", ":")) + "\n").encode()
    source_history = next((history_template.root / "journal").rglob("*.jsonl"))
    historical = root / source_history.relative_to(history_template.root)
    historical.parent.mkdir(parents=True)
    historical.write_bytes(terminal_line * 100_001)
    prepared_at = datetime.fromisoformat(receipt["prepared_at"].removesuffix("Z") + "+00:00")
    old_timestamp = prepared_at.timestamp() - 60
    os.utime(historical, (old_timestamp, old_timestamp))
    assert historical.stat().st_size == len(terminal_line) * 100_001

    template = _file_cohort_repository(tmp_path / "current", member_count=1, with_runtime_rows=False)
    _bind_current_file_cohort(template, key, slurm_job_id="17667")
    source_job = template.root / "pipeline-jobs" / f"{job_id}.json"
    destination = root / "pipeline-jobs" / source_job.name
    destination.parent.mkdir(parents=True)
    shutil.copyfile(source_job, destination)

    real_read_jsonl = repository._read_jsonl

    def bounded_read(path: Any, *, segment_index: int = 0) -> Any:
        if path == historical:
            raise AssertionError("rollback query consumed 100001 historical terminal records")
        return real_read_jsonl(path, segment_index=segment_index)

    monkeypatch.setattr(repository, "_read_jsonl", bounded_read)
    assert [job.job_id for job in repository.query_rollback_unsettled_jobs()] == [job_id]


def test_round21_prepared_only_rollforward_completes_without_ever_launching(
    tmp_path: Any,
) -> None:
    import json

    from packages.common.rollback_execution_binding import (
        read_rollback_execution_binding,
        rollback_execution_binding_archive_path,
    )
    from services.orchestrator.file_orchestration_journal import FileOrchestrationJournalRepository
    from services.orchestrator.file_orchestration_migration import (
        complete_file_journal_rollforward,
        prepare_file_journal_rollback,
    )

    root = tmp_path / "journal"
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    assert FileOrchestrationJournalRepository(root).query_inflight_jobs() == []
    receipt = prepare_file_journal_rollback(
        journal_root=root,
        workspace_root=workspace,
        scheduler_state="stopped",
        active_scheduler_processes=0,
        checked_at=datetime.now(UTC),
        checked_by="round21-prepared-only",
        target_writer_generation="a" * 40,
    )
    prepared = read_rollback_execution_binding(workspace, required=True, require_artifacts=False)
    assert prepared is not None and prepared["status"] == "prepared"
    assert prepared["target_python_runtime"] is None

    completed = complete_file_journal_rollforward(
        journal_root=root,
        workspace_root=workspace,
        preparation_receipt_id=receipt["receipt_id"],
    )

    binding = read_rollback_execution_binding(workspace, required=True, require_artifacts=False)
    assert binding is not None and binding["status"] == "completed"
    assert binding["binding_id"] == completed["rollback_execution_binding_id"]
    assert not (root / "reconcile-inventory-rollback-preparation-v2.json").exists()
    assert (root / "reconcile-inventory-migration-v1.json").is_file()
    assert (root / "reconcile-inventory-rollforward-v1.json").is_file()
    archived = json.loads(
        rollback_execution_binding_archive_path(workspace, binding["binding_id"]).read_text(
            encoding="utf-8"
        )
    )
    assert archived == binding


def test_round21_rollforward_reopens_after_inventory_completion_cut(
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
    assert FileOrchestrationJournalRepository(root).query_inflight_jobs() == []
    receipt = prepare_file_journal_rollback(
        journal_root=root,
        workspace_root=workspace,
        scheduler_state="stopped",
        active_scheduler_processes=0,
        checked_at=datetime.now(UTC),
        checked_by="round21-inventory-cut",
        target_writer_generation="a" * 40,
    )
    real_complete = (
        FileOrchestrationJournalRepository._complete_reconcile_inventory_rollforward_under_scheduler_lease
    )
    injected = False

    def complete_then_crash(self: Any, **kwargs: Any) -> dict[str, Any]:
        nonlocal injected
        result = real_complete(self, **kwargs)
        if not injected:
            injected = True
            raise RuntimeError("injected crash after inventory completion durability")
        return result

    monkeypatch.setattr(
        FileOrchestrationJournalRepository,
        "_complete_reconcile_inventory_rollforward_under_scheduler_lease",
        complete_then_crash,
    )
    with pytest.raises(RuntimeError, match="inventory completion"):
        complete_file_journal_rollforward(
            journal_root=root,
            workspace_root=workspace,
            preparation_receipt_id=receipt["receipt_id"],
        )
    cut_binding = read_rollback_execution_binding(workspace, required=True, require_artifacts=False)
    assert cut_binding is not None and cut_binding["status"] == "rolling_forward"
    assert not (root / "reconcile-inventory-rollback-preparation-v2.json").exists()
    assert (root / "reconcile-inventory-rollforward-v1.json").is_file()

    monkeypatch.setattr(
        FileOrchestrationJournalRepository,
        "query_rollback_unsettled_jobs",
        lambda _self: pytest.fail("reopen must not repeat quiescence"),
    )
    completed = complete_file_journal_rollforward(
        journal_root=root,
        workspace_root=workspace,
        preparation_receipt_id=receipt["receipt_id"],
    )
    assert completed["rollback_execution_binding_status"] == "completed"


def test_round21_rollforward_reopens_after_completed_binding_durability_cut(
    tmp_path: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from packages.common.rollback_execution_binding import read_rollback_execution_binding
    from services.orchestrator import file_orchestration_migration as migration_module
    from services.orchestrator.file_orchestration_journal import FileOrchestrationJournalRepository
    from services.orchestrator.file_orchestration_migration import (
        complete_file_journal_rollforward,
        prepare_file_journal_rollback,
    )

    root = tmp_path / "journal"
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    assert FileOrchestrationJournalRepository(root).query_inflight_jobs() == []
    receipt = prepare_file_journal_rollback(
        journal_root=root,
        workspace_root=workspace,
        scheduler_state="stopped",
        active_scheduler_processes=0,
        checked_at=datetime.now(UTC),
        checked_by="round21-completed-cut",
        target_writer_generation="a" * 40,
    )
    real_transition = migration_module._transition_rollback_execution_binding
    injected = False

    def transition_then_crash(
        workspace_root: Any,
        binding: Any,
        *,
        status: str,
    ) -> dict[str, Any]:
        nonlocal injected
        result = real_transition(workspace_root, binding, status=status)
        if status == "completed" and not injected:
            injected = True
            raise RuntimeError("injected crash after completed binding durability")
        return result

    monkeypatch.setattr(
        migration_module,
        "_transition_rollback_execution_binding",
        transition_then_crash,
    )
    with pytest.raises(RuntimeError, match="completed binding"):
        complete_file_journal_rollforward(
            journal_root=root,
            workspace_root=workspace,
            preparation_receipt_id=receipt["receipt_id"],
        )
    durable = read_rollback_execution_binding(workspace, required=True, require_artifacts=False)
    assert durable is not None and durable["status"] == "completed"

    completed = complete_file_journal_rollforward(
        journal_root=root,
        workspace_root=workspace,
        preparation_receipt_id=receipt["receipt_id"],
    )
    assert completed["rollback_execution_binding_id"] == durable["binding_id"]


def test_round21_rollforward_archive_failure_recovers_without_reopening_launch(
    tmp_path: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
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
        prepare_file_journal_rollback,
    )

    root = tmp_path / "journal"
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    assert FileOrchestrationJournalRepository(root).query_inflight_jobs() == []
    receipt = prepare_file_journal_rollback(
        journal_root=root,
        workspace_root=workspace,
        scheduler_state="stopped",
        active_scheduler_processes=0,
        checked_at=datetime.now(UTC),
        checked_by="round21-archive-cut",
        target_writer_generation="a" * 40,
    )
    real_archive = migration_module.archive_completed_rollback_execution_binding

    monkeypatch.setattr(
        migration_module,
        "archive_completed_rollback_execution_binding",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            RollbackExecutionBindingError("injected rollforward archive failure")
        ),
    )
    with pytest.raises(
        FileOrchestrationJournalError,
        match="file_journal_rollforward_execution_binding_archive_unavailable",
    ):
        complete_file_journal_rollforward(
            journal_root=root,
            workspace_root=workspace,
            preparation_receipt_id=receipt["receipt_id"],
        )
    durable = read_rollback_execution_binding(workspace, required=True, require_artifacts=False)
    assert durable is not None and durable["status"] == "completed"
    archive_path = rollback_execution_binding_archive_path(workspace, durable["binding_id"])
    assert not archive_path.exists()

    monkeypatch.setattr(
        migration_module,
        "archive_completed_rollback_execution_binding",
        real_archive,
    )
    completed = complete_file_journal_rollforward(
        journal_root=root,
        workspace_root=workspace,
        preparation_receipt_id=receipt["receipt_id"],
    )
    assert completed["rollback_execution_binding_id"] == durable["binding_id"]
    assert archive_path.is_file()


@pytest.mark.parametrize(
    "cut",
    [
        "inventory_read",
        "journal_enumerate",
        "journal_stat",
        "journal_read",
        "journal_replace_read",
        "direct_read",
        "latest_read",
        "latest_stat",
        "latest_replace_read",
        "legacy_read",
    ],
)
def test_round21_rollforward_authority_disappearance_is_zero_mutation(
    tmp_path: Any,
    monkeypatch: pytest.MonkeyPatch,
    cut: str,
) -> None:
    import shutil

    from packages.common.rollback_execution_binding import rollback_execution_binding_path
    from services.orchestrator import file_orchestration_journal as journal_module
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
    assert FileOrchestrationJournalRepository(root).query_inflight_jobs() == []
    receipt = prepare_file_journal_rollback(
        journal_root=root,
        workspace_root=workspace,
        scheduler_state="stopped",
        active_scheduler_processes=0,
        checked_at=datetime.now(UTC),
        checked_by=f"round21-disappearance-{cut}",
        target_writer_generation="a" * 40,
    )
    _round20_write_execution_binding(
        workspace=workspace,
        receipt=receipt,
        generation="a" * 40,
    )
    template = _file_cohort_repository(
        tmp_path / "authority",
        member_count=1,
        with_runtime_rows=cut.startswith("latest"),
    )
    key = "cycle_gfs_2026071200_forecast_fixture:forecast"
    _bind_current_file_cohort(template, key, slurm_job_id="17667")
    job_id = "job_cycle_gfs_2026071200_forecast_fixture_forecast"
    target: Any
    if cut == "direct_read":
        source = template.root / "pipeline-jobs" / f"{job_id}.json"
        target = root / "pipeline-jobs" / source.name
        target.parent.mkdir(parents=True)
        shutil.copyfile(source, target)
    elif cut.startswith("latest"):
        source = next((template.root / "latest").rglob("*.json"))
        target = root / source.relative_to(template.root)
        target.parent.mkdir(parents=True)
        shutil.copyfile(source, target)
    elif cut == "legacy_read":
        source = template.root / "pipeline-jobs" / f"{job_id}.json"
        target = root / "active-reconcile" / source.name
        target.parent.mkdir(parents=True)
        shutil.copyfile(source, target)
    else:
        source_journal = next((template.root / "journal").rglob("*.jsonl"))
        target = root / source_journal.relative_to(template.root)
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source_journal, target)
        source_anchor = template.root / "reconcile-inventory" / f"{job_id}.json"
        anchor = root / "reconcile-inventory" / source_anchor.name
        anchor.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source_anchor, anchor)

    fence = root / "reconcile-inventory-rollback-preparation-v2.json"
    binding_path = rollback_execution_binding_path(workspace)
    fence_before = fence.read_bytes()
    binding_before = binding_path.read_bytes()

    if cut in {
        "inventory_read",
        "journal_read",
        "journal_replace_read",
        "direct_read",
        "latest_read",
        "latest_replace_read",
        "legacy_read",
    }:
        method_name = "_read_jsonl" if cut.startswith("journal") else "_read_optional_json"
        real_method = getattr(FileOrchestrationJournalRepository, method_name)

        def disappear_after_read(self: Any, path: Any, **kwargs: Any) -> Any:
            payload = real_method(self, path, **kwargs)
            selected = anchor if cut == "inventory_read" else target
            if path == selected and selected.exists():
                if "replace" in cut:
                    content = selected.read_bytes()
                    selected.unlink()
                    selected.write_bytes(content)
                else:
                    selected.unlink()
            return payload

        monkeypatch.setattr(FileOrchestrationJournalRepository, method_name, disappear_after_read)
    elif cut in {"journal_stat", "latest_stat"}:
        real_signature = journal_module._stat_signature
        target_calls = 0

        def disappear_during_stat(path: Any) -> Any:
            nonlocal target_calls
            if path == target:
                target_calls += 1
                if target_calls == 2 and target.exists():
                    target.unlink()
            return real_signature(path)

        monkeypatch.setattr(journal_module, "_stat_signature", disappear_during_stat)
    else:
        real_list = journal_module.list_directory_no_follow_limited

        def disappear_after_enumerate(directory: Any, **kwargs: Any) -> Any:
            names = real_list(directory, **kwargs)
            if directory == target.parent and target.exists():
                target.unlink()
            return names

        monkeypatch.setattr(
            journal_module,
            "list_directory_no_follow_limited",
            disappear_after_enumerate,
        )

    with pytest.raises(
        FileOrchestrationJournalError,
        match="file_journal_rollforward_quiescence_unavailable",
    ) as exc_info:
        complete_file_journal_rollforward(
            journal_root=root,
            workspace_root=workspace,
            preparation_receipt_id=receipt["receipt_id"],
        )
    assert isinstance(exc_info.value.__cause__, FileOrchestrationJournalError)
    assert exc_info.value.__cause__.reason == "file_journal_quiescence_authority_changed"
    assert fence.read_bytes() == fence_before
    assert binding_path.read_bytes() == binding_before


def test_round21_strict_quiescence_query_is_read_only_for_current_authority(
    tmp_path: Any,
) -> None:
    import shutil

    from services.orchestrator.file_orchestration_journal import FileOrchestrationJournalRepository
    from services.orchestrator.file_orchestration_migration import prepare_file_journal_rollback

    root = tmp_path / "journal"
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    repository = FileOrchestrationJournalRepository(root)
    assert repository.query_inflight_jobs() == []
    prepare_file_journal_rollback(
        journal_root=root,
        workspace_root=workspace,
        scheduler_state="stopped",
        active_scheduler_processes=0,
        checked_at=datetime.now(UTC),
        checked_by="round21-read-only-query",
        target_writer_generation="a" * 40,
    )
    template = _file_cohort_repository(tmp_path / "authority", member_count=1, with_runtime_rows=False)
    key = "cycle_gfs_2026071200_forecast_fixture:forecast"
    _bind_current_file_cohort(template, key, slurm_job_id="17667")
    job_id = "job_cycle_gfs_2026071200_forecast_fixture_forecast"
    for source in (
        next((template.root / "journal").rglob("*.jsonl")),
        template.root / "reconcile-inventory" / f"{job_id}.json",
    ):
        destination = root / source.relative_to(template.root)
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, destination)
    before = {
        path.relative_to(root): path.read_bytes()
        for path in root.rglob("*")
        if path.is_file()
    }

    assert [job.job_id for job in repository.query_rollback_unsettled_jobs()] == [job_id]

    after = {
        path.relative_to(root): path.read_bytes()
        for path in root.rglob("*")
        if path.is_file()
    }
    assert after == before


@pytest.mark.parametrize(
    "authority_root_name",
    ["reconcile-inventory", "journal", "latest", "pipeline-jobs", "active-reconcile"],
)
@pytest.mark.parametrize("boundary", ["stat_to_list_disappear", "after_list_replace"])
def test_round22_strict_authority_root_change_is_uniform_zero_mutation(
    tmp_path: Any,
    monkeypatch: pytest.MonkeyPatch,
    authority_root_name: str,
    boundary: str,
) -> None:
    import shutil

    from packages.common.rollback_execution_binding import rollback_execution_binding_path
    from services.orchestrator import file_orchestration_journal as journal_module
    from services.orchestrator.file_orchestration_journal import (
        FileOrchestrationJournalError,
        FileOrchestrationJournalRepository,
    )
    from services.orchestrator.file_orchestration_migration import (
        complete_file_journal_rollforward,
        prepare_file_journal_rollback,
    )

    root = tmp_path / "journal-root"
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    assert FileOrchestrationJournalRepository(root).query_inflight_jobs() == []
    receipt = prepare_file_journal_rollback(
        journal_root=root,
        workspace_root=workspace,
        scheduler_state="stopped",
        active_scheduler_processes=0,
        checked_at=datetime.now(UTC),
        checked_by=f"round22-root-{authority_root_name}-{boundary}",
        target_writer_generation="a" * 40,
    )
    _round20_write_execution_binding(
        workspace=workspace,
        receipt=receipt,
        generation="a" * 40,
    )
    authority_root = root / authority_root_name
    authority_root.mkdir(exist_ok=True)
    fence = root / "reconcile-inventory-rollback-preparation-v2.json"
    binding_path = rollback_execution_binding_path(workspace)
    fence_before = fence.read_bytes()
    binding_before = binding_path.read_bytes()
    real_list = journal_module.list_directory_no_follow_limited
    injected = False

    def change_root(directory: Any, **kwargs: Any) -> Any:
        nonlocal injected
        if directory != authority_root or injected:
            return real_list(directory, **kwargs)
        injected = True
        if boundary == "stat_to_list_disappear":
            shutil.rmtree(authority_root)
            return real_list(directory, **kwargs)
        names = real_list(directory, **kwargs)
        displaced = authority_root.with_name(f"{authority_root.name}-displaced")
        authority_root.rename(displaced)
        authority_root.mkdir()
        return names

    monkeypatch.setattr(journal_module, "list_directory_no_follow_limited", change_root)

    with pytest.raises(
        FileOrchestrationJournalError,
        match="file_journal_rollforward_quiescence_unavailable",
    ) as exc_info:
        complete_file_journal_rollforward(
            journal_root=root,
            workspace_root=workspace,
            preparation_receipt_id=receipt["receipt_id"],
        )
    assert isinstance(exc_info.value.__cause__, FileOrchestrationJournalError)
    assert exc_info.value.__cause__.reason == "file_journal_quiescence_authority_changed"
    assert fence.read_bytes() == fence_before
    assert binding_path.read_bytes() == binding_before


@pytest.mark.parametrize("surface", ["journal", "latest"])
@pytest.mark.parametrize("mutation", ["delete", "replace", "add"])
def test_round23_nested_authority_change_before_first_list_is_zero_mutation(
    tmp_path: Any,
    monkeypatch: pytest.MonkeyPatch,
    surface: str,
    mutation: str,
) -> None:
    import shutil

    from packages.common.rollback_execution_binding import rollback_execution_binding_path
    from services.orchestrator import file_orchestration_journal as journal_module
    from services.orchestrator.file_orchestration_journal import (
        FileOrchestrationJournalError,
        FileOrchestrationJournalRepository,
    )
    from services.orchestrator.file_orchestration_migration import (
        complete_file_journal_rollforward,
        prepare_file_journal_rollback,
    )

    root = tmp_path / "journal-root"
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
        checked_by=f"round23-nested-{surface}-{mutation}",
        target_writer_generation="a" * 40,
    )
    _round20_write_execution_binding(
        workspace=workspace,
        receipt=receipt,
        generation="a" * 40,
    )
    template = _file_cohort_repository(
        tmp_path / "authority",
        member_count=1,
        with_runtime_rows=surface == "latest",
    )
    key = "cycle_gfs_2026071200_forecast_fixture:forecast"
    _bind_current_file_cohort(template, key, slurm_job_id="17667")
    job_id = "job_cycle_gfs_2026071200_forecast_fixture_forecast"
    if surface == "journal":
        source = next((template.root / "journal").rglob("*.jsonl"))
    else:
        source = next((template.root / "latest").rglob("*.json"))
    target = root / source.relative_to(template.root)
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(source, target)
    visible_before_mutation = [job.job_id for job in repository.query_rollback_unsettled_jobs()]
    if surface == "journal":
        assert visible_before_mutation == [job_id]
    fence = root / "reconcile-inventory-rollback-preparation-v2.json"
    binding_path = rollback_execution_binding_path(workspace)
    fence_before = fence.read_bytes()
    binding_before = binding_path.read_bytes()
    target_content = target.read_bytes()
    real_list = journal_module.list_directory_no_follow_limited
    injected = False

    def change_nested_directory(directory: Any, **kwargs: Any) -> Any:
        nonlocal injected
        if directory != target.parent or injected:
            return real_list(directory, **kwargs)
        injected = True
        if mutation == "delete":
            target.unlink()
        elif mutation == "replace":
            target.unlink()
            target.write_bytes(target_content)
        else:
            suffix = ".jsonl" if surface == "journal" else ".json"
            (target.parent / f"added{suffix}").write_bytes(target_content)
        return real_list(directory, **kwargs)

    monkeypatch.setattr(
        journal_module,
        "list_directory_no_follow_limited",
        change_nested_directory,
    )
    with pytest.raises(
        FileOrchestrationJournalError,
        match="file_journal_rollforward_quiescence_unavailable",
    ) as exc_info:
        complete_file_journal_rollforward(
            journal_root=root,
            workspace_root=workspace,
            preparation_receipt_id=receipt["receipt_id"],
        )
    assert isinstance(exc_info.value.__cause__, FileOrchestrationJournalError)
    assert exc_info.value.__cause__.reason == "file_journal_quiescence_authority_changed"
    assert fence.read_bytes() == fence_before
    assert binding_path.read_bytes() == binding_before


def test_round14_writer_generation_git_probe_timeout_fails_closed(
    tmp_path: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import subprocess

    from services.orchestrator import file_orchestration_migration as migration_module
    from services.orchestrator.file_orchestration_journal import FileOrchestrationJournalError

    repository_root = tmp_path / "writer"
    repository_root.mkdir()
    observed: list[dict[str, Any]] = []

    def timeout(*_args: Any, **kwargs: Any) -> Any:
        observed.append(kwargs)
        raise subprocess.TimeoutExpired(cmd="git", timeout=kwargs["timeout"])

    monkeypatch.setattr(migration_module.subprocess, "run", timeout)
    with pytest.raises(
        FileOrchestrationJournalError,
        match="file_journal_rollback_writer_generation_unresolvable",
    ):
        migration_module._resolve_clean_writer_generation(repository_root)

    assert observed == [
        {
            "cwd": repository_root.resolve(),
            "check": False,
            "capture_output": True,
            "shell": False,
            "text": False,
            "timeout": migration_module.ROLLBACK_WRITER_GIT_TIMEOUT_SECONDS,
        }
    ]
