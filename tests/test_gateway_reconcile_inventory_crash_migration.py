"""Reconcile inventory: crash residue, fail-closed temp entries and migration repair.

Partition of ``tests/test_gateway_reconcile_inventory.py`` (#3177): atomic temp
crash residue after a real child kill, unknown or malformed temp siblings, the
migration marker held until every authority surface is repaired, and the
migration handoff anchor.
"""

from __future__ import annotations

import os
from datetime import (
    UTC,
    datetime,
    timedelta,
)
from typing import Any

import pytest

from tests.gateway_reconcile_helpers import _file_cohort_repository


@pytest.mark.parametrize(
    "target_kind",
    ["inventory", "migration_marker", "rollback_receipt", "rollforward_receipt"],
)
def test_round8_atomic_temp_crash_residue_is_cleaned_after_real_child_kill(
    tmp_path: Any,
    target_kind: str,
) -> None:
    import signal
    import subprocess
    import sys

    repository = _file_cohort_repository(tmp_path, member_count=1, with_runtime_rows=False)
    job_id = "job_cycle_gfs_2026071200_forecast_fixture_forecast"
    assert [job.job_id for job in repository.query_reserved_unbound_jobs()] == [job_id]
    if target_kind == "inventory":
        target = repository.root / "reconcile-inventory" / f"{job_id}.json"
    elif target_kind == "rollback_receipt":
        target = repository.root / "reconcile-inventory-rollback-preparation-v2.json"
    elif target_kind == "rollforward_receipt":
        target = repository.root / "reconcile-inventory-rollforward-v1.json"
    else:
        target = repository.root / "reconcile-inventory-migration-v1.json"
        target.unlink()
    script = """
import os, signal, sys
from pathlib import Path
from packages.common import safe_fs
target = Path(sys.argv[1])
root = Path(sys.argv[2])
def crash(*_args, **_kwargs):
    os.kill(os.getpid(), signal.SIGKILL)
safe_fs.os.replace = crash
safe_fs.atomic_write_bytes_no_follow(
    target, b'{}', containment_root=root, require_durable_replace=True
)
"""
    completed = subprocess.run(
        [sys.executable, "-c", script, str(target), str(repository.root)],
        cwd=os.getcwd(),
        check=False,
    )
    assert completed.returncode == -signal.SIGKILL
    residues = tuple(target.parent.glob(f".{target.name}.*.tmp"))
    assert len(residues) == 1

    reopened = type(repository)(repository.root)
    assert [job.job_id for job in reopened.query_reserved_unbound_jobs()] == [job_id]
    assert not residues[0].exists()
    assert (repository.root / "reconcile-inventory-migration-v1.json").is_file()


@pytest.mark.parametrize(
    ("entry_name", "make_symlink"),
    [
        ("unknown.tmp", False),
        (".job_cycle_gfs_2026071200_forecast_fixture_forecast.json.bad.tmp", False),
        (
            ".job_cycle_gfs_2026071200_forecast_fixture_forecast.json."
            "0123456789abcdef0123456789abcdef.tmp",
            True,
        ),
    ],
)
def test_round8_inventory_unknown_or_nonregular_temp_entry_fails_closed(
    tmp_path: Any,
    entry_name: str,
    make_symlink: bool,
) -> None:
    from services.orchestrator.file_orchestration_journal import FileOrchestrationJournalError

    repository = _file_cohort_repository(tmp_path, member_count=1, with_runtime_rows=False)
    assert len(repository.query_reserved_unbound_jobs()) == 1
    entry = repository.root / "reconcile-inventory" / entry_name
    if make_symlink:
        entry.symlink_to(repository.root / "reconcile-inventory-migration-v1.json")
    else:
        entry.write_text("residue", encoding="utf-8")

    with pytest.raises(FileOrchestrationJournalError):
        type(repository)(repository.root).query_reserved_unbound_jobs()
    assert entry.exists() or entry.is_symlink()


def test_round8_migration_marker_malformed_temp_sibling_fails_closed(tmp_path: Any) -> None:
    from services.orchestrator.file_orchestration_journal import FileOrchestrationJournalError

    repository = _file_cohort_repository(tmp_path, member_count=1, with_runtime_rows=False)
    marker = repository.root / "reconcile-inventory-migration-v1.json"
    marker.unlink(missing_ok=True)
    sibling = repository.root / ".reconcile-inventory-migration-v1.json.bad.tmp"
    sibling.write_text("residue", encoding="utf-8")
    with pytest.raises(FileOrchestrationJournalError):
        type(repository)(repository.root).query_reserved_unbound_jobs()
    assert sibling.is_file()
    assert not marker.exists()


