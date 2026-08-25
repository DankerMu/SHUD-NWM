"""Reconcile inventory: bounded active discovery, terminal cleanup, anchor
self-heal, migration crash recovery and repair.
"""

from __future__ import annotations

import copy
import json
import os
from datetime import (
    UTC,
    datetime,
    timedelta,
)
from typing import Any

import pytest

from tests.gateway_reconcile_helpers import (
    _bind_current_file_cohort,
    _file_cohort_repository,
)


def test_active_reconcile_partition_finds_oldest_active_after_one_year_of_two_daily_cycles_and_two_sources(
    tmp_path: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from services.orchestrator import file_orchestration_journal as journal_module
    from services.orchestrator.accepted_submit_identity import (
        canonical_forecast_cohort_members,
        forecast_cohort_digest,
    )
    from services.orchestrator.file_orchestration_journal import _journal_record_for_write
    from services.orchestrator.reservation import slurm_comment_for

    repository = _file_cohort_repository(tmp_path, member_count=1, with_runtime_rows=False)
    key = "cycle_gfs_2026071200_forecast_fixture:forecast"
    job_id = "job_cycle_gfs_2026071200_forecast_fixture_forecast"
    _bind_current_file_cohort(repository, key, slurm_job_id="17667")
    master_path = repository.root / "pipeline-jobs" / f"{job_id}.json"
    template = json.loads(master_path.read_text(encoding="utf-8"))
    day_count = 365
    cycle_hours = (0, 12)
    source_ids = ("gfs", "ifs")
    candidates_per_master = 256
    history_count = 0
    history_start = datetime(2025, 7, 12, tzinfo=UTC)
    # Two cycles/day (00Z, 12Z) x two sources/cycle (GFS, IFS) is four
    # terminal cohort masters/day: 4 x 365 = 1,460 flat history records.
    for day in range(day_count):
        for cycle_hour in cycle_hours:
            cycle_time = history_start + timedelta(days=day, hours=cycle_hour)
            cycle_segment = cycle_time.strftime("%Y%m%d%H")
            cycle_iso = cycle_time.isoformat().replace("+00:00", "Z")
            for source_id in source_ids:
                history_count += 1
                run_id = f"cycle_{source_id}_{cycle_segment}_terminal_history"
                historical_job_id = f"job_{run_id}_forecast"
                cycle_id = f"{source_id}_{cycle_segment}"
                idempotency_key = f"{run_id}:forecast"
                slurm_job_id = str(20_000 + history_count)
                members = canonical_forecast_cohort_members(
                    source_id=source_id,
                    cycle_time=cycle_time,
                    basins=[
                        {
                            "model_id": f"history_{source_id}",
                            "basin_id": f"history_basin_{source_id}",
                            "task_id": 0,
                        }
                    ],
                )
                payload = copy.deepcopy(template["payload"])
                payload.update(
                    {
                        "job_id": historical_job_id,
                        "run_id": run_id,
                        "cycle_id": cycle_id,
                        "source_id": source_id,
                        "cycle_time": cycle_iso,
                        "idempotency_key": idempotency_key,
                        "slurm_comment": slurm_comment_for(idempotency_key),
                        "slurm_job_id": slurm_job_id,
                        "status": "succeeded",
                        "submit_outcome": "accepted",
                        "reconciliation_source": "slurm_exact_comment",
                        "reconciliation_decision": "matched_bound",
                        "reconciliation_reason_class": None,
                        "matched_slurm_job_id": slurm_job_id,
                        "cohort_members": list(members),
                        "candidate_projections": [
                            {
                                "candidate_id": members[0]["candidate_id"],
                                "run_id": members[0]["run_id"],
                                "model_id": members[0]["model_id"],
                                "array_task_id": 0,
                                "array_task_outcome": "succeeded",
                                "restart_stage": "state_save_qc",
                                "native_shud_resubmitted": False,
                            }
                        ],
                        "finished_at": cycle_iso,
                        "exit_code": 0,
                        "error_code": None,
                        "error_message": None,
                    }
                )
                payload["cohort_digest"] = forecast_cohort_digest(payload)
                historical = copy.deepcopy(template)
                historical.update(
                    {
                        "source_id": source_id,
                        "cycle_time": cycle_iso,
                        "job_id": historical_job_id,
                        "run_id": run_id,
                        "cycle_id": cycle_id,
                        "payload": payload,
                    }
                )
                repository._validated_direct_pipeline_job_record(
                    historical,
                    expected_job_id=historical_job_id,
                )
                path = repository.root / "pipeline-jobs" / f"{historical_job_id}.json"
                path.write_text(json.dumps(historical, sort_keys=True), encoding="utf-8")
                journal_record = _journal_record_for_write(
                    "pipeline_job",
                    payload,
                    source_id=source_id,
                    cycle_time=cycle_time,
                    model_id=None,
                    sequence=1,
                )
                repository._validate_outgoing_record(
                    journal_record,
                    source_id=source_id,
                    cycle_time=cycle_time,
                    record_type="pipeline_job",
                    model_id=None,
                )
                journal_path = (
                    repository.root / "journal" / source_id / f"{cycle_segment}.jsonl"
                )
                journal_path.parent.mkdir(parents=True, exist_ok=True)
                journal_path.write_text(
                    json.dumps(journal_record, separators=(",", ":"), sort_keys=True) + "\n",
                    encoding="utf-8",
                )

    # The first process performs the one strict migration over 1,460 real
    # terminal journal files. Every later process must trust only marker + inventory.
    migrated = type(repository)(repository.root)
    assert [job.job_id for job in migrated.query_inflight_jobs()] == [job_id]
    assert (repository.root / "reconcile-inventory-migration-v1.json").is_file()

    # Candidate history is sharded below by-cycle in production. Its annual
    # conceptual cardinality is 1,460 masters x 256 candidates = 373,760;
    # steady-state reconcile must not enumerate that tree, so materializing it
    # here would only make the test slower without strengthening the invariant.
    conceptual_candidate_count = (
        day_count * len(cycle_hours) * len(source_ids) * candidates_per_master
    )
    reopened = type(repository)(repository.root)
    read_optional_json = reopened._read_optional_json
    flat_history_reads = 0
    forbidden_scan_calls = 0
    virtual_candidate_accesses = 0
    directory_calls: list[str] = []
    stat_calls: list[str] = []
    read_calls: list[str] = []
    original_list_directory = journal_module.list_directory_no_follow_limited
    original_stat = journal_module.stat_no_follow

    def count_flat_history_reads(path: Any) -> Any:
        nonlocal flat_history_reads
        relative = str(path.relative_to(repository.root))
        read_calls.append(relative)
        if "by-cycle" in path.parts:
            return virtual_candidate_history_walker()
        if path.parent == repository.root / "pipeline-jobs" and "terminal_history" in path.name:
            flat_history_reads += 1
        return read_optional_json(path)

    def virtual_candidate_history_walker(*_args: Any, **_kwargs: Any) -> Any:
        nonlocal virtual_candidate_accesses
        virtual_candidate_accesses += conceptual_candidate_count
        raise AssertionError(
            f"candidate-history traversal attempted {conceptual_candidate_count} virtual reads"
        )

    def reject_global_scan(*_args: Any, **_kwargs: Any) -> Any:
        nonlocal forbidden_scan_calls
        forbidden_scan_calls += 1
        raise AssertionError("steady-state restart reconcile must use only the durable inventory")

    def bounded_list(path: Any, **kwargs: Any) -> Any:
        relative = str(path.relative_to(repository.root)) if path != repository.root else "."
        directory_calls.append(relative)
        if "by-cycle" in path.parts:
            return virtual_candidate_history_walker()
        assert relative not in {"pipeline-jobs", "journal", "pipeline-jobs/by-cycle"}
        return original_list_directory(path, **kwargs)

    def bounded_stat(path: Any, **kwargs: Any) -> Any:
        relative = str(path.relative_to(repository.root))
        stat_calls.append(relative)
        assert "terminal_history" not in relative
        if "by-cycle" in path.parts:
            return virtual_candidate_history_walker()
        return original_stat(path, **kwargs)

    monkeypatch.setattr(reopened, "_read_optional_json", count_flat_history_reads)
    monkeypatch.setattr(reopened, "_iter_reconcile_direct_pipeline_job_records", reject_global_scan)
    monkeypatch.setattr(
        reopened,
        "_iter_direct_pipeline_job_records_for_cycle",
        virtual_candidate_history_walker,
    )
    monkeypatch.setattr(
        reopened,
        "_direct_pipeline_job_records_for_cycle_cached",
        virtual_candidate_history_walker,
    )
    monkeypatch.setattr(journal_module, "list_directory_no_follow_limited", bounded_list)
    monkeypatch.setattr(journal_module, "stat_no_follow", bounded_stat)
    inflight = reopened.query_inflight_jobs()

    assert history_count == day_count * len(cycle_hours) * len(source_ids) == 1460
    historical_journal_paths = tuple(
        path
        for path in (repository.root / "journal").rglob("*.jsonl")
        if path.name != "2026071200.jsonl"
    )
    assert len(historical_journal_paths) == 1460
    assert conceptual_candidate_count == 365 * 2 * 2 * 256 == 373760
    assert [job.job_id for job in inflight] == [job_id]
    assert len(tuple((repository.root / "reconcile-inventory").glob("*.json"))) == 1
    assert forbidden_scan_calls == 0
    assert flat_history_reads == 0
    assert virtual_candidate_accesses == 0
    assert directory_calls == [".", "reconcile-inventory"]
    assert len(stat_calls) <= 8
    assert len(read_calls) <= 5


def test_reconcile_inventory_terminal_cleanup_and_stale_anchor_self_heal(tmp_path: Any) -> None:
    repository = _file_cohort_repository(tmp_path, member_count=1)
    key = "cycle_gfs_2026071200_forecast_fixture:forecast"
    job_id = "job_cycle_gfs_2026071200_forecast_fixture_forecast"
    _bind_current_file_cohort(repository, key, slurm_job_id="17667")
    active_path = repository.root / "reconcile-inventory" / f"{job_id}.json"
    assert active_path.is_file()

    stale_active = active_path.read_bytes()
    member = repository.get_pipeline_job(job_id)["cohort_members"][0]
    result = repository.project_forecast_cohort_tasks(
        job_id,
        master_slurm_job_id="17667",
        projections=[
            {
                **member,
                "array_task_outcome": "succeeded",
                "task_slurm_job_id": "17667_0",
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
    assert not active_path.exists()
    assert type(repository)(repository.root).query_inflight_jobs() == []

    # A crash after the canonical terminal write but before active-index
    # cleanup may leave a stale marker. Exact cycle replay wins, and the marker
    # is repaired while holding the same cycle lock.
    active_path.write_bytes(stale_active)
    assert type(repository)(repository.root).query_inflight_jobs() == []
    assert not active_path.exists()


def test_rejected_current_master_never_occupies_reconcile_inventory(tmp_path: Any) -> None:
    repository = _file_cohort_repository(tmp_path, member_count=1, with_runtime_rows=False)
    job_id = "job_cycle_gfs_2026071200_forecast_fixture_forecast"
    inventory_path = repository.root / "reconcile-inventory" / f"{job_id}.json"
    assert inventory_path.is_file()

    rejected = repository.reject_pipeline_job_submit_attempt(
        "cycle_gfs_2026071200_forecast_fixture:forecast",
        pipeline_job_id=job_id,
        expected_submission_attempt=1,
        finished_at=datetime(2026, 7, 12, 0, 1, tzinfo=UTC),
        error_code="SBATCH_SUBMISSION_FAILED",
        error_message="submit rejected",
        stage="forecast",
        job_type="run_shud_forecast_array",
    )
    assert rejected.committed
    assert not inventory_path.exists()
    assert type(repository)(repository.root).query_inflight_jobs() == []


def test_reservation_lost_current_master_never_becomes_task_projection_work(tmp_path: Any) -> None:
    from services.orchestrator.accepted_submit_identity import ACCEPTED_SUBMIT_CONTRACT_VERSION

    repository = _file_cohort_repository(tmp_path, member_count=1, with_runtime_rows=False)
    job_id = "job_cycle_gfs_2026071200_forecast_fixture_forecast"
    current = repository.get_pipeline_job(job_id)
    changed = repository.permit_pipeline_job_retry(
        job_id,
        accepted_submit_contract_version=ACCEPTED_SUBMIT_CONTRACT_VERSION,
        expected_submission_attempt=1,
        expected_submission_attempt_started_at=current["submission_attempt_started_at"],
    )

    assert changed == 1
    assert repository.get_pipeline_job(job_id)["status"] == "reservation_lost"
    assert not (repository.root / "reconcile-inventory" / f"{job_id}.json").exists()
    reopened = type(repository)(repository.root)
    assert reopened.query_reserved_unbound_jobs() == []
    assert reopened.query_inflight_jobs() == []


def test_reconcile_inventory_rolls_back_anchor_on_ordinary_pre_journal_failure(
    tmp_path: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from services.orchestrator.file_orchestration_journal import FileOrchestrationJournalRepository

    template_repository = _file_cohort_repository(tmp_path / "template", member_count=1)
    job_id = "job_cycle_gfs_2026071200_forecast_fixture_forecast"
    clean = dict(template_repository.get_pipeline_job(job_id))
    clean.update(
        {
            "status": "reserved",
            "slurm_job_id": None,
            "submit_outcome": None,
            "reconciliation_source": None,
            "reconciliation_decision": None,
            "reconciliation_reason_class": None,
            "matched_slurm_job_id": None,
            "candidate_projections": [],
        }
    )
    repository = FileOrchestrationJournalRepository(tmp_path / "failed" / "journal")

    def fail_append(**_kwargs: Any) -> None:
        raise RuntimeError("ordinary append failure")

    monkeypatch.setattr(repository, "_append_journal_record_unlocked", fail_append)
    with pytest.raises(RuntimeError, match="ordinary append failure"):
        repository.reserve_pipeline_job(clean)

    assert not (repository.root / "reconcile-inventory" / f"{job_id}.json").exists()
    assert not (repository.root / "pipeline-jobs" / f"{job_id}.json").exists()


def test_reconcile_inventory_orphan_anchor_self_cleans_after_pre_journal_crash(
    tmp_path: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from services.orchestrator.file_orchestration_journal import FileOrchestrationJournalRepository

    class SimulatedCrash(BaseException):
        pass

    template_repository = _file_cohort_repository(tmp_path / "template", member_count=1)
    job_id = "job_cycle_gfs_2026071200_forecast_fixture_forecast"
    clean = dict(template_repository.get_pipeline_job(job_id))
    clean.update(
        {
            "status": "reserved",
            "slurm_job_id": None,
            "submit_outcome": None,
            "reconciliation_source": None,
            "reconciliation_decision": None,
            "reconciliation_reason_class": None,
            "matched_slurm_job_id": None,
            "candidate_projections": [],
        }
    )
    repository = FileOrchestrationJournalRepository(tmp_path / "crashed" / "journal")

    def crash_before_journal(**_kwargs: Any) -> None:
        raise SimulatedCrash

    monkeypatch.setattr(repository, "_append_journal_record_unlocked", crash_before_journal)
    with pytest.raises(SimulatedCrash):
        repository.reserve_pipeline_job(clean)

    inventory_path = repository.root / "reconcile-inventory" / f"{job_id}.json"
    assert inventory_path.is_file()
    assert type(repository)(repository.root).query_reserved_unbound_jobs() == []
    assert not inventory_path.exists()


def test_reconcile_inventory_recovers_active_from_post_journal_direct_crash(
    tmp_path: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from services.orchestrator.file_orchestration_journal import FileOrchestrationJournalRepository

    class SimulatedCrash(BaseException):
        pass

    template_repository = _file_cohort_repository(tmp_path / "template", member_count=1)
    job_id = "job_cycle_gfs_2026071200_forecast_fixture_forecast"
    clean = dict(template_repository.get_pipeline_job(job_id))
    clean.update(
        {
            "status": "reserved",
            "slurm_job_id": None,
            "submit_outcome": None,
            "reconciliation_source": None,
            "reconciliation_decision": None,
            "reconciliation_reason_class": None,
            "matched_slurm_job_id": None,
            "candidate_projections": [],
        }
    )
    repository = FileOrchestrationJournalRepository(tmp_path / "crashed" / "journal")

    def crash_before_direct(*_args: Any, **_kwargs: Any) -> None:
        raise SimulatedCrash

    monkeypatch.setattr(repository, "_write_pipeline_job_direct_unlocked", crash_before_direct)
    with pytest.raises(SimulatedCrash):
        repository.reserve_pipeline_job(clean)

    assert not (repository.root / "pipeline-jobs" / f"{job_id}.json").exists()
    recovered = type(repository)(repository.root).query_reserved_unbound_jobs()
    assert [job.job_id for job in recovered] == [job_id]


def test_reconcile_inventory_terminal_journal_wins_after_direct_cleanup_crash(
    tmp_path: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class SimulatedCrash(BaseException):
        pass

    repository = _file_cohort_repository(tmp_path, member_count=1)
    key = "cycle_gfs_2026071200_forecast_fixture:forecast"
    job_id = "job_cycle_gfs_2026071200_forecast_fixture_forecast"
    _bind_current_file_cohort(repository, key, slurm_job_id="99006")
    member = repository.get_pipeline_job(job_id)["cohort_members"][0]

    def crash_before_direct(*_args: Any, **_kwargs: Any) -> None:
        raise SimulatedCrash

    monkeypatch.setattr(repository, "_write_pipeline_job_direct_unlocked", crash_before_direct)
    with pytest.raises(SimulatedCrash):
        repository.project_forecast_cohort_tasks(
            job_id,
            master_slurm_job_id="99006",
            projections=[
                {
                    **member,
                    "array_task_outcome": "succeeded",
                    "task_slurm_job_id": "99006_0",
                    "restart_stage": "state_save_qc",
                    "native_shud_resubmitted": False,
                }
            ],
            complete=True,
            master_status="succeeded",
            master_error_code=None,
            reconciliation_decision="matched_bound",
        )

    inventory_path = repository.root / "reconcile-inventory" / f"{job_id}.json"
    assert inventory_path.is_file()
    reopened = type(repository)(repository.root)
    assert reopened.query_inflight_jobs() == []
    assert reopened.get_pipeline_job(job_id)["status"] == "succeeded"
    assert not inventory_path.exists()


def test_reconcile_inventory_migration_is_resumable_and_backfills_marker_free_active(
    tmp_path: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repository = _file_cohort_repository(tmp_path, member_count=1, source_id="gfs")
    repository = _file_cohort_repository(
        tmp_path,
        member_count=1,
        source_id="ifs",
        versioned=False,
    )
    inventory_directory = repository.root / "reconcile-inventory"
    for path in inventory_directory.glob("*.json"):
        path.unlink()
    marker_path = repository.root / "reconcile-inventory-migration-v1.json"
    marker_path.unlink(missing_ok=True)

    class SimulatedCrash(BaseException):
        pass

    original_sync = repository._sync_reconcile_inventory_for_row_unlocked
    active_syncs = 0

    def interrupt_second_active(row: Any) -> bool:
        nonlocal active_syncs
        result = original_sync(row)
        if result:
            active_syncs += 1
            if active_syncs == 2:
                raise SimulatedCrash
        return result

    monkeypatch.setattr(repository, "_sync_reconcile_inventory_for_row_unlocked", interrupt_second_active)
    with pytest.raises(SimulatedCrash):
        repository.query_reserved_unbound_jobs()
    assert not marker_path.exists()
    assert len(tuple(inventory_directory.glob("*.json"))) == 2

    reopened = type(repository)(repository.root)
    recovered = reopened.query_reserved_unbound_jobs()
    assert {job.job_id for job in recovered} == {
        "job_cycle_gfs_2026071200_forecast_fixture_forecast",
        "job_cycle_ifs_2026071200_forecast_fixture_forecast",
    }
    assert marker_path.is_file()
    assert len(tuple(inventory_directory.glob("*.json"))) == 2


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
