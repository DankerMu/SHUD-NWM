"""Rollback/rollforward receipts: CLI receipt emission, tamper and
wrong-root fail-closed receipts.
"""

from __future__ import annotations

import copy
import json
from datetime import (
    UTC,
    datetime,
    timedelta,
)
from pathlib import Path
from typing import Any

import pytest

from tests.gateway_reconcile_helpers import _file_cohort_repository
from tests.gateway_reconcile_writer_helpers import (
    _round14_clean_writer_checkout,
    _round20_write_execution_binding,
)


@pytest.mark.parametrize("entrypoint", ["click", "argparse"])
def test_round12_rollback_commands_emit_verifiable_receipts(
    tmp_path: Any,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
    entrypoint: str,
) -> None:
    import inspect

    from services.orchestrator import cli as cli_module
    from services.orchestrator import file_orchestration_migration as migration_module
    from services.orchestrator.file_orchestration_journal import (
        FileOrchestrationJournalError,
        FileOrchestrationJournalRepository,
    )
    from services.orchestrator.file_orchestration_migration import (
        require_file_journal_rollback_prepared,
    )

    root = tmp_path / entrypoint / "journal"
    workspace = tmp_path / entrypoint / "workspace"
    workspace.mkdir(parents=True)
    repository = FileOrchestrationJournalRepository(root)
    assert repository.query_inflight_jobs() == []
    assert "actual_writer_generation" not in inspect.signature(
        cli_module._launch_file_journal_rollback_writer
    ).parameters
    checkout_a, generation_a = _round14_clean_writer_checkout(
        tmp_path / entrypoint / "writer-a",
        content="writer A\n",
    )
    checkout_b, _generation_b = _round14_clean_writer_checkout(
        tmp_path / entrypoint / "writer-b",
        content="writer B\n",
    )

    argv = [
        "prepare-file-journal-rollback",
        "--journal-root",
        str(root),
        "--workspace-root",
        str(workspace),
        "--scheduler-state",
        "stopped",
        "--active-scheduler-processes",
        "0",
        "--checked-at",
        "2026-07-23T12:00:00Z",
        "--checked-by",
        "node-22-operator",
        "--target-writer-generation",
        generation_a,
    ]
    result = (
        cli_module._click_main(argv)
        if entrypoint == "click"
        else cli_module._argparse_main(argv)
    )

    assert result == 0
    receipt = json.loads(capsys.readouterr().out)
    assert receipt["status"] == "prepared"
    assert receipt["preflight"]["dry_run"] is False
    assert require_file_journal_rollback_prepared(
        journal_root=root,
        workspace_root=workspace,
        receipt_id=receipt["receipt_id"],
        actual_writer_generation=generation_a,
    )["receipt_id"] == receipt["receipt_id"]

    started: list[tuple[tuple[str, ...], Any]] = []

    def run_writer(
        argv: tuple[str, ...],
        *,
        cwd: Any,
        check: bool,
        env: Any,
        pass_fds: Any,
    ) -> Any:
        assert check is False
        assert env["NHMS_TARGET_PYTHON_RUNTIME"] == argv[0]
        assert len(pass_fds) == 1
        started.append((argv, cwd))
        return type("Completed", (), {"returncode": 0})()

    monkeypatch.setattr(migration_module, "_run_rollback_writer", run_writer)
    launch_prefix = [
        "launch-file-journal-rollback-writer",
        "--journal-root",
        str(root),
        "--workspace-root",
        str(workspace),
        "--receipt-id",
        receipt["receipt_id"],
        "--writer-repository-root",
        str(checkout_a),
        "--",
    ]
    for invalid_writer_args in (
        ("plan-production",),
        ("plan-production", "--plan"),
        ("plan-production", "--dry-run"),
        ("plan-production", "--submit", "--help"),
        ("plan-production", "--submit", "-h"),
        ("plan-production", "--submit", "--version"),
        ("plan-production", "--submit", "--workspace-root", str(workspace)),
        ("plan-production", "--submit", "--lock-path", str(workspace / "other.lock")),
        ("trigger-analysis", "--submit"),
    ):
        invalid_argv = [*launch_prefix, *invalid_writer_args]
        if entrypoint == "click":
            with pytest.raises(SystemExit) as error:
                cli_module._click_main(invalid_argv)
            assert error.value.code == 2
        else:
            assert cli_module._argparse_main(invalid_argv) == 2
        capsys.readouterr()
        assert started == []

    launch_argv = [
        *launch_prefix,
        "plan-production",
        "--submit",
    ]
    result = (
        cli_module._click_main(launch_argv)
        if entrypoint == "click"
        else cli_module._argparse_main(launch_argv)
    )
    assert result == 0
    launch = json.loads(capsys.readouterr().out)
    assert launch["actual_writer_generation"] == generation_a
    assert launch["writer_repository_root"] == str(checkout_a.resolve())
    assert Path(launch["target_python_runtime"]).is_file()
    assert launch["target_python_runtime_retention"] == ("retained_fail_closed_until_operator_cleanup")
    assert launch["dry_run"] is False
    assert len(started) == 1
    assert started[0][1] != checkout_a.resolve()
    assert started[0][1] == Path(launch["target_python_source_root"])
    assert started[0][1].is_dir()

    mismatch_argv = list(launch_argv)
    mismatch_argv[mismatch_argv.index(str(checkout_a))] = str(checkout_b)
    if entrypoint == "click":
        with pytest.raises(SystemExit) as error:
            cli_module._click_main(mismatch_argv)
        assert error.value.code == 2
    else:
        assert cli_module._argparse_main(mismatch_argv) == 2
    capsys.readouterr()
    assert len(started) == 1

    rollforward_argv = [
        "complete-file-journal-rollforward",
        "--journal-root",
        str(root),
        "--workspace-root",
        str(workspace),
        "--preparation-receipt-id",
        receipt["receipt_id"],
    ]
    result = (
        cli_module._click_main(rollforward_argv)
        if entrypoint == "click"
        else cli_module._argparse_main(rollforward_argv)
    )
    assert result == 0
    rollforward = json.loads(capsys.readouterr().out)
    assert rollforward["preparation_receipt_id"] == receipt["receipt_id"]
    with pytest.raises(FileOrchestrationJournalError, match="file_journal_rollback_not_prepared"):
        require_file_journal_rollback_prepared(
            journal_root=root,
            workspace_root=workspace,
            receipt_id=receipt["receipt_id"],
            actual_writer_generation=generation_a,
        )