@pytest.mark.parametrize(
    "surface",
    [
        "direct",
        "direct_bytes",
        "direct_nonregular",
        "direct_unreadable",
        "journal",
        "journal_records",
        "journal_unreadable",
        "legacy",
        "over_limit",
    ],
)
def test_round8_migration_blocks_marker_until_every_authority_surface_is_repaired(
    tmp_path: Any,
    monkeypatch: pytest.MonkeyPatch,
    surface: str,
) -> None:
    from services.orchestrator import file_orchestration_journal as journal_module
    from services.orchestrator.file_orchestration_journal import (
        FileOrchestrationJournalError,
        FileOrchestrationJournalRepository,
    )

    repository = _file_cohort_repository(tmp_path, member_count=1, with_runtime_rows=False)
    job_id = "job_cycle_gfs_2026071200_forecast_fixture_forecast"
    marker = repository.root / "reconcile-inventory-migration-v1.json"
    marker.unlink(missing_ok=True)
    for path in (repository.root / "reconcile-inventory").glob("*.json"):
        path.unlink()
    repairs: list[tuple[Any, bytes | None]] = []
    unreadable_path = None
    if surface in {"direct", "direct_bytes", "direct_nonregular", "direct_unreadable"}:
        path = repository.root / "pipeline-jobs" / f"{job_id}.json"
        if surface != "direct_unreadable":
            repairs.append((path, path.read_bytes()))
        if surface == "direct_nonregular":
            path.unlink()
            path.symlink_to(repository.root / "journal" / "gfs" / "2026071200.jsonl")
        elif surface == "direct_bytes":
            path.write_bytes(b"x" * 65)
        elif surface == "direct_unreadable":
            unreadable_path = path
        else:
            path.write_bytes(b"{not-json")
    elif surface in {"journal", "journal_records", "journal_unreadable"}:
        path = repository.root / "journal" / "gfs" / "2026071200.jsonl"
        if surface != "journal_unreadable":
            repairs.append((path, path.read_bytes()))
        if surface == "journal_records":
            path.write_bytes(path.read_bytes() + path.read_bytes())
        elif surface == "journal_unreadable":
            unreadable_path = path
        else:
            path.write_bytes(b"{not-json\n")
    elif surface == "legacy":
        path = repository.root / "active-reconcile" / "malformed.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        repairs.append((path, None))
        path.write_bytes(b"{not-json")
    else:
        path = repository.root / "pipeline-jobs" / "extra.json"
        repairs.append((path, None))
        path.write_text("{}", encoding="utf-8")

    limits: dict[str, int] = {}
    original_record_limit = journal_module.MAX_FILE_JOURNAL_RECORDS
    if surface == "over_limit":
        limits["max_files"] = 1
    elif surface == "direct_bytes":
        limits["max_bytes"] = 64
    elif surface == "journal_records":
        monkeypatch.setattr(journal_module, "MAX_FILE_JOURNAL_RECORDS", 1)
    original_read = journal_module.read_bytes_limited_no_follow
    if unreadable_path is not None:

        def deny_authority_read(path: Any, **kwargs: Any) -> Any:
            if path == unreadable_path:
                raise PermissionError("injected unreadable authority surface")
            return original_read(path, **kwargs)

        monkeypatch.setattr(journal_module, "read_bytes_limited_no_follow", deny_authority_read)
    reopened = FileOrchestrationJournalRepository(repository.root, **limits)
    with pytest.raises(FileOrchestrationJournalError):
        reopened.query_reserved_unbound_jobs()
    assert not marker.exists()

    for path, content in repairs:
        if path.is_symlink():
            path.unlink()
        if content is None:
            path.unlink()
        else:
            path.write_bytes(content)
    if surface == "journal_records":
        monkeypatch.setattr(journal_module, "MAX_FILE_JOURNAL_RECORDS", original_record_limit)
    if unreadable_path is not None:
        monkeypatch.setattr(journal_module, "read_bytes_limited_no_follow", original_read)
    repaired = FileOrchestrationJournalRepository(repository.root)
    assert [job.job_id for job in repaired.query_reserved_unbound_jobs()] == [job_id]
    assert marker.is_file()


