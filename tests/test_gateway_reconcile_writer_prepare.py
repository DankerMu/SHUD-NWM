"""Rollback writer preparation: scheduler-lease mutation authority,
generation validation, and prepare-time authority publication.
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

from tests.gateway_reconcile_helpers import (
    _bind_current_file_cohort,
    _file_cohort_repository,
)
from tests.gateway_reconcile_writer_helpers import _round20_write_execution_binding


@pytest.mark.parametrize("surface", ["direct", "legacy", "journal"])
def test_round12_rollforward_strictly_backfills_once_under_real_scheduler_lease(
    tmp_path: Any,
    monkeypatch: pytest.MonkeyPatch,
    surface: str,
) -> None:
    from services.orchestrator.file_orchestration_journal import (
        FileOrchestrationJournalError,
        FileOrchestrationJournalRepository,
    )
    from services.orchestrator.file_orchestration_migration import (
        complete_file_journal_rollforward,
        prepare_file_journal_rollback,
        require_file_journal_rollback_prepared,
    )

    template = _file_cohort_repository(
        tmp_path / f"template-{surface}",
        member_count=1,
        with_runtime_rows=False,
    )
    job_id = "job_cycle_gfs_2026071200_forecast_fixture_forecast"
    key = "cycle_gfs_2026071200_forecast_fixture:forecast"
    if surface == "journal":
        _bind_current_file_cohort(template, key, slurm_job_id="88201", status="running")

    root = tmp_path / f"target-{surface}" / "journal"
    workspace = tmp_path / f"workspace-{surface}"
    workspace.mkdir()
    target = FileOrchestrationJournalRepository(root)
    assert target.query_reserved_unbound_jobs() == []
    marker = root / "reconcile-inventory-migration-v1.json"
    assert marker.is_file()
    receipt = prepare_file_journal_rollback(
        journal_root=root,
        workspace_root=workspace,
        scheduler_state="stopped",
        active_scheduler_processes=0,
        checked_at=datetime.now(UTC),
        checked_by="round11-test-operator",
        target_writer_generation="a" * 40,
    )
    assert receipt["status"] == "prepared"
    assert not marker.exists()
    assert require_file_journal_rollback_prepared(
        journal_root=root,
        workspace_root=workspace,
        receipt_id=receipt["receipt_id"],
        actual_writer_generation="a" * 40,
    )["receipt_id"] == receipt["receipt_id"]
    _round20_write_execution_binding(
        workspace=workspace,
        receipt=receipt,
        generation="a" * 40,
    )

    direct = template.root / "pipeline-jobs" / f"{job_id}.json"
    if surface == "direct":
        destination = root / "pipeline-jobs" / f"{job_id}.json"
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(direct.read_bytes())
    elif surface == "legacy":
        destination = root / "active-reconcile" / f"{job_id}.json"
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(direct.read_bytes())
    else:
        source_journal = template.root / "journal" / "gfs" / "2026071200.jsonl"
        destination = root / "journal" / "gfs" / "2026071200.jsonl"
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(source_journal.read_bytes())

    with pytest.raises(FileOrchestrationJournalError, match="file_journal_rollforward_required"):
        FileOrchestrationJournalRepository(root).query_inflight_jobs()

    with pytest.raises(FileOrchestrationJournalError, match="file_journal_rollforward_jobs_unsettled"):
        complete_file_journal_rollforward(
            journal_root=root,
            workspace_root=workspace,
            preparation_receipt_id=receipt["receipt_id"],
        )
    assert not (root / "reconcile-inventory" / f"{job_id}.json").exists()
    destination.unlink()
    rollforward = complete_file_journal_rollforward(
        journal_root=root,
        workspace_root=workspace,
        preparation_receipt_id=receipt["receipt_id"],
    )
    assert rollforward["preparation_receipt_id"] == receipt["receipt_id"]
    reopened = FileOrchestrationJournalRepository(root)
    assert reopened.query_inflight_jobs() == []
    assert reopened.query_reserved_unbound_jobs() == []
    assert not (root / "reconcile-inventory" / f"{job_id}.json").exists()
    assert marker.is_file()
    with pytest.raises(FileOrchestrationJournalError, match="file_journal_rollback_not_prepared"):
        require_file_journal_rollback_prepared(
            journal_root=root,
            workspace_root=workspace,
            receipt_id=receipt["receipt_id"],
            actual_writer_generation="a" * 40,
        )

    steady = FileOrchestrationJournalRepository(root)

    def backfill_forbidden() -> str:
        raise AssertionError("migration marker must make steady-state history replay impossible")

    monkeypatch.setattr(steady, "_stable_backfill_reconcile_inventory_unlocked", backfill_forbidden)
    assert steady.query_inflight_jobs() == []
    assert steady.query_reserved_unbound_jobs() == []


def test_round12_live_scheduler_lease_blocks_prepare_without_mutating_authority(
    tmp_path: Any,
) -> None:
    from services.orchestrator.file_orchestration_journal import (
        FileOrchestrationJournalError,
        FileOrchestrationJournalRepository,
    )
    from services.orchestrator.file_orchestration_migration import prepare_file_journal_rollback
    from services.orchestrator.scheduler_lease import FileSchedulerLease

    repository = FileOrchestrationJournalRepository(tmp_path / "journal")
    assert repository.query_inflight_jobs() == []
    marker = repository.root / "reconcile-inventory-migration-v1.json"
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    lock_path = workspace / "scheduler" / "production-scheduler.lock"
    holder = FileSchedulerLease(lock_path, ttl_seconds=60, workspace_root=workspace)
    assert holder.acquire(
        pass_id="live-production-scheduler",
        started_at=datetime.now(UTC),
    )["acquired"] is True
    assert marker.is_file()
    marker_before = marker.read_bytes()
    receipt_path = repository.root / "reconcile-inventory-rollback-preparation-v2.json"

    try:
        with pytest.raises(FileOrchestrationJournalError, match="file_journal_scheduler_lease_contended"):
            prepare_file_journal_rollback(
                journal_root=repository.root,
                workspace_root=workspace,
                scheduler_state="stopped",
                active_scheduler_processes=0,
                checked_at=datetime.now(UTC),
                checked_by="round12-test-operator",
                target_writer_generation="a" * 40,
            )
    finally:
        holder.release(pass_id="live-production-scheduler")

    assert marker.read_bytes() == marker_before
    assert not receipt_path.exists()


def test_round12_concurrent_prepare_holds_the_real_scheduler_mutation_authority(
    tmp_path: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:

    from services.orchestrator.file_orchestration_journal import FileOrchestrationJournalRepository
    from services.orchestrator.file_orchestration_migration import prepare_file_journal_rollback
    from services.orchestrator.scheduler_lease import FileSchedulerLease

    root = tmp_path / "journal"
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    repository = FileOrchestrationJournalRepository(root)
    assert repository.query_inflight_jobs() == []
    marker = root / "reconcile-inventory-migration-v1.json"
    marker_before = marker.read_bytes()
    entered = threading.Event()
    continue_prepare = threading.Event()
    original_prepare = (
        FileOrchestrationJournalRepository._prepare_reconcile_inventory_rollback_under_scheduler_lease
    )

    def pause_after_lease_acquisition(self: Any, **kwargs: Any) -> dict[str, Any]:
        entered.set()
        assert continue_prepare.wait(timeout=5)
        return original_prepare(self, **kwargs)

    monkeypatch.setattr(
        FileOrchestrationJournalRepository,
        "_prepare_reconcile_inventory_rollback_under_scheduler_lease",
        pause_after_lease_acquisition,
    )
    outcome: dict[str, Any] = {}

    def prepare() -> None:
        outcome.update(
            prepare_file_journal_rollback(
                journal_root=root,
                workspace_root=workspace,
                scheduler_state="stopped",
                active_scheduler_processes=0,
                checked_at=datetime.now(UTC),
                checked_by="round12-test-operator",
                target_writer_generation="a" * 40,
            )
        )

    thread = threading.Thread(target=prepare)
    thread.start()
    assert entered.wait(timeout=5)
    contender = FileSchedulerLease(
        workspace / "scheduler" / "production-scheduler.lock",
        ttl_seconds=60,
        workspace_root=workspace,
    )
    contender_result = contender.acquire(
        pass_id="concurrent-current-scheduler",
        started_at=datetime.now(UTC),
    )
    assert contender_result["acquired"] is False
    assert marker.read_bytes() == marker_before

    continue_prepare.set()
    thread.join(timeout=5)
    assert not thread.is_alive()
    assert outcome["status"] == "prepared"
    assert not marker.exists()


def test_round16_prepare_rejects_non_full_git_generations_before_any_mutation(
    tmp_path: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
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
    marker = root / "reconcile-inventory-migration-v1.json"
    marker_before = marker.read_bytes()
    receipt_path = root / "reconcile-inventory-rollback-preparation-v2.json"
    lock_path = workspace / "scheduler" / "production-scheduler.lock"
    starts: list[tuple[str, ...]] = []

    def runner(command: tuple[str, ...], **_kwargs: Any) -> Any:
        starts.append(command)
        return type("Completed", (), {"returncode": 0})()

    monkeypatch.setattr(migration_module, "_run_rollback_writer", runner)
    invalid_generations = (
        "deadbee",
        "HEAD",
        "refs/heads/main",
        "arbitrary-generation",
        f"{'a' * 40}-dirty",
    )
    for generation in invalid_generations:
        with pytest.raises(
            FileOrchestrationJournalError,
            match="file_journal_rollback_target_writer_generation_invalid",
        ):
            prepare_file_journal_rollback(
                journal_root=root,
                workspace_root=workspace,
                scheduler_state="stopped",
                active_scheduler_processes=0,
                checked_at=datetime.now(UTC),
                checked_by="round16-operator",
                target_writer_generation=generation,
            )
        with pytest.raises(
            FileOrchestrationJournalError,
            match="file_journal_rollback_target_writer_generation_invalid",
        ):
            repository._prepare_reconcile_inventory_rollback_under_scheduler_lease(
                scheduler_lease_identity={},
                scheduler_lease_guard=lambda: True,
                scheduler_state="stopped",
                active_scheduler_processes=0,
                checked_at=datetime.now(UTC),
                checked_by="round16-core",
                target_writer_generation=generation,
            )
        assert marker.read_bytes() == marker_before
        assert not receipt_path.exists()
        assert not lock_path.exists()
        assert repository.current_generation_scheduler_rollback_blocker() is None
        assert starts == []


@pytest.mark.parametrize("missing_field", ["target_python_runtime", "target_python_source_root"])
def test_round20_rollforward_rejects_missing_bound_artifact_without_mutation(
    tmp_path: Any,
    missing_field: str,
) -> None:

    from packages.common.rollback_execution_binding import read_rollback_execution_binding
    from services.orchestrator.file_orchestration_journal import (
        FileOrchestrationJournalError,
        FileOrchestrationJournalRepository,
    )
    from services.orchestrator.file_orchestration_migration import (
        complete_file_journal_rollforward,
        prepare_file_journal_rollback,
    )

    root = tmp_path / missing_field / "journal"
    workspace = tmp_path / missing_field / "workspace"
    workspace.mkdir(parents=True)
    repository = FileOrchestrationJournalRepository(root)
    assert repository.query_inflight_jobs() == []
    receipt = prepare_file_journal_rollback(
        journal_root=root,
        workspace_root=workspace,
        scheduler_state="stopped",
        active_scheduler_processes=0,
        checked_at=datetime.now(UTC),
        checked_by="round20-operator",
        target_writer_generation="a" * 40,
    )
    binding = _round20_write_execution_binding(
        workspace=workspace,
        receipt=receipt,
        generation="a" * 40,
    )
    artifact = Path(binding[missing_field])
    artifact.parent.chmod(0o700)
    artifact.chmod(0o700)
    os.replace(artifact, artifact.with_name(f"{artifact.name}.missing"))
    fence = root / "reconcile-inventory-rollback-preparation-v2.json"
    fence_before = fence.read_bytes()

    with pytest.raises(
        FileOrchestrationJournalError,
        match="file_journal_rollforward_execution_binding_invalid",
    ):
        complete_file_journal_rollforward(
            journal_root=root,
            workspace_root=workspace,
            preparation_receipt_id=receipt["receipt_id"],
        )

    assert fence.read_bytes() == fence_before
    persisted = read_rollback_execution_binding(workspace, require_artifacts=False)
    assert persisted is not None and persisted["status"] == "active"


def test_round20_rollforward_query_failure_leaves_fence_and_binding_active(
    tmp_path: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from packages.common.rollback_execution_binding import rollback_execution_binding_path
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
        checked_by="round20-query-failure-operator",
        target_writer_generation="a" * 40,
    )
    _round20_write_execution_binding(
        workspace=workspace,
        receipt=receipt,
        generation="a" * 40,
    )
    fence = root / "reconcile-inventory-rollback-preparation-v2.json"
    binding_path = rollback_execution_binding_path(workspace)
    fence_before = fence.read_bytes()
    binding_before = binding_path.read_bytes()

    def unavailable(_self: Any) -> list[Any]:
        raise OSError("injected global rollback-job query failure")

    monkeypatch.setattr(
        FileOrchestrationJournalRepository,
        "query_rollback_unsettled_jobs",
        unavailable,
    )
    with pytest.raises(
        FileOrchestrationJournalError,
        match="file_journal_rollforward_quiescence_unavailable",
    ):
        complete_file_journal_rollforward(
            journal_root=root,
            workspace_root=workspace,
            preparation_receipt_id=receipt["receipt_id"],
        )

    assert fence.read_bytes() == fence_before
    assert binding_path.read_bytes() == binding_before
    assert not (root / "reconcile-inventory").exists()


@pytest.mark.parametrize("damage", ["deleted", "tampered"])
def test_round20_prepare_only_authority_damage_blocks_rollforward_without_fence_mutation(
    tmp_path: Any,
    damage: str,
) -> None:
    from packages.common.rollback_execution_binding import rollback_execution_binding_path
    from services.orchestrator.file_orchestration_journal import (
        FileOrchestrationJournalError,
        FileOrchestrationJournalRepository,
    )
    from services.orchestrator.file_orchestration_migration import (
        complete_file_journal_rollforward,
        prepare_file_journal_rollback,
    )

    root = tmp_path / damage / "journal"
    workspace = tmp_path / damage / "workspace"
    workspace.mkdir(parents=True)
    repository = FileOrchestrationJournalRepository(root)
    assert repository.query_inflight_jobs() == []
    receipt = prepare_file_journal_rollback(
        journal_root=root,
        workspace_root=workspace,
        scheduler_state="stopped",
        active_scheduler_processes=0,
        checked_at=datetime.now(UTC),
        checked_by="round20-prepared-authority-negative",
        target_writer_generation="a" * 40,
    )
    authority_path = rollback_execution_binding_path(workspace)
    authority = json.loads(authority_path.read_text(encoding="utf-8"))
    assert authority["status"] == "prepared"
    assert authority["preparation_receipt_id"] == receipt["receipt_id"]
    fence = root / "reconcile-inventory-rollback-preparation-v2.json"
    fence_before = fence.read_bytes()
    if damage == "deleted":
        authority_path.unlink()
    else:
        authority["preparation_receipt_id"] = "f" * 64
        authority_path.write_text(json.dumps(authority), encoding="utf-8")
        authority_path.chmod(0o600)

    with pytest.raises(
        FileOrchestrationJournalError,
        match="file_journal_rollforward_execution_binding_invalid",
    ):
        complete_file_journal_rollforward(
            journal_root=root,
            workspace_root=workspace,
            preparation_receipt_id=receipt["receipt_id"],
        )

    assert fence.read_bytes() == fence_before
    assert not (root / "reconcile-inventory").exists()