def test_round12_tampered_and_wrong_root_receipts_fail_closed(tmp_path: Any) -> None:
    from services.orchestrator.file_orchestration_journal import (
        FileOrchestrationJournalError,
        FileOrchestrationJournalRepository,
    )
    from services.orchestrator.file_orchestration_migration import (
        complete_file_journal_rollforward,
        prepare_file_journal_rollback,
        require_file_journal_rollback_prepared,
    )

    root = tmp_path / "source" / "journal"
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
        checked_by="round12-test-operator",
        target_writer_generation="a" * 40,
    )
    _round20_write_execution_binding(
        workspace=workspace,
        receipt=receipt,
        generation="a" * 40,
    )
    receipt_path = root / "reconcile-inventory-rollback-preparation-v2.json"
    original = receipt_path.read_bytes()
    malformed = json.loads(original)
    malformed["receipt_id"] = "0" * 64
    receipt_path.write_text(json.dumps(malformed), encoding="utf-8")
    with pytest.raises(FileOrchestrationJournalError, match="file_journal_rollback_receipt_invalid"):
        require_file_journal_rollback_prepared(
            journal_root=root,
            workspace_root=workspace,
            receipt_id=receipt["receipt_id"],
            actual_writer_generation="a" * 40,
        )
    with pytest.raises(FileOrchestrationJournalError, match="file_journal_rollback_receipt_invalid"):
        complete_file_journal_rollforward(
            journal_root=root,
            workspace_root=workspace,
            preparation_receipt_id=receipt["receipt_id"],
        )
    receipt_path.write_bytes(original)
    with pytest.raises(
        FileOrchestrationJournalError,
        match="file_journal_rollforward_execution_binding_conflict",
    ):
        complete_file_journal_rollforward(
            journal_root=root,
            workspace_root=workspace,
            preparation_receipt_id="f" * 64,
        )

    wrong_root = tmp_path / "wrong" / "journal"
    wrong_repository = FileOrchestrationJournalRepository(wrong_root)
    assert wrong_repository.query_inflight_jobs() == []
    (wrong_root / "reconcile-inventory-migration-v1.json").unlink()
    wrong_receipt_path = wrong_root / receipt_path.name
    wrong_receipt_path.write_bytes(original)
    with pytest.raises(FileOrchestrationJournalError, match="file_journal_rollback_receipt_wrong_root"):
        require_file_journal_rollback_prepared(
            journal_root=wrong_root,
            workspace_root=workspace,
            receipt_id=receipt["receipt_id"],
            actual_writer_generation="a" * 40,
        )
    with pytest.raises(
        FileOrchestrationJournalError,
        match="file_journal_rollforward_execution_binding_conflict",
    ):
        complete_file_journal_rollforward(
            journal_root=wrong_root,
            workspace_root=workspace,
            preparation_receipt_id=receipt["receipt_id"],
        )