def test_round8_migration_discovers_journal_only_active_row(tmp_path: Any) -> None:
    repository = _file_cohort_repository(tmp_path, member_count=1, with_runtime_rows=False)
    job_id = "job_cycle_gfs_2026071200_forecast_fixture_forecast"
    (repository.root / "pipeline-jobs" / f"{job_id}.json").unlink()
    (repository.root / "reconcile-inventory-migration-v1.json").unlink(missing_ok=True)
    for path in (repository.root / "reconcile-inventory").glob("*.json"):
        path.unlink()

    reopened = type(repository)(repository.root)
    assert [job.job_id for job in reopened.query_reserved_unbound_jobs()] == [job_id]


@pytest.mark.parametrize("surface", ["journal", "legacy"])
@pytest.mark.parametrize("boundary", ["stat", "read"])
def test_round11_migration_disappearance_fails_without_marker_and_reopens_after_repair(
    tmp_path: Any,
    monkeypatch: pytest.MonkeyPatch,
    surface: str,
    boundary: str,
) -> None:
    from services.orchestrator.file_orchestration_journal import (
        FileOrchestrationJournalError,
        FileOrchestrationJournalRepository,
    )

    template = _file_cohort_repository(
        tmp_path / "template",
        member_count=1,
        with_runtime_rows=False,
    )
    job_id = "job_cycle_gfs_2026071200_forecast_fixture_forecast"
    direct = template.root / "pipeline-jobs" / f"{job_id}.json"
    journal = template.root / "journal" / "gfs" / "2026071200.jsonl"
    root = tmp_path / f"{surface}-{boundary}" / "journal"
    root.mkdir(parents=True)
    if surface == "journal":
        target = root / "journal" / "gfs" / "2026071200.jsonl"
        target.parent.mkdir(parents=True)
        target.write_bytes(journal.read_bytes())
    else:
        target = root / "active-reconcile" / f"{job_id}.json"
        target.parent.mkdir(parents=True)
        target.write_bytes(direct.read_bytes())
    original = target.read_bytes()
    repository = FileOrchestrationJournalRepository(root)
    marker = root / "reconcile-inventory-migration-v1.json"

    if boundary == "stat":
        iterator_name = (
            "_iter_migration_journal_paths"
            if surface == "journal"
            else "_iter_migration_legacy_active_paths"
        )
        original_iterator = getattr(repository, iterator_name)

        def disappear_after_enumeration() -> list[Any]:
            paths = original_iterator()
            target.unlink()
            return paths

        monkeypatch.setattr(repository, iterator_name, disappear_after_enumeration)
    elif surface == "journal":
        original_read = repository._read_jsonl

        def disappear_after_journal_read(path: Any, *, segment_index: int = 0) -> list[dict[str, Any]]:
            records = original_read(path, segment_index=segment_index)
            if path == target:
                target.unlink()
            return records

        monkeypatch.setattr(repository, "_read_jsonl", disappear_after_journal_read)
    else:
        original_read = repository._read_optional_json

        def disappear_after_legacy_read(path: Any) -> dict[str, Any] | None:
            payload = original_read(path)
            if path == target:
                target.unlink()
            return payload

        monkeypatch.setattr(repository, "_read_optional_json", disappear_after_legacy_read)

    with pytest.raises(FileOrchestrationJournalError):
        repository.query_reserved_unbound_jobs()
    assert repository._reconcile_inventory_migration_checked is False
    assert not marker.exists()

    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(original)
    repaired = FileOrchestrationJournalRepository(root)
    assert [job.job_id for job in repaired.query_reserved_unbound_jobs()] == [job_id]
    assert marker.is_file()


def test_migration_handoff_anchor_preserves_locator_for_settled_master_with_missing_direct(
    tmp_path: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Phase 6i migration-lane handoff: a settled current forecast master with
    a MISSING flat derived direct must leave a handoff anchor (not prune the
    only locator), the migration marker must complete with an UNCHANGED
    authority fingerprint, and the steady-state iterator must then restore the
    direct and prune the anchor. Crash/resume before the marker must stay
    idempotent. Healthy terminal rows (direct present) must still prune."""

    from services.orchestrator.accepted_submit_identity import AcceptedSubmitTransition
    from services.orchestrator.file_orchestration_journal import FileOrchestrationJournalRepository

    anchor = datetime(2026, 7, 12, tzinfo=UTC)
    repository = _file_cohort_repository(
        tmp_path / "ifs-seed",
        created_at=anchor,
        member_count=1,
        expected_user="scheduler",
        expected_account="account",
        source_id="ifs",
    )
    ifs_job_id = "job_cycle_ifs_2026071200_forecast_fixture_forecast"
    ifs_key = "cycle_ifs_2026071200_forecast_fixture:forecast"
    bound = repository.commit_pipeline_job_submit_attempt(
        ifs_key,
        pipeline_job_id=ifs_job_id,
        expected_submission_attempt=1,
        slurm_job_id="72001",
        submitted_at=anchor + timedelta(hours=1),
        slurm_accounting_submitted_at=anchor + timedelta(hours=1),
        transition=AcceptedSubmitTransition.accounting(
            "matched_bound",
            submit_outcome="accepted",
            matched_slurm_job_id="72001",
            status="submitted",
            reconciliation_source="slurm_name_window_unique",
        ),
    )
    assert bound.committed
    assert not (repository.root / "reconcile-inventory-migration-v1.json").exists()
    projections = [
        {
            "candidate_id": repository.get_pipeline_job(ifs_job_id)["cohort_members"][0][
                "candidate_id"
            ],
            "run_id": repository.get_pipeline_job(ifs_job_id)["cohort_members"][0]["run_id"],
            "model_id": repository.get_pipeline_job(ifs_job_id)["cohort_members"][0]["model_id"],
            "array_task_id": repository.get_pipeline_job(ifs_job_id)["cohort_members"][0][
                "array_task_id"
            ],
            "array_task_outcome": "succeeded",
            "task_slurm_job_id": "72001_0",
            "error_code": None,
            "restart_stage": "state_save_qc",
            "native_shud_resubmitted": False,
        }
    ]
    (repository.root / "pipeline-jobs" / f"{ifs_job_id}.json").unlink()
    real_direct = repository._write_pipeline_job_direct_unlocked

    def failing_direct(row: Any, record: Any) -> None:
        job_id = str((row or {}).get("job_id") or "")
        if job_id == ifs_job_id:
            raise OSError("simulated terminal master direct failure")
        return real_direct(row, record)

    repository._write_pipeline_job_direct_unlocked = failing_direct
    try:
        repository.project_forecast_cohort_tasks(
            ifs_job_id,
            master_slurm_job_id="72001",
            projections=projections,
            complete=True,
            master_status="succeeded",
            master_error_code=None,
            reconciliation_decision="matched_bound",
        )
    except Exception:
        pass
    finally:
        repository._write_pipeline_job_direct_unlocked = real_direct

    # Crash state: settled canonical, stale anchor, missing direct, no marker.
    assert not (repository.root / "pipeline-jobs" / f"{ifs_job_id}.json").exists()
    assert (repository.root / "reconcile-inventory" / f"{ifs_job_id}.json").exists()
    assert not (repository.root / "reconcile-inventory-migration-v1.json").exists()

    # Run the stable backfill: fingerprint unchanged, handoff anchor retained.
    fresh = FileOrchestrationJournalRepository(repository.root)
    before = fresh._reconcile_authority_fingerprint_unlocked()
    fingerprint = fresh._stable_backfill_reconcile_inventory_unlocked()
    assert fingerprint == before, "authority fingerprint must be byte-identical"
    assert (
        repository.root / "reconcile-inventory" / f"{ifs_job_id}.json"
    ).exists(), "migration must retain the handoff anchor for a missing-direct settled master"
    assert not (
        repository.root / "pipeline-jobs" / f"{ifs_job_id}.json"
    ).exists(), "backfill alone must not restore the direct"

    # Steady-state iterator: restore direct then prune anchor.
    rows = list(fresh._iter_reconcile_inventory_records())
    assert rows == []
    assert (
        repository.root / "pipeline-jobs" / f"{ifs_job_id}.json"
    ).exists(), "steady-state handoff must restore the derived direct"
    assert not (
        repository.root / "reconcile-inventory" / f"{ifs_job_id}.json"
    ).exists(), "handoff anchor must be pruned after the direct is restored"

    # Crash/resume before the marker: idempotent. Recreate the crash state
    # (missing direct + stale anchor) on a fresh root, then run the migration
    # twice; the second run is a no-op with the same final state.
    repository2 = _file_cohort_repository(
        tmp_path / "ifs-seed2",
        created_at=anchor,
        member_count=1,
        expected_user="scheduler",
        expected_account="account",
        source_id="ifs",
    )
    bound2 = repository2.commit_pipeline_job_submit_attempt(
        ifs_key,
        pipeline_job_id=ifs_job_id,
        expected_submission_attempt=1,
        slurm_job_id="72001",
        submitted_at=anchor + timedelta(hours=1),
        slurm_accounting_submitted_at=anchor + timedelta(hours=1),
        transition=AcceptedSubmitTransition.accounting(
            "matched_bound",
            submit_outcome="accepted",
            matched_slurm_job_id="72001",
            status="submitted",
            reconciliation_source="slurm_name_window_unique",
        ),
    )
    assert bound2.committed
    (repository2.root / "pipeline-jobs" / f"{ifs_job_id}.json").unlink()
    real_direct2 = repository2._write_pipeline_job_direct_unlocked

    def failing_direct2(row: Any, record: Any) -> None:
        if str((row or {}).get("job_id") or "") == ifs_job_id:
            raise OSError("simulated terminal master direct failure")
        return real_direct2(row, record)

    repository2._write_pipeline_job_direct_unlocked = failing_direct2
    try:
        repository2.project_forecast_cohort_tasks(
            ifs_job_id,
            master_slurm_job_id="72001",
            projections=projections,
            complete=True,
            master_status="succeeded",
            master_error_code=None,
            reconciliation_decision="matched_bound",
        )
    except Exception:
        pass
    finally:
        repository2._write_pipeline_job_direct_unlocked = real_direct2

    fresh2 = FileOrchestrationJournalRepository(repository2.root)
    _ = fresh2._stable_backfill_reconcile_inventory_unlocked()
    assert (repository2.root / "reconcile-inventory" / f"{ifs_job_id}.json").exists()
    _ = fresh2._stable_backfill_reconcile_inventory_unlocked()
    assert (repository2.root / "reconcile-inventory" / f"{ifs_job_id}.json").exists()
    list(fresh2._iter_reconcile_inventory_records())
    assert (repository2.root / "pipeline-jobs" / f"{ifs_job_id}.json").exists()
    assert not (repository2.root / "reconcile-inventory" / f"{ifs_job_id}.json").exists()


def test_migration_healthy_terminal_master_with_direct_leaves_no_anchor(
    tmp_path: Any,
) -> None:
    """Phase 6i: terminal current forecast masters WITH healthy flat directs
    must NOT leave handoff anchors — the migration preserves the ordinary prune
    so steady state stays bounded (e.g. 1,460 terminal histories leave no
    anchors)."""

    from services.orchestrator.accepted_submit_identity import AcceptedSubmitTransition
    from services.orchestrator.file_orchestration_journal import FileOrchestrationJournalRepository

    anchor = datetime(2026, 7, 12, tzinfo=UTC)
    repository = _file_cohort_repository(
        tmp_path / "ifs-seed",
        created_at=anchor,
        member_count=1,
        expected_user="scheduler",
        expected_account="account",
        source_id="ifs",
    )
    ifs_job_id = "job_cycle_ifs_2026071200_forecast_fixture_forecast"
    ifs_key = "cycle_ifs_2026071200_forecast_fixture:forecast"
    bound = repository.commit_pipeline_job_submit_attempt(
        ifs_key,
        pipeline_job_id=ifs_job_id,
        expected_submission_attempt=1,
        slurm_job_id="72001",
        submitted_at=anchor + timedelta(hours=1),
        slurm_accounting_submitted_at=anchor + timedelta(hours=1),
        transition=AcceptedSubmitTransition.accounting(
            "matched_bound",
            submit_outcome="accepted",
            matched_slurm_job_id="72001",
            status="submitted",
            reconciliation_source="slurm_name_window_unique",
        ),
    )
    assert bound.committed
    # Healthy terminal: direct present (projection succeeds).
    member = repository.get_pipeline_job(ifs_job_id)["cohort_members"][0]
    result = repository.project_forecast_cohort_tasks(
        ifs_job_id,
        master_slurm_job_id="72001",
        projections=[
            {
                **member,
                "array_task_outcome": "succeeded",
                "task_slurm_job_id": "72001_0",
                "restart_stage": "state_save_qc",
                "native_shud_resubmitted": False,
            }
        ],
        complete=True,
        master_status="succeeded",
        master_error_code=None,
        reconciliation_decision="matched_bound",
    )
    assert result["total"] > 0
    assert (repository.root / "pipeline-jobs" / f"{ifs_job_id}.json").exists()
    assert not (repository.root / "reconcile-inventory-migration-v1.json").exists()

    fresh = FileOrchestrationJournalRepository(repository.root)
    before = fresh._reconcile_authority_fingerprint_unlocked()
    fingerprint = fresh._stable_backfill_reconcile_inventory_unlocked()
    assert fingerprint == before
    # Healthy terminal with direct present: anchor pruned, no handoff anchor.
    assert not (
        repository.root / "reconcile-inventory" / f"{ifs_job_id}.json"
    ).exists(), "healthy terminal master must not leave a handoff anchor"
    assert list(fresh._iter_reconcile_inventory_records()) == []