def test_round8_legacy_active_migration_retains_oldest_across_513_rows(
    tmp_path: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from services.orchestrator import file_orchestration_journal as journal_module
    from services.orchestrator.reservation import slurm_comment_for

    repository = _file_cohort_repository(
        tmp_path / "template",
        member_count=1,
        with_runtime_rows=False,
        versioned=False,
    )
    template_job_id = "job_cycle_gfs_2026071200_forecast_fixture_forecast"
    template = json.loads(
        (repository.root / "pipeline-jobs" / f"{template_job_id}.json").read_text(encoding="utf-8")
    )
    target_root = tmp_path / "target" / "journal"
    active = target_root / "active-reconcile"
    active.mkdir(parents=True)
    expected_ids: list[str] = []
    for index in range(513):
        job_id = f"job_legacy_active_{index:04d}"
        run_id = f"cycle_gfs_2026071200_legacy_active_{index:04d}"
        idempotency_key = f"{run_id}:forecast"
        created_at = datetime(2025, 1, 1, tzinfo=UTC) + timedelta(seconds=index)
        created = created_at.isoformat().replace("+00:00", "Z")
        record = copy.deepcopy(template)
        record.update({"job_id": job_id, "run_id": run_id})
        record["payload"].update(
            {
                "job_id": job_id,
                "run_id": run_id,
                "idempotency_key": idempotency_key,
                "slurm_comment": slurm_comment_for(idempotency_key),
                "created_at": created,
                "updated_at": created,
            }
        )
        (active / f"{job_id}.json").write_text(json.dumps(record, sort_keys=True), encoding="utf-8")
        expected_ids.append(job_id)

    reopened = type(repository)(target_root)
    recovered = reopened.query_reserved_unbound_jobs()
    assert len(recovered) == 513
    assert recovered[0].job_id == expected_ids[0]
    assert {job.job_id for job in recovered} == set(expected_ids)
    assert len(tuple((target_root / "reconcile-inventory").glob("*.json"))) == 513

    # A fresh steady-state process must trust the completed migration marker
    # for discovery and enumerate only the bounded inventory. Exact authority
    # reads may still validate an anchor, but no legacy/direct/journal history
    # walker is allowed to restart the one-time migration.
    steady = type(repository)(target_root)
    history_discovery_calls: list[str] = []

    def history_discovery_forbidden(*_args: Any, **_kwargs: Any) -> Any:
        history_discovery_calls.append("called")
        raise AssertionError("steady reopen must not enumerate migration history")

    monkeypatch.setattr(steady, "_iter_legacy_active_reconcile_records", history_discovery_forbidden)
    monkeypatch.setattr(steady, "_iter_reconcile_direct_pipeline_job_records", history_discovery_forbidden)
    monkeypatch.setattr(journal_module, "_iter_jsonl_files", history_discovery_forbidden)
    steady_recovered = steady.query_reserved_unbound_jobs()

    assert history_discovery_calls == []
    assert len(steady_recovered) == 513
    assert steady_recovered[0].job_id == expected_ids[0]
    assert {job.job_id for job in steady_recovered} == set(expected_ids)
